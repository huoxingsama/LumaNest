import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from test_integrations import Config


def archive(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as output:
        for name, contents in entries:
            output.writestr(name, contents)
    return data.getvalue()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        from personal_cinema.component_installer import ComponentInstaller
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = Config(**{'network.mode': 'direct', 'foundation.mpv_path': ''})
        self.installer = ComponentInstaller(self.root, self.config)
        self.body = archive([('mpv/mpv.exe', b'MZfake-executable-not-executed'), ('mpv/LICENSE', b'license')])
        self.payload = {'version': 'v0.40.0', 'url': 'https://github.com/mpv-player/mpv/releases/download/v0.40.0/mpv-win64.zip',
                        'sha256': hashlib.sha256(self.body).hexdigest()}

    def download(self, url, destination):
        destination.write_bytes(self.body)

    def test_plan_never_downloads_or_runs_and_apply_installs_once(self):
        with patch.object(self.installer, '_download', side_effect=self.download) as download:
            plan = self.installer.plan('mpv', self.payload)
            download.assert_not_called()
            first = self.installer.apply('mpv', {'plan_id': plan['plan_id']})
            second = self.installer.apply('mpv', {'plan_id': plan['plan_id']})
            self.assertEqual(first, second)
            self.assertEqual(1, download.call_count)
        self.assertEqual('complete', first['state'])
        executable = Path(self.config.values['foundation.mpv_path'])
        self.assertTrue(executable.is_file())
        self.assertTrue(executable.is_relative_to(self.root / 'components'))
        self.assertFalse(first['started'])

    def test_hash_mismatch_cannot_install_or_change_configuration(self):
        plan = self.installer.plan('mpv', {**self.payload, 'sha256': '0' * 64})
        with patch.object(self.installer, '_download', side_effect=self.download), self.assertRaises(ValueError):
            self.installer.apply('mpv', {'plan_id': plan['plan_id']})
        self.assertEqual('', self.config.values['foundation.mpv_path'])

    def test_archive_traversal_is_rejected_before_extraction(self):
        for unsafe in ('../outside.exe', 'C:/outside.exe', '/absolute.exe', 'safe/../../x', 'safe\\..\\outside', 'CON.txt', 'safe/file:stream', 'safe/trailing.'):
            with self.subTest(unsafe=unsafe):
                self.body = archive([('mpv.exe', b'MZ'), (unsafe, b'x')])
                plan = self.installer.plan('mpv', {**self.payload, 'sha256': hashlib.sha256(self.body).hexdigest()})
                with patch.object(self.installer, '_download', side_effect=self.download), self.assertRaises(ValueError):
                    self.installer.apply('mpv', {'plan_id': plan['plan_id']})
        self.assertEqual('', self.config.values['foundation.mpv_path'])

    def test_zip_symbolic_link_and_duplicate_case_paths_are_rejected(self):
        symlink = zipfile.ZipInfo('link')
        symlink.create_system = 3
        symlink.external_attr = (0o120777 << 16)
        for entries in ([('mpv.exe', b'MZ'), (symlink, b'/outside')], [('mpv.exe', b'MZ'), ('MPV.exe', b'MZ')]):
            self.body = archive(entries)
            plan = self.installer.plan('mpv', {**self.payload, 'sha256': hashlib.sha256(self.body).hexdigest()})
            with patch.object(self.installer, '_download', side_effect=self.download), self.assertRaises(ValueError):
                self.installer.apply('mpv', {'plan_id': plan['plan_id']})

    def test_unknown_sources_mutable_latest_and_bad_hash_rejected(self):
        urls = ['https://evil.invalid/mpv.zip', 'http://github.com/mpv-player/mpv/releases/download/v1/mpv.zip',
                'https://github.com/attacker/mpv/releases/download/v1/mpv.zip',
                'https://github.com/mpv-player/mpv/releases/latest/download/mpv.zip',
                'https://github.com/mpv-player/mpv/releases/download/v1/../../../../evil.zip',
                'https://github.com/mpv-player/mpv/releases/download/v1/mpv.zip?token=private']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.installer.plan('mpv', {**self.payload, 'url': url})
        with self.assertRaises(ValueError):
            self.installer.plan('mpv', {**self.payload, 'sha256': 'bad'})

    def test_changed_path_requires_a_new_plan(self):
        plan = self.installer.plan('mpv', self.payload)
        self.config.values['foundation.mpv_path'] = str(self.root / 'custom-mpv.exe')
        with patch.object(self.installer, '_download') as download, self.assertRaises(ValueError):
            self.installer.apply('mpv', {'plan_id': plan['plan_id']})
        download.assert_not_called()

    def test_missing_or_ambiguous_executable_leaves_existing_config_untouched(self):
        for entries in ([('readme.txt', b'x')], [('a/mpv.exe', b'MZ'), ('b/mpv.exe', b'MZ')]):
            self.body = archive(entries)
            plan = self.installer.plan('mpv', {**self.payload, 'sha256': hashlib.sha256(self.body).hexdigest()})
            with patch.object(self.installer, '_download', side_effect=self.download), self.assertRaises(ValueError):
                self.installer.apply('mpv', {'plan_id': plan['plan_id']})
        self.assertEqual('', self.config.values['foundation.mpv_path'])

    def test_redirects_only_allow_vendor_release_asset_cdn(self):
        from personal_cinema.component_installer import validate_download_url
        validate_download_url('mpv', 'https://release-assets.githubusercontent.com/github-production-release-asset/123/test?sig=abc', redirect=True)
        for url in ('http://release-assets.githubusercontent.com/file', 'https://evil.invalid/file', 'https://127.0.0.1/file'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_download_url('mpv', url, redirect=True)

    def test_archive_resource_limits_are_enforced(self):
        plan = self.installer.plan('mpv', self.payload)
        with patch.object(self.installer, '_download', side_effect=self.download), patch('personal_cinema.component_installer.MAX_EXTRACTED', 4), self.assertRaises(ValueError):
            self.installer.apply('mpv', {'plan_id': plan['plan_id']})

    def test_receipt_allows_recovery_after_configuration_write_failure(self):
        plan = self.installer.plan('mpv', self.payload)
        update = self.config.update
        with patch.object(self.installer, '_download', side_effect=self.download) as download:
            with patch.object(self.config, 'update', side_effect=ValueError('concurrent update')):
                with self.assertRaises(ValueError): self.installer.apply('mpv', {'plan_id': plan['plan_id']})
            self.config.update = update
            result = self.installer.apply('mpv', {'plan_id': plan['plan_id']})
            self.assertEqual('complete', result['state'])
            self.assertEqual(1, download.call_count)


if __name__ == '__main__': unittest.main()

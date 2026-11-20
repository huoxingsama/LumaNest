import hashlib
import io
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from personal_cinema import embedded_runtime as runtime


def pe(machine=0x8664):
    data = bytearray(256)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 132, machine)
    return bytes(data)


class EmbeddedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'offline'
        self.source.mkdir()
        self.prefix = self.root / 'python'
        self.prefix.mkdir()
        self.file = self.source / 'mpv-2.dll'
        self.file.write_bytes(pe())
        self.write_manifest()

    def write_manifest(self, **extra):
        files = {'mpv-2.dll': hashlib.sha256(self.file.read_bytes()).hexdigest(), **extra}
        (self.source / 'manifest.json').write_text(json.dumps({'format': 1, 'architecture': 'x86_64',
            'files': files, 'sources': [{'name': 'offline user-selected package'}]}), encoding='utf-8')

    def test_offline_install_validates_then_commits_and_reuses(self):
        with patch.object(runtime, 'verify_library', return_value={'api_version': 2}) as verify:
            result = runtime.install(self.prefix, from_directory=self.source)
            self.assertEqual(result['state'], 'installed')
            self.assertEqual((self.prefix / 'player/mpv-2.dll').read_bytes(), pe())
            again = runtime.install(self.prefix, from_directory=self.source)
            self.assertEqual(again['state'], 'ready')
            self.assertTrue(verify.called)
        self.assertEqual(self.file.read_bytes(), pe())

    def test_bad_hash_never_loads_or_changes_existing_runtime(self):
        with patch.object(runtime, 'verify_library', return_value={'api_version': 2}):
            runtime.install(self.prefix, from_directory=self.source)
        before = (self.prefix / 'player/mpv-2.dll').read_bytes()
        self.file.write_bytes(pe() + b'changed')
        with patch.object(runtime, 'verify_library') as verify, self.assertRaises(ValueError):
            runtime.install(self.prefix, from_directory=self.source)
        verify.assert_not_called()
        self.assertEqual((self.prefix / 'player/mpv-2.dll').read_bytes(), before)

    def test_failed_load_preserves_previous_directory(self):
        with patch.object(runtime, 'verify_library', return_value={'api_version': 2}):
            runtime.install(self.prefix, from_directory=self.source)
        self.file.write_bytes(pe() + b'new-version')
        self.write_manifest()
        with patch.object(runtime, 'verify_library', side_effect=RuntimeError('load failed')), self.assertRaises(RuntimeError):
            runtime.install(self.prefix, from_directory=self.source)
        self.assertEqual((self.prefix / 'player/mpv-2.dll').read_bytes(), pe())
        self.assertFalse(list(self.prefix.glob('.player-stage-*')))

    def test_commit_failure_rolls_back_existing_runtime(self):
        with patch.object(runtime, 'verify_library', return_value={'api_version': 2}):
            runtime.install(self.prefix, from_directory=self.source)
        self.file.write_bytes(pe() + b'new-version')
        self.write_manifest()
        rename = Path.rename
        def failed_commit(path, destination):
            if path.name.startswith('.player-stage-'):
                raise PermissionError('fixture file in use')
            return rename(path, destination)
        with patch.object(runtime, 'verify_library', return_value={'api_version': 2}), patch.object(Path, 'rename', failed_commit):
            with self.assertRaises(PermissionError):
                runtime.install(self.prefix, from_directory=self.source)
        self.assertEqual((self.prefix / 'player/mpv-2.dll').read_bytes(), pe())

    def test_existing_unlisted_files_are_never_removed(self):
        with patch.object(runtime, 'verify_library', return_value={'api_version': 2}):
            runtime.install(self.prefix, from_directory=self.source)
        retained = self.prefix / 'player/private-settings.txt'
        retained.write_text('fixture')
        with self.assertRaises(ValueError):
            runtime.install(self.prefix, from_directory=self.source)
        self.assertEqual(retained.read_text(), 'fixture')

    def test_malformed_manifest_is_reported_as_validation_error(self):
        (self.source / 'manifest.json').write_text('[]')
        with self.assertRaises(ValueError):
            runtime.install(self.prefix, from_directory=self.source)

    def test_paths_unknown_dll_and_wrong_architecture_are_rejected(self):
        for name in ('../evil.dll', 'folder/mpv-2.dll', 'evil.dll', 'mpv-2.dll:stream'):
            with self.subTest(name=name):
                self.write_manifest(**{name: '0' * 64})
                with patch.object(runtime, 'verify_library') as verify, self.assertRaises(ValueError):
                    runtime.install(self.prefix, from_directory=self.source)
                verify.assert_not_called()
        self.file.write_bytes(pe(0x14c))
        self.write_manifest()
        with self.assertRaises(ValueError):
            runtime.install(self.prefix, from_directory=self.source)

    def test_support_zip_rejects_traversal_and_unlisted_payload(self):
        for name in ('../vulkan-1.dll', '/vulkan-1.dll', 'evil.exe', 'folder/vulkan-1.dll'):
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                archive.writestr(name, pe())
            path = self.root / 'support.zip'
            path.write_bytes(data.getvalue())
            with self.subTest(name=name), self.assertRaises(ValueError):
                runtime.extract_support(path, self.source)

    def test_offline_source_link_cannot_escape_selected_directory(self):
        outside = self.root / 'outside.dll'
        outside.write_bytes(pe())
        self.file.unlink()
        try:
            self.file.symlink_to(outside)
        except OSError:
            self.skipTest('symlink unavailable')
        self.write_manifest()
        with self.assertRaises(ValueError):
            runtime.install(self.prefix, from_directory=self.source)

    def test_download_hash_failure_does_not_extract(self):
        def download(url, destination, digest):
            destination.write_bytes(b'bad archive')
            runtime.require_hash(destination, digest)
        with patch.object(runtime, 'download', side_effect=download), patch.object(runtime, 'extract_core') as extract:
            with self.assertRaises(ValueError):
                runtime.install(self.prefix)
        extract.assert_not_called()
        self.assertFalse((self.prefix / 'player').exists())

    def test_redirect_only_accepts_pinned_origin_and_github_asset_cdn(self):
        runtime.validate_url(runtime.CORE_URL)
        runtime.validate_url('https://release-assets.githubusercontent.com/github-production-release-asset/1/file?sig=fixture', redirect=True)
        for url in ('http://github.com/file', 'https://evil.invalid/file',
                    'https://github.com/attacker/tool/releases/download/v1/runtime.zip',
                    'https://release-assets.githubusercontent.com/other/file'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                runtime.validate_url(url, redirect=True)


if __name__ == '__main__':
    unittest.main()

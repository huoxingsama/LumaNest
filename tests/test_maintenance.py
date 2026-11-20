import importlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import warnings
from unittest.mock import patch
import zipfile

from personal_cinema.configuration import ConfigStore, RevisionConflict
from personal_cinema.credentials import SecretStore


class FixtureCipher:
    def encrypt(self, value): return b'test-user:' + value[::-1]
    def decrypt(self, value):
        if not value.startswith(b'test-user:'): raise ValueError('wrong account')
        return value[10:][::-1]


class FakeRegistry:
    def __init__(self): self.values = {'SomeoneElsesSoftware': 'leave this alone'}
    def set(self, name, command): self.values[name] = command
    def delete(self, name): self.values.pop(name, None)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'cinema home'
        self.config = ConfigStore(self.root)
        self.cipher = FixtureCipher()
        self.secrets = SecretStore(self.root, cipher=self.cipher)
        self.config.update({'cache.playback_gib': 12}, 0)
        self.secrets.set('jellyfin.token', 'fixture-secret-only')
        self.module = importlib.import_module('personal_cinema.maintenance')
        self.registry = FakeRegistry()
        self.manager = self.module.MaintenanceManager(self.root, running=lambda: False, cipher=self.cipher, registry=self.registry)

    def test_backup_contains_only_configuration_and_encrypted_credentials(self):
        media = self.root / 'runtime/catalog/Movies/movie.mkv'
        media.parent.mkdir(parents=True); media.write_bytes(b'media must not be included')
        backup = self.manager.backup()
        with zipfile.ZipFile(self.root / 'backups' / backup['id']) as archive:
            self.assertEqual({'manifest.json', 'config.json', 'secrets/credentials.json'}, set(archive.namelist()))
            self.assertNotIn(b'fixture-secret-only', archive.read('secrets/credentials.json'))
        self.assertEqual(backup['id'], self.manager.list_backups()[0]['id'])
        self.assertTrue(backup['includes_credentials'])

    def test_restore_keeps_the_current_settings_process_handshake(self):
        self.secrets.set('settings.launcher', 'fixture-old-handshake')
        backup = self.manager.backup()
        with zipfile.ZipFile(self.root / 'backups' / backup['id']) as archive:
            self.assertNotIn('settings.launcher', json.loads(archive.read('secrets/credentials.json'))['values'])
        self.secrets.set('settings.launcher', 'fixture-current-handshake')
        self.manager.restore(backup['id'], expected_revision=self.config.read()['revision'])
        self.assertEqual(self.secrets.get('settings.launcher'), 'fixture-current-handshake')

    def test_restore_increments_revision_and_creates_safety_backup(self):
        backup = self.manager.backup()
        self.config.update({'cache.playback_gib': 24}, 1)
        self.secrets.set('jellyfin.token', 'fixture-new-secret')
        result = self.manager.restore(backup['id'], expected_revision=2)
        self.assertEqual('restored', result['state'])
        self.assertEqual(3, self.config.read()['revision'])
        self.assertEqual(12, self.config.read()['values']['cache.playback_gib'])
        self.assertEqual('fixture-secret-only', self.secrets.get('jellyfin.token'))
        self.assertNotEqual(backup['id'], result['safety_backup'])
        with zipfile.ZipFile(self.root / 'backups' / result['safety_backup']) as archive:
            self.assertEqual(24, json.loads(archive.read('config.json'))['values']['cache.playback_gib'])

    def test_restore_refuses_active_or_unknown_runtime(self):
        backup = self.manager.backup()
        for running in (None, lambda: True):
            manager = self.module.MaintenanceManager(self.root, running=running, cipher=self.cipher)
            with self.assertRaises(ValueError): manager.restore(backup['id'])
        self.assertEqual(1, self.config.read()['revision'])

    def test_restore_checks_revision_before_mutation(self):
        backup = self.manager.backup()
        with self.assertRaises(RevisionConflict): self.manager.restore(backup['id'], expected_revision=0)
        self.assertEqual(1, len(self.manager.list_backups()))

    def test_restore_with_real_runtime_stopped_check_can_reread_locked_configuration(self):
        from personal_cinema.runtime import Runtime
        runtime = Runtime(self.root, self.config, self.secrets, None)

        def running():
            status = runtime.status()
            # A real authorization/status adapter may also read the vault.
            self.assertEqual(self.secrets.get('jellyfin.token'), 'fixture-secret-only')
            return bool(status['playing'] or any(status['processes'].values()))

        manager = self.module.MaintenanceManager(self.root, running=running, cipher=self.cipher, registry=self.registry)
        backup = manager.backup()
        with patch.object(runtime, 'playing', return_value=False):
            result = manager.restore(backup['id'], expected_revision=1)
        self.assertEqual('restored', result['state'])
        self.assertEqual(2, self.config.read()['revision'])

    def test_corrupt_or_other_user_credentials_are_rejected_before_writes(self):
        backup = self.manager.backup()
        other = self.module.MaintenanceManager(self.root, running=lambda: False, cipher=type('OtherCipher', (), {'decrypt': lambda self, value: (_ for _ in ()).throw(ValueError())})())
        with self.assertRaises(ValueError): other.restore(backup['id'])
        self.assertEqual('fixture-secret-only', self.secrets.get('jellyfin.token'))

    def test_archive_traversal_extra_media_and_duplicate_members_are_rejected(self):
        for index, member in enumerate(('../config.json', 'runtime/catalog/movie.mkv', '/config.json')):
            path = Path(self.temp.name) / f'bad{index}.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr(member, '{}')
            with self.assertRaises(ValueError): self.manager.import_archive(path)
        with self.assertRaises(ValueError): self.manager.restore('../config.json')

    def test_duplicate_or_symlink_archive_members_are_rejected(self):
        good = self.manager.backup()
        with zipfile.ZipFile(self.root / 'backups' / good['id']) as original:
            members = {name: original.read(name) for name in original.namelist()}
        for variant in ('duplicate', 'symlink'):
            path = Path(self.temp.name) / (variant + '.zip')
            with warnings.catch_warnings(), zipfile.ZipFile(path, 'w') as archive:
                warnings.simplefilter('ignore', UserWarning)
                for name, data in members.items():
                    info = zipfile.ZipInfo(name)
                    if variant == 'symlink' and name == 'config.json':
                        info.create_system = 3
                        info.external_attr = 0o120777 << 16
                    archive.writestr(info, data)
                if variant == 'duplicate': archive.writestr('config.json', members['config.json'])
            with self.assertRaises(ValueError): self.manager.import_archive(path)

    def test_valid_external_archive_can_be_imported_without_restoring(self):
        backup = self.manager.backup()
        imported = self.manager.import_archive(self.root / 'backups' / backup['id'])
        self.assertNotEqual(imported['id'], backup['id'])
        self.assertEqual(1, self.config.read()['revision'])

    def test_restore_write_failure_rolls_back_configuration(self):
        backup = self.manager.backup()
        self.config.update({'cache.playback_gib': 24}, 1)
        real = self.module._atomic_write
        failures = [True]
        def fail_once(path, value):
            if Path(path).name == 'credentials.json' and failures:
                failures.pop(); raise OSError('fixture disk failure')
            return real(path, value)
        with patch.object(self.module, '_atomic_write', side_effect=fail_once):
            with self.assertRaises(OSError): self.manager.restore(backup['id'])
        self.assertEqual(24, self.config.read()['values']['cache.playback_gib'])
        self.assertEqual(2, self.config.read()['revision'])

    def test_autostart_is_instance_owned_and_disabling_preserves_other_entries(self):
        self.config.update({'maintenance.autostart': True}, 1)
        result = self.manager.sync_autostart()
        command = self.registry.values[result['name']]
        self.assertIn('--no-browser', command); self.assertIn('--start-runtime', command)
        self.assertIn('"' + str(self.root.resolve()) + '"', command)
        self.config.update({'maintenance.autostart': False}, 2)
        self.manager.sync_autostart()
        self.assertEqual({'SomeoneElsesSoftware': 'leave this alone'}, self.registry.values)
        other = self.module.MaintenanceManager(Path(self.temp.name) / 'other', registry=self.registry, cipher=self.cipher)
        self.assertNotEqual(result['name'], other.autostart_name)

    def test_retention_deletes_only_old_owned_logs_and_never_media(self):
        self.config.update({'maintenance.diagnostic_retention_days': 5}, 1)
        now = time.time()
        old = self.root / 'runtime/logs/cache.log'
        fresh = self.root / 'logs/settings.log'
        media = self.root / 'runtime/catalog/Movies/media.log'
        for path in (old, fresh, media):
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture')
            os.utime(path, (now - 6 * 86400, now - 6 * 86400))
        os.utime(fresh, (now, now))
        result = self.manager.prune_diagnostics(now=now)
        self.assertEqual(1, result['removed'])
        self.assertFalse(old.exists()); self.assertTrue(fresh.exists()); self.assertTrue(media.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI requires Windows')
    def test_real_windows_dpapi_backup_restore_uses_current_user(self):
        root = Path(self.temp.name) / 'actual-dpapi-fixture'
        vault = SecretStore(root)
        vault.set('test.credential', 'only-a-test-value')
        manager = self.module.MaintenanceManager(root, running=lambda: False)
        backup = manager.backup()
        vault.set('test.credential', 'replaced-test-value')
        manager.restore(backup['id'])
        self.assertEqual('only-a-test-value', vault.get('test.credential'))


if __name__ == '__main__': unittest.main()

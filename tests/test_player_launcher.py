import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from personal_cinema.credentials import SecretStore


class PlayerCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cinema-player-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SecretStore(self.root)
        self.module = importlib.import_module('personal_cinema.player_launcher')

    def test_profile_registry_supports_large_payload_and_keeps_backup_encrypted(self):
        registry = self.module.ProfileVault(self.store, self.root / 'player')
        payload = {'users': [{'credentials': [{'AccessToken': 'private-player-token'}],
                              'name': '影院' * 45000}], 'active': 'profile-one'}
        registry.write(payload)
        self.assertEqual(registry.read(), payload)
        registry.write({'users': [], 'active': None})
        self.assertEqual(registry.read('backup'), payload)
        for file in self.root.rglob('*.json'):
            self.assertNotIn('private-player-token', file.read_text('utf-8'))

    def test_authorized_profile_uses_configured_reference_and_gateway(self):
        self.store.set('custom.jellyfin', 'private-player-token')
        self.store.set('jellyfin.user_id', 'user-one')
        self.store.set('jellyfin.server_id', 'server-one')
        profile = self.module.authorized_credential({'foundation.jellyfin_credential': 'custom.jellyfin',
            'foundation.jellyfin_username': 'Viewer'}, self.store, 'http://127.0.0.1:18797')
        self.assertEqual(profile['AccessToken'], 'private-player-token')
        self.assertEqual(profile['address'], 'http://127.0.0.1:18797')
        self.assertEqual(profile['UserId'], 'user-one')
        self.assertEqual(profile['Id'], 'server-one')
        with self.assertRaises(ValueError):
            self.module.authorized_credential({}, self.store, 'https://external.invalid')

    def test_missing_authorized_identity_fails_without_launching(self):
        self.store.set('jellyfin.token', 'private-player-token')
        with self.assertRaisesRegex(ValueError, '登录'):
            self.module.authorized_credential({}, self.store, 'http://127.0.0.1:18797')

    def test_users_migrate_legacy_and_preserve_profiles_without_plaintext(self):
        vendor = Path(__file__).resolve().parents[1] / 'vendor'
        sys.path.insert(0, str(vendor))
        self.addCleanup(sys.path.remove, str(vendor))
        from jellyfin_mpv_shim import users
        player_dir = self.root / 'player'; player_dir.mkdir()
        legacy = {'active': 'first', 'users': [{'id': 'first', 'name': '原来用户',
            'device_id': 'device-one', 'credentials': [{'AccessToken': 'legacy-private-token',
                'Id': 'server-one', 'UserId': 'user-one', 'uuid': 'login-one'}]}]}
        old = player_dir / 'users.json'
        old.write_text(json.dumps(legacy), encoding='utf-8')
        with patch.object(users.conffile, 'get', side_effect=lambda app, name: str(player_dir / name)), \
             patch.object(users, '_credential_vault', return_value=self.store):
            manager = users.UserManager(); manager.load()
            self.assertEqual(manager.active_user['name'], '原来用户')
            self.assertEqual(manager.active_user['credentials'][0]['AccessToken'], 'legacy-private-token')
            self.assertFalse(old.exists())
            second = manager.add_user('另外一位')
            manager.set_active(second['id'])
            loaded = users.UserManager(); loaded.load()
            self.assertEqual(loaded.active_id, second['id'])
            self.assertEqual(len(loaded.users), 2)
        for file in self.root.rglob('*.json'):
            self.assertNotIn('legacy-private-token', file.read_text('utf-8'))

    def test_main_hands_off_to_native_without_secrets_in_arguments(self):
        from personal_cinema import runtime_paths, windows_bootstrap
        directory = self.root / 'runtime/data/mpv-shim'
        self.store.set('jellyfin.token', 'secret-native-launch')
        self.store.set('jellyfin.user_id', 'user-one')
        self.store.set('jellyfin.server_id', 'server-one')
        native_args = types.SimpleNamespace(_args=None)
        package = types.ModuleType('jellyfin_mpv_shim'); package.args = native_args
        manager = Mock()
        native_main = Mock()
        startup = Mock()
        startup.attach_mock(native_main, 'native_main')
        startup.attach_mock(Mock(), 'prepare_process')
        startup.attach_mock(Mock(), 'prepare_window')
        modules = {'jellyfin_mpv_shim': package,
            'jellyfin_mpv_shim.conf': types.SimpleNamespace(settings=types.SimpleNamespace(load=Mock())),
            'jellyfin_mpv_shim.users': types.SimpleNamespace(userManager=manager),
            'jellyfin_mpv_shim.mpv_shim': types.SimpleNamespace(main=native_main)}
        with patch.dict(sys.modules, modules), patch.object(sys, 'argv', ['test']), \
             patch.object(windows_bootstrap, 'prepare_process', startup.prepare_process), \
             patch.object(windows_bootstrap, 'prepare_window', startup.prepare_window), \
             patch.object(runtime_paths, 'root_path', return_value=str(directory)), \
             patch.object(runtime_paths, 'vault', return_value=self.store), \
             patch.object(runtime_paths, 'settings', return_value={}), \
             patch.object(runtime_paths, 'endpoint', return_value='http://127.0.0.1:18797'), \
             patch.object(self.module, 'synchronize_profile') as sync:
            self.module.main(['--config', str(directory)])
            native_main.assert_called_once_with()
            self.assertEqual([call[0] for call in startup.mock_calls],
                             ['prepare_process', 'prepare_window', 'native_main'])
            startup.prepare_window.assert_called_once_with(directory.resolve())
            self.assertEqual(sync.call_args.args[1]['AccessToken'], 'secret-native-launch')
            self.assertNotIn('secret-native-launch', ' '.join(sys.argv))

    def test_damaged_encrypted_registry_cannot_silently_be_read_as_empty(self):
        registry = self.module.ProfileVault(self.store, self.root / 'player')
        registry.write({'users': ['first']})
        pointer = registry._pointer('current')
        self.store.delete(pointer['chunks'][0])
        with self.assertRaises(Exception):
            registry.read()


if __name__ == '__main__':
    unittest.main()

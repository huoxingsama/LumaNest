"""The same distribution can adopt data without adopting a second code tree."""
import json
import os
import socket
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from personal_cinema.configuration import ConfigStore
from personal_cinema.credentials import SecretStore
from personal_cinema.runtime import Runtime
from personal_cinema.runtime_paths import instance_context, root_path


class Cipher:
    def encrypt(self, value): return b'fixture:' + value[::-1]
    def decrypt(self, value): return value[len(b'fixture:'):][::-1]


class ExistingInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home, self.data = self.base / 'settings', self.base / 'media-data'
        self.config = ConfigStore(self.home)
        self.vault = SecretStore(self.home, Cipher())
        self.native = self.data / 'data/mpv-shim'
        self.native.mkdir(parents=True)
        self.dll = self.base / 'player/mpv-2.dll'
        self.dll.parent.mkdir()
        self.dll.write_bytes(b'synthetic library')

    def put(self, relative, value):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), 'utf-8')
        return path

    def configure(self, **extra):
        values = {'foundation.runtime_directory': str(self.data),
                  'foundation.mpv_library_path': str(self.dll),
                  'foundation.gateway_url': 'http://127.0.0.1:18097',
                  'network.stream_url': 'http://192.0.2.42:18066',
                  'network.stream_bind_host': '0.0.0.0'}
        values.update(extra)
        return self.config.update(values, self.config.read()['revision'])

    def test_runtime_paths_use_configured_root_and_explicit_instance_beats_environment(self):
        self.configure()
        with patch.dict(os.environ, {'MEDIA_LIBRARY_ROOT': str(self.base / 'wrong')}):
            with instance_context(self.home):
                self.assertEqual(Path(root_path('data')), self.data / 'data')
        runtime = Runtime(self.home, self.config, self.vault, None)
        self.assertEqual(runtime.folder, self.data)
        self.assertEqual(runtime.environment()['MEDIA_LIBRARY_ROOT'], str(self.data))

    def test_apply_preserves_unknown_user_options_and_manual_overrides(self):
        self.configure()
        self.put('data/chinese-credits-config.json', {'source_urls': {'movie': 'https://example.org/person'}, 'max_cast': 42})
        self.put('data/cache-config.json', {'download_connections': 6, 'custom_existing': True})
        self.put('data/mpv-shim/conf.json', {'hide_title_bar': True, 'window_controls': 'always', 'volume': 77})
        (self.native / 'mpv.conf').write_text('# personal\nvolume=77\nbrightness=9\n[extension]\nbrightness=4\n', 'utf-8')
        runtime = Runtime(self.home, self.config, self.vault, None)
        runtime.apply()
        native = json.loads((self.native / 'conf.json').read_text('utf-8'))
        self.assertFalse(native['mpv_ext'])
        self.assertEqual(native['theme'], 'lumanest')
        self.assertEqual(native['poster_scale'], 1.3)
        self.assertTrue(native['hide_title_bar'])
        self.assertEqual(native['window_controls'], 'always')
        self.assertEqual(native['volume'], 77)
        self.assertEqual(json.loads((self.data / 'data/chinese-credits-config.json').read_text())['max_cast'], 42)
        cache = json.loads((self.data / 'data/cache-config.json').read_text())
        self.assertEqual(cache['download_connections'], 6)
        self.assertTrue(cache['custom_existing'])
        mpv = (self.native / 'mpv.conf').read_text('utf-8')
        self.assertIn('volume=77', mpv)
        self.assertIn('[extension]\nbrightness=4', mpv)
        self.assertIn('brightness=0', mpv)
        self.assertNotIn('brightness=9', mpv)
        manifest = json.loads((self.home / 'runtime-manifest.json').read_text())
        self.assertEqual(manifest['endpoints']['gateway'], 'http://127.0.0.1:18097')
        self.assertEqual(manifest['endpoints']['stream'], 'http://192.0.2.42:18066')
        self.assertEqual(manifest['bind_hosts']['stream'], '0.0.0.0')

    def test_embedded_player_does_not_require_external_mpv(self):
        self.configure()
        runtime = Runtime(self.home, self.config, self.vault, None)
        with patch.object(runtime, 'start'), patch.object(runtime, '_spawn') as spawn:
            runtime.launch_player()
        self.assertIn('personal_cinema.player_launcher', spawn.call_args.args[1])
        self.assertEqual(runtime.environment()['PATH'].split(os.pathsep)[0], str(self.dll.parent))

    def test_embedded_probe_checks_the_library_in_child_process(self):
        from personal_cinema.integrations import IntegrationManager
        self.configure()
        manager = IntegrationManager(self.home, self.config, self.vault)
        with patch('personal_cinema.integrations.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='libmpv-api 131072\n')) as run:
            result = manager.perform('mpv', 'probe')
        self.assertEqual(result['backend'], 'embedded')
        self.assertEqual(run.call_args.args[0][-1], str(self.dll))
        self.assertIn('mpv_client_api_version', run.call_args.args[0][-2])
        self.assertNotIn('install_plan', next(row for row in manager.status() if row['id'] == 'mpv')['actions'])
        with self.assertRaises(ValueError): manager.perform('mpv', 'install_plan')

    def test_bundled_library_is_found_without_external_executable(self):
        from personal_cinema.embedded_mpv import library_path
        with patch('personal_cinema.embedded_mpv.sys.prefix', str(self.base)):
            self.assertEqual(library_path({}), self.dll)

    def test_source_policy_uses_instance_home_with_external_runtime_directory(self):
        import sys
        legacy = Path(__file__).resolve().parents[1] / 'src/personal_cinema/legacy'
        with patch.object(sys, 'path', [str(legacy)] + sys.path):
            import source_policy
        self.configure(**{'sources.entries': [{'id': 'chosen', 'name': 'Chosen', 'kind': 'openlist', 'path': '/films', 'enabled': True, 'allow_write': False}]})
        with instance_context(self.home):
            self.assertEqual(source_policy.source_roots(self.data), ['/films'])
            with self.assertRaises(ValueError): source_policy.source_roots(self.base / 'other')

    def test_invalid_listeners_cannot_be_saved(self):
        for values in ({'network.gateway_bind_host': '192.0.2.9'}, {'network.stream_bind_host': 'https://example.invalid'},
                       {'foundation.gateway_url': 'http://example.invalid:80'}, {'network.stream_url': 'https://example.invalid:443/route'}):
            with self.assertRaises(ValueError): self.config.update(values, 0)

    def test_old_listening_backend_blocks_apply_before_existing_data_changes(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            self.configure(**{'foundation.gateway_url': 'http://127.0.0.1:' + str(listener.getsockname()[1])})
            conf = self.put('data/mpv-shim/conf.json', {'theme': 'my-custom-theme'})
            before = conf.read_bytes()
            runtime = Runtime(self.home, self.config, self.vault, None)
            with self.assertRaisesRegex(ValueError, '旧版本'):
                runtime.apply()
            self.assertEqual(conf.read_bytes(), before)
            self.assertFalse((self.home / 'runtime-manifest.json').exists())

    def test_runtime_directory_cannot_silently_move_existing_data(self):
        self.configure()
        runtime = Runtime(self.home, self.config, self.vault, None)
        runtime.apply()
        self.config.update({'foundation.runtime_directory': str(self.base / 'other')}, self.config.read()['revision'])
        with self.assertRaisesRegex(ValueError, '迁移'):
            runtime.apply()
        self.assertEqual(Runtime(self.home, self.config, self.vault, None).folder, self.data)

    def test_import_and_apply_preserve_effective_include_settings(self):
        from personal_cinema.existing_installation import ExistingInstallation
        from personal_cinema.mpv_config import global_options
        included = self.base / 'existing-mpv.conf'
        included.write_text('demuxer-max-bytes=1GiB\nslang=zh-Hans,cmn-Hans,eng\nbrightness=7\n[cinema]\nbrightness=20\n', 'utf-8')
        self.put('data/mpv-shim/conf.json', {'lang': 'zh_Hans', 'mpv_ext': False})
        (self.native / 'mpv.conf').write_text('include=' + str(included) + '\n', 'utf-8')
        self.put('data/cache-config.json', {'pause_cache_for_steam': True, 'session_poll_seconds': 2})
        importer = ExistingInstallation(self.home, self.data, credentials=self.vault)
        importer.apply()
        self.assertEqual(self.config.get_effective()['playback.subtitle_languages'], ['zh-Hans','cmn-Hans','eng'])
        Runtime(self.home, self.config, self.vault, None).apply()
        actual = global_options(self.native / 'mpv.conf')
        self.assertEqual(actual['demuxer-max-bytes'], '1GiB')
        self.assertEqual(actual['slang'], 'zh-Hans,cmn-Hans,eng')
        self.assertEqual(actual['brightness'], '7')
        self.assertEqual(json.loads((self.native / 'conf.json').read_text())['lang'], 'zh_Hans')
        cache = json.loads((self.data / 'data/cache-config.json').read_text())
        self.assertTrue(cache['pause_cache_for_steam'])
        self.assertEqual(cache['session_poll_seconds'], 2)
        self.assertIn('[cinema]\nbrightness=20', included.read_text())

    def test_import_dry_run_is_read_only_and_apply_keeps_ids_and_encrypts_tokens(self):
        from personal_cinema.existing_installation import ExistingInstallation
        self.put('data/mpv-shim/conf.json', {'mpv_ext': False, 'theme': 'cinema-night', 'poster_scale': None,
            'hide_title_bar': True, 'window_controls': 'always', 'hwdec': 'auto-copy'})
        self.put('data/cache-config.json', {'max_cache_bytes': 400_000_000_000,
            'reserve_free_bytes': 100_000_000_000, 'playback_buffer_bytes': 10 * 1024**3,
            'source_roots': ['/example/films'], 'stream_base_url': 'http://192.0.2.42:18066'})
        cred = {'Id': 'server1', 'UserId': 'user1', 'AccessToken': 'private-player', 'address': 'http://127.0.0.1:18097', 'username': 'viewer', 'uuid': 'known-device'}
        self.put('secrets/accounts.json', {'jellyfin_admin': {'username': 'admin', 'password': 'private-admin-pass'},
                                         'jellyfin': {'username': 'viewer', 'password': 'private-viewer-pass'}})
        profiles = {'active': 'profile1', 'users': [{'id': 'profile1', 'name': 'Viewer', 'pin_hash': 'fixture-pin', 'credentials': [cred]}]}
        old_users = self.put('data/mpv-shim/users.json', profiles)
        for name, value in [('jellyfin-admin-token.txt', 'private-admin'), ('stream-key.txt', 'private-stream')]:
            path = self.data / 'secrets' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(value)
        importer = ExistingInstallation(self.home, self.data, credentials=self.vault,
            options={'foundation.jellyfin_url': 'http://127.0.0.1:18096', 'foundation.mpv_library_path': str(self.dll)})
        preview = importer.preview()
        self.assertFalse(self.config.path.exists())
        self.assertFalse(self.vault.path.exists())
        self.assertNotIn('private-player', json.dumps(preview))
        self.assertNotIn('private-admin', json.dumps(preview))
        result = importer.apply(expected_revision=0)
        self.assertEqual(result['state'], 'imported')
        values = self.config.get_effective('device')
        self.assertEqual(values['appearance.theme'], 'lumanest')
        self.assertEqual(values['appearance.poster_scale'], 1.3)
        self.assertEqual(values['cache.offline_gib'], 373)
        self.assertEqual(values['cache.reserve_gib'], 94)
        self.assertEqual(values['playback.hwdec'], 'auto-copy')
        self.assertFalse(values['sources.entries'][0]['allow_write'])
        self.assertEqual(self.vault.get('stream.key'), 'private-stream')
        self.assertEqual(self.vault.get('jellyfin.token'), 'private-admin')
        self.assertEqual(self.vault.get('player.jellyfin.token'), 'private-player')
        self.assertEqual(self.vault.get('player.jellyfin.password'), 'private-viewer-pass')
        self.assertEqual(self.vault.get('jellyfin.password'), 'private-admin-pass')
        from personal_cinema.player_launcher import ProfileVault, authorized_credential
        stored = ProfileVault(self.vault, self.native).read()
        self.assertEqual(stored['active'], 'profile1')
        self.assertEqual(stored['users'][0]['pin_hash'], 'fixture-pin')
        self.assertEqual(stored['users'][0]['credentials'][0]['uuid'], 'known-device')
        self.assertEqual(authorized_credential(values, self.vault, values['foundation.gateway_url'])['AccessToken'], 'private-player')
        self.assertEqual(json.loads(old_users.read_text()), profiles)
        self.assertFalse((self.home / 'setup-complete.json').exists())

    def test_import_does_not_override_existing_configuration_or_mutate_data_on_error(self):
        from personal_cinema.existing_installation import ExistingInstallation
        self.config.update({'appearance.poster_scale': 1.6}, 0)
        before = self.config.path.read_bytes()
        importer = ExistingInstallation(self.home, self.data, credentials=self.vault)
        with self.assertRaises(ValueError): importer.apply(expected_revision=1)
        self.assertEqual(self.config.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

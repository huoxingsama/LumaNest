import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('personal_cinema.runtime'), 'Runtime settings bridge is missing')
        from personal_cinema.configuration import ConfigStore
        from personal_cinema.credentials import SecretStore
        from personal_cinema.runtime import Runtime
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ConfigStore(self.root)
        self.secrets = SecretStore(self.root)
        self.runtime = Runtime(self.root, self.config, self.secrets, None)

    def tearDown(self):
        if hasattr(self, 'temp'):
            self.temp.cleanup()

    def test_apply_generates_real_runtime_settings_without_plaintext_credentials(self):
        current = self.config.read()
        self.config.update({'cache.playback_gib': 8, 'cache.next_episodes': 3,
                            'playback.hdr_output': 'sdr', 'appearance.text_scale': 1.1}, current['revision'])
        self.secrets.set('jellyfin.password', 'test-private-pass')
        result = self.runtime.apply()
        self.assertEqual(result['state'], 'applied')
        cache = json.loads((self.root / 'runtime/data/cache-config.json').read_text())
        mpv = json.loads((self.root / 'runtime/data/mpv-shim/conf.json').read_text())
        self.assertEqual(cache['playback_buffer_bytes'], 8 * 1024 ** 3)
        self.assertEqual(cache['next_episodes'], 3)
        self.assertEqual(mpv['hdr_output'], 'sdr')
        self.assertEqual(mpv['ui_text_scale'], 1.1)
        self.assertFalse((self.root / 'runtime/secrets/accounts.json').exists())
        for file in (self.root / 'runtime').rglob('*.json'):
            self.assertNotIn('test-private-pass', file.read_text())

    def test_secret_compatibility_reads_are_decrypted_in_memory_only(self):
        from personal_cinema.runtime_paths import RuntimePath
        self.secrets.set('stream.key', 'private-buffer-key')
        with patch.dict(os.environ, {'PERSONAL_CINEMA_HOME': str(self.root), 'MEDIA_LIBRARY_ROOT': str(self.root / 'runtime')}):
            path = RuntimePath(self.root / 'runtime/secrets/stream-key.txt')
            self.assertEqual(path.read_text(), 'private-buffer-key')
            self.assertTrue(path.exists())
            path.write_text('replacement-private-key')
            self.assertEqual(self.secrets.get('stream.key'), 'replacement-private-key')
            self.assertFalse(Path(str(path)).exists())

    def test_active_playback_defers_reconfiguration(self):
        self.runtime.apply()
        path = self.root / 'runtime/data/cache-status.json'
        path.write_text(json.dumps({'playback_present': True, 'playback_paused': True}))
        current = self.config.read()
        self.config.update({'cache.playback_gib': 6}, current['revision'])
        result = self.runtime.apply()
        self.assertEqual(result['state'], 'waiting_for_playback')
        cache = json.loads((self.root / 'runtime/data/cache-config.json').read_text())
        self.assertNotEqual(cache['playback_buffer_bytes'], 6 * 1024 ** 3)

    def test_tasks_cannot_be_queued_before_configured_runtime(self):
        with self.assertRaises(ValueError):
            self.runtime.task_command('scan', {})

    def test_device_overrides_reach_actual_runtime_configuration(self):
        current = self.config.read()
        self.config.update({'cache.playback_gib': 12, 'playback.audio_languages': ['eng', 'jpn']}, current['revision'], 'device')
        self.runtime.apply()
        cache = json.loads((self.root / 'runtime/data/cache-config.json').read_text())
        self.assertEqual(cache['playback_buffer_bytes'], 12 * 1024 ** 3)
        self.assertIn('alang=eng,jpn', (self.root / 'runtime/data/mpv-shim/mpv.conf').read_text())

    def test_supervisor_retries_only_desired_workers_and_obeys_limit(self):
        from personal_cinema.runtime import write
        self.runtime.apply()
        write(self.root / 'runtime-desired.json', {'enabled': True, 'workers': ['gateway']})
        with patch.object(self.runtime, '_alive', return_value=False), patch.object(self.runtime, '_spawn') as spawn:
            self.runtime.supervise(now=1000)
            self.assertEqual(spawn.call_count, 1)
            self.runtime.supervise(now=1001)
            self.assertEqual(spawn.call_count, 1)
            self.runtime.supervise(now=2000)
            self.runtime.supervise(now=3000)
            self.runtime.supervise(now=4000)
            self.assertEqual(spawn.call_count, 3)
            self.assertEqual(spawn.call_args.args[0], 'gateway')


if __name__ == '__main__':
    unittest.main()

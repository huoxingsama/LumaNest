"""An apply receipt must describe the values written, not a newer save."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from personal_cinema.configuration import ConfigStore
from personal_cinema.runtime import Runtime


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class RuntimeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ConfigStore(self.root)
        self.runtime = Runtime(self.root, self.config, MemoryCredentials(), None)
        for method in ('playing', '_alive'):
            mock = patch.object(self.runtime, method, return_value=False)
            mock.start()
            self.addCleanup(mock.stop)

    def test_effective_snapshot_carries_revision_and_device_values(self):
        self.config.update({'cache.playback_gib': 4}, 0)
        self.config.update({'cache.playback_gib': 8}, 1, 'device')
        snapshot = self.config.effective_snapshot('device')
        self.assertEqual(snapshot['revision'], 2)
        self.assertEqual(snapshot['values']['cache.playback_gib'], 8)
        self.config.update({'cache.playback_gib': 12}, 2, 'device')
        self.assertEqual(snapshot['revision'], 2)
        self.assertEqual(snapshot['values']['cache.playback_gib'], 8)
        self.assertEqual(self.config.effective_snapshot('device')['revision'], 3)

    def test_save_during_manifest_generation_remains_pending(self):
        self.config.update({'cache.playback_gib': 4}, 0)
        other_window = ConfigStore(self.root)
        calls = []

        def port():
            if not calls:
                other_window.update({'cache.playback_gib': 12}, 1)
            calls.append(True)
            return 20000 + len(calls)

        with patch('personal_cinema.runtime.free_port', side_effect=port):
            result = self.runtime.apply()
        self.assert_written_revision(result, 1, 4)
        self.assertEqual(self.runtime.status()['saved_revision'], 2)
        self.assertTrue(self.runtime.status()['pending'])
        result = self.runtime.apply()
        self.assert_written_revision(result, 2, 12)
        self.assertFalse(self.runtime.status()['pending'])

    def test_save_before_receipt_is_not_misreported_as_applied(self):
        self.config.update({'cache.playback_gib': 4}, 0)
        from personal_cinema import runtime
        copyfile = runtime.shutil.copyfile

        def late_save(source, destination):
            ConfigStore(self.root).update({'cache.playback_gib': 16}, 1)
            return copyfile(source, destination)

        with patch('personal_cinema.runtime.shutil.copyfile', side_effect=late_save):
            result = self.runtime.apply()
        self.assert_written_revision(result, 1, 4)
        self.assertTrue(self.runtime.status()['pending'])

    def assert_written_revision(self, result, revision, gib):
        self.assertEqual(result['revision'], revision)
        for name in ('runtime-manifest.json', 'runtime-applied.json'):
            value = json.loads((self.root / name).read_text('utf-8'))
            self.assertEqual(value['revision'], revision, name)
        cache = json.loads((self.root / 'runtime/data/cache-config.json').read_text('utf-8'))
        self.assertEqual(cache['playback_buffer_bytes'], gib * 1024 ** 3)


if __name__ == '__main__':
    unittest.main()

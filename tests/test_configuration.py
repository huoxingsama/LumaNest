import copy
import importlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('personal_cinema.configuration')
            self.schema_module = importlib.import_module('personal_cinema.schema')
        except ImportError:
            self.fail('Unified configuration has not been implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.module.ConfigStore(self.root)

    def test_defaults_have_all_twelve_sections_and_are_independent(self):
        self.assertEqual(len(self.schema_module.SECTIONS), 12)
        state = self.store.read()
        self.assertEqual(state['revision'], 0)
        self.assertEqual(state['values']['cache.playback_gib'], 10)
        self.assertNotIn('E:', state['values']['cache.directory'])
        state['values']['metadata.languages'].append('bad')
        self.assertNotIn('bad', self.store.read()['values']['metadata.languages'])

    def test_atomic_revision_update_and_optimistic_conflict(self):
        result = self.store.update({'cache.playback_gib': 12}, 0)
        self.assertEqual(result['revision'], 1)
        self.assertEqual(self.module.ConfigStore(self.root).read(), result)
        with self.assertRaises(self.module.RevisionConflict):
            self.store.update({'cache.playback_gib': 5}, 0)
        self.assertEqual(self.store.read()['values']['cache.playback_gib'], 12)

    def test_concurrent_instances_do_not_lose_update(self):
        barrier = threading.Barrier(2)
        results = []
        def write(value):
            barrier.wait()
            try:
                self.module.ConfigStore(self.root).update({'cache.playback_gib': value}, 0)
                results.append('ok')
            except self.module.RevisionConflict:
                results.append('conflict')
        threads = [threading.Thread(target=write, args=(value,)) for value in (8, 12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(8)
        self.assertCountEqual(results, ['ok', 'conflict'])
        self.assertEqual(self.store.read()['revision'], 1)

    def test_reentrant_read_keeps_outer_lock_until_other_writer_can_proceed(self):
        entered, done = threading.Event(), threading.Event()
        errors = []

        def update():
            entered.set()
            try: self.module.ConfigStore(self.root).update({'cache.playback_gib': 8}, 0)
            except Exception as error: errors.append(error)
            finally: done.set()

        with self.module._file_lock(self.store.lock_path):
            self.assertEqual(self.store.read()['revision'], 0)
            writer = threading.Thread(target=update)
            writer.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(done.wait(.05))
            self.assertEqual(self.store.effective_snapshot()['revision'], 0)
            self.assertFalse(done.is_set())
        writer.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.read()['revision'], 1)

    def test_validation_does_not_mutate_and_rejects_bool_as_int(self):
        for changes in ({'cache.playback_gib': True}, {'cache.playback_gib': -1},
                        {'missing.setting': 1}, {'playback.hdr_output': 'fake'},
                        {'foundation.jellyfin_url': 'http://' + 'user:pass@localhost:8096'},
                        {'foundation.jellyfin_url': 'http://localhost:8096?token=secret'}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError): self.store.update(changes, 0)
        self.assertEqual(self.store.read()['revision'], 0)

    def test_per_scope_inheritance_and_reset(self):
        # The inheritance engine supports ancestry, but no current runtime
        # consumer offers media overrides. Explicitly enable this fixture only.
        fields = self.schema_module.BY_KEY
        scope = patch.dict(fields, {'metadata.languages': {**fields['metadata.languages'],
            'scope': ['global', 'source', 'item', 'season']}})
        scope.start(); self.addCleanup(scope.stop)
        self.store.update({'metadata.languages': ['zh-CN', 'en']}, 0)
        self.store.update({'metadata.languages': ['ja', 'en']}, 1, 'source:films')
        self.store.update({'metadata.languages': ['fr']}, 2, 'item:film1')
        self.assertEqual(self.store.get_effective('season:s1', parents=['source:films', 'item:film1'])['metadata.languages'], ['fr'])
        self.store.update({'metadata.languages': None}, 3, 'item:film1')
        self.assertEqual(self.store.get_effective('item:film1', parents=['source:films'])['metadata.languages'], ['ja', 'en'])
        with self.assertRaises(ValueError):
            self.store.update({'network.mode': 'system'}, 4, 'item:film1')
        with self.assertRaises(ValueError):
            self.store.read_scope('../escape')

    def test_source_schema_rejects_secret_and_dangerous_paths(self):
        source = {'id': 'films', 'name': '电影', 'kind': 'openlist', 'path': '/Movies',
                  'enabled': True, 'allow_write': False}
        self.store.update({'sources.entries': [source]}, 0)
        for edit in ({'path': '/Movies/../Secrets'}, {'token': 'test'},
                     {'kind': 'arbitrary-shell'}, {'path': '/', 'allow_write': True}):
            with self.subTest(edit=edit):
                with self.assertRaises(ValueError):
                    self.store.update({'sources.entries': [dict(source, **edit)]}, 1)
        with self.assertRaises(ValueError):
            self.store.update({'sources.entries': [source, source]}, 1)

    def test_export_excludes_identity_and_import_is_revision_guarded(self):
        self.store.update({'foundation.jellyfin_username': 'sample-user', 'cache.playback_gib': 11}, 0)
        exported = self.store.export()
        self.assertNotIn('foundation.jellyfin_username', exported['values'])
        self.assertEqual(exported['values']['cache.playback_gib'], 11)
        exported['values']['cache.playback_gib'] = 14
        result = self.store.import_data(exported, 1)
        self.assertEqual(result['values']['cache.playback_gib'], 14)
        self.assertEqual(result['values']['foundation.jellyfin_username'], 'sample-user')
        with self.assertRaises(self.module.RevisionConflict): self.store.import_data(exported, 1)
        invalid = copy.deepcopy(exported)
        invalid['values']['secret.token'] = 'sample'
        with self.assertRaises(ValueError): self.store.import_data(invalid, 2)

    def test_unknown_newer_version_and_corruption_preserve_file(self):
        self.store.update({'cache.playback_gib': 8}, 0)
        file = self.root / 'config.json'
        file.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError): self.store.read()
        self.assertEqual(file.read_text('utf-8'), '{broken')
        file.write_text(json.dumps({'version': 100, 'revision': 1, 'values': {}}), 'utf-8')
        with self.assertRaises(ValueError): self.store.read()

    def test_failed_replace_preserves_previous_state_and_cleans_temp(self):
        self.store.update({'cache.playback_gib': 8}, 0)
        old = (self.root / 'config.json').read_bytes()
        with patch.object(self.module.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): self.store.update({'cache.playback_gib': 16}, 1)
        self.assertEqual((self.root / 'config.json').read_bytes(), old)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_previous_schema_migrates_without_silently_losing_unknown_fields(self):
        file = self.root / 'config.json'
        file.write_text(json.dumps({'version': 0, 'revision': 4, 'values': {
            'jellyfin_url': 'http://localhost:8096', 'cache_gib': 9}}), 'utf-8')
        state = self.store.read()
        self.assertEqual(state['version'], 1)
        self.assertEqual(state['revision'], 4)
        self.assertEqual(state['values']['cache.playback_gib'], 9)
        self.store.update({'cache.playback_gib': 10}, 4)
        self.assertEqual(json.loads(file.read_text('utf-8'))['version'], 1)

    def test_unavailable_control_cannot_claim_successful_change(self):
        with self.assertRaisesRegex(ValueError, '尚未'):
            self.store.update({'maintenance.data_root': 'C:/OtherCinema'}, 0)
        self.assertEqual(self.store.read()['revision'], 0)

    def test_source_does_not_allow_write_permission_on_entire_local_drive(self):
        source = {'id': 'drive', 'name': 'drive', 'kind': 'local', 'path': 'C:\\',
                  'enabled': True, 'allow_write': True}
        with self.assertRaises(ValueError):
            self.store.update({'sources.entries': [source]}, 0)

    def test_bad_source_url_type_is_a_validation_error(self):
        source = {'id': 'dav', 'name': 'dav', 'kind': 'openlist', 'path': '/media',
                  'enabled': True, 'allow_write': False, 'url': 123}
        with self.assertRaises(ValueError):
            self.store.update({'sources.entries': [source]}, 0)

    def test_source_native_root_requires_a_bounded_safe_string(self):
        source = {'id': 'cloud', 'name': 'cloud', 'kind': 'openlist', 'path': '/media',
                  'enabled': True, 'allow_write': True, 'driver': 'Quark', 'native_root': 'abcdef1234'}
        self.store.update({'sources.entries': [source]}, 0)
        for root in ('/root/../private', 'abc\x00def', 'root\nother', 123, 'a' * 1025):
            with self.subTest(root=str(root)[:20]):
                with self.assertRaises(ValueError):
                    self.store.update({'sources.entries': [{**source, 'native_root': root}]}, 1)

    def test_unsupported_webdav_and_local_writes_cannot_be_configured(self):
        source = {'id': 'media', 'name': 'media', 'kind': 'webdav', 'path': '/media',
                  'enabled': True, 'allow_write': False}
        with self.assertRaisesRegex(ValueError, 'OpenList'):
            self.store.update({'sources.entries': [source]}, 0)
        with self.assertRaisesRegex(ValueError, '只读'):
            self.store.update({'sources.entries': [{**source, 'kind': 'local', 'path': 'C:/Media', 'allow_write': True}]}, 0)

    def test_portable_export_never_contains_any_local_executable_path(self):
        self.store.update({'foundation.jellyfin_path': 'C:/PrivateUser/Apps/jellyfin.exe'}, 0)
        self.assertNotIn('PrivateUser', json.dumps(self.store.export()))


if __name__ == '__main__': unittest.main()

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from personal_cinema.configuration import ConfigStore

LEGACY = Path(__file__).resolve().parents[1] / 'src/personal_cinema/legacy'
if str(LEGACY) not in sys.path:
    sys.path.insert(0, str(LEGACY))
import source_policy
from quark_manage import Quark


class SourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cinema-sources-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'runtime'
        self.root.mkdir()
        self.config = ConfigStore(self.root.parent)
        self.entry = {'id': 'chosen', 'name': '我的云盘', 'kind': 'openlist', 'path': '/custom/films',
            'allow_write': True, 'enabled': True, 'driver': 'Quark', 'native_root': 'folder7'}
        self.update({'sources.entries': [self.entry], 'organizing.enabled': True})
        self.manager = Quark(self.root, entry=self.entry, auth={'cookie': 'synthetic-cookie'})
        self.manager._remember('video1', '/custom/films/old.mkv', False, 500)
        self.manager._remember('empty1', '/custom/films/old-empty', True)

    def update(self, changes):
        self.config.update(changes, self.config.read()['revision'])

    def test_preview_never_calls_remote_mutation_until_exact_plan_approved(self):
        with patch.object(self.manager, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, '预览'):
                self.manager.rename('video1', 'new.mkv')
            api.assert_not_called()
            plan = {'source_id': 'chosen', 'operations': [{'fid': 'video1', 'source': '/custom/films/old.mkv',
                'target': '/custom/films/new.mkv', 'size': 500}]}
            path = source_policy.journal_path(self.root, 'chosen')
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(plan), encoding='utf-8')
            ident = source_policy.plan_id(plan)
            source_policy.approve_plan(self.root, 'chosen', ident)
            self.manager.approval = ident
            self.manager.rename('video1', 'new.mkv')
            api.assert_called_once()
            self.assertEqual(self.manager._path('video1'), '/custom/films/new.mkv')

    def test_revoking_write_permission_takes_effect_on_existing_manager(self):
        self.update({'organizing.preview_only': False})
        self.entry['allow_write'] = False
        self.update({'sources.entries': [self.entry]})
        with patch.object(self.manager, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, '只读'):
                self.manager.rename('video1', 'new.mkv')
            api.assert_not_called()

    def test_root_unknown_and_nested_objects_cannot_be_mutated(self):
        self.update({'organizing.preview_only': False})
        with patch.object(self.manager, 'api') as api:
            for fid in ('folder7', 'unlisted'):
                with self.assertRaises(RuntimeError):
                    self.manager.rename(fid, 'changed')
            nested = dict(self.entry, id='nested', path='/custom/films/protected', allow_write=False)
            self.update({'sources.entries': [self.entry, nested]})
            self.manager._remember('nested-file', '/custom/films/protected/video.mkv', False, 10)
            with self.assertRaisesRegex(RuntimeError, '嵌套'):
                self.manager.rename('nested-file', 'changed.mkv')
            api.assert_not_called()

    def test_cleanup_requires_option_and_rechecks_empty_directory(self):
        self.update({'organizing.preview_only': False})
        with patch.object(self.manager, 'api', return_value={'data': {}}) as api, patch.object(self.manager, 'listing', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, '回收'):
                self.manager.recycle(['empty1'])
            api.assert_not_called()
            self.update({'organizing.cleanup_empty': True})
            with self.assertRaisesRegex(RuntimeError, '空'):
                self.manager.recycle(['video1'])
            api.assert_not_called()
            self.manager.recycle(['empty1'])
            api.assert_called_once()
        with patch.object(self.manager, 'api') as api, patch.object(self.manager, 'listing', return_value=[{'fid':'x'}]):
            with self.assertRaisesRegex(RuntimeError, '空'):
                self.manager.recycle(['empty1'])
            api.assert_not_called()

    def test_old_mount_names_are_not_assumed_and_other_drivers_fail_closed(self):
        with self.assertRaises(RuntimeError):
            Quark(self.root)
        self.assertEqual(self.manager.source_root, '/custom/films')
        self.assertEqual(self.manager.movie_root_fid, 'folder7')
        unsupported = dict(self.entry, driver='UnknownProvider')
        self.update({'sources.entries': [unsupported]})
        worker = type('Worker', (), {'root': self.root})()
        with self.assertRaisesRegex(RuntimeError, '仅支持读取'):
            source_policy.manager_for(worker, 'chosen')

    def test_baidu_uses_chosen_native_directory_and_honors_preview(self):
        from baidu_manage import Baidu
        entry = dict(self.entry, driver='BaiduNetdisk', native_root='/chosen/media')
        self.update({'sources.entries': [entry]})
        folder = {'fs_id': 17, 'path': '/chosen/media', 'server_filename': 'media', 'isdir': 1}
        with patch.object(Baidu, 'api', return_value={'list': [folder]}):
            manager = Baidu(self.root, entry=entry, auth={'AccessToken': 'synthetic'})
        manager.paths['video1'] = '/chosen/media/old.mkv'
        manager._remember('video1', '/custom/films/old.mkv', False, 500)
        batch = [{'fid': 'video1', 'target': '/custom/films/new.mkv', 'size': 500}]
        with patch.object(manager, 'api', return_value={'info': [{'errno': 0}]}) as api:
            with self.assertRaisesRegex(RuntimeError, '预览'):
                manager.relocate(batch, '17')
            api.assert_not_called()
            self.update({'organizing.preview_only': False})
            manager.relocate(batch, '17')
            self.assertEqual(manager.paths['video1'], '/chosen/media/new.mkv')
            self.assertEqual(manager._path('video1'), '/custom/films/new.mkv')
            api.assert_called_once()

    def test_real_plan_builder_only_inventories_and_does_not_create_directories(self):
        import cloud_organizer
        worker = type('Worker', (), {})()
        worker.root = self.root
        worker.entries = {'entry1': {'id': 'entry1', 'remote': '/custom/films/old.mkv',
            'kind': 'Movies', 'relative': 'Movies/Named/old.mkv', 'size': 500}}
        film = {'Id': 'movie-one', 'Name': '示例电影', 'ProductionYear': 2024,
                'ProviderIds': {'Tmdb': '123'}}
        movies = {str((self.root / 'catalog/Movies/Named').absolute()).casefold(): film}
        row = {'fid': 'video1', 'file_name': 'old.mkv', 'size': 500, 'dir': False}
        with patch.object(self.manager, 'listing', return_value=[row]), \
             patch.object(self.manager, 'mkdir') as mkdir, \
             patch.object(self.manager, 'rename') as rename, \
             patch.object(self.manager, 'move') as move:
            plan, _ = cloud_organizer.make_plan(self.root, self.manager, worker, None,
                {'movie-one': film}, {}, movies)
            self.assertEqual(plan['source_id'], 'chosen')
            self.assertEqual(len(plan['operations']), 1)
            self.assertTrue(plan['operations'][0]['target'].startswith('/custom/films/示例电影'))
            mkdir.assert_not_called(); rename.assert_not_called(); move.assert_not_called()

    def test_read_only_openlist_source_still_scans_when_other_source_organizing_enabled(self):
        from cloud_identity import CloudIdentity
        entry = dict(self.entry, driver='WebDav', allow_write=False)
        self.update({'sources.entries': [entry]})
        worker = type('Worker', (), {'root': self.root, 'entries': {}})()
        identity = CloudIdentity(worker, entry['path'])
        objects = identity.directory(entry['path'])
        identity.validate_listing(entry['path'], [{'name': 'video.mkv', 'size': 500, 'is_dir': False}], objects)
        self.assertFalse(identity.failed)
        self.assertEqual(identity.identify('/custom/films/video.mkv', 500, objects), (None, None))
        # A native identity check is optional for a read-only source. Failure
        # after constructing its tree must not reject a valid OpenList listing.
        identity.supports_native_identity = True
        identity.tree = type('Tree', (), {'rows': lambda self, path: (_ for _ in ()).throw(RuntimeError('offline'))})()
        objects = identity.directory(entry['path'])
        identity.validate_listing(entry['path'], [{'name': 'video.mkv', 'size': 500, 'is_dir': False}], objects)
        self.assertFalse(identity.failed)


if __name__ == '__main__':
    unittest.main()

"""Policy settings exercise real consumers, with temporary data and no network."""
import json
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor'))
from PIL import Image
from personal_cinema.configuration import ConfigStore
from personal_cinema.legacy import artwork_quality as artwork, external_ratings as ratings, people_localization as people
from personal_cinema.schema import BY_KEY
from jellyfin_mpv_shim.mpvtk import widgets
from jellyfin_mpv_shim.mpvtk_browser import thumbnails, cinema_featured
from jellyfin_mpv_shim.mpvtk_browser.components import cinema_presentation, detail


class PolicySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(os.environ, {'PERSONAL_CINEMA_HOME': str(self.root), 'MEDIA_LIBRARY_ROOT': str(self.root / 'runtime')})
        environment.start(); self.addCleanup(environment.stop)

    def policy(self, module, **values):
        mocked = patch.object(module, 'option', side_effect=lambda key, default=None: values.get(key, default), create=True)
        mocked.start(); self.addCleanup(mocked.stop)

    def test_artwork_real_width_and_candidate_filter_follow_setting(self):
        self.policy(artwork, **{'artwork.minimum_width': 1000, 'artwork.backdrop_width': 2560})
        poster = self.root / 'poster.png'
        Image.new('RGB', (800, 1200)).save(poster)
        self.assertFalse(artwork.inspect(poster, 'Primary')['adequate'])
        choices = artwork.candidates([{'Url': 'https://image.tmdb.org/t/p/original/abc.jpg', 'Type': 'Backdrop', 'ProviderName': 'TheMovieDb', 'Language': '', 'Width': 1920, 'Height': 1080}], 'Backdrop', None)
        self.assertEqual([], choices)

    def test_artwork_changed_threshold_does_not_wait_old_retry_window(self):
        path = self.root / 'backdrop.png'
        Image.new('RGB', (1920, 1080)).save(path)
        self.policy(artwork, **{'artwork.backdrop_width': 2560})
        record = {'policy_version': artwork.BACKDROP_POLICY_VERSION, 'version': artwork.VERSION, 'file_stamp': [path.stat().st_size, path.stat().st_mtime_ns], 'selection': 'no_adequate_source', 'checked_at': 100, 'retry_at': 999999, 'minimum_width': 1920}
        self.assertTrue(artwork.needs_upgrade(path, 'Backdrop', record, now=101))

    @staticmethod
    def item():
        return {'Id': 'movie1', 'Type': 'Movie', 'ProviderIds': {'Tmdb': '123', 'Imdb': 'tt1234567'}}

    def seed_ratings(self):
        ident = ratings.identity(self.item())
        rows = [{'source': source, 'scope': 'movie', 'value': value, 'scale': scale, 'fetched_at': '2026-01-01T00:00:00Z', 'provider': 'mdblist'} for source, value, scale in [('imdb', 8, 10), ('audience', 90, 100)]]
        path = self.root / ratings.CACHE_FILE
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema': 1, 'items': {ident['key']: {**ident, 'ratings': rows, 'providers': {'mdblist': {'status': 'updated', 'checked_at': 100, 'parser_version': ratings.MDBLIST_PARSER_VERSION}}}}, 'item_ids': {'movie1': ident['key']}}), 'utf-8')
        return path

    def test_ratings_disabled_makes_no_request_and_hides_cached_values(self):
        self.seed_ratings()
        self.policy(ratings, **{'ratings.enabled': False})
        imdb, client = Mock(return_value={}), Mock()
        result = ratings.sync_ratings(self.root, [self.item()], imdb_loader=imdb, mdblist_client=client, force=True)
        self.assertEqual('disabled', result['state'])
        imdb.assert_not_called(); client.fetch.assert_not_called()
        output = ratings.CachedRatings(self.root).apply({**self.item(), 'ExternalRatings': [{'source': 'imdb'}]})
        self.assertEqual([], output['ExternalRatings'])

    def test_selected_ratings_order_is_applied_without_destroying_cache(self):
        path = self.seed_ratings()
        before = path.read_bytes()
        self.policy(ratings, **{'ratings.providers': ['audience', 'imdb']})
        output = ratings.CachedRatings(self.root).apply(self.item())
        self.assertEqual(['audience', 'imdb'], [r['source'] for r in output['ExternalRatings']])
        self.assertEqual(before, path.read_bytes())

    def test_native_rating_icons_preserve_selected_order(self):
        self.seed_ratings()
        self.policy(ratings, **{'ratings.providers': ['audience', 'imdb']})
        item = ratings.CachedRatings(self.root).apply(self.item())
        self.assertEqual(['audience', 'imdb'], [r['source'] for r in cinema_presentation.rating_records(item)])

    def test_native_people_card_explains_missing_image(self):
        tiles = Mock()
        self.policy(detail, **{'artwork.missing_portrait': 'placeholder'})
        detail.people_row(tiles, [{'Id': 'person', 'Name': 'Person', 'Role': '导演'}])
        cast = tiles.tile_row.call_args.args[1]
        self.assertEqual('导演 · 暂无照片', cast[0]['_subtitle'])

    def test_provider_selection_skips_unselected_fetch_and_refresh_respects_days(self):
        self.seed_ratings()
        self.policy(ratings, **{'ratings.providers': ['audience'], 'ratings.refresh_days': 7})
        imdb, client = Mock(return_value={}), Mock()
        client.fetch.return_value = {'tmdbid': 123, 'mediatype': 'movie', 'ratings': []}
        ratings.sync_ratings(self.root, [self.item()], imdb_loader=imdb, mdblist_client=client, now=100 + 2 * ratings.DAY)
        imdb.assert_not_called(); client.fetch.assert_not_called()
        ratings.sync_ratings(self.root, [self.item()], imdb_loader=imdb, mdblist_client=client, now=100 + 8 * ratings.DAY)
        client.fetch.assert_called_once()

    def test_imdb_dataset_obeys_configured_refresh_interval(self):
        path = self.root / ratings.DATASET_FILE
        path.parent.mkdir(parents=True)
        path.write_bytes(b'existing cached fixture')
        now = 1000000
        os.utime(path, (now - 2 * ratings.DAY, now - 2 * ratings.DAY))
        opener = Mock(side_effect=AssertionError('unexpected download'))
        self.policy(ratings, **{'ratings.refresh_days': 7})
        self.assertEqual(path, ratings.ensure_imdb_dataset(self.root, now, opener=opener))
        opener.assert_not_called()

    def test_portrait_hide_applies_after_merging_roles_without_mutation(self):
        original = {'People': [{'Id': 'a', 'Type': 'Director', 'Name': 'A'}, {'Id': 'a', 'Type': 'Writer', 'Name': 'A', 'PrimaryImageTag': 'photo'}, {'Id': 'b', 'Type': 'Actor', 'Name': 'B'}]}
        self.policy(people, **{'artwork.missing_portrait': 'hide'})
        result = people.localize_people(original)
        self.assertEqual(['a'], [p['Id'] for p in result['People']])
        self.assertEqual(3, len(original['People']))

    def test_portrait_placeholder_explicitly_marks_unavailable(self):
        self.policy(people, **{'artwork.missing_portrait': 'placeholder'})
        result = people.localize_people({'People': [{'Id': 'a', 'Name': 'A', 'Type': 'Actor'}]})
        self.assertEqual('unavailable', result['People'][0].get('PortraitStatus'))

    def test_disk_image_budget_uses_gib_and_zero_never_persists(self):
        self.policy(thumbnails, **{'cache.image_gib': 3})
        with patch('jellyfin_mpv_shim.conffile.get_cache_dir', return_value=str(self.root)):
            self.assertEqual((str(self.root), 3072), thumbnails.disk_cache())
        store = thumbnails.ThumbnailStore(str(self.root / 'thumbs'), max_disk_mb=0)
        self.addCleanup(store.shutdown)
        target = self.root / 'thumbs' / 'image.jpg'
        store._persist(str(target), b'image')
        self.assertFalse(target.exists())

    def test_reduced_motion_removes_pull_and_animated_blur_layers(self):
        self.policy(widgets, **{'appearance.reduced_motion': True})
        if hasattr(widgets, 'reduced_motion'): widgets.reduced_motion.cache_clear(); self.addCleanup(widgets.reduced_motion.cache_clear)
        scroll = widgets.VScroll(widgets.Box([]), cinema_pull=True)
        self.assertFalse(scroll.cinema_pull)
        frame = {'src': 'fixture', 'iw': 100, 'ih': 100}
        backdrop = widgets.CinemaBackdrop([frame, frame], [], 'home', mask_in_foreground=True)
        self.assertEqual(1, len(backdrop.frames))

    def test_explicit_home_and_new_session_have_different_shuffle_policy(self):
        self.policy(cinema_featured, **{'appearance.home_refresh': 'new_session'})
        self.assertFalse(cinema_featured.refresh_on_home())

    def test_settings_are_available_and_fixed_rules_cannot_be_disabled(self):
        config = ConfigStore(self.root)
        result = config.update({'ratings.enabled': False, 'cache.image_gib': 3}, 0)
        self.assertFalse(result['values']['ratings.enabled'])
        for name in ('metadata.lock_manual', 'organizing.attachments_in_movie'):
            self.assertTrue(BY_KEY[name].get('readonly'))
            self.assertTrue(BY_KEY[name].get('invariant'))
            with self.assertRaises(ValueError): config.update({name: False}, result['revision'])

    def test_saved_device_policy_reaches_real_thumbnail_budget(self):
        config = ConfigStore(self.root)
        config.update({'cache.image_gib': 4}, 0, scope='device')
        with patch('jellyfin_mpv_shim.conffile.get_cache_dir', return_value=str(self.root / 'pictures')):
            self.assertEqual(4096, thumbnails.disk_cache()[1])

    def test_ratings_network_uses_runtime_proxy_policy(self):
        with patch.object(ratings, 'proxy_handler', create=True) as proxy, patch.object(ratings.urllib.request, 'build_opener') as opener:
            ratings.direct_http()
            proxy.assert_called_once_with()
            self.assertIs(proxy.return_value, opener.call_args.args[0])

    def matcher_module(self):
        legacy = str(Path(__file__).resolve().parents[1] / 'src' / 'personal_cinema' / 'legacy')
        with patch.object(sys, 'path', [legacy, *sys.path]):
            return importlib.import_module('personal_cinema.legacy.metadata_matcher')

    def test_saved_languages_reach_next_metadata_search_in_order(self):
        module = self.matcher_module()
        client = Mock()
        client.json.return_value = []
        matcher = module.Matcher(self.root, client=client)
        ConfigStore(self.root).update({'metadata.languages': ['ja', 'zh-CN']}, 0)
        matcher.search({'kind': 'Movie', 'titles': ['Fixture']})
        languages = [call.args[1]['SearchInfo']['MetadataLanguage'] for call in client.json.call_args_list]
        self.assertEqual(['ja', 'zh-CN'], languages)

    def test_metadata_does_not_offer_unwired_media_overrides(self):
        with self.assertRaises(ValueError):
            ConfigStore(self.root).update({'metadata.languages': ['ja']}, 0, scope='item:example')

    def test_metadata_client_uses_selected_credential_reference(self):
        module = self.matcher_module()
        ConfigStore(self.root).update({'foundation.jellyfin_credential': 'jellyfin.example'}, 0)
        with patch.object(module, '_cinema_vault') as vault:
            module.Jellyfin(self.root)
            vault.return_value.get.assert_called_once_with('jellyfin.example')


if __name__ == '__main__': unittest.main()

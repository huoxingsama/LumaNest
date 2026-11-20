"""Public cinema contracts using synthetic artwork and an in-memory library.

These exercise the released renderer, image pipeline and route state without
opening MPV, contacting a server, or reading a private media directory.
"""
from contextlib import ExitStack
import copy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

VENDOR = Path(__file__).resolve().parents[1] / 'vendor'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from jellyfin_mpv_shim.mpvtk import scaling
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk.widgets import ImageMap, Row, Spacer
from jellyfin_mpv_shim.mpvtk_browser import home_sections
from jellyfin_mpv_shim.mpvtk_browser.components import cinema_home, cinema_detail, cinema_composition
from jellyfin_mpv_shim.mpvtk_browser.pages.home import HomePage
from jellyfin_mpv_shim.mpvtk_browser.pages.detail import DetailPage
from jellyfin_mpv_shim.mpvtk_browser.thumbnails import ThumbnailStore


def movie(ident='film'):
    return {'Id': ident, 'Type': 'Movie', 'Name': '测试电影',
            'ProductionYear': 2024, 'CommunityRating': 8.2,
            'Overview': '这是一段中文剧情简介。' * 30,
            'BackdropImageTags': ['backdrop-' + ident],
            'ImageTags': {'Primary': 'poster-' + ident},
            'UserData': {'PlaybackPositionTicks': 100}}


class BitmapStore:
    def __init__(self):
        self.entries = {}
        self.images = {}

    def bitmap(self, key, image, lsize=None):
        if key not in self.entries:
            image = image() if callable(image) else image
            lw, lh = lsize or (scaling.dip(image.width), scaling.dip(image.height))
            self.images[key] = image
            self.entries[key] = dict(src='fixture.bgra', iw=image.width, ih=image.height,
                                     lw=lw, lh=lh, v=len(self.entries) + 1)
        return self.entries[key]


class Library:
    def __init__(self):
        self.items = [movie(), movie('second')]
        self.listings = 0

    def backdrop_spec(self, item):
        tags = item.get('BackdropImageTags')
        return (item['Id'], 'Backdrop', tags[0]) if tags else None

    def image_spec(self, item, kind='Primary', *args, **kwargs):
        tag = (item.get('ImageTags') or {}).get(kind)
        return (item['Id'], kind, tag) if tag else None

    def image_url(self, server, owner, kind, *args, **kwargs):
        return 'fixture://' + kind + '/' + owner

    def backdrop_url(self, server, item, **kwargs):
        return 'fixture://Backdrop/' + item['Id']

    def get_libraries(self, server):
        return [{'Id': 'movies', 'Name': '电影', 'CollectionType': 'movies'}]

    def get_home_prefs(self, server):
        return [home_sections.RESUME, home_sections.LIBRARIES], frozenset()

    def get_user_prefs(self, server):
        return {}

    def get_home_rows(self, server, libraries, sections, **kwargs):
        return []

    def get_library_items(self, server, parent, **kwargs):
        self.listings += 1
        return copy.deepcopy(self.items), len(self.items)

    def get_item(self, server, ident):
        return copy.deepcopy(next(item for item in self.items if item['Id'] == ident))


def fixture():
    store = BitmapStore()
    source = Library()
    image = Image.new('RGB', (1280, 720), '#9a6847')
    ImageDraw.Draw(image).rectangle((460, 130, 820, 620), fill='#e7d7b7')
    requests = []

    def request(key, url, box, **kwargs):
        requests.append((key, url))
        return image

    scroll = SimpleNamespace(offset=lambda _: 0, pending=lambda _: None, on_scroll=Mock())
    tiles = SimpleNamespace(_banner_fetch_w=lambda width: width,
                            _request_image=request, on_open=Mock(),
                            art=SimpleNamespace(thumbs=SimpleNamespace(is_gone=lambda key: False)),
                            body_w=lambda width: width - 48,
                            tile_row=lambda *a, **k: Spacer(h=60),
                            window_rows=lambda rows, *a, **k: rows)
    art = SimpleNamespace(strips=store, tiles=tiles, scroll=scroll,
                          geom=object(), geom_wide=object(), geom_square=object())
    run = SimpleNamespace(epoch=1)
    ctx = SimpleNamespace(art=art, source=source, server='fixture-server', run=run,
                          player=SimpleNamespace(), actions=Mock(), nav=Mock(),
                          dialogs=Mock(), status=Mock(), invalidate=Mock())
    ctx.shell = SimpleNamespace(_route_async=lambda route, work, done, epoch: done(work()))
    return SimpleNamespace(route={}, ctx=ctx), store, requests, image


class NativeArtworkTests(unittest.TestCase):
    def setUp(self):
        self.scale = scaling.scale()
        scaling.set_scale(1)
        # Artwork tests never read a user's manually reviewed framing data.
        reviewed = patch('jellyfin_mpv_shim.mpvtk_browser.components.subject_framing._reviewed_profiles', return_value=[])
        reviewed.start()
        self.addCleanup(reviewed.stop)

    def tearDown(self):
        scaling.set_scale(self.scale)

    def test_cover_fills_every_pixel_and_preserves_centered_single_image(self):
        source = Image.new('RGB', (600, 300), '#648d40')
        draw = ImageDraw.Draw(source)
        draw.rectangle((250, 0, 349, 299), fill='#f0b060')
        before = source.tobytes()
        result = cinema_composition.fit_background(source, (300, 300))
        self.assertEqual(result.getchannel('A').getextrema(), (255, 255))
        self.assertEqual(result.getpixel((150, 100))[:3], (240, 176, 96))
        self.assertEqual(result.getpixel((0, 100))[:3], result.getpixel((299, 100))[:3])
        self.assertEqual(source.tobytes(), before)

    def test_default_install_has_both_local_subject_detectors(self):
        from jellyfin_mpv_shim.mpvtk_browser.components import subject_framing
        with patch.object(subject_framing, '_DETECTOR_READY', False), patch.object(subject_framing, '_DETECTOR', None):
            detector = subject_framing._local_detector()
            self.assertIsNotNone(detector, 'Default installation must include OpenCV and NumPy')
            cv2, numpy, classifiers = detector
            self.assertEqual(len(classifiers), 2, 'Both anime and photographic face models must load')
            self.assertTrue(all(not model.empty() for model in classifiers))
            image = numpy.zeros((128, 128), dtype=numpy.uint8)
            for model in classifiers:
                # Real native calls detect missing binaries/ABI problems, even
                # though this blank fixture correctly contains no person.
                self.assertEqual(len(model.detectMultiScale(image)), 0)
            self.assertTrue(Path(cv2.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()))
            self.assertTrue(Path(numpy.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()))

    def test_known_subject_profile_changes_vertical_crop_without_horizontal_shift(self):
        source = Image.new('RGB', (400, 800), '#456789')
        center = cinema_composition.framing_center_y(source, (800, 400))
        lower = cinema_composition.framing_center_y(source, (800, 400),
                    {'focal_y': .7, 'subject_box': [.2, .6, .8, .76]})
        self.assertGreater(lower, center)
        self.assertLessEqual(lower, 1)

    def test_home_and_detail_use_same_background_and_ambient(self):
        home, _, _, _ = fixture()
        detail, _, _, _ = fixture()
        item = movie()
        cinema_home.render_featured(home, (1280, 800), [item], 0, lambda _: None)
        cinema_detail.render_header(detail, (1280, 800), item, 'detail')
        self.assertEqual(home.route['_cinema_ambient'], detail.route['_cinema_ambient'])
        a = home.route['_cinema_layers']['background']
        b = detail.route['_cinema_detail_layers']['background']
        self.assertEqual(a['source'].tobytes(), b['source'].tobytes())
        self.assertEqual(a['physical'], b['physical'])

    def test_scroll_reuses_immutable_buffers_and_keeps_art_outside_scroll(self):
        page, store, _, _ = fixture()
        node = cinema_home.render_featured(page, (1280, 800), [movie()], 0, lambda _: None)
        surface = cinema_home.page_surface(page, [node], 'home')
        previous = dict(store.entries)
        foreground = page.route['_cinema_layers']['foreground'].tobytes()
        for offset in (0, 120, 600, -200, 0):
            page.ctx.art.scroll.offset = lambda _, value=offset: value
            node = cinema_home.render_featured(page, (1280, 800), [movie()], 0, lambda _: None)
            cinema_home.page_surface(page, [node], 'home')
        self.assertEqual(store.entries, previous)
        self.assertEqual(page.route['_cinema_layers']['foreground'].tobytes(), foreground)
        nodes, _ = layout(surface, 1280, 800)
        background = next(node for node in nodes if node.get('cbg', {}).get('kind') == 'art')
        self.assertEqual(background['y'], 0)
        self.assertNotIn('sc', background)

    def test_pending_next_image_keeps_visible_hero_until_ready(self):
        page, _, _, image = fixture()
        items = [movie(), movie('next')]
        visible = cinema_home.render_featured(page, (1280, 800), items, 0, lambda _: None)
        page.ctx.art.tiles._request_image = lambda *args, **kwargs: None
        pending = cinema_home.render_featured(page, (1280, 800), items, 1, lambda _: None)
        self.assertIs(pending, visible)
        self.assertTrue(page.route['_cinema_hero_pending'])
        page.ctx.art.tiles._request_image = lambda *args, **kwargs: image
        ready = cinema_home.render_featured(page, (1280, 800), items, 1, lambda _: None)
        self.assertEqual((ready.w, ready.h), (visible.w, visible.h))
        self.assertFalse(page.route['_cinema_hero_pending'])

    def test_prewarm_builds_next_hero_before_selection_and_reuses_it(self):
        page, _, requests, _ = fixture()
        page.ctx.run.run = lambda work, done, epoch, **kwargs: (done(work()), kwargs['always']())
        items = [movie(), movie('next')]
        cinema_home.preload_featured(page, (1280, 800), items, 0)
        self.assertEqual({url for _, url in requests}, {'fixture://Backdrop/film', 'fixture://Backdrop/next'})
        prepared = page.route['_cinema_prepared']
        self.assertEqual(len(prepared), 2)
        with patch.object(cinema_home, 'prepare_background_layers', side_effect=AssertionError('unexpected recompose')):
            cinema_home.render_featured(page, (1280, 800), items, 1, lambda _: None)
        self.assertIn(page.route['_cinema_layers'], prepared.values())

    def test_return_to_home_preserves_recommendations_and_current_slide(self):
        fixture_page, _, _, _ = fixture()
        page = HomePage(fixture_page.ctx, {'kind': 'home'})
        page.load(1)
        order = [item['Id'] for item in page.route['_data']['featured']]
        page.route['_cinema_featured_index'] = 1
        page.close()
        # A new page object is created by navigation with the parked route.
        restored = HomePage(page.ctx, page.route)
        with patch('jellyfin_mpv_shim.mpvtk_browser.pages.home.time.monotonic', return_value=99999999):
            restored.load(1)
        self.assertEqual([item['Id'] for item in restored.route['_data']['featured']], order)
        self.assertEqual(restored.route['_cinema_featured_index'], 1)
        self.assertEqual(page.ctx.source.listings, 1)
        restored.route['_cinema_shuffle_due'] = True
        restored.load(1)
        self.assertEqual(restored.route['_cinema_featured_index'], 0)
        self.assertEqual(page.ctx.source.listings, 2)

    def test_real_home_page_uses_cinema_header_and_prewarm(self):
        fixture_page, _, _, _ = fixture()
        page = HomePage(fixture_page.ctx, {'kind': 'home'})
        page.load(1)
        with patch.object(cinema_home, 'preload_featured', wraps=cinema_home.preload_featured) as preload:
            tree = page.render((1280, 800))
        nodes, _ = layout(tree, 1280, 800)
        self.assertTrue(preload.called)
        self.assertTrue(any(node.get('cbg', {}).get('kind') == 'art' for node in nodes))
        self.assertTrue(any(node.get('id') == 'cinema-hero-play' for node in nodes))

    def test_real_detail_page_keeps_cinema_header_with_native_actions(self):
        fixture_page, _, _, _ = fixture()
        page = DetailPage(fixture_page.ctx, {'kind': 'detail', '_data': {'item': movie()}})
        from jellyfin_mpv_shim.mpvtk_browser.components import detail
        with ExitStack() as stack:
            for name in ('_play_buttons', '_detail_actions'):
                stack.enter_context(patch.object(page, name, return_value=Row([])))
            for name, value in (('_track_pickers', []), ('_scenes_row', None), ('web_url', None)):
                stack.enter_context(patch.object(page, name, return_value=value))
            for name, value in (('more_entries', []), ('provider_links', None), ('people_row', None)):
                stack.enter_context(patch.object(detail, name, return_value=value))
            tree = page.render((1280, 800))
        nodes, _ = layout(tree, 1280, 800)
        self.assertTrue(any(node.get('cbg', {}).get('kind') == 'art' for node in nodes))
        self.assertIn('_cinema_detail_layers', page.route)

    def test_cached_thumbnail_is_decoded_off_thread_then_reused_without_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.png'
            Image.new('RGB', (640, 360), '#9a6847').save(path)
            ready = threading.Event()
            store = ThumbnailStore(str(Path(directory) / 'cache'), workers=1, notify=ready.set)
            received = []
            try:
                store.request('fixture', str(path), (320, 180), received.append)
                self.assertTrue(ready.wait(5))
                store.pump()
                self.assertEqual(received[0].size, (320, 180))
                path.unlink()
                store.request('fixture', str(path), (320, 180), received.append)
                self.assertIs(received[0], received[1])
            finally:
                store.shutdown()
                store._pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()

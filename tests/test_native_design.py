"""Offline native layout regressions; no MPV, server or user profile needed."""
from pathlib import Path
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

VENDOR = Path(__file__).resolve().parents[1] / 'vendor'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from jellyfin_mpv_shim.mpvtk import theme as toolkit, scaling, pilfont, metrics
from jellyfin_mpv_shim.mpvtk.layout import layout, measure
from jellyfin_mpv_shim.mpvtk.widgets import Row, ImageMap
from jellyfin_mpv_shim.mpvtk_browser import theme, themes, window_chrome
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile, TileGeom
from jellyfin_mpv_shim.mpvtk_browser.components import controls, cinema_detail, cinema_presentation as hero


class NativeDesignTests(unittest.TestCase):
    def setUp(self):
        self.palette = toolkit.palette()
        self.active = theme._active
        self.browser_palette = dict(theme._palette)
        self.scale = scaling.scale()
        theme._active = themes.resolve({})
        theme._palette.clear()
        theme._palette.update(theme._active['palette'])
        toolkit.set_type_scale()
        scaling.set_scale(1)

    def tearDown(self):
        theme._active = self.active
        theme._palette.clear()
        theme._palette.update(self.browser_palette)
        toolkit.set_tokens(**{k: v for k, v in self.palette.items() if k != 'glow'})
        toolkit.set_type_scale()
        scaling.set_scale(self.scale)

    def test_default_palette_has_neutral_action_and_readable_secondary_text(self):
        self.assertEqual(theme.WINDOW_BG, '12151b')
        self.assertEqual(theme.PANEL_BG, '1c2129')
        rgb = theme.rgb(theme.ACCENT)
        self.assertLess(max(rgb) - min(rgb), 20)
        self.assertGreater(min(theme.rgb(theme.SUBTLE_FG)), 150)

    def test_primary_actions_share_home_dimensions_without_id_specific_patch(self):
        callback = lambda: None
        primary = controls.action_btn('play_arrow', '播放', 'any-primary', callback, primary=True)
        geometry = hero.geometry((1280, 720))['controls']['play']
        self.assertEqual(measure(primary), (geometry[2], geometry[3]))
        self.assertEqual(primary.bg, hero.PLAY_BG)
        secondary = controls.action_btn('favorite', '收藏', 'favorite', callback, on=True)
        self.assertNotEqual(secondary.bg, primary.bg)
        self.assertEqual(measure(secondary)[1], 44)
        cinema_detail.action_surface(Row([primary, secondary]), 800)
        self.assertIs(primary.on_click, callback)
        self.assertEqual(primary.radius, hero.PLAY_RADIUS)

    def test_larger_text_keeps_primary_labels_inside_home_and_detail_controls(self):
        toolkit.set_type_scale(factor=1.5)
        primary = controls.action_btn('play_arrow', '继续播放', 'resume', lambda: None, primary=True)
        geometry = hero.geometry((1280, 720))['controls']['play']
        self.assertEqual(measure(primary), (geometry[2], geometry[3]))
        self.assertGreaterEqual(measure(primary)[1], primary.children[-1].size + 16)

    def test_chrome_has_one_size_no_search_field_and_no_title_overlap(self):
        noop = lambda *args: None
        browser = SimpleNamespace(route={'kind': 'detail', 'title': '非常长的电影标题' * 30},
            _nav=SimpleNamespace(can_go_back=True, forward=[]), _offline=False,
            window_controls=True, maximized=False, server=None, controller=None, source=None,
            go_back=noop, go_home=noop, navigate=noop, minimize_window=noop,
            toggle_maximized=noop, close_window=noop, _open_settings=noop,
            _open_syncplay=noop, _search_box={}, _search=noop, _users=lambda: [])
        for width, compact in ((640, True), (960, True), (1920, False)):
            with self.subTest(width=width):
                tree = window_chrome.chrome_bar(browser, compact, servers=[], users=[], immersive=True)
                nodes, _ = layout(tree, width, 60)
                controls_ = [n for n in nodes if n.get('click')]
                self.assertFalse(any(n.get('id') == 'nav-search' for n in nodes))
                self.assertTrue(all(n['h'] == 44 for n in controls_))
                self.assertTrue(all(n['x'] >= 0 and n['x'] + n['w'] <= width for n in controls_))
                ordered = sorted(controls_, key=lambda n: n['x'])
                self.assertTrue(all(a['x'] + a['w'] + 7.9 <= b['x'] for a, b in zip(ordered, ordered[1:])))

    def test_synopsis_is_limited_to_readable_width(self):
        self.assertLessEqual(hero.geometry((2560, 1100))['text_width'], 1200)

    def test_ratings_wrap_without_tiny_text_and_share_each_row_baseline(self):
        from PIL import Image, ImageDraw
        records = [{'kind': 'imdb', 'label': 'IMDb', 'value': '9.8', 'color': 'f5c518'},
                   {'kind': 'season_rating', 'label': 'Bangumi 本季', 'value': '8.2', 'color': 'f4a6c0'}] * 4
        draw = ImageDraw.Draw(Image.new('RGBA', (640, 480)))
        font, rows, gap, line_height = hero.rating_layout(draw, records, 260, narrow=True)
        self.assertGreaterEqual(font.size, 14)
        self.assertGreater(len(rows), 1)
        for row in rows:
            self.assertLessEqual(sum(width for _, width in row) + gap * (len(row) - 1), 260)
        self.assertGreaterEqual(line_height, hero._rating_line_height(font, 'Bangumi 本季'))

    def test_builtin_cinema_theme_keeps_existing_card_geometry(self):
        path = VENDOR / 'jellyfin_mpv_shim/themes/lumanest.json'
        resolved = themes.resolve(json.loads(path.read_text('utf-8')))
        self.assertEqual(resolved['tile_landscape'], (300, 169))
        self.assertEqual(resolved['poster_scale'], 1.3)
        self.assertTrue(resolved['rounded'])
        self.assertFalse(resolved['glow'])
        self.assertEqual(resolved['palette']['ACCENT'], theme.ACCENT)

    def test_pointer_regions_keep_pill_geometry_for_keyboard_focus(self):
        picture = ImageMap('fixture.bgra', 156, 48, w=156, h=48,
                           regions=[{'id': 'play', 'x': 0, 'y': 0, 'w': 156, 'h': 48,
                                     'radius': 24, 'hover': {}, 'on_click': lambda: None}])
        nodes, _ = layout(picture, 156, 48)
        target = next(node for node in nodes if node.get('id') == 'play')
        self.assertEqual(target['radius'], 24)
        self.assertEqual(target['hover'], {})
        self.assertTrue(target['click'])

    def test_card_has_clean_edge_and_neutral_progress_without_baking_hover(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGBA', (240, 185))
        geom = TileGeom(tile_w=240, tile_h=135)
        tile = Tile(key='fixture', poster=Image.new('RGB', (240, 135), '#b8a88e'), progress=.5)
        store = StripStore.__new__(StripStore)
        draw = ImageDraw.Draw(image)
        store._paint_poster(image, draw, 0, tile, geom)
        store._paint_decorations(image, draw, 0, tile, geom)
        self.assertEqual(image.getpixel((120, 0))[:3], (184, 168, 142))
        self.assertEqual(image.getpixel((40, 133))[:3], theme.rgb(theme.ACCENT))
        self.assertEqual(image.getpixel((0, 0))[3], 0)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows typography')
    def test_native_bitmap_and_ass_metrics_use_same_windows_family(self):
        measured = metrics._load_font()
        latin = pilfont.font_for('Play 8.2', 18)
        chinese = pilfont.font_for('播放与评分', 18)
        self.assertEqual(measured.getname()[0], latin.getname()[0])
        from PIL import ImageFont
        try:
            ImageFont.truetype('msyh.ttc', 18)
        except OSError:
            self.skipTest('Chinese language font is not installed on this Windows host')
        self.assertEqual(latin.getname()[0], chinese.getname()[0])


if __name__ == '__main__':
    unittest.main()

"""Floating native navigation above artwork without mpv's ASS/bitmap conflict.

Buttons and labels are a transparent bitmap. Search opens a native editing
dialog on demand; server/user switchers retain their toolkit controls.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
from ...mpvtk import pilfont
from ...mpvtk.layout import layout
from ...mpvtk.scaling import px, raster
from ...mpvtk.vector import icon_image
from ...mpvtk.widgets import Button, Column, Dialog, Dropdown, Icon, ImageMap, Row, Stack, Text, TextBox
from ...imageutil import pil_font
from .. import theme, cinema_paint

def open_search(browser):
    """Show a native editing surface only when search is requested."""
    from ...i18n import _
    draft = {'term': browser._search_box.get('term', '')}
    width = min(520, max(260, (getattr(browser, '_size', None) or (640, 480))[0] - 48))

    def changed(value):
        draft['term'] = value
        browser._search_box['term'] = value

    def submit(value=None):
        term = str(draft['term'] if value is None else value).strip()
        if not term:
            return
        browser._search_box['term'] = term
        browser._close_dialog()
        browser._search(term)

    def build():
        return Dialog('cinema-search', Column([Text(_('Search'), size='title', bold=True), TextBox('cinema-search-input', text=draft['term'], placeholder=_('Search…'), w=width - 40, size='large', on_change=changed, on_submit=submit, autofocus=True), Row([Button(_('Cancel'), id='cinema-search-cancel', on_click=browser._close_dialog, radius=20), Button(_('Search'), id='cinema-search-submit', icon='search', on_click=submit, radius=20)], gap=12, justify='end', w=width - 40)], w=width, pad=20, gap=18, bg=theme.PANEL_BG, radius=20), on_dismiss=browser._close_dialog)
    browser._show_dialog(build)

def active(browser):
    return browser.route.get('kind') in {'home', 'detail', 'series', 'season'}

def render(browser, bar, width):
    """Reuse the actual topbar's geometry, callbacks and native switchers."""
    height = theme.NAV_BAR_HEIGHT
    icons, native = ({}, {})

    def visit(node):
        if isinstance(node, Icon):
            node.id = 'cinema-nav-icon-' + str(len(icons))
            icons[node.id] = node.name
        if isinstance(node, (TextBox, Dropdown)):
            native[node.id] = node
        for child in getattr(node, 'children', []):
            visit(child)
    visit(bar)
    nodes, handlers = layout(bar, width, height)
    hovered = browser.route.get('_cinema_nav_hover')
    visible = [n for n in nodes if n.get('t') in ('text', 'icon') or (n.get('id') in handlers and n.get('click'))]
    key = hashlib.sha256(json.dumps([visible, width, raster(width, height), hovered], sort_keys=True, default=str).encode()).hexdigest()

    def compose():
        from PIL import Image, ImageDraw
        canvas = Image.new('RGBA', raster(width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        for node in visible:
            x, y, w, h = (px(node[k]) for k in ('x', 'y', 'w', 'h'))
            ident = node['id']
            if node.get('click'):
                fill = (53, 60, 70, 210) if hovered == ident else (28, 33, 41, 164)
                if ident == 'win-close' and hovered == ident:
                    fill = (192, 52, 70, 240)
                cinema_paint.rounded(canvas, (x, y, x + w, y + h), radius=h / 2, fill=fill)
            if node['t'] == 'icon' and ident in icons:
                glyph = icon_image(icons[ident], max(1, min(w, h)), theme.rgb(theme.TEXT_FG))
                canvas.alpha_composite(glyph, (x + (w - glyph.width) // 2, y + (h - glyph.height) // 2))
            elif node['t'] == 'text':
                text = node.get('text', '')
                font = pil_font(max(1, px(node['size'])), bold=bool(node.get('bold')), text=text)
                tw = pilfont.text_length(draw, text, font)
                tx = x + (w - tw) / 2 if node.get('align') == 'center' else x + w - tw if node.get('align') == 'right' else x
                bounds = font.getbbox(text or ' ')
                ty = y + (h - (bounds[3] - bounds[1])) / 2 - bounds[1]
                pilfont.draw_text(draw, (tx, ty + px(1)), text, font, fill=(0, 0, 0, 110))
                pilfont.draw_text(draw, (tx, ty), text, font, fill=theme.rgb(theme.TEXT_FG, 255))
        return canvas

    def hover(ident):
        if browser.route.get('_cinema_nav_hover') != ident:
            browser.route['_cinema_nav_hover'] = ident
            browser.invalidate()

    def leave(ident):
        if browser.route.get('_cinema_nav_hover') == ident:
            browser.route.pop('_cinema_nav_hover', None)
            browser.invalidate()
    regions = []
    for node in nodes:
        callbacks = handlers.get(node['id'], {})
        if not callbacks.get('click'):
            continue
        ident = node['id']
        region = {k: node[k] for k in ('id', 'x', 'y', 'w', 'h')}
        region['radius'] = node.get('radius', node['h'] / 2)
        region.update(on_click=callbacks['click'], on_context=callbacks.get('context'), hover={}, on_hover=lambda _v=None, ident=ident: hover(ident), on_hover_end=lambda ident=ident: leave(ident))
        regions.append(region)
    bitmap = browser.strips.bitmap('cinema-nav-v5-lumanest|' + key, compose, lsize=(width, height))
    picture = ImageMap(bitmap['src'], bitmap['iw'], bitmap['ih'], regions=regions, v=bitmap.get('v', 0), w=bitmap['lw'], h=bitmap['lh'], anchor='nw', id='cinema-navigation')
    drag = Row([], h=height, w=width, window_drag=browser.window_controls, anchor='nw')
    children = [drag, picture]
    for node in nodes:
        field = native.get(node['id'])
        if field is None:
            continue
        field.w, field.h = (node['w'], node['h'])
        field.anchor, field.dx, field.dy, field.occlude = ('nw', node['x'], node['y'], True)
        children.append(field)
    return Stack(children, w=width, h=height, anchor='nw', id='cinema-floating-navigation')

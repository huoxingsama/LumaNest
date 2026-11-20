"""Artwork-led headers for movie, series and season pages.

Only the background is blurred. The clear text layer is prepared once per
item/size and scrolls inside the same VScroll as the existing page controls.
No player, metadata queries, global theme changes or extra workers are used.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
from ...mpvtk import pilfont
from ...mpvtk.layout import SCROLLBAR_W
from ...mpvtk.scaling import px, raster
from ...mpvtk.widgets import Box, Column, ImageMap, Spacer, Text
from .. import theme
from ..thumbnails import make_key
from . import cinema_home, detail

def hero_box(size):
    return cinema_home._box(size)

def _image(page, item, box):
    """A season's own poster wins over the show's inherited backdrop."""
    art = page.ctx.art
    resources = getattr(art.tiles, 'art', art)
    server = getattr(resources, 'server', getattr(page.ctx, 'server', None))
    source = getattr(resources, 'source', getattr(page.ctx, 'source', None))
    if server is None or source is None:
        return (None, None, None)
    own = bool((item.get('ImageTags') or {}).get('Primary'))
    primary = item.get('Type') == 'Season' and own and (not item.get('BackdropImageTags'))
    spec = None if primary else source.backdrop_spec(item)
    if not spec and own:
        primary = True
        spec = source.image_spec(item, 'Primary', raster(*box)[0], inherit=False)
    if not spec:
        return (None, None, None)
    width = cinema_home.artwork_fetch_width(art.tiles, box)
    owner, image_type, tag = spec
    url = source.image_url(server, owner, image_type, tag, width, fill=False) if primary else source.backdrop_url(server, item, width=width, fill=False)
    image = art.tiles._request_image(cinema_home.hero_image_key(spec, width), url, (width, width))
    return (spec, image, None)

def _foreground(box, title, context, meta, item=None, logo=None):
    from .cinema_presentation import foreground
    return foreground(box, item or {'Name': title}, logo, home=False, title=title, context=context)

def render_header(page, size, item, scroll_id, title=None, context=None):
    """Full-width, stable-size header using the currently selected DTO."""
    title = str(title if title is not None else item.get('Name') or '')
    meta = detail.meta_line(item)
    box = hero_box(size)
    spec, image, poster = _image(page, item, box)
    from .cinema_presentation import logo_image
    logo_spec, logo = logo_image(page, item, box)
    route = page.route
    width = cinema_home.artwork_fetch_width(page.ctx.art.tiles, box)
    image_key = cinema_home.hero_image_key(spec, width) if spec else None
    logo_key = make_key(*logo_spec, min(1200, max(500, raster(*box)[0] // 2))) if logo_spec else None
    if cinema_home.image_pending(page.ctx.art.tiles, image_key, image) or cinema_home.image_pending(page.ctx.art.tiles, logo_key, logo):
        route['_cinema_hero_pending'] = True
        return route.get('_cinema_detail_surface') or Box([], w=box[0], h=box[1])
    image, hero_variant = cinema_home.select_hero_artwork(page, item, box, spec, image, poster)
    ambient = cinema_home.image_ambient(image, ('hero-background-v2', spec, hero_variant), page)
    if not spec:
        cinema_home.clear_background(page)
        route.pop('_cinema_detail_layers', None)
        rows = [Spacer(h=28)]
        if context:
            rows.append(Text(context, color=theme.SUBTLE_FG))
        rows.append(Text(title, size='hero', bold=True, wrap=True, w=max(120, box[0] - 2 * 24)))
        if meta:
            rows.append(Text(meta, color=theme.SUBTLE_FG, wrap=True))
        return Column(rows, pad=(24, 16), gap=8, bg=ambient, align='stretch')
    content = [item.get('Id'), spec, hero_variant, logo_spec, logo is not None, item.get('CriticRating'), item.get('ExternalRatings'), box, raster(*box), title, context, meta, ambient, image is not None, theme.baked_text(px(20))]
    key = hashlib.sha256(json.dumps(content, ensure_ascii=False, default=str).encode()).hexdigest()
    cached = route.get('_cinema_detail_layers')
    if not cached or cached['key'] != key:

        def build():
            return cinema_home.finish_layers({'key': key, 'background': cinema_home.prepare_background_layers(image, box, ambient), 'foreground': _foreground(box, title, context, meta, item, logo)}, ambient)
        cached = cinema_home.staged_layers(page, key, build)
    if cached is False:
        route['_cinema_hero_pending'] = False
        return route.get('_cinema_detail_surface') or Box([Text(title, size='hero'), Text('海报暂不可用')], w=box[0], h=box[1])
    if cached is None:
        route['_cinema_hero_pending'] = True
        return route.get('_cinema_detail_surface') or Box([], w=box[0], h=box[1])
    route.update(_cinema_detail_layers=cached, _cinema_ambient=ambient, _cinema_hero_height=box[1], _cinema_hero_pending=False)
    cinema_home.remember_background(page, key, cached['background'], box, ambient, scroll_id, spec)
    surface = cinema_home.foreground_surface(page.ctx.art.strips, 'cinema-detail|' + key, cached, box, [], scroll_id + '-cinema-hero')
    route['_cinema_detail_surface'] = surface
    return surface

def scrolled(page, scroll_id, offset, maximum):
    """Keep grid virtualization and background blur in the same scroll event."""
    cinema_home.scrolled(page, scroll_id, offset, maximum)

def page_surface(page, children):
    """The ambient continues below the hero without tinting any text."""
    record = page.route.get('_cinema_background') or {}
    return cinema_home.page_surface(page, children, record.get('scroll_id', page.route.get('kind', 'detail')))

def action_surface(node, width):
    """Center the existing controls, retaining their IDs, focus and callbacks."""
    from ...mpvtk.widgets import Row
    # controls.action_btn owns appearance and scalable geometry. Centering a
    # row must not identify primary actions by node ID or style them twice.
    if isinstance(node, Column):
        node.align = 'center'
        for row in node.children:
            if isinstance(row, Row):
                row.w = width
                row.justify = 'center'
    else:
        node.w = width
        node.justify = 'center'
    return node

def release(page):
    """Navigation history retains light route data, never full RGBA layers."""
    page.route.pop('_cinema_detail_layers', None)
    page.route.pop('_cinema_prepared', None)
    page.route.pop('_cinema_detail_surface', None)
    cinema_home.clear_background(page)

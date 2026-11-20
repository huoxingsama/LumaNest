"""``TileRenderer`` -- artwork plumbing and tile/row/grid construction.

A component by ``docs/archive/ARCHITECTURE_TARGET.md`` §1.4's test: it needs
render resources and callbacks, never ``nav``, ``source`` or ``route``. What it
takes instead:

``art``
    Render resources: the strip store, the thumbnail store, and the three tile
    geometries. Held live rather than snapshotted, because the browser swaps
    its stores when mpv is re-created.
``scroll``
    A :class:`~.scroll_state.ScrollState`. Virtualized rows are windowed
    against it, which is why it and ``art`` had to be extracted together --
    apart means threading a callback between two half-built objects.

Plus three callbacks for what only the shell can answer: what a tile click
should do, whether keyboard navigation is engaged (carousel arrows hide under
the mouse), and the live app handle (page-scroll buttons drive the renderer
directly). The downloaded-id sets live here because the badge is the only
thing that reads them.

What deliberately did NOT move is the tile context menu: it needs ``route``,
``navigate``, ``run_async`` and the gateway, so it is page work rather than
rendering. Shapes, plating and decorations are in docs/artwork-pipeline.md.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import dataclasses
import logging
from collections import Counter
import time
from ..extras import is_extra
from ..i18n import _
from ..mpvtk.scaling import px, raster
from ..mpvtk.widgets import Box, Button, Column, HScroll, Icon, Image, ImageMap, Row, Spacer, Stack, Table, Text, virtual_window
from . import components, theme
from .components import chrome
from .components.detail import fmt_ticks
from .strips import Tile, logo_plate
from .thumbnails import make_key
log = logging.getLogger('mpvtk_browser.tile_renderer')
GRID_GAP = 12
ARROW_W = 38
TRACK_ROW_H = 34
RING_PAD = 5
ROW_LEAD = chrome.CONTENT_PAD
TITLE_GAP = 2
ARROW_INSET = 8
ARROW_BG = '202020'
ARROW_ALPHA = 180
ARROW_DISABLED_ALPHA = 0.35
PAGE_SLIDE_MS = 180
_EDGE_EPS = 1.0
_SNAP_RATIOS = ((2 / 3, 0.15), (16 / 9, 0.2), (1.0, 0.15), (4 / 3, 0.15))

def _snap_ratio(value):
    """Round a measured median onto the nearest canonical ratio, or leave it
    alone. jellyfin-web's ``getPrimaryImageAspectRatio`` tail."""
    for target, tolerance in _SNAP_RATIOS:
        if abs(target - value) <= tolerance:
            return target
    return value

def page_geometry(view, count, geom):
    """``(pitch, per_page, max_offset)`` for a carousel of ``count`` tiles of
    ``geom`` laid out in a ``view``-wide viewport. All logical px.

    ``pitch`` is tile-to-tile distance. ``per_page`` is how many tiles are
    *fully* visible at an item-aligned offset — the paging step. ``max_offset``
    is the offset at which the last tile sits flush against the right edge.

    The offset convention is the one :meth:`TileRenderer.hscroll_row` sets up:
    the strip is inset by ``ROW_LEAD``, so an offset of ``k * pitch`` puts
    tile ``k`` on the page margin rather than against the window edge.
    Aligning to ``ROW_LEAD + k * pitch`` instead would double the inset and
    leave the row hanging a margin short of where it was asked to go.
    """
    pitch = geom.tile_w + geom.gap
    per = max(1, int((view - ROW_LEAD - geom.tile_w) // pitch) + 1)
    total = 2 * ROW_LEAD + count * pitch - geom.gap
    return (pitch, per, max(0.0, total - view))

def page_target(offset, direction, view, count, geom):
    """Where a page button should scroll to, or ``None`` if the row is already
    against that end (which is also what disables the button).

    This is jellyfin-web's ``scrollerItemSlideIntoView`` behaviour: a page
    advances by whole tiles, and a tile left half-cut at the trailing edge
    becomes the leading tile of the next page rather than being skipped. Ported
    by behaviour, not line by line — jf-web's index arithmetic assumes a bare
    grid with no gap and no leading pad, and carries a compensating ``-1`` that
    does not survive the translation.
    """
    pitch, per, max_offset = page_geometry(view, count, geom)
    if direction < 0 and offset <= _EDGE_EPS:
        return None
    if direction > 0 and offset >= max_offset - _EDGE_EPS:
        return None
    leading = int((offset + _EDGE_EPS) // pitch)
    target = (leading + direction * per) * pitch
    return min(max(0.0, float(target)), max_offset)
CHIP_FRACTION = 0.34
CHIP_MIN = 28
CHIP_MAX = 56
CHIP_HOT_SCALE = 1.06
CHIP_HOT_ALPHA = 218

def _flat_image(box, colour):
    """A solid ``colour`` panel at physical ``box``, as a PIL image.

    Only for the banner's waiting state, which is composed through the same
    ``compose_banner`` as the real thing so the heading lands in the same
    place. Made at full size rather than 1x1-and-scaled because
    ``scale_to_cover`` would resample it, and a resample of one pixel is
    both wasteful and (with some filters) not the colour asked for.
    """
    from PIL import Image as PILImage
    return PILImage.new('RGBA', (max(1, box[0]), max(1, box[1])), theme.rgb(colour, 255))

def _play_chip_bitmap(size, hot=False):
    """The round play button that appears on a hovered tile, at physical size.

    A BITMAP for the same reason the carousel arrows are one: mpv composites
    overlay bitmaps above all script ASS, so anything drawn as a node would
    be *under* the poster strip it is meant to sit on (GUIDE §6). Same
    supersample-and-shrink, which is what makes the circle read as round
    rather than as a notch cut out of the artwork.

    ``hot`` is the pointer being on the chip: a brighter neutral disc. The renderer
    cannot do this for us -- it restyles nodes, and a bitmap has no style to
    change -- so the state is baked into a second bitmap and cached under its
    own key.
    """
    from PIL import Image as PILImage, ImageDraw
    active = theme.active() or {}
    w = px(size)
    ss = 3
    big_w = w * ss
    big = PILImage.new('RGBA', (big_w, big_w), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    fill = theme.rgb('ffffff', CHIP_HOT_ALPHA) if hot else theme.rgb(active.get('arrow_bg', ARROW_BG), int(active.get('arrow_alpha', ARROW_ALPHA)))
    d.ellipse([0, 0, big_w - 1, big_w - 1], fill=fill)
    c = big_w / 2.0
    r = big_w * 0.22
    d.polygon([(c - r * 0.75 + r * 0.25, c - r), (c - r * 0.75 + r * 0.25, c + r), (c + r * 0.95, c)], fill=theme.rgb('20242b' if hot else theme.TEXT_FG, 255))
    lanczos = PILImage.LANCZOS
    return big.resize((w, w), lanczos)

def _arrow_bitmap(direction, disabled=False):
    """A round, translucent carousel page button as a PIL bitmap at physical
    size, for the ``overlay`` arrow mode.

    Drawn as a BITMAP (not an ASS box) so it composites above the poster strip
    and alpha-blends — its transparent corners show the artwork underneath,
    with no occlusion rect whose corners would reveal the dark window
    background. Supersampled and downscaled: the circle's antialiased edge is
    what sells it as round rather than as a notch cut out of the strip.

    ``disabled`` fades the whole button rather than dropping it, so the row's
    controls do not jump around as it reaches an end. It has to be baked in:
    the renderer has no disabled state, and could not restyle a bitmap anyway.
    """
    from PIL import Image as PILImage, ImageDraw
    active = theme.active() or {}
    fade = ARROW_DISABLED_ALPHA if disabled else 1.0
    w = px(ARROW_W)
    ss = 3
    big_w = w * ss
    big = PILImage.new('RGBA', (big_w, big_w), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.ellipse([0, 0, big_w - 1, big_w - 1], fill=theme.rgb(active.get('arrow_bg', ARROW_BG), int(active.get('arrow_alpha', ARROW_ALPHA) * fade)))
    s = big_w * 0.2
    c = big_w / 2.0
    lw = max(2, int(round(big_w * 0.09)))
    base = c + s * 0.5 if direction < 0 else c - s * 0.5
    tip = c - s * 0.5 if direction < 0 else c + s * 0.5
    d.line([(base, c - s), (tip, c), (base, c + s)], fill=theme.rgb(theme.TEXT_FG, int(255 * fade)), width=lw, joint='curve')
    lanczos = PILImage.LANCZOS
    return big.resize((w, w), lanczos)

class TileRenderer:
    """Builds tiles, rows and grids. Owns the decoded-poster cache."""
    IMG_RETRY_BACKOFF = 5.0
    IMG_MAX_ATTEMPTS = 4
    BANNER_RATIO = 9 / 16 * 2 / 3

    def __init__(self, art, scroll, on_open=None, nav_mode=None, get_app=None, on_play=None, can_play=None):
        self.art = art
        self.scroll = scroll
        self.on_open = on_open or (lambda _item: None)
        self.nav_mode = nav_mode or (lambda: False)
        self.get_app = get_app or (lambda: None)
        self.on_play = on_play
        self.can_play = can_play or (lambda _item: False)
        self._hover = None
        self._hover_node = None
        self._requested = set()
        self._img_retry = {}
        self._downloaded = set()
        self._downloaded_series = set()
        self._downloaded_seasons = set()
        self._downloaded_playlists = set()
    CHIP_SUFFIX = '-play'

    def set_hover(self, node_id):
        """Pointer entered a tile, or the chip drawn on one. Repaints if the
        TILE changed -- moving from a tile onto its own chip is still the
        same tile, and repainting there would take the chip away under the
        pointer that had just reached it. The chip's own appearance does
        change on arrival -- it lights up, like every other control -- so the
        repaint happens for either."""
        was_node, was_tile = (self._hover_node, self._hover)
        self._hover_node = node_id
        self._hover = self._tile_of(node_id)
        if (was_node, was_tile) == (self._hover_node, self._hover):
            return
        invalidate = getattr(self.art, 'invalidate', None)
        if invalidate is not None:
            invalidate()

    def end_hover(self, node_id):
        """Pointer left ``node_id``. Ignored unless that is still where the
        pointer was last known to be: the renderer reports the arrival before
        the departure (see update_slider_hover), so a leave for something
        already moved on from describes a state that is over -- and taking it
        at face value would blank the chip that had just been asked for."""
        if node_id == self._hover_node:
            self.set_hover(None)

    def _tile_of(self, node_id):
        """The tile a hovered node belongs to. The chip is its own node
        inside the tile it is drawn on, so both answer the same tile."""
        if node_id and node_id.endswith(self.CHIP_SUFFIX):
            return node_id[:-len(self.CHIP_SUFFIX)]
        return node_id

    def set_downloaded(self, items, series, seasons, playlists):
        """Install fresh downloaded-id sets (from the sync catalog)."""
        self._downloaded = items
        self._downloaded_series = series
        self._downloaded_seasons = seasons
        self._downloaded_playlists = playlists

    def body_w(self, w):
        return chrome.body_width(w, chrome.CONTENT_PAD)
    SQUARE_TYPES = {'Playlist', 'MusicAlbum', 'MusicArtist', 'Audio', 'MusicGenre'}
    LANDSCAPE_RATIO = 1.33
    SQUARE_RATIO = 0.8

    def auto_geom(self, items, default=None, default_type='Primary'):
        """``(geom, image_type)`` for a row, chosen the way jellyfin-web does.

        Its card builder resolves a shape from the **median**
        ``PrimaryImageAspectRatio`` across the row and applies it to every
        card — so a Live TV row whose programmes carry 2:3 posters renders
        as posters, and one carrying 16:9 stills renders as landscape. That
        is why the same screen shows both, and it is a *per row* decision,
        which is exactly what a strip can reproduce (one strip is composited
        at one tile size).

        The median, not the mean: one oddly-shaped entry in a row of twenty
        must not reshape the row.

        ``image_type`` follows the same rule jellyfin-web's ``preferThumb:
        'auto'`` does — thumbs only where the row came out landscape.
        Items with no ratio at all fall back to ``default``.
        """
        ratios = sorted((r for r in (i.get('PrimaryImageAspectRatio') for i in items or () if i) if isinstance(r, (int, float)) and r > 0))
        if not ratios:
            return (self.caption_geom(items, default or self.art.geom), default_type)
        middle = len(ratios) // 2
        median = ratios[middle] if len(ratios) % 2 else (ratios[middle - 1] + ratios[middle]) / 2.0
        median = _snap_ratio(median)
        if median >= self.LANDSCAPE_RATIO:
            return (self.caption_geom(items, self.art.geom_wide), 'Thumb')
        if median > self.SQUARE_RATIO:
            return (self.caption_geom(items, self.art.geom_square), 'Primary')
        return (self.caption_geom(items, self.art.geom), 'Primary')
    THIRD_LINE_MAX_W = 200
    GLYPH_MIN = 64

    def caption_geom(self, items, geom):
        """``geom``, resized to the caption lines these items actually use.

        Idempotent and cheap, so it can be asked wherever a geometry meets a
        list of items. It has to be asked by whoever *keeps* the geometry,
        though, not deep inside the compositor: a grid computes its row
        pitch, its scroll snap and its virtualization window from
        ``strip_h``, so a band that grew after those were taken windows the
        wrong rows and snaps to the wrong stops.
        """
        if components.has_no_subtitles(items):
            return geom.single_line()
        if geom.tile_w > self.THIRD_LINE_MAX_W:
            return geom
        for item in items or ():
            if item and components.air_time_line(item):
                return geom.with_caption_lines(3)
        return geom

    def square_geom(self, items):
        """``geom_square`` when every item's art is square, else None.

        A strip is composited at one tile size, so this is a per-grid
        decision, not per-tile — hence "every item".

        Was ``ViewsMixin._square_geom``; it is a question about artwork
        geometry, and the geometries live here.
        """
        types = {i.get('Type') for i in items or ()}
        if types and types <= self.SQUARE_TYPES:
            return self.art.geom_square
        return None

    def _request_image(self, key, url, box, cover=False):
        """Return a cached decoded PIL image for ``key`` (poster/backdrop/…),
        or None while it loads — requesting it once from the thumbnail pool.
        The next repaint (woken by the pool's notify) picks it up.

        ``cover`` says the box will be FILLED rather than fitted, so the
        decode crops to its shape instead of contain-ing inside it. Only the
        one pairing ``_contains`` covers passes it; see there and
        ``thumbnails._fit_into``.

        Read *through* the store's cache rather than keeping a copy: it is the
        thing the memory budget is enforced on, and a second dict here made
        that budget decorative. An eviction therefore costs a re-request —
        which is the trade the budget exists to make, and lands on the disk
        cache rather than the server."""
        if self.art.thumbs is None or not url:
            return None
        img = self.art.thumbs.get_cached(key)
        if img is not None:
            return img
        if key in self._requested:
            return None
        retry_at = self._img_retry.get(key)
        if retry_at is not None and time.time() < retry_at[1]:
            return None
        self._requested.add(key)
        self.art.thumbs.request(key, url, box, lambda im, k=key: self._image_done(k, im), cover=cover)
        return None

    def _image_done(self, key, image):
        """Thumbnail delivery, on the loop thread.

        ``image`` is None when the fetch failed. Releasing the dedup marker
        is the whole point: it used to be set before dispatch and never
        cleared, so one timed-out poster stayed blank for the rest of the
        process — no navigation, scroll or re-open would ask again. A
        permanent miss (the server says there's no such image) keeps the
        marker, so it isn't asked for again either."""
        if image is not None:
            self._requested.discard(key)
            self._img_retry.pop(key, None)
            return
        if self.art.thumbs is not None and self.art.thumbs.is_gone(key):
            return
        attempts = self._img_retry.get(key, (0, 0.0))[0] + 1
        if attempts > self.IMG_MAX_ATTEMPTS:
            return
        self._img_retry[key] = (attempts, time.time() + self.IMG_RETRY_BACKOFF * 2 ** (attempts - 1))
        self._requested.discard(key)

    @classmethod
    def _contains(cls, resolved, geom, item=None):
        """Whether this artwork must be drawn WHOLE rather than filling the
        tile. **Contain is the default; one pairing covers.**

        The one is a POSTER GOING INTO A POSTER TILE, where a crop costs a few
        percent off 2:3 key art and buys a perfectly uniform grid. Everywhere
        else the tile and the artwork can disagree by a lot -- a Home Videos
        library, one film in a row of episodes, a wordmark with no frame it
        was cut for -- and a crop is destructive in proportion.

        The per-row shape decision cannot fix that, because the mismatch is
        per ITEM; this is the half that can. Nearly free where it changes
        nothing: agreeing artwork gives the same picture either way.
        See docs/artwork-pipeline.md section 3.
        """
        if geom is None:
            return True
        if resolved != 'Primary':
            return True
        if not cls._poster_shaped(geom.tile_w / float(geom.tile_h or 1)):
            return True
        return not cls._poster_shaped(cls._item_ratio(item))

    @classmethod
    def _poster_shaped(cls, ratio):
        """Taller than square, or unmeasurable.

        Unmeasurable counts as a poster ON PURPOSE, and only ever reaches
        here about artwork already going into a poster tile: most library
        items carry no ``PrimaryImageAspectRatio`` at all, and treating that
        as "not a poster" would letterbox every ordinary film grid on the
        strength of a missing field.
        """
        return ratio is None or ratio <= cls.SQUARE_RATIO

    @staticmethod
    def _item_ratio(item):
        ratio = (item or {}).get('PrimaryImageAspectRatio')
        if isinstance(ratio, (int, float)) and ratio > 0:
            return float(ratio)
        return None

    def poster_for(self, item, geom, image_type='Primary', inherit=True):
        """Return (PIL image or None, cache tag, contain). Requests the
        poster once if absent; the strip recomposites when it arrives (tag
        changes). ``contain`` says the artwork must be drawn whole rather
        than filling the tile -- see :meth:`_contains`.

        ``inherit`` is passed straight to ``image_spec`` — see there. It is
        not part of the cache key because it does not need to be: the key is
        built from the spec the chain *resolved to*, so two rows asking about
        the same item with different inherit settings land on different keys
        whenever they resolve to different images, and share one when they
        do not."""
        w, h = raster(geom.tile_w, geom.tile_h)
        if '_image_url' in item:
            spec, url = (item.get('_image_spec'), item.get('_image_url'))
            if not spec or not url:
                return (None, '', True)
            key = make_key(spec[0], spec[1], spec[2], w, h)
            return (self._request_image(key, url, (w, h)), key, True)
        spec = self.art.source.image_spec(item, image_type, w, inherit=inherit)
        if not spec or self.art.server is None:
            return (None, '', True)
        item_id, itype, itag = spec
        contain = self._contains(itype, geom, item)
        key = make_key(item_id, itype, itag, w, h, fit='fit' if contain else '')
        url = self.art.source.image_url(self.art.server, item_id, itype, itag, w, h, fill=not contain)
        return (self._request_image(key, url, (w, h), cover=not contain), key, contain)

    def art_cell(self, item, size=28):
        """Small square album-art bitmap for a table cell (track lists);
        a placeholder box while it loads or when the item has none.
        Each cell is its own overlay, so only use in virtualized or
        short tables (the 63-overlay budget is shared)."""
        ps = px(size)
        spec = self.art.source.image_spec(item, 'Primary', ps)
        if spec and self.art.server is not None:
            item_id, itype, itag = spec
            key = make_key(item_id, itype, itag, ps, ps)
            url = self.art.source.image_url(self.art.server, item_id, itype, itag, ps, fill=False)
            img = self._request_image(key, url, (ps, ps))
            if img is not None:
                from . import live_tv
                live = live_tv.is_channel_artwork(item)
                img = self._plated(img, live)
                key = key + ':l' if live else key
                b = self.art.strips.bitmap(key, img)
                return Image(b['src'], b['iw'], b['ih'], v=b.get('v', 0), w=b['lw'], h=b['lh'])
        return self._art_placeholder(size, item)

    @staticmethod
    def _plated(img, live=False):
        """``img``, given the backing transparent artwork should sit on.

        An art cell is its own overlay with nothing drawn behind it, so a
        transparent logo composites straight onto ``WINDOW_BG`` — which is
        where the guide's channel column draws every logo it has. A tile gets
        the same treatment by recolouring its card (see
        ``StripStore._paint_poster``); this is the no-card version of it.

        Which backing that is comes from ``strips.logo_plate``, so this and
        the tile compositor answer #637's settings the same way. ``live``
        picks which of the two applies — see ``live_tv.is_channel_artwork``.
        """
        from ..imageutil import flatten_onto, with_shadow
        plate = logo_plate(img, live)
        if plate is None:
            return img
        if plate.shadow:
            img = with_shadow(img)
        return flatten_onto(img, plate.color, radius=px(4))

    @staticmethod
    def _art_placeholder(size=28, item=None):
        """Same-sized stand-in for an art cell — while it loads, when the
        item has none, and for rows outside the virtual window (which must
        not composite: see _track_list).

        ``item`` earns the cell the same glyph a *tile* with no artwork
        gets. Without it an album or artist header was a blank rounded
        rectangle sitting directly above a row of tiles that all carried
        the person icon: one missing-image condition, two renderings, side
        by side.

        Only above ``GLYPH_MIN``. A track table's 28px cell is too small
        for an icon to be anything but noise, and it is a cell in a list
        that already names what it is."""
        kids = []
        if item is not None and size >= TileRenderer.GLYPH_MIN:
            glyph = components.placeholder_glyph(item)
            kids = [Icon(glyph, size // 3, color=theme.SUBTLE_FG) if len(glyph) > 1 else Text(glyph, size=max(14, size // 3), bold=True, color=theme.SUBTLE_FG)]
        return Box(kids, w=size, h=size, bg=theme.PLACEHOLDER_BG, radius=4, align='center', direction='row', justify='center')

    def is_downloaded(self, item):
        iid, t = (item.get('Id'), item.get('Type'))
        if iid in self._downloaded:
            return True
        if t == 'Series':
            return iid in self._downloaded_series
        if t == 'Season':
            return iid in self._downloaded_seasons
        if t != 'Playlist':
            return False
        from ..constants import split_offline_playlist_id
        return split_offline_playlist_id(iid)[0] in self._downloaded_playlists
    BANNER_STEP = 128

    def full_bleed_header(self, item=None):
        """Whether this header runs edge to edge (``backdrop_full_width``).

        Asked once per render and passed to both :meth:`banner_box` and
        ``chrome.header_body``, so the box and the padding around it cannot
        answer differently.

        **No, when the item has no artwork of any kind.** There is nothing to
        bleed: :meth:`backdrop_node` returns a rounded placeholder panel, and
        running that to both edges is a full-width empty grey band across the
        top of the page -- which is not the jellyfin-web look the option is
        for, it is just a bigger version of the thing that look replaces. The
        height is the same either way, so nothing below it moves whichever
        answer this gives. ``item=None`` skips the question, for a caller
        that has not got one.
        """
        from ..conf import settings
        if not getattr(settings, 'backdrop_full_width', True):
            return False
        return item is None or self.header_bakes_heading(item)
    BANNER_MAX_H = 0.6

    def banner_box(self, width, full_bleed=None, height=None):
        """The banner's LAID-OUT size: exactly the space it has.

        Deliberately not quantised. The header spans the content width, so
        rounding this up overhangs the scrollbar and rounding it down leaves
        a gap beside content that does reach the edge. What gets quantised is
        the *fetch* -- see ``_banner_fetch_w``.

        Full bleed widens it to the whole scroll viewport -- the window less
        the scrollbar the view reserves, which is the honest full width;
        asking for ``width`` itself overhangs into the bar.

        **The height does not change.** It is still computed from the padded,
        1100-capped width, so a header takes exactly the vertical space it
        took before and nothing below it moves. What the extra width buys is
        more backdrop, not more page: the shape gets wider, and
        ``compose_banner``'s cover-crop takes the difference off the top and
        bottom of the artwork.
        """
        bw = min(width - 2 * chrome.CONTENT_PAD, 1100)
        bh = int(bw * self.BANNER_RATIO)
        if height:
            bh = min(bh, int(height * self.BANNER_MAX_H))
        if self.full_bleed_header() if full_bleed is None else full_bleed:
            from ..mpvtk.layout import SCROLLBAR_W
            return (max(bw, width - SCROLLBAR_W), bh)
        return (bw, bh)

    def _banner_fetch_w(self, physical_w):
        """Physical width to ask the server for, given the drawn width.

        Rounded UP to a step so a resize stops minting a new request per
        pixel (issue #592) -- the artwork cache keys on exact dimensions, so
        a continuous width meant a continuous stream of misses. Up rather
        than down because the fetched image is *cropped* into the banner by
        compose_banner: larger than needed costs nothing, smaller would have
        to be upscaled.

        Only the fetch. The composed banner is still built at the exact drawn
        size, because that one has to match the box it is drawn in.
        """
        step = self.BANNER_STEP
        if physical_w <= 0:
            return 0
        return int(-(-physical_w // step) * step)

    def header_bakes_heading(self, item):
        """Whether :meth:`backdrop_node` will bake the heading into its
        bitmap — answerable *now*, before any image has arrived.

        The whole point is that it needs no image: both specs are pure
        functions of the DTO the page already has, so a header can reserve
        the right space on its very first paint instead of finding out when
        the bitmap lands. See :meth:`backdrop_node` for what that prevents.

        Called ``has_backdrop`` until the poster fallback below existed, and
        renamed because that stopped being the question: a header with no
        backdrop but a poster to inset draws a banner, bakes its heading,
        and needs the caller *not* to draw one underneath.
        """
        if self.art.server is None:
            return False
        if self.art.source.backdrop_spec(item) is not None:
            return True
        return self._header_poster_spec(item) is not None

    def _header_poster_spec(self, item):
        """The spec for a header's inset poster, or None — the DTO half of
        :meth:`_banner_poster`, split out so ``header_bakes_heading`` can ask
        the question without starting a fetch."""
        from ..conf import settings
        if (item or {}).get('Type') == 'Episode':
            wanted = settings.detail_episode_image
        else:
            wanted = settings.detail_poster
        if self.art.server is None or not wanted:
            return None
        return self.art.source.image_spec(item, 'Primary', 0, inherit=False)
    POSTER_STEP = 64

    def _poster_fetch(self, physical):
        """Physical size to ask for, given a drawn size. Rounded UP to
        :data:`POSTER_STEP` -- see :meth:`_banner_fetch_w` for the rule and
        the reason."""
        step = self.POSTER_STEP
        if physical <= 0:
            return 0
        return int(-(-physical // step) * step)

    def _banner_poster(self, item, box, backdrop_spec):
        """``(image or None, cache tag)`` for the poster inset into a header.

        The item's **own** Primary, with no inheritance: on an episode that
        is the still, which is what belongs beside a series backdrop, and
        borrowing the series poster there would draw the same series twice.

        Returns nothing when the poster would *be* the backdrop -- a home
        video whose landscape Primary is already the banner (see
        ``backdrop_spec``'s Primary step). Drawing it inset over itself is
        the one case that looks like a rendering fault rather than a
        feature.
        """
        from ..conf import settings
        from . import live_tv
        if (item or {}).get('Type') == 'Episode':
            wanted = settings.detail_episode_image
        else:
            wanted = settings.detail_poster
        if self.art.server is None or not wanted:
            return (None, '')
        slot = components.poster_box(box)
        if slot is None:
            return (None, '')
        spec = self.art.source.image_spec(item, 'Primary', slot[2], inherit=False)
        if not spec or spec == backdrop_spec:
            return (None, '')
        item_id, itype, itag = spec
        pw = self._poster_fetch(int(slot[2]))
        ph = self._poster_fetch(int(slot[3]))
        key = make_key(item_id, itype, itag, pw, fit='fit')
        url = self.art.source.image_url(self.art.server, item_id, itype, itag, pw, ph, fill=False)
        img = self._request_image(key, url, (pw, ph))
        if img is None:
            return (None, key)
        return (self._plated(img, live_tv.is_channel_artwork(item)), key)

    def backdrop_node(self, item, box, node_id, title=None, meta=None, context=None):
        """A backdrop banner for detail/series headers.

        With ``title`` the heading is *baked into the bitmap* over a bottom
        gradient -- text drawn as ASS would sit under the image, since bitmaps
        composite above all script ASS.

        **The waiting state bakes the same heading over a flat panel**, and
        that is not cosmetic: the heading is inside the banner's fixed box
        when the art is there, so a header that drew it below the banner while
        waiting moved everything under it (play buttons included) the moment
        the image arrived. Composing the placeholder through the same function
        fixes the geometry at the first paint and leaves the title readable if
        the fetch never succeeds.

        A plain placeholder Box comes back only when the item has no artwork
        **of any kind**, because then there is no baked heading to match.
        :meth:`header_bakes_heading` is how a caller tells the two apart --
        *not* the returned node's type, which cannot distinguish "none" from
        "not yet". See docs/artwork-pipeline.md section 6.
        """
        spec = None
        if self.art.server is not None:
            spec = self.art.source.backdrop_spec(item)
        if spec:
            owner_id, itype, tag = spec
            pbox = raster(*box)
            key = make_key(owner_id, itype, tag, pbox[0], pbox[1])
            if title:
                key += '|' + make_key(title, meta or '', context or '', pbox[0], pbox[1])
            fetch_w = self._banner_fetch_w(pbox[0])
            fetch_key = make_key(owner_id, itype, tag, fetch_w)
            url = self.art.source.backdrop_url(self.art.server, item, width=fetch_w, fill=False)
            img = self._request_image(fetch_key, url, (fetch_w, fetch_w))
            poster, poster_key = self._banner_poster(item, pbox, spec)
            key += '|' + (poster_key if poster is not None else 'nopo')
            if img is not None:
                b = self.art.strips.bitmap(key, lambda: components.compose_banner(img, pbox, title, meta, context, poster=poster), lsize=box)
                return Image(b['src'], b['iw'], b['ih'], id=node_id, v=b.get('v', 0), w=b['lw'], h=b['lh'])
            if title:
                pending = 'pending|' + key
                b = self.art.strips.bitmap(pending, lambda: components.compose_banner(_flat_image(pbox, theme.PLACEHOLDER_BG), pbox, title, meta, context, poster=poster), lsize=box)
                return Image(b['src'], b['iw'], b['ih'], id=node_id, v=b.get('v', 0), w=b['lw'], h=b['lh'])
        if title and self._header_poster_spec(item) is not None:
            pbox = raster(*box)
            poster, poster_key = self._banner_poster(item, pbox, None)
            key = 'noart|' + make_key(title, meta or '', context or '', pbox[0], pbox[1]) + '|' + (poster_key if poster is not None else 'nopo')
            b = self.art.strips.bitmap(key, lambda: components.compose_banner(_flat_image(pbox, theme.PLACEHOLDER_BG), pbox, title, meta, context, poster=poster), lsize=box)
            return Image(b['src'], b['iw'], b['ih'], id=node_id, v=b.get('v', 0), w=b['lw'], h=b['lh'])
        return Box(w=box[0], h=box[1], bg=theme.PLACEHOLDER_BG, radius=6, id=node_id)

    def _tile(self, item, geom, image_type='Primary', parent_item=False, inherit=True, labels=None):
        """One tile. ``parent_item`` draws an Episode as its *series* —
        the show's name and the show's poster instead of the episode's name
        over the episode still — which is what a Latest-TV row is a list of.
        It changes which image is fetched, not the tile's shape: the caller's
        ``geom`` still decides that."""
        ud = item.get('UserData') or {}
        pos = ud.get('PlaybackPositionTicks') or 0
        rt = item.get('RunTimeTicks') or 0
        from . import live_tv
        progress = pos / rt if pos and rt else 0.0
        if not progress and item.get('IsFolder'):
            progress = float(ud.get('PlayedPercentage') or 0.0) / 100.0
        recording = live_tv.is_recording_now(item)
        record = live_tv.timer_state(item) or ''
        if item.get('_recording') and (not record):
            record = 'recording'
        if item.get('Type') == 'Program' or recording:
            progress = live_tv.program_progress(item) or progress
        if parent_item and item.get('Type') == 'Episode':
            image_type = 'ParentPrimary'
        poster, tag, contain = self.poster_for(item, geom, image_type, inherit=inherit)
        show_title, show_year = labels or (True, True)
        third = geom.caption_lines >= 3
        title, subtitle = components.tile_lines(item, parent_item, show_year=show_year, air_time=not third)
        subtitle2 = components.air_time_line(item) if third else ''
        if third and is_extra(item):
            ticks = item.get('RunTimeTicks')
            lines = [s for s in (subtitle, fmt_ticks(ticks) if ticks else '') if s]
            subtitle, subtitle2 = (lines + ['', ''])[:2]
        if not show_title:
            title = ''
            if not show_year:
                subtitle = ''
                subtitle2 = ''
        return Tile(key=item.get('Id', ''), title=title, subtitle=subtitle, subtitle2=subtitle2, poster=poster, poster_tag=tag, glyph=components.placeholder_glyph(item), watched=components.is_watched(item), badge=int(ud.get('UnplayedItemCount') or 0), tag=components.virtual_episode_label(item) or '', progress=progress, downloaded=self.is_downloaded(item), kind=components.type_indicator_icon(item), recording=recording, record=record, contain=contain, live=live_tv.is_channel_artwork(item), sources=int(item.get('MediaSourceCount') or 0))

    def section_heading(self, title, row_id=None, see_all=None):
        """A section title, indented to sit exactly above the first tile.

        Both spellings of the heading are the SAME box, and only the contents
        and the handlers differ. They used to be a bare Text and a padded Box,
        which meant a linked row's title sat 6px right and 2px down of a plain
        one and the whole row measured 4px taller -- so on a page that mixes
        them (the home screen: Next Up links, Continue Watching does not) the
        titles jogged sideways row to row.

        Callers that draw a heading with no carousel under it (the home
        screen's Live TV button row) take it from here rather than
        open-coding the indent, which is how they drifted apart before.
        """
        title_size = theme.heading_size()
        inner: list = [Text(title, size=title_size, bold=True)]
        link: dict = {}
        if see_all is not None:
            inner.append(Icon('chevron_right', int(title_size * 0.9), color=theme.TEXT_FG))
            link = {'id': '%s-more' % row_id, 'hover': {'fill': theme.BUTTON_ACTIVE}, 'on_click': see_all}
        return Row([Spacer(w=max(0, chrome.CONTENT_PAD - RING_PAD)), Box(inner, direction='row', align='center', gap=max(6, title_size // 4), pad=(RING_PAD, 2), radius=6, **link)], align='center')

    def tile_row(self, title, items, row_id, geom=None, image_type='Primary', on_click=None, parent_item=False, inherit=True, see_all=None, autofocus_first=False, lazy=False):
        """A titled horizontal carousel, full width.

        The strip runs edge to edge and carries its own margins *inside* the
        scroll viewport, so the first tile starts at the page margin and a
        paged one travels out to the window edge instead of being clipped
        there. jellyfin-web does exactly this -- the same `padded-left` on
        `.sectionTitleContainer` and on `.emby-scroller`, which is padding
        and therefore scrollable. ``parent_item`` is passed through to the
        tiles (see ``_tile``).

        The returned block is marked :func:`chrome.bleed`, which is what lets
        a page put it in an otherwise padded column.

        ``lazy`` returns a stand-in instead: the same heading and the same
        scroll container around a blank of the strip's exact size, asking
        for no artwork, which :meth:`window_rows` swaps for the real row
        when it comes near the viewport.
        """
        geom = geom or self.art.geom
        if lazy:
            args = (title, items, row_id)
            kw = dict(geom=geom, image_type=image_type, on_click=on_click, parent_item=parent_item, inherit=inherit, see_all=see_all, autofocus_first=autofocus_first)
            n = len(items)
            stand_in = self._tile_row(title, items, row_id, geom, see_all, Spacer(w=n * geom.tile_w + (n - 1) * geom.gap if n else 1, h=geom.strip_h))
            stand_in._jms_realize = lambda: self.tile_row(*args, **kw)
            return stand_in
        return self._tile_row(title, items, row_id, geom, see_all, self.image_map(items, row_id, geom, image_type, on_click=on_click, parent_item=parent_item, inherit=inherit, autofocus_first=autofocus_first))

    def _tile_row(self, title, items, row_id, geom, see_all, content):
        heading = self.section_heading(title, row_id, see_all)
        buttons = self.page_buttons(row_id, len(items), geom)
        if buttons is not None:
            heading = Row(list(heading.children) + [Spacer(flex=1)] + buttons + [Spacer(w=chrome.CONTENT_PAD)], align='center', w=self.row_view_w())
        return chrome.bleed(Column([heading, self.hscroll_row(content, row_id, geom.strip_h + 2 * RING_PAD, len(items), geom)], gap=TITLE_GAP))

    def window_rows(self, blocks, scroll_id, size, gap, pad=0):
        """``blocks`` with every lazy :meth:`tile_row` that is within a
        screen of the viewport made real -- :meth:`row_window`'s rule, for a
        stack of carousels rather than a grid.

        Without it a screen of carousels asks for every poster on it at
        every build, and past the decoded-image budget (a home screen with
        a dozen libraries on a HiDPI display) each build evicts the art the
        last one fetched: rows flash between placeholders and posters.
        ``blocks`` is the scroll's direct children; the page must also
        repaint on scroll (``ScrollState.on_scroll``), or rows that scroll
        into the window are never made real.
        """
        from ..mpvtk.layout import measure
        vh = max(240.0, float(size[1]))
        top = self.scroll.offset(scroll_id)
        lo, hi = (top - vh, top + 2 * vh)
        out, y = ([], float(pad))
        for b in blocks:
            if b is None:
                continue
            h = measure(b)[1]
            realize = getattr(b, '_jms_realize', None)
            if realize is not None and y + h >= lo and (y <= hi):
                b = realize()
            out.append(b)
            y += h + gap
        return out

    def header_offset(self, header):
        """Exact content-y of the first tile row that follows ``header`` in a
        ``Column(pad=CONTENT_PAD, gap=GRID_GAP)``: the top pad, each header
        item's measured height, and one gap after each. This is the snap offset
        for a snapping grid — the virtualizer's approximate ``head_h`` would
        land a stop a few px short and leave the previous row's caption (its
        year label) peeking at the top edge. ``header`` may be empty (grid
        fills the scroll), and None entries are dropped to match Column."""
        from ..mpvtk.layout import measure
        hs = [h for h in header if h is not None]
        return chrome.CONTENT_PAD + len(hs) * GRID_GAP + sum((measure(h)[1] for h in hs))

    def row_window(self, size, geom, scroll_id, head_h=0):
        """``(first, last)`` grid row indexes that get composited: the
        viewport plus a screen of slack either side.

        Public because the *page* needs the same answer. A windowed grid
        fetches the items it is about to draw (Paginator.window), and asking
        that question a second way is how the rows drawn and the rows
        fetched come to differ.
        """
        rh = geom.strip_h + GRID_GAP
        vh = max(240.0, float(size[1]))
        top = max(0.0, self.scroll.offset(scroll_id) - head_h)
        return (int(max(0.0, top - vh) // rh), int((top + 2 * vh) // rh))

    def grid_of(self, items, prefix, size, geom=None, image_type='Primary', scroll_id=None, head_h=0, on_click=None, inherit=True, labels=None):
        """Tile rows for a vertical grid.

        With ``scroll_id`` the rows are **virtualized**: only those within a
        screen of the viewport are composited, the rest become fixed-height
        Spacers. Without it a long library blows past both the strip cache and
        mpv's 63-overlay budget, which showed up as tiles that came back blank
        after scrolling away and back."""
        geom = geom or self.art.geom
        cols = self.cols(size[0], geom)
        rows = []
        nrows = (len(items) + cols - 1) // cols
        first, last = (0, nrows - 1)
        if scroll_id is not None:
            first, last = self.row_window(size, geom, scroll_id, head_h)
        for r in range(nrows):
            if first <= r <= last:
                start = r * cols
                rows.append(self.image_map(items[start:start + cols], '%s-%d' % (prefix, start), geom, image_type, on_click=on_click, inherit=inherit, labels=labels, async_=scroll_id is not None))
            else:
                rows.append(Spacer(h=geom.strip_h))
        if not items:
            rows.append(Text(_('Nothing here.'), size='large', color=theme.SUBTLE_FG))
        return rows

    def image_map(self, items, prefix, geom=None, image_type='Primary', on_click=None, async_=False, parent_item=False, inherit=True, labels=None, autofocus_first=False):
        geom = geom or self.art.geom
        tiles = [self._tile(it, geom, image_type, parent_item, inherit, labels) if it else Tile(key='_pending%d' % n) for n, it in enumerate(items)]
        s = self.art.strips.strip(tiles, geom, async_=async_)
        regions = []
        act = on_click or self.on_open
        chip = None
        repeated = Counter((r['key'] for r in s['regions']))
        for i, (r, it) in enumerate(zip(s['regions'], items)):
            if not it:
                continue
            rid = '%s-%s' % (prefix, r['key'])
            if repeated[r['key']] > 1:
                rid = '%s-%d' % (rid, i)
            playable = self.on_play is not None and self.can_play(it)
            regions.append(dict(r, id=rid, on_click=lambda i=it: act(i), on_context=lambda x, y, i=it: self.on_context(i, x, y), on_hover=(lambda _v=None, k=rid: self.set_hover(k)) if playable else None, on_hover_end=(lambda k=rid: self.end_hover(k)) if playable else None, autofocus=autofocus_first and i == 0, hover={}))
            if playable and self._hover == rid:
                chip = self._play_chip(rid, it, r, geom)
        node = ImageMap(s['src'], s['iw'], s['ih'], regions=regions, id='%s#strip' % prefix, v=s.get('v', 0), w=s['lw'], h=s['lh'])
        if chip is None:
            return node
        return Stack([node, chip], w=s['lw'], h=s['lh'])

    def _play_chip(self, region_id, item, region, geom):
        """The round play button drawn over the hovered tile.

        Centred on the ARTWORK rather than the tile: the region includes the
        caption block underneath, and a chip centred on that sits visibly low
        on the picture.
        """
        cid = region_id + self.CHIP_SUFFIX
        hot = self._hover_node == cid
        size = int(max(CHIP_MIN, min(CHIP_MAX, min(geom.tile_w, geom.tile_h) * CHIP_FRACTION)) * (CHIP_HOT_SCALE if hot else 1.0))
        bm = self.art.strips.bitmap(('play-chip', px(size), hot), lambda: _play_chip_bitmap(size, hot=hot), lsize=(size, size))
        dx = region['x'] + (geom.tile_w - size) / 2
        dy = region['y'] + (geom.tile_h - size) / 2
        return Image(bm['src'], bm['iw'], bm['ih'], v=bm.get('v', 0), w=bm['lw'], h=bm['lh'], id=cid, anchor='nw', dx=dx, dy=dy, on_click=lambda: self.on_play(item), on_context=lambda x, y, i=item: self.on_context(i, x, y), on_hover=lambda _v=None, k=cid: self.set_hover(k), on_hover_end=lambda k=cid: self.end_hover(k))

    def row_view_w(self):
        """Laid-out width of a carousel's scroll viewport. Both the overflow
        test and the paging arithmetic measure against this.

        The whole window: a row is full width and holds its margins inside
        the viewport, so nothing is subtracted here. ``page_geometry`` is
        where ``ROW_LEAD`` enters the arithmetic."""
        return self.art._size[0] if self.art._size else 1280

    def _page_state(self, row_id, count, geom):
        """``(view, prev_target, next_target)`` for a carousel's page buttons,
        or ``None`` when it should not have any.

        A ``None`` target is an end stop, i.e. a disabled button. Computed once
        and shared by both arrow modes so the two cannot disagree about whether
        a row overflows.
        """
        view = self.row_view_w()
        content_w = 2 * ROW_LEAD + count * geom.tile_w + max(0, count - 1) * geom.gap
        if content_w <= view or self.nav_mode():
            return None
        offset = self.scroll.offset(row_id)
        return (view, page_target(offset, -1, view, count, geom), page_target(offset, 1, view, count, geom))

    def page_buttons(self, row_id, count, geom):
        """The heading-row page buttons (``header`` arrow mode), or ``None``.

        jellyfin-web's design, and the default: the pair sits in the section
        heading rather than on the artwork, so it can be an ordinary pair of
        flat HUD-style buttons — hover works, and an exhausted direction dims
        instead of misleading. ``None`` for themes in ``overlay`` mode, whose
        buttons are drawn by :meth:`hscroll_row` instead.
        """
        if (theme.active() or {}).get('arrow_mode', 'header') != 'header':
            return None
        state = self._page_state(row_id, count, geom)
        if state is None:
            return None
        view, prev_to, next_to = state

        def button(icon, node_id, direction, target, label):
            off = target is None
            return Button('', id=node_id, icon=icon, flat=True, icon_size=22, tip=label, fg=theme.mix(theme.SUBTLE_FG, theme.WINDOW_BG, 0.5) if off else theme.TEXT_FG, hover=None if off else {'fill': theme.ACCENT, 'circle': True}, repeat=not off, on_click=lambda: self.page_row(row_id, direction, count, geom))
        return [button('chevron_left', row_id + '-pl', -1, prev_to, _('Previous')), button('chevron_right', row_id + '-pr', 1, next_to, _('Next'))]

    def hscroll_row(self, content, row_id, h, count, geom):
        """An HScroll, with ◀ ▶ page buttons floating over its edges in the
        ``overlay`` arrow mode.

        Those arrows genuinely overlay the poster strip: a Stack layers them on
        top, and each is a composited BITMAP, which is the only thing that can
        draw *over* a strip and alpha-blend with it (bitmaps composite above
        all script ASS — GUIDE §6). They hold-repeat while pressed, fade at an
        end stop, and are omitted when the row doesn't overflow.

        In the default ``header`` mode there is nothing over the strip at all —
        :meth:`page_buttons` puts the pair in the section heading instead.

        The strip is inset vertically by RING_PAD so a tile's hover ring has
        room inside the viewport; the renderer clips it to the container, and
        without the inset its top edge was shaved off under the heading
        above."""
        state = self._page_state(row_id, count, geom)
        watch = None
        if state is not None:
            watch = lambda off, mx, rid=row_id: self.scroll.on_scroll(rid, off, mx, edges_only=True)
        scroll = HScroll(Box([content], pad=(ROW_LEAD, RING_PAD)), id=row_id, h=h, flex=1, on_scroll=watch, offset=self.scroll.pending(row_id))
        if state is None or (theme.active() or {}).get('arrow_mode', 'header') != 'overlay':
            return Row([scroll], h=h)
        _view, prev_to, next_to = state

        def arrow(node_id, direction, anchor, target):
            dy = -(geom.strip_h - geom.tile_h) / 2
            dx = ARROW_INSET if anchor == 'w' else -ARROW_INSET
            off = target is None
            img = _arrow_bitmap(direction, disabled=off)
            bm = self.art.strips.bitmap(('carousel-arrow', direction, px(ARROW_W), off), img, lsize=(ARROW_W, ARROW_W))
            return Image(bm['src'], bm['iw'], bm['ih'], v=bm.get('v', 0), w=bm['lw'], h=bm['lh'], id=node_id, anchor=anchor, dx=dx, dy=dy, repeat=not off, on_click=lambda: self.page_row(row_id, direction, count, geom))
        return Stack([scroll, arrow(row_id + '-pl', -1, 'w', prev_to), arrow(row_id + '-pr', 1, 'e', next_to)], h=h)

    def page_row(self, row_id, direction, count, geom):
        """Page a carousel one screenful of whole tiles (jellyfin-web's
        ``scrollerItemSlideIntoView``; see :func:`page_target`).

        The offset is re-read from the renderer here rather than reused from
        build time: a scroll shorter than ``ScrollState.STEP`` never triggered
        a rebuild, so the built-in copy can be that far behind at the moment of
        a click — and paging from a stale offset lands on the wrong tile.
        """
        app = self.get_app()
        if app is None or not hasattr(app, 'scroll_to'):
            return
        self.scroll.refresh(app)
        target = page_target(self.scroll.offset(row_id), direction, self.row_view_w(), count, geom)
        if target is None:
            return
        app.scroll_to(row_id, target, ms=PAGE_SLIDE_MS)
    JUSTIFY_SLACK = 32

    def justified_gap(self, w, geom, cols=None):
        """``geom`` with its gap widened to take up the slack, so a row runs
        from margin to margin instead of leaving the remainder on one side.

        The alternative to centring (:meth:`grid_pad`), and the difference is
        which number stays still: centring keeps the tiles evenly placed and
        makes BOTH margins move with the window, justifying keeps the margins
        pinned at CONTENT_PAD and moves the tiles apart. A page margin that
        changes as you resize is the more noticeable of the two, because it
        is the edge every other element on the page lines up against.
        """
        cols = self.cols(w, geom) if cols is None else cols
        if cols < 2:
            return geom
        gaps = cols - 1
        slack = max(0, self.body_w(w) - (cols * geom.tile_w + gaps * geom.gap))
        slack = min(slack, geom.gap * gaps)
        step = max(1, self.JUSTIFY_SLACK // gaps)
        extra = slack // gaps // step * step
        if extra <= 0:
            return geom
        return dataclasses.replace(geom, gap=geom.gap + extra)

    def grid_layout(self, w, geom, cols=None):
        """``(horizontal pad, geom)`` for a grid, per ``grid_fill``.

        A grid of fixed-size tiles almost never divides the width exactly,
        and the three settings are three answers to where the remainder goes:

        * ``justify`` -- into the gaps. Margins stay pinned at CONTENT_PAD on
          both sides, which is the edge every other element on the page lines
          up against; the tiles move apart instead.
        * ``center`` -- split between the two margins. The tiles keep their
          spacing and both margins move with the window.
        * ``off`` -- all of it on the right, which is what this did before
          any of them existed.

        One call rather than a pad here and a geometry there, because the two
        answers are exclusive and a caller that mixed them would double-count
        the same slack.
        """
        from ..conf import settings
        mode = getattr(settings, 'grid_fill', 'justify')
        if mode == 'center':
            return (self.grid_pad(w, geom, cols), geom)
        if mode == 'justify':
            return (chrome.CONTENT_PAD, self.justified_gap(w, geom, cols))
        return (chrome.CONTENT_PAD, geom)

    def grid_pad(self, w, geom, cols=None):
        """Horizontal padding for a grid, so its two margins match.

        A whole number of tiles rarely divides the width exactly, and the
        remainder used to land entirely on the right -- at some sizes most of a
        tile's width down one side, which reads as misalignment rather than as
        a grid fitting what it can.

        Measured against ``body_w`` (inside the scrollbar gutter) because the
        bar is furniture the reader can see. Returned as a padding rather than
        applied to the rows, so the header moves with it and the title stays
        aligned with the first tile. See docs/artwork-pipeline.md section 7.
        """
        cols = self.cols(w, geom) if cols is None else cols
        used = cols * geom.tile_w + max(0, cols - 1) * geom.gap
        return chrome.CONTENT_PAD + max(0, self.body_w(w) - used) // 2

    def cols(self, w, geom):
        return max(1, int((self.body_w(w) + geom.gap) // (geom.tile_w + geom.gap)))

    def item_list(self, rows, prefix, on_click, scroll_id=None, head_h=0):
        """A library as a plain table -- jellyfin-web's List view type.

        Deliberately without an art column, unlike ``track_list``: the point
        of choosing this view is that the artwork was not helping, and it is
        also what makes it cheap — no strips, and no overlays to spend.

        **Virtualized all the same.** "No overlays" was read as "no need to
        virtualize", and it is only half the cost: a row is still a Row, two
        margin Spacers and three cell Boxes to build, lay out and serialize,
        on the loop thread, every repaint. Infinite scroll grows this list
        without bound and List view is the one people pick *because* the
        library is large — at 2000 rows that measured ~8k nodes, 85ms and
        1.4MB per push, repeated every 120px of scroll.
        """
        columns = [{'label': _('Name'), 'flex': 4}, {'label': _('Year'), 'w': 80}, {'label': _('Length'), 'w': 110, 'align': 'right'}]
        table_rows = []
        for i, row in enumerate(rows):
            item = row.get('item')
            table_rows.append({'id': '%s-%d-%s' % (prefix, i, (item or {}).get('Id', '')), 'cells': row['cells'], 'on_click': (lambda i=i: on_click(i)) if item else None, 'on_context': (lambda x, y, it=item: self.on_context(it, x, y)) if item else None})
        return Table(columns, table_rows, row_h=TRACK_ROW_H, id=(scroll_id or prefix) + '-table', hover_bg=theme.BUTTON_ACTIVE, virtual=self.list_virtual(scroll_id, head_h))

    def list_virtual(self, scroll_id, head_h=0):
        """The ``virtual`` spec a table gets: the same window ``track_list``
        computes, and for the same reason minus the overlays. None when the
        caller names no scroll container -- without one there is no live
        offset to window against."""
        if scroll_id is None or getattr(self.art, '_size', None) is None:
            return None
        return {'offset': max(0.0, self.scroll.offset(scroll_id) - head_h), 'height': float(self.art._size[1])}

    def list_window(self, count, scroll_id, head_h=0):
        """``(first, last)`` table rows an ``item_list`` materializes.

        The list view's ``row_window``, and public for the same reason: a
        windowed page fetches the rows it is about to draw, and asking that
        question a second way is how the two come to differ."""
        return virtual_window(self.list_virtual(scroll_id, head_h), TRACK_ROW_H, count)

    def track_list(self, tracks, prefix, on_play, playing_id=None, selected=None, on_select=None, album=True, art=False, scroll_id=None, head_h=0, menu=False, watched=False):
        """Tabular track list (album, playlist, queue, search songs).

        Uses the toolkit's Table so header and cells come from one column
        spec — hand-laid Rows drifted out of alignment as soon as a cell's
        text width changed.

        With ``on_select`` the row click selects (mods-aware) and the first
        column becomes a play button, so a selectable list still has a
        one-click way to jump to a track. Without it, clicking the row plays.

        ``art=True`` adds a leading album-art thumbnail column — useful in
        mixed-album lists (playlists); redundant on an album page.

        ``watched=True`` adds a leading tick column for entries that have
        been played. Off by default because it is dead weight on a music
        playlist, where nobody tracks which songs they have heard — but on
        an audiobook it is the whole state of the thing: which chapters are
        behind you is what Resume is computed from, and without it marking
        one had no visible effect at all.
        """
        selected = selected or set()
        columns = []
        if watched:
            columns.append({'label': '', 'w': 26})
        if art:
            columns.append({'label': '', 'w': 32})
        columns += [{'label': '#', 'w': 46, 'align': 'right'}, {'label': _('Title'), 'flex': 3}, {'label': _('Artist'), 'flex': 2}]
        if album:
            columns.append({'label': _('Album'), 'flex': 2})
        columns.append({'label': _('Time'), 'w': 70, 'align': 'right'})

        def first_cell(i, tr):
            if on_select is None:
                return str(tr.get('IndexNumber') or i + 1)
            return Box([Icon('play_arrow', 16, color=theme.ACCENT if tr.get('Id') == playing_id else theme.SUBTLE_FG)], id='%s-play-%d' % (prefix, i), w=40, h=26, direction='row', align='center', justify='center', radius=4, hover={'fill': theme.BUTTON_ACTIVE}, on_click=lambda i=i: on_play(i))
        virtual = None
        if scroll_id is not None and self.art._size is not None:
            virtual = {'offset': max(0.0, self.scroll.offset(scroll_id) - head_h), 'height': float(self.art._size[1])}
        art_first, art_last = virtual_window(virtual, TRACK_ROW_H, len(tracks))
        rows = []
        for i, tr in enumerate(tracks):
            if not tr:
                cells = ([''] if watched else []) + ([self._art_placeholder()] if art else []) + ['', '', ''] + ([''] if album else []) + ['']
                rows.append({'id': '%s-%d' % (prefix, i), 'cells': cells})
                continue
            playing = playing_id is not None and tr.get('Id') == playing_id
            cells = [first_cell(i, tr), tr.get('Name', ''), components.track_artists(tr)]
            if art:
                cells.insert(0, self.art_cell(tr) if art_first <= i < art_last else self._art_placeholder())
            if watched:
                cells.insert(0, Icon('check', 15, color=theme.ACCENT) if components.is_watched(tr) else '')
            if album:
                cells.append(tr.get('Album', '') or '')
            cells.append(components.track_duration(tr))
            row = {'id': '%s-%d' % (prefix, i), 'selected': i in selected or playing, 'cells': cells, 'on_click': (lambda mods, i=i: on_select(i, mods)) if on_select is not None else lambda i=i: on_play(i)}
            if on_select is not None:
                row['on_dbl'] = lambda i=i: on_play(i)
            if menu:
                row['on_context'] = lambda x, y, tr=tr: self.on_context(tr, x, y)
            rows.append(row)
        return Table(columns, rows, size='normal', row_h=TRACK_ROW_H, hover_bg=theme.BUTTON_ACTIVE, virtual=virtual)

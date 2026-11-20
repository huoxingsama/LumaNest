"""Production strip compositor for the mpvtk browser.

A "strip" is one BGRA bitmap holding a whole row of tiles -- posters plus
baked-in captions, year/subtitle, watched checkmarks, unwatched-count badges
and resume progress bars -- declared to the renderer as a single ``ImageMap``
with one transparent hit-region per tile. This is what makes tiles scale
(GUIDE §5/§6): a screenful is a handful of overlays instead of one per poster,
and scrolling is pure crop math on cached bitmaps.

Strips are **content-keyed**: the key folds in every visible property, so
changing any of them composites a new bitmap under a new src.

**A new src alone does NOT guarantee the renderer refreshes.** On libmpv src is
a malloc address, and addresses are recycled once a freed buffer leaves
MemoryStore's graveyard -- so a new entry can be handed a departed entry's
exact src. Every entry also carries a monotonic ``v`` (see ``_store``), and
that is what keeps the renderer's overlay cache off stale content.

Backends: libmpv gets a ``MemoryStore`` (ctypes buffers, ``&<addr>`` src, no
fs); jsonipc gets BGRA files. A tile with no poster yet renders a placeholder
and recomposites when the poster arrives (its ``poster_tag`` changes the key).

See docs/artwork-pipeline.md for shapes, plating and the cache policy.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import dataclasses
import logging
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional
from ..mpvtk.rawimage import write_bgra
from . import theme
log = logging.getLogger('mpvtk_browser.strips')

def _px(v):
    """Logical -> physical, for constants baked into a strip bitmap."""
    from ..mpvtk.scaling import px
    return px(v)
_AA_SS = 4
_aa_masks: dict = {}

def _aa_mask(w, h, radius=None):
    """An antialiased ``L`` coverage mask of an ellipse (``radius`` None) or
    a rounded rectangle, ``w`` x ``h``.

    Drawn at ``_AA_SS`` times the size and box-averaged down -- BOX rather
    than LANCZOS because the ratio is exact, which makes it the true area
    coverage; LANCZOS's negative lobes put a faint dark ring outside a shape
    whose mask should simply reach zero.
    """
    key = (int(w), int(h), radius if radius is None else int(radius))
    mask = _aa_masks.get(key)
    if mask is not None:
        return mask
    from PIL import Image as PILImage, ImageDraw
    w, h = (max(1, int(w)), max(1, int(h)))
    big = PILImage.new('L', (w * _AA_SS, h * _AA_SS), 0)
    box = [0, 0, w * _AA_SS - 1, h * _AA_SS - 1]
    draw = ImageDraw.Draw(big)
    if radius is None:
        draw.ellipse(box, fill=255)
    else:
        draw.rounded_rectangle(box, radius=int(radius) * _AA_SS, fill=255)
    mask = big.resize((w, h), PILImage.BOX)
    _aa_masks[key] = mask
    if len(_aa_masks) > 64:
        _aa_masks.pop(next(iter(_aa_masks)))
    return mask

def _card_mask(g, radius):
    """The rounded card's silhouette as an ``L`` mask, tile-sized, for
    clipping anything painted edge to edge into a rounded tile.

    One coverage mask is applied to the completed opaque card, including
    its artwork and border. This prevents double alpha coverage at edges.
    """
    return _aa_mask(g.tile_w, g.tile_h, radius)

def _aa_fill(img, box, colour, radius=None):
    """Fill ``box`` (``[x0, y0, x1, y1]``, inclusive) in ``img`` with an
    antialiased ellipse or rounded rectangle.

    ``colour`` is an ``(r, g, b)`` or ``(r, g, b, a)`` tuple; an alpha there
    scales the coverage rather than replacing it, so a translucent badge
    keeps its soft edge. Uses ``paste``, not ``alpha_composite``, because a
    badge near the end of a strip may hang past its bounds and paste clips
    where the other raises.
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = (x1 - x0 + 1, y1 - y0 + 1)
    if w <= 0 or h <= 0:
        return
    mask = _aa_mask(w, h, radius)
    alpha = colour[3] if len(colour) > 3 else 255
    if alpha < 255:
        mask = mask.point(lambda v, a=alpha: v * a // 255)
    img.paste(tuple(colour[:3]) + (255,), (x0, y0), mask)

def logo_plate(image, live=False):
    """The plate for transparent artwork, or ``None`` to leave it alone.

    ``imageutil.plate_for`` decides *whether* a picture is a logo on a
    transparent background; this is the one place that decides what the app
    does about it, so the tile compositor and the table's art cells cannot
    drift apart.

    ``live`` says the artwork is a channel logo rather than a film's or
    series' own Logo art (from ``live_tv.is_channel_artwork`` -- only the
    caller knows what it is drawing). The two are settings apart because they
    are **conventions apart**: channel logos are dark ink drawn for a white
    page and need the plate to be visible at all, film logos are white and do
    not (#637).

    Only the colour and the shadow move. Whether there is a plate at all is
    still ``plate_for``'s answer, because the callers read it for a second
    thing -- a mark rather than a photograph, so letterboxed rather than
    cover-cropped either way. See docs/artwork-pipeline.md section 2.
    """
    from ..conf import settings
    from ..imageutil import Plate, plate_for
    key = 'logo_legibility_live_tv' if live else 'logo_legibility_library'
    if getattr(settings, key, live):
        return plate_for(image)
    grey = theme.rgb(theme.CARD_BG)
    plate = plate_for(image, light=grey)
    return None if plate is None else Plate(grey, False)

def _font(size, bold=False, text=None):
    """Font for baked caption text. ``text`` selects the script-appropriate
    face — Pillow does no font fallback, so a Japanese title drawn with the
    Latin face is a row of tofu (see mpvtk.pilfont)."""
    from ..mpvtk import pilfont
    if text is None:
        return pilfont.font('latin', size, bold)
    return pilfont.font_for(text, size, bold)

@dataclass
class TileGeom:
    """Pixel geometry for a tile (poster + caption block below it)."""
    tile_w: int = 140
    tile_h: int = 210
    gap: int = 16
    caption_h: int = 50
    title_size: int = 16
    sub_size: int = 14
    badge_size: int = 14
    caption_lines: int = 2
    SUB_GAP = 4
    TITLE_GAP = 8

    def with_caption_lines(self, n):
        """A copy with room for ``n`` caption lines instead of two.

        **Idempotent** -- ``caption_lines`` is stored, so a geometry that
        already has three is returned unchanged. That is what lets more than
        one caller ask (see ``caption_lines``) without the band growing once
        per caller.

        The band, not just the permission: ``_paint_caption`` lays out inside
        a fixed ``caption_h`` and nothing clips it back, so a third line
        drawn into a two-line band draws over the top of the next grid row.
        """
        if n <= self.caption_lines:
            return self
        extra = (n - self.caption_lines) * (self.sub_size + self.SUB_GAP)
        return dataclasses.replace(self, caption_lines=n, caption_h=self.caption_h + extra)

    def single_line(self):
        """A copy whose band holds a title and nothing else.

        The counterpart of :meth:`with_caption_lines`, and separate from it
        because the arithmetic is not symmetric: growing 2 -> 3 buys a line
        at :data:`SUB_GAP`, shrinking 2 -> 1 gives one back at
        :data:`TITLE_GAP`. Folding them into one signed method got that
        wrong in the obvious way.

        Only from two. A three-line band was asked for by its row (who asks:
        ``caption_lines``) and is kept even where a tile leaves a line empty.

        Reclaims the gap and the subtitle, keeping whatever slack the band
        carries under its last line -- so a one-line caption sits in the
        band exactly as a two-line one does, rather than being cropped to
        its own text.
        """
        if self.caption_lines != 2:
            return self
        return dataclasses.replace(self, caption_lines=1, caption_h=self.caption_h - (self.sub_size + self.TITLE_GAP))

    def with_text_scale(self, factor, minimum=0):
        """A copy with the caption text scaled, and room made for it.

        The caption sizes are **geometry**, not type-scale tiers: they are
        baked into the strip bitmap by Pillow, so the user's text preference
        has to be applied here separately -- it does not arrive through
        `theme.size()` like everything drawn as a text node, which is why
        turning text up left the tiles alone.

        This is the ONLY thing that moves a tile's type. Cover Size used to
        as well, silently; see :meth:`scaled` for why it no longer does.

        ``caption_h`` grows with them. It is a fixed band under the
        artwork and `_paint_caption` lays out inside it as title, a 7px
        gap, then subtitle -- 15 + 7 + 13 in 46px at stock. Scale the type
        without the band and the subtitle is simply clipped away.

        The band is measured against ``caption_lines``, not against two, or
        a three-line Live TV caption loses its air time to exactly the same
        clipping the moment somebody turns text up.
        """

        def scaled(value):
            return max(1, int(round(value * factor)), int(minimum or 0))
        title, sub = (scaled(self.title_size), scaled(self.sub_size))
        subs = max(0, self.caption_lines - 1)
        slack = self.caption_h - (self.title_size + self.sub_size * subs)
        return dataclasses.replace(self, title_size=title, sub_size=sub, badge_size=scaled(self.badge_size), caption_h=max(self.caption_h, title + sub * subs + max(slack, 0)))

    @property
    def strip_h(self):
        return self.tile_h + self.caption_h

    def physical(self):
        """A copy with every pixel field converted to physical px.

        TileGeom is authored in LOGICAL units like the rest of the view
        layer; only the compositor below works in physical pixels, because
        the renderer crops bitmaps rather than resampling them.

        ``replace``, not a fresh TileGeom: the fields that are NOT pixels
        (``caption_lines``) have to survive the conversion, and spelling the
        pixel ones out in a constructor call is how one gets left behind."""
        from ..mpvtk.scaling import px
        return dataclasses.replace(self, tile_w=px(self.tile_w), tile_h=px(self.tile_h), gap=px(self.gap), caption_h=px(self.caption_h), title_size=px(self.title_size), sub_size=px(self.sub_size), badge_size=px(self.badge_size))

    def scaled(self, f):
        """A copy whose ARTWORK is scaled by factor ``f`` -- the active theme's
        cover size, or the user's Cover Size setting.

        **The type is not touched, and neither is the caption band.** Scaling
        them too is a second, unlabelled text-size control; it drives captions
        below the ``ui_text_min`` floor at Extra Compact, and it desynchronizes
        ``badge_size`` from the fixed logical offsets in
        ``_paint_decorations`` (a 24px numeral in a 22px disc). Text has two
        controls that do it properly -- ``ui_scale`` via :meth:`physical` and
        ``ui_text_scale``/``ui_text_min`` via :meth:`with_text_scale`, applied
        after this. See docs/artwork-pipeline.md section 4.

        The gap is kept too, so rows keep their rhythm as the art grows.
        """
        if not f or abs(f - 1.0) < 1e-06:
            return self

        def r(v):
            return max(1, int(round(v * f)))
        return dataclasses.replace(self, tile_w=r(self.tile_w), tile_h=r(self.tile_h))
POSTER_GEOM = TileGeom(tile_w=150, tile_h=225, caption_h=50)
LANDSCAPE_GEOM = TileGeom(tile_w=240, tile_h=135, caption_h=50)
SQUARE_GEOM = TileGeom(tile_w=170, tile_h=170, caption_h=50)
BANNER_GEOM = TileGeom(tile_w=486, tile_h=90, caption_h=50)
WIDE_GEOM = LANDSCAPE_GEOM

@dataclass
class Tile:
    """One tile's data. ``key`` is the stable identity (usually the
    Jellyfin item id) — it becomes the hit-region id and part of the
    cache key. ``poster`` is a decoded PIL image (any size; cover-cropped to
    fill the tile, unless ``contain`` or ``logo_plate`` says this artwork is a
    mark rather than a photograph) or None for a placeholder. ``poster_tag``
    identifies the poster's content for cache keying ("" when absent, so
    the strip recomposites when the real poster lands). ``glyph`` is the
    placeholder character drawn when there's no poster (initial / ♪)."""
    key: str
    title: str = ''
    subtitle: str = ''
    subtitle2: str = ''
    poster: Optional[object] = None
    poster_tag: str = ''
    watched: bool = False
    badge: int = 0
    tag: str = ''
    progress: float = 0.0
    downloaded: bool = False
    kind: str = ''
    glyph: str = ''
    recording: bool = False
    record: str = ''
    contain: bool = False
    live: bool = False
    sources: int = 0

class StripStore:
    MAX_ENTRIES = 80
    MAX_BYTES = 128 * 1024 * 1024
    TIGHT_MAX_BYTES = 32 * 1024 * 1024
    PROTECT_GENERATIONS = 2

    def __init__(self, cache_dir=None, mem_store=None, geom=None, notify=None, workers=2):
        self.dir = cache_dir
        self.mem = mem_store
        self.geom = geom or TileGeom()
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._counter = 0
        self._bytes = 0
        self._gen = 0
        self._marked = False
        self._trim_at = None
        self.hits = 0
        self.misses = 0
        self._notify = notify
        self._closed = False
        self._inflight = set()
        self.tag = ''
        self._tag_theme = ''
        self._retag = 0
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='strip')

    def set_theme_tag(self, tag):
        """Invalidate every cached bitmap by changing what the keys look
        like. Safe to call while mpv is compositing; see ``tag``."""
        self._tag_theme = str(tag)
        self._apply_tag()

    def retag(self):
        """Invalidate every cached bitmap without changing theme.

        For a setting that is baked into a composited strip but is not the
        theme -- ``logo_legibility``, which decides what goes behind a
        transparent logo. Same mechanism and the same safety argument as
        :meth:`set_theme_tag`; a counter rather than a value because the
        caller has nothing to name the new state with, only the fact that it
        changed.
        """
        self._retag += 1
        self._apply_tag()

    def _apply_tag(self):
        self.tag = '%s~%d' % (self._tag_theme, self._retag) if self._retag else self._tag_theme

    def set_notify(self, notify):
        """Attach the wake-up callback after construction (mirrors
        ThumbnailStore.set_notify). A plain assignment: workers only read it."""
        self._notify = notify

    def _tile_key(self, t):
        return (t.key, t.poster_tag if t.poster is not None else '', t.title, t.subtitle, t.subtitle2, bool(t.watched), int(t.badge), t.tag, round(float(t.progress), 2), bool(t.downloaded), t.kind, bool(t.recording), t.record, t.glyph if t.poster is None else '', bool(t.contain), bool(t.live), int(t.sources))

    @staticmethod
    def _geom_key(g):
        return (g.tile_w, g.tile_h, g.gap, g.caption_h, g.title_size, g.sub_size, g.badge_size, g.caption_lines)

    def strip(self, tiles, geom=None, async_=False):
        """Composite ``tiles`` into one strip (in ``geom``'s shape, default
        the store's). Returns ``{"src", "iw", "ih", "v", "regions"}`` where each
        region is ``{"x", "y", "w", "h", "key"}`` in image-local coords (the
        view wraps these with on_click/on_context/id).

        ``async_=True`` composites on a worker pool instead of inline — a full
        4K row is 20-140ms and stalled the build. On a cache miss it returns a
        cheap placeholder (blank cards, real hit-regions) immediately and
        schedules the real bitmap; when the worker lands it, notify() wakes the
        loop and the next build gets the finished strip (marked
        ``"placeholder": True`` until then). Used for the virtualized grid,
        whose rows share one uniform (bounded) blank shape. Carousels keep the
        inline path (``async_=False``): each is a distinct, up-to-50-tile row,
        so a per-row blank would cost as much as the composite it defers."""
        g = geom or self.geom
        tiles = list(tiles)
        key = (self.tag, self._geom_key(g), tuple((self._tile_key(t) for t in tiles)))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._touch(key)
                self.hits += 1
                return hit
            self.misses += 1
            do_async = async_ and (not self._closed)
            if do_async:
                submit = key not in self._inflight
                if submit:
                    self._inflight.add(key)
        if do_async:
            if submit:
                self._pool.submit(self._compose_task, key, tiles, g)
            return self._placeholder(tiles, g)
        entry = self._compose(tiles, g)
        with self._lock:
            return self._insert(key, entry)

    def _compose_task(self, key, tiles, g):
        """Pool worker: composite off the loop thread, then insert + notify."""
        entry = None
        try:
            entry = self._compose(tiles, g)
        except Exception:
            log.warning('strip composite failed', exc_info=True)
        with self._lock:
            self._inflight.discard(key)
            if entry is None:
                return
            if self._closed:
                self._free(entry['src'])
                return
            self._insert(key, entry)
        if self._notify is not None:
            try:
                self._notify()
            except Exception:
                log.debug('strip notify failed', exc_info=True)

    def _placeholder(self, tiles, g):
        """A stand-in entry for a not-yet-composited strip: a blank-card
        bitmap shared across every row of this shape, wrapped with the real
        per-tile hit-regions so clicks work before the artwork lands."""
        n = len(tiles)
        blank = self._blank_strip(g, n)
        regions = [{'x': col * (g.tile_w + g.gap), 'y': 0, 'w': g.tile_w, 'h': g.strip_h, 'key': t.key} for col, t in enumerate(tiles)]
        lw = n * g.tile_w + (n - 1) * g.gap if n else 1
        return {'src': blank['src'], 'iw': blank['iw'], 'ih': blank['ih'], 'lw': lw, 'lh': g.strip_h, 'v': blank['v'], 'regions': regions, 'placeholder': True}

    def _blank_strip(self, g, n):
        """Blank-card bitmap for a shape (geom, tile count), cached in the same
        LRU so it's freed like any strip. Shared by every pending row of that
        shape, so a screenful of placeholders is one composite, not one each."""
        bkey = ('blank', self.tag, self._geom_key(g), n)
        with self._lock:
            hit = self._cache.get(bkey)
            if hit is not None:
                self._touch(bkey)
                return hit
        entry = self._compose_blank(n, g)
        with self._lock:
            return self._insert(bkey, entry)

    def shutdown(self):
        """Stop the compositor pool. Call before clear() on teardown so no
        worker inserts into a cache that clear() is about to free."""
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True)

    def bitmap(self, key, image, lsize=None):
        """Cache a single arbitrary image as BGRA (backdrops, logos, art) in
        the same store/LRU as strips. ``key`` must identify the content;
        ``image`` is a PIL image already at **physical** display size.
        ``lsize`` is the logical (w, h) box it was rasterized for — pass it
        whenever a logical box drove the size, so the Image widget can check
        the two agree. Returns ``{"src", "iw", "ih", "lw", "lh", "v"}``.

        ``image`` may instead be a zero-argument callable, called only on a
        miss. For a bitmap that is asked for on most frames and drawn on
        almost none of them, rasterizing to hand the answer to a cache that
        already has it is the whole cost — the hovered tile's play chip is
        re-derived every time its strip is rebuilt, and supersamples a disc
        at 3x to do it."""
        ck = ('bitmap', self.tag, key)
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None:
                self._touch(ck)
                self.hits += 1
                return hit
            self.misses += 1
        src, w, h, v = self._store(image() if callable(image) else image)
        from ..mpvtk.scaling import dip
        lw, lh = lsize if lsize is not None else (dip(w), dip(h))
        entry = {'src': src, 'iw': w, 'ih': h, 'lw': lw, 'lh': lh, 'v': v}
        with self._lock:
            return self._insert(ck, entry)

    def clear(self):
        """Drop every cached bitmap and release its backing buffer.

        On the libmpv path those buffers are read BY ADDRESS by mpv, so this
        must only be called once mpv is genuinely dead — see
        mpvtk_browser.ui.on_mpv_terminated. Calling it while mpv is still
        compositing is a segfault, not a leak."""
        with self._lock:
            entries = list(self._cache.values())
            self._cache.clear()
            self._bytes = 0
        for entry in entries:
            self._free(entry['src'])

    def _compose(self, tiles, g):
        from PIL import Image as PILImage, ImageDraw
        from ..mpvtk.scaling import px, raster
        n = len(tiles)
        lw = n * g.tile_w + (n - 1) * g.gap if n else 1
        lh = g.strip_h
        pg = g.physical()
        img = PILImage.new('RGBA', raster(lw, lh), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        regions = []
        for col, t in enumerate(tiles):
            lx = col * (g.tile_w + g.gap)
            self._paint_poster(img, dr, px(lx), t, pg)
            self._paint_decorations(img, dr, px(lx), t, pg)
            self._paint_caption(dr, px(lx), t, pg)
            regions.append({'x': lx, 'y': 0, 'w': g.tile_w, 'h': g.strip_h, 'key': t.key})
        src, iw2, ih2, v = self._store(img)
        return {'src': src, 'iw': iw2, 'ih': ih2, 'lw': lw, 'lh': lh, 'v': v, 'regions': regions}

    def _compose_blank(self, n, g):
        """A row of empty cards — the placeholder bitmap shown while the real
        strip composites. Just the card fill + border per tile: no posters,
        decorations or captions, so it's the cheap part of _compose (the cost
        that remains, _store of a full-width buffer, is amortized because one
        blank serves every pending row of this shape)."""
        from PIL import Image as PILImage, ImageDraw
        from ..mpvtk.scaling import px, raster
        lw = n * g.tile_w + (n - 1) * g.gap if n else 1
        lh = g.strip_h
        pg = g.physical()
        img = PILImage.new('RGBA', raster(lw, lh), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        rounded = bool((theme.active() or {}).get('rounded'))
        r = _px(14)
        for col in range(n):
            x = px(col * (g.tile_w + g.gap))
            box = [x, 0, x + pg.tile_w - 1, pg.tile_h - 1]
            if rounded:
                from . import cinema_paint
                card = PILImage.new('RGBA', (pg.tile_w, pg.tile_h), theme.rgb(theme.PLACEHOLDER_BG, 255))
                cinema_paint.rounded(card, (0, 0, pg.tile_w - 1, pg.tile_h - 1), r, outline=theme.rgb('101012', 255))
                card.putalpha(_card_mask(pg, r))
                img.alpha_composite(card, (int(x), 0))
            else:
                dr.rectangle(box, fill=theme.rgb(theme.PLACEHOLDER_BG, 255))
                dr.rectangle(box, outline=theme.rgb('101012', 255))
        src, iw2, ih2, v = self._store(img)
        return {'src': src, 'iw': iw2, 'ih': ih2, 'lw': lw, 'lh': lh, 'v': v}

    def _paint_poster(self, img, dr, x, t, g):
        if not bool((theme.active() or {}).get('rounded')):
            return self._paint_poster_inner(img, dr, x, t, g)
        from PIL import Image as PILImage, ImageDraw
        from . import cinema_paint
        tile = PILImage.new('RGBA', (g.tile_w, g.tile_h), (0, 0, 0, 0))
        self._paint_poster_inner(tile, ImageDraw.Draw(tile), 0, t, g, rounded=False, border=False)
        radius = _px(14)
        tile.putalpha(_card_mask(g, radius))
        img.alpha_composite(tile, (int(x), 0))

    def _paint_poster_inner(self, img, dr, x, t, g, rounded=None, border=True):
        from PIL import Image as PILImage, ImageOps
        if rounded is None:
            rounded = bool((theme.active() or {}).get('rounded'))
        r = _px(14)
        box = [x, 0, x + g.tile_w - 1, g.tile_h - 1]
        card = theme.rgb(theme.PLACEHOLDER_BG if t.poster is None else theme.CARD_BG, 255)
        plate = None
        if t.poster is not None:
            plate = logo_plate(t.poster, t.live)
            if plate is not None:
                card = tuple(plate.color) + (255,)
        if rounded:
            dr.rounded_rectangle(box, radius=r, fill=card)
        else:
            dr.rectangle(box, fill=card)
        if t.poster is not None:
            poster = t.poster
            if plate is not None and plate.shadow:
                from ..imageutil import with_shadow
                poster = with_shadow(poster)
            lanczos = PILImage.LANCZOS
            if plate is None and (not t.contain):
                if poster.size != (g.tile_w, g.tile_h):
                    poster = ImageOps.fit(poster, (g.tile_w, g.tile_h), lanczos, centering=(0.5, 0.5))
                mask = None
                if rounded:
                    mask = _card_mask(g, r)
                if poster.mode == 'RGBA':
                    from PIL import ImageChops
                    alpha = poster.getchannel('A')
                    mask = alpha if mask is None else ImageChops.multiply(mask, alpha)
                img.paste(poster, (x, 0), mask)
            else:
                if poster.size != (g.tile_w, g.tile_h):
                    poster = poster.copy()
                    poster.thumbnail((g.tile_w, g.tile_h), lanczos)
                px = (g.tile_w - poster.width) // 2
                py = (g.tile_h - poster.height) // 2
                if rounded:
                    from PIL import ImageChops
                    layer = PILImage.new('RGBA', (g.tile_w, g.tile_h), (0, 0, 0, 0))
                    layer.paste(poster.convert('RGBA'), (px, py))
                    layer.putalpha(ImageChops.multiply(layer.getchannel('A'), _card_mask(g, r)))
                    img.paste(layer, (x, 0), layer)
                else:
                    img.paste(poster, (x + px, py), poster if poster.mode == 'RGBA' else None)
        elif t.glyph:
            from ..mpvtk import vector
            gsize = max(_px(24), g.tile_h // 4)
            if t.glyph in vector.ICON_PATHS:
                glyph = vector.icon_image(t.glyph, gsize, theme.rgb(theme.SUBTLE_FG))
                img.paste(glyph, (int(x + (g.tile_w - gsize) // 2), int((g.tile_h - gsize) // 2)), glyph)
            else:
                from ..mpvtk import pilfont
                pilfont.draw_text(dr, (x + g.tile_w / 2, g.tile_h / 2), t.glyph, _font(gsize, bold=True, text=t.glyph), fill=theme.rgb(theme.SUBTLE_FG), anchor='mm')
        if not border:
            return
        if rounded:
            dr.rounded_rectangle(box, radius=r, outline=theme.rgb('101012', 255))
        else:
            dr.rectangle(box, outline=theme.rgb('101012', 255))

    def _paint_decorations(self, img, dr, x, t, g):
        inset = _px(17)
        if t.tag:
            self._paint_text_chip(img, dr, x + inset, inset, t.tag, g.badge_size, max_w=g.tile_w - _px(8))
        elif t.sources > 1:
            self._paint_count_badge(img, dr, x + inset, inset, str(t.sources), g.badge_size)
        cy = inset
        cx = x + g.tile_w - inset
        if t.badge:
            cx -= self._paint_count_chip(img, dr, cx, cy, str(t.badge), g.badge_size)
        elif t.watched:
            self._paint_glyph_badge(img, dr, cx, cy, 'check', theme.ACCENT)
            cx -= _px(self.BADGE_PITCH)
        if t.kind:
            self._paint_kind(img, dr, cx, cy, t.kind)
            cx -= _px(self.BADGE_PITCH)
        if t.record:
            self._paint_record(img, cx, cy, t.record)
            cx -= _px(self.BADGE_PITCH)
        if t.downloaded:
            self._paint_glyph_badge(img, dr, cx, cy, 'file_download', theme.ACCENT)
            cx -= _px(self.BADGE_PITCH)
        if t.progress and t.progress > 0:
            frac = max(0.0, min(1.0, t.progress))
            bar = _px(4)
            fill = theme.FAV_RED if t.recording else theme.ACCENT
            if bool((theme.active() or {}).get('rounded')):
                from PIL import Image as PILImage, ImageChops, ImageDraw
                r = _px(14)
                layer = PILImage.new('RGBA', (g.tile_w, g.tile_h), (0, 0, 0, 0))
                ld = ImageDraw.Draw(layer)
                ld.rectangle([0, g.tile_h - bar, g.tile_w - 1, g.tile_h - 1], fill=theme.rgb(theme.PROGRESS_TRACK, StripStore.PROGRESS_TRACK_ALPHA))
                ld.rectangle([0, g.tile_h - bar, int((g.tile_w - 1) * frac), g.tile_h - 1], fill=theme.rgb(fill, 255))
                layer.putalpha(ImageChops.multiply(layer.getchannel('A'), _card_mask(g, r)))
                img.paste(layer, (x, 0), layer)
            else:
                dr.rectangle([x, g.tile_h - bar, x + g.tile_w - 1, g.tile_h - 1], fill=theme.rgb(theme.PROGRESS_TRACK, StripStore.PROGRESS_TRACK_ALPHA))
                dr.rectangle([x, g.tile_h - bar, x + int((g.tile_w - 1) * frac), g.tile_h - 1], fill=theme.rgb(fill, 255))
    RECORD_BOX = 27
    BADGE_PITCH = 26
    PROGRESS_TRACK_ALPHA = 204
    LINE_GLYPH = 20
    SOLID_GLYPH = 16
    SHADOW_GLYPH = 1.15
    SHADOW_BLUR = 9.0
    SHADOW_DROP = 11.0

    @staticmethod
    def shadow_pad(size):
        """Room to leave round a mark of ``size`` px for its shadow.

        Derived from the blur, not a constant. It was a flat 5px, and a
        Gaussian needs about three sigma before it has faded: at the badge
        sizes in play sigma is 2 or more, so the halo was still visibly dark
        where the layer ended and every numeric badge carried a squared-off
        corner of shadow. Under-padding a blur does not soften the edge, it
        *makes* one.
        """
        blur = max(1.0, size / StripStore.SHADOW_BLUR)
        return int(-(-blur * 3 // 1)) + max(1, round(size / StripStore.SHADOW_DROP))

    @staticmethod
    def shadowed_badges():
        """Whether the active theme draws badges as a mark plus a drop shadow
        instead of a mark on a filled pill.

        Read per paint rather than captured: a theme change retags the strip
        store, so every strip recomposites through here with the new answer
        (see ``MpvtkBrowser.set_theme``).
        """
        return bool((theme.active() or {}).get('badge_shadow'))
    SHADOW_GAIN = 2.4

    @staticmethod
    def _shadowed(img, layer, span, cx, cy):
        """Composite ``layer`` -- a mark on transparency, padded by
        :meth:`shadow_pad` -- centred on (cx, cy), over a dark halo of its own
        silhouette.

        ``span`` is the size the blur is derived from: the mark's own, NOT the
        padded layer's, which is computed from the blur and would grow every
        time the other grew to hold it. For text it is the CAP HEIGHT -- a
        shadow is proportional to the weight of the ink, and taking the width
        made a three-digit count three times blurrier than a one-digit one and
        clipped it.

        **Not ``imageutil.with_shadow``**, which is tuned for a logo wanting a
        hint of separation from its plate. Here the halo is the entire reason a
        20px glyph over arbitrary artwork is visible.
        See docs/artwork-pipeline.md section 5.
        """
        from PIL import Image as PILImage, ImageFilter
        drop = max(1, round(span / StripStore.SHADOW_DROP))
        silhouette = PILImage.new('L', layer.size, 0)
        silhouette.paste(layer.getchannel('A'), (0, drop))
        silhouette = silhouette.filter(ImageFilter.GaussianBlur(max(1.0, span / StripStore.SHADOW_BLUR)))
        gain = StripStore.SHADOW_GAIN
        silhouette = silhouette.point(lambda v: min(255, int(v * gain)))
        shadow = PILImage.new('RGBA', layer.size, (0, 0, 0, 0))
        shadow.putalpha(silhouette)
        layer = PILImage.alpha_composite(shadow, layer)
        img.paste(layer, (int(cx - layer.width // 2), int(cy - layer.height // 2)), layer)

    @staticmethod
    def _mark_layer(size):
        """A transparent square with room round it for :meth:`_shadowed`."""
        from PIL import Image as PILImage
        pad = StripStore.shadow_pad(size)
        return (PILImage.new('RGBA', (size + 2 * pad, size + 2 * pad), (0, 0, 0, 0)), pad)

    @staticmethod
    def _paint_glyph_badge(img, dr, cx, cy, name, fill, size=None):
        """A Material glyph in white on a filled disc, centred on (cx, cy).

        The shape the watched check and the downloaded arrow share; the type
        marker (``_paint_kind``) is the same disc in the window colour,
        because it labels the item rather than reporting a state and should
        not read as another accent badge.

        With ``badge_shadow`` the disc goes and a drop shadow takes over the
        job of separating the mark from the artwork. **The mark stays white**
        rather than adopting ``fill``: the pill is what makes an accent
        legible on a photograph, so an accent-coloured mark with no pill is
        legible only for themes whose accent happens to be bright, and
        nothing guarantees one is -- Super Dark, the first theme to ask for
        this, has a light grey accent, but a theme is free to set a dark
        one and the option must not depend on which. White reads on
        everything, which is the property being bought here.
        """
        from ..mpvtk import vector
        size = _px(size or StripStore.LINE_GLYPH)
        if StripStore.shadowed_badges():
            size = int(size * StripStore.SHADOW_GLYPH)
            layer, pad = StripStore._mark_layer(size)
            glyph = vector.icon_image(name, size, (255, 255, 255))
            layer.paste(glyph, (pad, pad), glyph)
            StripStore._shadowed(img, layer, size, cx, cy)
            return
        r = _px(11)
        _aa_fill(img, [cx - r, cy - r, cx + r, cy + r], theme.rgb(fill, 255))
        glyph = vector.icon_image(name, size, (255, 255, 255))
        img.paste(glyph, (int(cx - size // 2), int(cy - size // 2)), glyph)

    @staticmethod
    def _paint_count_chip(img, dr, cx, cy, text, size):
        """The unplayed-episode chip, centred on ``cx`` and pinned by its
        RIGHT edge. Returns the horizontal pitch the next badge must clear.

        **Sized to the number it carries**, not to a guess about how big
        numbers get: at a fixed 26 logical px a three-digit count hung out of
        both ends of its own chip. jellyfin-web's `.countIndicator` grows the
        same way (`padding: 0 .5em` over a min-width), and is likewise a
        rounded rectangle where every other badge is a disc.

        Pinned by its right edge because that edge lines up with the badge
        stack beside it. The returned pitch is why this is a function: it sits
        IN the corner, so the badge to its left must clear a width that
        depends on the text -- BADGE_PITCH is a floor here, not the answer.
        See docs/artwork-pipeline.md section 5.
        """
        font = _font(size, bold=True)
        right = cx + _px(13)
        if StripStore.shadowed_badges():
            width = StripStore._shadowed_text(img, text, font, 0, cy, right=right)
            return max(_px(StripStore.BADGE_PITCH), width + _px(4))
        bw = max(_px(26), int(dr.textlength(text, font=font)) + _px(14))
        _aa_fill(img, [right - bw, _px(5), right, _px(25)], theme.rgb(theme.ACCENT, 255), radius=_px(6))
        dr.text((right - bw / 2, _px(15)), text, font=font, anchor='mm', fill=(255, 255, 255))
        return max(_px(StripStore.BADGE_PITCH), bw + _px(4))

    @staticmethod
    def _paint_text_chip(img, dr, cx, cy, text, size, max_w=None):
        """A word on a filled chip, pinned by its LEFT edge at ``cx``.

        The count chip's shape (`_paint_count_chip`) with two differences, and
        both are about what the word means. The fill is the placeholder grey
        rather than the accent, because the accent is what marks things that
        are yours and this says the opposite; and it is pinned left, because
        nothing shares this corner with it, so there is no stack to line up
        against and no pitch to return.

        Sized to its text for the same reason the count chip is: "Unaired" is
        wider than "Missing", and a fixed width would clip one of them.

        ``max_w`` is the widest chip this tile can hold. It is not optional in
        spirit: several tiles share one strip bitmap, so an over-wide chip
        draws over the tile to its RIGHT instead of being clipped -- the same
        reason the captions ellipsize to ``g.tile_w``. English fits at every
        size; a long translation of "Unaired" need not.
        """
        font = _font(size, bold=True)
        if StripStore.shadowed_badges():
            StripStore._shadowed_text(img, text, font, cx, cy, left=cx - _px(6), max_w=max_w)
            return
        if max_w is not None:
            text = StripStore._ellipsize(dr, text, font, max_w - _px(14))
        bw = int(dr.textlength(text, font=font)) + _px(14)
        if max_w is not None:
            bw = min(bw, max_w)
        _aa_fill(img, [cx - _px(13), _px(5), cx - _px(13) + bw, _px(25)], theme.rgb(theme.PLACEHOLDER_BG, 255), radius=_px(6))
        dr.text((cx - _px(13) + bw / 2, _px(15)), text, font=font, anchor='mm', fill=(255, 255, 255))

    @staticmethod
    def _paint_count_badge(img, dr, cx, cy, text, size):
        """A number in white on a filled accent disc, centred on (cx, cy).

        The same disc ``_paint_glyph_badge`` draws, carrying a digit instead
        of a glyph -- which is how jellyfin-web draws its version count too
        (`.mediaSourceIndicator` is a 2em circle in the primary colour). The
        unplayed-episode badge on the other corner is deliberately a rounded
        *rectangle*: it runs to three digits, where this one is a count of
        files on disk and stays one.
        """
        font = _font(size, bold=True)
        if StripStore.shadowed_badges():
            StripStore._shadowed_text(img, str(text), font, cx, cy)
            return
        r = _px(11)
        _aa_fill(img, [cx - r, cy - r, cx + r, cy + r], theme.rgb(theme.ACCENT, 255))
        dr.text((cx, cy), text, font=font, anchor='mm', fill=(255, 255, 255))

    @staticmethod
    def _shadowed_text(img, text, font, cx, cy, right=None, left=None, max_w=None):
        """White text with a drop shadow, no chip behind it.

        Centred on (cx, cy), or pinned by one edge:

        * ``right`` pins the **layer's** right edge -- glyphs plus the shadow
          pad. That is what the episode count needs, for the reason
          ``_paint_decorations`` gives: that edge is the one lined up with the
          badge stack, and a wide chip growing from its middle walks off the
          corner of the card.
        * ``left`` pins the **glyphs'** left edge, and the asymmetry with
          ``right`` is deliberate rather than an oversight. The one caller
          lines this word up with where the same word sits on a *filled*
          theme, and the pad is invisible halo -- including it would shift the
          text right by a few px when the theme changed. ``right``'s callers
          are lining up against a stack of shadowed marks, where the layer
          edge is the one that matches.

        ``max_w`` ellipsizes to fit, and the budget is ``max_w`` **minus the
        pad on both sides**, because the pad is drawn width. Neither
        ``max_w`` nor ``max_w`` less the filled chip's padding is right: they
        happen to coincide near 14px and diverge at other sizes, so a wrong
        choice here passes at the one size a test is written at. It lives
        here rather than in the caller because the pad comes off the cap
        height of the rendered glyphs, which the caller cannot compute.
        """
        from PIL import Image as PILImage, ImageDraw
        probe = ImageDraw.Draw(PILImage.new('RGBA', (1, 1)))
        if max_w is not None:
            box = probe.textbbox((0, 0), text, font=font, anchor='lt')
            text = StripStore._ellipsize(probe, text, font, max_w - 2 * StripStore.shadow_pad(max(1, box[3] - box[1])))
        box = probe.textbbox((0, 0), text, font=font, anchor='lt')
        tw, th = (max(1, box[2] - box[0]), max(1, box[3] - box[1]))
        pad = StripStore.shadow_pad(th)
        layer = PILImage.new('RGBA', (tw + 2 * pad, th + 2 * pad), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((pad - box[0], pad - box[1]), text, font=font, anchor='lt', fill=(255, 255, 255, 255))
        if right is not None:
            cx = right - layer.width // 2
        elif left is not None:
            cx = left + layer.width // 2 - pad
        StripStore._shadowed(img, layer, th, cx, cy)
        return layer.width

    @staticmethod
    def _paint_kind(img, dr, cx, cy, name):
        """The type marker, centred on (cx, cy).

        Same disc as the state badges beside it but in the window colour,
        not the accent: this one says what the item *is*, permanently, and
        an accent chip on every tile in a Home Videos library reads as
        something having happened to all of them.

        With ``badge_shadow`` it keeps TEXT_FG rather than going white with
        the state badges: the distinction this marker exists to make is that
        it is *not* one of them, and the pill going away is exactly when that
        distinction has the least left to stand on.
        """
        from ..mpvtk import vector
        size = _px(StripStore.SOLID_GLYPH)
        if StripStore.shadowed_badges():
            size = int(size * StripStore.SHADOW_GLYPH)
            layer, pad = StripStore._mark_layer(size)
            glyph = vector.icon_image(name, size, theme.rgb(theme.TEXT_FG))
            layer.paste(glyph, (pad, pad), glyph)
            StripStore._shadowed(img, layer, size, cx, cy)
            return
        r = _px(11)
        _aa_fill(img, [cx - r, cy - r, cx + r, cy + r], theme.rgb(theme.WINDOW_BG, 210))
        glyph = vector.icon_image(name, size, theme.rgb(theme.TEXT_FG))
        img.paste(glyph, (int(cx - size // 2), int(cy - size // 2)), glyph)

    @staticmethod
    def _paint_record(img, cx, cy, state):
        """The recording symbol, centred on (cx, cy).

        The **real Material glyphs**, rasterized from the same
        ``ui_icon_paths`` data the renderer draws icons from:
        ``fiber_manual_record`` for a single recording and
        ``fiber_smart_record`` for one covered by a series rule. Blitting
        rather than drawing by hand matters because the guide cell right
        next to this tile draws those same two icons as ASS — a hand-rolled
        approximation is a second drawing of the same symbol, and the two
        drift.

        Baked into the bitmap rather than drawn as a widget because mpv
        composites overlay bitmaps ABOVE all script ASS (GUIDE §6): an ASS
        glyph over a tile would vanish under the artwork.

        No outline. The red dot is the record symbol everywhere else in the
        app, and ringing this one in white made the same thing look like a
        different badge.
        """
        from ..mpvtk import vector
        name = 'fiber_smart_record' if state.startswith('series') else 'fiber_manual_record'
        colour = theme.rgb(theme.SUBTLE_FG if state == 'series_inactive' else theme.FAV_RED)
        size = _px(StripStore.RECORD_BOX)
        if StripStore.shadowed_badges():
            layer, pad = StripStore._mark_layer(size)
            glyph = vector.icon_image(name, size, colour)
            layer.paste(glyph, (pad, pad), glyph)
            StripStore._shadowed(img, layer, size, cx, cy)
            return
        glyph = vector.icon_image(name, size, colour)
        img.paste(glyph, (int(cx - size // 2), int(cy - size // 2)), glyph)

    def _paint_caption(self, dr, x, t, g):
        y = g.tile_h + _px(6)
        from ..mpvtk import pilfont
        if t.title:
            fnt = _font(g.title_size, text=t.title)
            title = self._ellipsize(dr, t.title, fnt, g.tile_w)
            pilfont.draw_text(dr, (x, y), title, fnt, fill=theme.rgb(theme.TEXT_FG))
            y += g.title_size + _px(TileGeom.TITLE_GAP)
        if t.subtitle:
            fnt = _font(g.sub_size, text=t.subtitle)
            sub = self._ellipsize(dr, t.subtitle, fnt, g.tile_w)
            pilfont.draw_text(dr, (x, y), sub, fnt, fill=theme.rgb(theme.SUBTLE_FG))
            y += g.sub_size + _px(TileGeom.SUB_GAP)
        if t.subtitle2 and g.caption_lines >= 3:
            fnt = _font(g.sub_size, text=t.subtitle2)
            sub = self._ellipsize(dr, t.subtitle2, fnt, g.tile_w)
            pilfont.draw_text(dr, (x, y), sub, fnt, fill=theme.rgb(theme.SUBTLE_FG))

    @staticmethod
    def _ellipsize(dr, text, font, max_w):
        from ..mpvtk import pilfont
        if pilfont.text_length(dr, text, font) <= max_w:
            return text
        while text and pilfont.text_length(dr, text + '…', font) > max_w:
            text = text[:-1]
        return text + '…'

    def _store(self, img):
        """Returns (src, w, h, v). ``v`` is a monotonic content version.

        It is what keeps the libmpv path honest. There ``src`` is a raw
        malloc address, and addresses ARE recycled: once a freed buffer
        falls out of MemoryStore's graveyard, the next allocation can land
        on the exact address an evicted entry used (measured: 60 strip-sized
        allocations produced only 20 distinct addresses). The renderer skips
        re-issuing an overlay whose args are unchanged, so a recycled address
        made the NEW entry indistinguishable from the old one and mpv went on
        compositing the PREVIOUS buffer's contents under the new entry's
        identity -- a stale poster that never refreshes. mpv copies at
        overlay-add rather than reading the address live, so bumping v (which
        lands in the renderer's arg string) forces the re-issue and the copy.
        """
        with self._lock:
            self._counter += 1
            counter = self._counter
        if self.mem is not None:
            src, w, h = self.mem.add(img)
            return (src, w, h, counter)
        src = os.path.join(self.dir, 'strip%d.bgra' % counter)
        w, h = write_bgra(img, src)
        return (src, w, h, counter)

    def _free(self, src):
        if self.mem is not None:
            self.mem.remove(src)
        else:
            try:
                os.remove(src)
            except OSError:
                pass

    @staticmethod
    def _entry_bytes(entry):
        """What one cached bitmap costs, wherever it is kept.

        The same number either way: a ctypes buffer on libmpv and a
        strip*.bgra on jsonipc are both iw*ih*4 bytes of premultiplied BGRA.
        """
        try:
            return int(entry['iw']) * int(entry['ih']) * 4
        except Exception:
            return 0

    def _insert(self, key, entry):
        """Put ``entry`` in the cache under ``key``, or drop it for the one
        already there. Returns the entry the caller should use. Callers hold
        ``_lock``.

        The re-check is the point. Every insert site drops the lock across
        the composite, so two callers can miss the same key and both
        allocate -- and the same key really does arrive by two routes: a
        grid composites through the pool (async_=True) while the paginated
        view of the same items composites inline, and the keys are equal.
        Overwriting would strand the loser's buffer with no cache reference
        to free it by, and -- since this cache now counts bytes -- would add
        its size to a total nothing ever subtracts, until the drift alone
        exceeds MAX_BYTES and the cache trims itself to nothing on every
        insert.
        """
        hit = self._cache.get(key)
        if hit is not None:
            if hit is not entry:
                self._free(entry['src'])
            self._touch(key)
            return hit
        self._cache[key] = entry
        self._bytes += self._entry_bytes(entry)
        self._touch(key)
        self._evict()
        return entry

    def on_scene_pushed(self):
        """A scene has been handed to the renderer. Call once per PUSH.

        This is the clock the byte bound is safe against, and "a scene was
        pushed" is deliberately not "build() ran" -- they are different
        events and using the wrong one frees bitmaps mpv is compositing:

        * ``MpvtkApp.render`` builds a SECOND time when a scene turns up
          glyphs whose widths were never measured, which is exactly what a
          new screen does. Two builds, one push.
        * A build that raises keeps the previous frame on screen (by design
          -- views index into state that arrives asynchronously). No push at
          all, and the scene still up is the one before it.

        Counting builds, both of those slide the protected window off the
        scene that is actually on screen. Counting pushes cannot: the
        generation only advances when the renderer has been given something
        new.
        """
        with self._lock:
            self._gen += 1
            self._marked = True
            if self._trim_at is not None and self._gen >= self._trim_at:
                self._trim_at = None
                self._trim_to(0)

    def keep(self, entry):
        """Say that ``entry`` is still on screen, for a caller that holds one
        across frames instead of asking for it again. Returns whether it is
        still cached: **a False means its buffer is freed**, and drawing it
        hands mpv a dead address. The caller must drop the entry and
        composite again (docs/browser-shell.md §6, "Held bitmaps").
        ``tools/audit_keep_result.py`` fails a call that ignores the answer.

        Almost everything re-requests what it draws every frame -- a strip
        row, a placeholder, an art cell -- so the ordinary cache hit is also
        the liveness signal. The cast screen does not: it composites one
        full-window bitmap on a worker, parks the entry, and renders from
        that entry forever after. Nothing would touch it again, so it would
        age out of the protected window while being the only thing on
        screen, and its src is the biggest single buffer this app makes.
        """
        if not entry:
            return False
        with self._lock:
            for key, cached in self._cache.items():
                if cached is entry:
                    self._touch(key)
                    return True
        return False

    def dead_srcs(self, nodes):
        """Image srcs in a laid-out scene that mpv must not read: an
        in-memory address this store no longer holds, or one whose buffer
        is not the ``iw*ih*4`` bytes the node declares. Only the libmpv
        ``&address`` form is checked -- a freed file on jsonipc is a blank
        picture, not a read of freed memory. For MpvtkApp.scene_check."""
        if self.mem is None:
            return []
        bufs = self.mem._bufs
        dead = []
        for node in nodes:
            src = node.get('src')
            if node.get('t') not in ('img', 'imgmap') or not src or (not src.startswith('&')):
                continue
            buf = bufs.get(src)
            if buf is None:
                dead.append('%s freed' % src)
            elif 'base' in node and (not isinstance(node['base'], int) or node['base'] < 0 or node['base'] + node['iw'] * node['ih'] * 4 > len(buf)) or ('base' not in node and len(buf) != node['iw'] * node['ih'] * 4):
                dead.append('%s is %d bytes, node says %dx%d' % (src, len(buf), node['iw'], node['ih']))
        return dead

    def set_memory_pressure(self, tight):
        """Switch between the roomy and the small-machine byte budget.

        Asked per screen change rather than once at startup, for the same
        reason the trim is: "busy" is a state, not a property. Lowering the
        cap takes effect at the next eviction, and _protected still decides
        what may actually go -- so this can never free something on screen,
        however far the budget drops.
        """
        want = self.TIGHT_MAX_BYTES if tight else type(self).MAX_BYTES
        if want == self.MAX_BYTES:
            return
        with self._lock:
            self.MAX_BYTES = want
            self._evict()

    def trim_soon(self):
        """Free everything the live scene is not using, at the first moment
        that is safe -- which is not now.

        For the small-machine path (see MpvtkBrowser._shed_caches_on_screen_
        change): on a machine short of RAM, the rows of the screen you just
        left are worth more as memory than as a fast trip back.

        Deferred to the next push, and then still filtered by _protected.
        When a screen changes the scene mpv is compositing is STILL the old
        one, so the old strips are precisely the ones that must not be freed
        at that instant; a few generations later they are nobody's and the
        ordinary gate lets them go.
        """
        with self._lock:
            self._trim_at = self._gen + self.PROTECT_GENERATIONS + 1

    def _touch(self, key):
        """Move ``key`` to the LRU tail and stamp it with the current
        generation. Callers hold ``_lock``."""
        self._cache.move_to_end(key)
        entry = self._cache.get(key)
        if entry is not None:
            entry['gen'] = self._gen

    def _protected(self, entry):
        """Whether mpv may still have this bitmap bound. Callers hold
        ``_lock``.

        Anything touched during the current build, the scene on screen, or
        the one it replaced. Two generations rather than one because a push
        is not the moment the renderer acts on it -- the scene message is
        processed on mpv's own loop, and the renderer re-issues overlays
        without a new push besides (a scroll, a slot renumber). One spare
        generation covers both lags.
        """
        return entry.get('gen', 0) >= self._gen - self.PROTECT_GENERATIONS

    def _evict(self):
        while len(self._cache) > self.MAX_ENTRIES:
            key = next(iter(self._cache))
            if self._marked and self._protected(self._cache[key]):
                break
            old = self._cache.pop(key)
            self._bytes -= self._entry_bytes(old)
            self._free(old['src'])
        if not self._marked:
            return
        self._trim_to(self.MAX_BYTES)

    def _trim_to(self, target):
        """Free from the LRU head until ``target`` bytes, stopping dead at
        the first entry the live scene is using. Callers hold ``_lock``.

        This is the refcount, such as it is: mpv holds overlay bindings to
        the bitmaps in the scene it was last pushed, and freeing one of
        those is a read of freed memory on the libmpv path rather than a
        missing picture. Approximated by generation -- see _protected and
        on_scene_pushed.
        """
        while self._bytes > target and self._cache:
            key = next(iter(self._cache))
            if self._protected(self._cache[key]):
                break
            old = self._cache.pop(key)
            self._bytes -= self._entry_bytes(old)
            self._free(old['src'])

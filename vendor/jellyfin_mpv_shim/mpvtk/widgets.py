"""Declarative element tree for mpvtk. The catalog is `mpvtk/GUIDE.md`
section 2; this file is the parameter contracts.

Widgets are plain descriptions; nothing here talks to mpv. A tree is
rebuilt from scratch on every render, so there is no retained widget state
on the Python side -- give stateful widgets (scrolls, textboxes,
dropdowns) explicit ids so the renderer-local state keyed by id survives
structural changes to the tree.

Every ``x = theme.Y if x is None else ...`` line here is the rule that
sizes and colours are resolved in the constructor BODY and never as a
default argument, since a default is evaluated once at import while the
palette and type scale are set at startup and on every theme swap. See
GUIDE.md section 7.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from personal_cinema.runtime_paths import option
from functools import lru_cache
from . import theme

@lru_cache(maxsize=1)
def reduced_motion():
    """A process preference; avoid configuration disk I/O on every frame."""
    return bool(option('appearance.reduced_motion', False))

class Element:
    """``anchor``/``dx``/``dy``/``occlude`` only apply to direct children
    of a :class:`Stack` (see its docstring); they are inert elsewhere.

    ``min_w``/``max_w``/``min_h``/``max_h`` bound the laid-out size: an
    int is pixels, a float in (0, 1] is a fraction of the available space
    (dialog children resolve fractions against the window)."""

    def __init__(self, id=None, w=None, h=None, flex=0, anchor=None, dx=0, dy=0, occlude=False, tip=None, min_w=None, max_w=None, min_h=None, max_h=None, autofocus=False, disabled=False, nav_gravity=False):
        self.id = id
        self.w = w
        self.h = h
        self.flex = flex
        self.anchor = anchor
        self.dx = dx
        self.dy = dy
        self.occlude = occlude
        self.tip = tip
        self.min_w = min_w
        self.max_w = max_w
        self.min_h = min_h
        self.max_h = max_h
        self.autofocus = autofocus
        self.disabled = disabled
        self.nav_gravity = nav_gravity

class Box(Element):
    """Rectangular container; stacks children along ``direction``."""

    def __init__(self, children=None, direction='column', pad=0, gap=0, align='start', justify='start', bg=None, alpha=255, radius=0, border=None, border_w=1, on_click=None, on_dbl=None, on_context=None, hover=None, repeat=False, window_drag=False, window_resize=None, **kw):
        super().__init__(**kw)
        self.children = [c for c in children or [] if c is not None]
        self.direction = direction
        self.pad = pad
        self.gap = gap
        self.align = align
        self.justify = justify
        self.bg = bg
        self.alpha = alpha
        self.radius = radius
        self.border = border
        self.border_w = border_w
        self.on_click = on_click
        self.on_dbl = on_dbl
        self.on_context = on_context
        self.hover = hover
        self.repeat = repeat
        self.window_drag = window_drag
        self.window_resize = window_resize

class Row(Box):

    def __init__(self, children=None, **kw):
        kw.setdefault('direction', 'row')
        super().__init__(children, **kw)

class Column(Box):

    def __init__(self, children=None, **kw):
        kw.setdefault('direction', 'column')
        super().__init__(children, **kw)

class Spacer(Element):
    """Flexible filler by default; a fixed-size stand-in (e.g. for
    virtualized content) when given an explicit w/h."""

    def __init__(self, flex=None, **kw):
        if flex is None:
            flex = 0 if 'w' in kw or 'h' in kw else 1
        super().__init__(flex=flex, **kw)

class Text(Element):
    """``wrap=True`` word-wraps to the laid-out width instead of
    ellipsizing, emitting one scene line per wrapped line (``\\n`` starts
    a new paragraph). ``max_lines`` caps the line count; the last kept
    line is ellipsized. A wrapped Text takes its width from the parent
    (give it ``w=`` or put it in a width-constrained column)."""

    def __init__(self, text, size=None, color=None, bold=False, align='left', on_click=None, hover=None, wrap=False, max_lines=None, **kw):
        size = theme.size('TITLE') if size is None else theme.text_size(size)
        super().__init__(**kw)
        self.text = text
        self.size = size
        self.color = color or theme.ON_SURFACE
        self.bold = bold
        self.align = align
        self.on_click = on_click
        self.hover = hover
        self.wrap = wrap
        self.max_lines = max_lines

def _check_raster(src, iw, ih, kw):
    """Fill in the logical footprint, or verify a declared one matches the
    bitmap -- the enforcement behind GUIDE.md section 8's rule that only
    rasterization thinks in physical pixels. A bitmap that is not
    ``scaling.raster()`` of its declared box means the producer skipped the
    conversion, and the renderer crops rather than resamples, so it would
    silently draw a fragment.

    **Only declare w/h for a canvas you sized yourself.** Decoded artwork
    must not: the server preserves aspect, so a square request comes back
    56x52 and what has to be scaled is the *request*, which this cannot see.
    """
    from . import scaling
    have_w, have_h = (kw.get('w') is not None, kw.get('h') is not None)
    if have_w != have_h:
        raise ValueError('image %r declared only %s; declare both or neither' % (src, 'w' if have_w else 'h'))
    if not have_w:
        kw['w'] = scaling.dip(iw)
        kw['h'] = scaling.dip(ih)
        return
    want = scaling.raster(kw['w'], kw['h'])
    if want != (iw, ih):
        raise ValueError('image %r is %dx%d but its %gx logical box %gx%g needs %dx%d; the producer did not rasterize at the UI scale' % (src, iw, ih, scaling.scale(), kw['w'], kw['h'], want[0], want[1]))

class Image(Element):
    """A pre-rasterized BGRA image (see rawimage.write_bgra).

    ``src`` is the path to the raw file, ``iw``/``ih`` its **physical**
    pixel size, w/h the **logical** footprint — see ``_check_raster``
    above, and omit w/h only for an image nothing derived from a logical
    box.

    ``v`` is a content version: bump it when rewriting the same path in
    place so the renderer re-reads it. On the libmpv path src is a malloc
    address and addresses get recycled, so entries carry one always —
    see mpvtk_browser.strips._store.

    ``repeat`` is hold-repeat, same as Box's: the renderer keys it off
    ``click``/``rpt`` and does not care what kind of node carries them. It
    exists here because a button that has to composite over a bitmap must
    itself be a bitmap (GUIDE §6), and paging arrows are exactly that —
    without it, going bitmap would silently cost press-and-hold paging.
    """

    def __init__(self, src, iw, ih, on_click=None, hover=None, v=0, repeat=False, on_hover=None, on_hover_end=None, on_context=None, **kw):
        _check_raster(src, iw, ih, kw)
        super().__init__(**kw)
        self.src = src
        self.iw = iw
        self.ih = ih
        self.lw = kw['w']
        self.lh = kw['h']
        self.v = v
        self.on_click = on_click
        self.hover = hover
        self.repeat = repeat
        self.on_context = on_context
        self.on_hover = on_hover
        self.on_hover_end = on_hover_end

class ImageMap(Element):
    """A composited bitmap with interactive sub-regions.

    This is the scalable way to draw tile strips: Python bakes a whole
    row of posters — captions, progress bars, badges included — into
    ONE image (dodging both the 63-overlay budget and the
    bitmaps-above-ASS z-order), and declares the clickable tile areas
    as regions. Each region dict (image-local coords):

        {"id": ..., "x":, "y":, "w":, "h":,
         "on_click": fn, "on_context": fn, "hover": {"bc": ...}}

    Regions become transparent hit-rects whose hover ring draws OUTSIDE
    their bounds (the bitmap would cover an inline ring).

    ``"zone": True`` marks a region that is a *place on the bitmap* rather
    than a thing in it (the reader's page-turn halves): no hover ring, out
    of the spatial-nav order — the ``nnav`` flag of GUIDE.md section 3. Use
    it only where the bitmap has no cells.
    """

    def __init__(self, src, iw, ih, regions=None, v=0, **kw):
        _check_raster(src, iw, ih, kw)
        super().__init__(**kw)
        self.src = src
        self.iw = iw
        self.ih = ih
        self.lw = kw['w']
        self.lh = kw['h']
        self.v = v
        self.regions = regions or []

class CinemaBackdrop(Element):
    """Fixed artwork animated locally against a vertical scroll offset.

    ``frames`` and ``masks`` are finite lists of bitmap-store records
    (src/iw/ih/v). Frame zero is the complete sharp source; later frames
    provide increasing blur. Masks are small transparent-to-surface-colour
    gradients, independently selected and stretched to the visible area.
    Sources can have different raster sizes; the renderer scales them.
    Set ``mask_in_foreground=True`` when the scrolling foreground already
    contains its gradient; ``masks`` may then be empty, avoiding a separate
    moving overlay and its full-atlas repack on every animation frame.

    ``w``/``h`` describe the resting logical hero rectangle. Only its bottom
    boundary moves when scrolling. Pulling reveals more of the whole source
    before proportional enlargement is needed. No Python repaint is needed
    for either scrolling or the return animation.
    """

    def __init__(self, frames, masks, scroll_id, focal_x=0.5, focal_y=0.5, mask_in_foreground=False, **kw):
        super().__init__(**kw)
        self.frames = list(frames)
        self.masks = list(masks)
        if reduced_motion():
            self.frames = self.frames[:1]
            self.masks = self.masks[:1]
        self.mask_in_foreground = bool(mask_in_foreground)
        if not self.frames or (not self.masks and (not self.mask_in_foreground)):
            raise ValueError('CinemaBackdrop needs artwork and gradient sources')
        for bitmap in self.frames + self.masks:
            if not bitmap.get('src') or bitmap.get('iw', 0) <= 0 or bitmap.get('ih', 0) <= 0:
                raise ValueError('CinemaBackdrop sources need positive raster dimensions')
        self.scroll_id = scroll_id
        self.focal_x = min(1.0, max(0.0, float(focal_x)))
        self.focal_y = min(1.0, max(0.0, float(focal_y)))

class Icon(Element):
    """Material vector icon (shared set with the Tk UI and the OSC),
    rendered as an ASS drawing — crisp at any size. Compose with Text
    in a Row for labelled buttons; Dropdown/Menu take per-item icons
    directly."""

    def __init__(self, name, size=None, color=None, on_click=None, hover=None, hover_parent=None, hover_tint=None, **kw):
        size = theme.size('NORMAL') if size is None else int(size)
        kw.setdefault('w', size)
        kw.setdefault('h', size)
        super().__init__(**kw)
        self.name = name
        self.color = color or theme.ON_SURFACE
        self.on_click = on_click
        self.hover = hover
        self.hover_parent = hover_parent
        self.hover_tint = hover_tint

class Button(Box):
    """Labelled button, optionally with a leading Material icon.

    ``icon`` takes the same names as :class:`Icon`; it inherits the label's
    colour so accented/active buttons stay legible. An icon-only button is
    just ``label=""``."""

    def __init__(self, label, on_click=None, size=None, fg=None, icon=None, icon_size=None, gap=None, flat=False, **kw):
        size = theme.size('NORMAL') if size is None else theme.text_size(size)
        themed_fg = fg is None
        fg = fg or theme.ON_SURFACE
        if kw.get('disabled'):
            themed_fg = False
            fg = theme.ON_SURFACE_FAINT
            on_click = None
            kw['hover'] = None
            kw.setdefault('bg', None if flat else theme.CONTROL_SUNKEN)
        if flat:
            kw.setdefault('bg', None)
            kw.setdefault('alpha', 70)
            kw.setdefault('hover', {'fill': theme.ACCENT, 'circle': True})
        else:
            kw.setdefault('bg', theme.CONTROL_BG)
            kw.setdefault('hover', {'fill': theme.CONTROL_HOVER})
        kw.setdefault('radius', 6)
        kw.setdefault('pad', 10)
        kw.setdefault('align', 'center')
        kw.setdefault('direction', 'row')
        children = []
        if icon:
            tint = theme.ACCENT if flat and themed_fg else None
            children.append(Icon(icon, icon_size or int(size * 0.95), color=fg, hover_parent=kw.get('id'), hover_tint=tint))
            kw.setdefault('gap', gap if gap is not None else 7)
        if label:
            children.append(Text(label, size=size, color=fg, align='center'))
        super().__init__(children, on_click=on_click, **kw)

class TextBox(Element):

    def __init__(self, id, text='', placeholder='', size=None, mask=False, on_change=None, on_submit=None, on_commit=None, force=False, **kw):
        size = theme.size('NORMAL') if size is None else theme.text_size(size)
        kw.setdefault('w', 240)
        super().__init__(id=id, **kw)
        self.text = text
        self.placeholder = placeholder
        self.size = size
        self.mask = mask
        self.on_change = on_change
        self.on_submit = on_submit
        self.on_commit = on_commit
        self.force = force

class Slider(Element):
    """Draggable value slider (volume, seek). on_change(value) fires
    throttled while the value is in flight (dragging / keyboard adjust
    mode) and once on release. Seek-style sliders additionally take
    ``on_commit(value)`` — fired once when the gesture ends (drag
    release, adjust mode toggled off) — and ``on_cancel()`` — the
    gesture was abandoned (ESC / focus moved away) and the renderer
    reverted to the scene value. With only on_change registered
    (volume) every in-flight change is live, as before."""

    def __init__(self, id, value=0.0, min=0.0, max=100.0, on_change=None, on_commit=None, on_cancel=None, preview=False, force=False, on_video=False, marks=None, ranges=None, always_adjust=False, **kw):
        kw.setdefault('w', 180)
        kw.setdefault('h', 28)
        super().__init__(id=id, **kw)
        self.value = value
        self.min = min
        self.max = max
        self.on_change = on_change
        self.on_commit = on_commit
        self.on_cancel = on_cancel
        self.preview = preview
        self.force = force
        self.on_video = on_video
        self.marks = marks
        self.ranges = ranges
        self.always_adjust = always_adjust

class Busy(Element):
    """Indeterminate activity spinner (animated renderer-side)."""

    def __init__(self, **kw):
        kw.setdefault('w', 28)
        kw.setdefault('h', 28)
        super().__init__(**kw)

class Checkbox(Row):
    """Labelled toggle — pure composite sugar over Row/Box/Text."""

    def __init__(self, label, checked, on_toggle=None, size=None, **kw):
        size = theme.size('NORMAL') if size is None else theme.text_size(size)
        off = bool(kw.get('disabled'))
        _tick = max(12, int(round(size * 1.18)))
        box = Box(w=_tick, h=_tick, bg=(theme.ACCENT if not off else theme.CONTROL_SUNKEN) if checked else theme.CONTROL_SUNKEN, border=theme.OUTLINE_STRONG if not checked or off else None, radius=5, align='center', direction='row', children=[Text('✓', size='small', align='center', flex=1, color=theme.ON_SURFACE_FAINT if off else theme.ON_ACCENT)] if checked else [])
        kw.setdefault('gap', 10)
        kw.setdefault('align', 'center')
        if off:
            kw['hover'] = None
            on_toggle = None
        else:
            kw.setdefault('hover', {'c': theme.ON_SURFACE_STRONG})
        super().__init__([box, Text(label, size=size, color=theme.ON_SURFACE_FAINT if off else None)], on_click=on_toggle, **kw)

class Grid(Element):
    """Cells laid out on shared column tracks — the cure for sibling
    rows faking column alignment with magic fixed widths.

    ``cols``: list of track specs — ``{"w": px}`` fixed, ``{"flex": n}``
    share of leftover, ``{}`` auto (sized to the widest cell in that
    column). Optional ``"align"``: "left"/"center"/"right" (default
    left) positions cells inside their track.

    ``rows``: list of cell lists. A cell is an Element, a str (becomes
    a Text at ``size``/``fg``), or None (empty). Text cells and cells
    with ``flex>0`` stretch to the track width; other Elements keep
    their natural size, positioned by the track's align. Cells are
    vertically centered in their row (rows size to their tallest cell,
    or ``row_h`` if given).

    A row may also be a dict ``{"cells": [...], "id":, "bg":,
    "radius":, "hover":, "on_click":, "on_dbl":}`` — the styling/
    interaction draws as a full-width row rect behind the cells (list
    rows with card backgrounds, à la the servers/downloads panels).
    ``row_pad`` insets cells from the row rect on every side.
    """

    def __init__(self, rows, cols, gap=12, row_gap=8, row_h=None, row_pad=0, size=None, fg=None, **kw):
        size = theme.size('LARGE') if size is None else theme.text_size(size)
        super().__init__(**kw)
        self.rows = rows
        self.cols = cols
        self.gap = gap
        self.row_gap = row_gap
        self.row_h = row_h
        self.row_pad = row_pad
        self.size = size
        self.fg = fg or theme.ON_SURFACE

class Form(Grid):
    """Label + input rows on shared tracks: the label column sizes to
    the widest label, the value column flexes. ``rows`` is a list of
    ``(label, element)`` pairs (label may be a str or an Element; a
    None element leaves the row's value cell empty)."""

    def __init__(self, rows, label_w=None, size=None, label_fg=None, **kw):
        size = theme.size('LARGE') if size is None else theme.text_size(size)
        label_fg = label_fg or theme.ON_SURFACE_MUTED
        cols = [{'w': label_w} if label_w else {}, {'flex': 1}]
        grid_rows = [[Text(l, size=size, color=label_fg) if isinstance(l, str) else l, v] for l, v in rows]
        super().__init__(grid_rows, cols, size=size, **kw)

class Gradient(Element):
    """A vertical fade — one solid ASS box with a gaussian-blurred fading
    edge, never stacked alpha bands (GUIDE.md section 2). The playback
    HUD's bottom scrim is ``Gradient(color="000000", top=0, bottom=200)``:
    transparent at the top edge, mostly-opaque at the bottom. Opacities
    are 0-255. Non-interactive.

    ``stops`` switches it from an alpha fade of one colour to a multi-stop
    COLOUR ramp: ``[(0.0, "0f3562"), (0.5, "1162a4"), (1.0, "03215f")]``,
    positions 0-1 along ``axis`` ("y" or "x"). Still not bands — each stop
    after the first is one blurred-edge layer over the ones before it, and
    alpha-compositing a ramp of B over solid A *is* a linear interpolation
    from A to B. So an n-stop gradient costs n ASS events, not n bands.

    Used for themed app backgrounds and top bars, which several of
    jellyfin-web's themes rely on.

    **Use the fewest stops that describe the shape.** Unlike CSS, more stops
    here do not buy smoothness — they cost it. Each layer's ramp begins and
    ends at ~zero slope, so every extra stop is a place where the colour
    briefly stops changing, and the eye reads those flat spots as bands. Two
    stops give one clean ease; subdividing a single ramp into six put visible
    strips across it. ``tools/gradient_fidelity.py`` renders a gradient
    through mpv and prints its slope profile, which is where this shows up
    and where per-pixel colour error does not.
    """

    def __init__(self, color=None, top=0, bottom=200, stops=None, axis='y', **kw):
        super().__init__(**kw)
        self.color = color or theme.SCRIM
        self.top = top
        self.bottom = bottom
        self.stops = list(stops) if stops else None
        self.axis = axis

class Progress(Element):
    """Determinate progress bar (composite drawn by layout as two
    rects). ``frac`` in [0, 1]; give it a width or ``flex``."""

    def __init__(self, frac, fg=None, bg=None, **kw):
        kw.setdefault('w', 180)
        kw.setdefault('h', 8)
        super().__init__(**kw)
        self.frac = min(1.0, max(0.0, frac))
        self.fg = fg or theme.ACCENT
        self.bg = bg or theme.CONTROL_SUNKEN

class Stack(Element):
    """Children share this element's rect; later children paint above
    earlier ones and everything scrolls with the page (unlike Float,
    which is screen-absolute). Per-child placement comes from the
    child's own ``anchor`` ("nw" "n" "ne" "w" "c" "e" "sw" "s" "se", or
    None to fill the whole rect) plus ``dx``/``dy`` pixel offsets.

    Z-order caveats (GUIDE.md section 6): a bitmap child over a bitmap
    sibling works, an ASS-drawn one (Text/Icon/Box) does not — mark it
    ``occlude=True`` to punch a hole in the image siblings below it and
    give it an opaque bg. Without ``occlude`` the image wins.

    ``occlude`` suits chrome MEANT to cover what is under it. A control
    that should look like it floats ON the artwork wants to be an
    :class:`Image` instead (it carries ``on_click``/``repeat`` like a Box)
    so it can alpha-blend -- see ``tile_renderer._arrow_bitmap``.
    """

    def __init__(self, children=None, **kw):
        super().__init__(**kw)
        self.children = [c for c in children or [] if c is not None]

def virtual_window(virtual, row_h, count):
    """``(first, last)`` row indexes a virtualized table will materialize.

    Public so a caller can window its *cell construction* too, not just the
    rows it hands over. That matters when a cell costs more than layout —
    a track list's album art is one composited bitmap and one mpv overlay
    per row, and building those for every row of a long playlist blew the
    strip cache and the overlay budget even though only a screenful was
    ever displayed.
    """
    if virtual is None or not count or row_h <= 0:
        return (0, count)
    over = virtual.get('overscan', 2)
    off = max(0.0, float(virtual.get('offset', 0)))
    view = float(virtual.get('height', 0))
    first = max(0, int(off // row_h) - over)
    last = min(count, int((off + view) // row_h) + 1 + over)
    return (first, max(first, last))

class Table(Column):
    """Header + body rows generated from ONE column spec, so header and
    cell geometry can never drift apart.

    ``columns``: list of dicts — ``{"label": str, "w": px}`` or
    ``{"label": str, "flex": n}``, optional ``"align"``
    ("left"/"center"/"right", default left).

    ``rows``: list of dicts —
    ``{"cells": [str | Element, ...], "id": optional, "selected": bool,
    "fg": row text color, "bg": row background (selected wins),
    "on_click": fn, "on_dbl": fn, "on_context": fn}``. ``on_click`` may
    declare one
    required parameter to receive the click modifier dict
    ``{"shift": bool, "ctrl": bool}`` for range/additive selection (see
    MpvtkApp click dispatch); zero-arg callables keep the bare call.
    ``on_dbl`` fires on double-click, after the two normal clicks.

    ``virtual``: optional ``{"offset": px, "height": px, "overscan":
    rows}`` — materialize only the rows intersecting the viewport
    (``offset`` from ``MpvtkApp.scroll_offsets()``), replacing the rest
    with two exact-height spacers. Give rows stable ``id``s so
    renderer-side state survives the window moving.
    """
    FLEX_MIN_W = 70

    def __init__(self, columns, rows, row_h=36, header_h=30, size=None, header_size=None, header_fg=None, fg=None, selected_bg=None, hover_bg=None, gap=12, pad_x=10, virtual=None, **kw):
        size = theme.size('LARGE') if size is None else theme.text_size(size)
        header_size = theme.size('SMALL') if header_size is None else theme.text_size(header_size)
        selected_bg = selected_bg or theme.SOFT
        header_fg = header_fg or theme.ON_SURFACE_MUTED
        fg = fg or theme.ON_SURFACE
        hover_bg = hover_bg or theme.CONTROL_BG

        def cell(col, content, text_size, color):
            if isinstance(content, Element):
                inner = content
            else:
                inner = Text(str(content), size=text_size, color=color, align=col.get('align', 'left'), flex=1)
            return Box([inner], direction='row', align='center', w=col.get('w'), flex=col.get('flex', 0) if col.get('w') is None else 0)

        def margin():
            return Spacer(w=pad_x, h=1)
        header = Row([margin()] + [cell(c, c.get('label', ''), header_size, header_fg) for c in columns] + [margin()], h=header_h, gap=gap, align='stretch')
        first, last = virtual_window(virtual, row_h, len(rows))
        lead_h = first * row_h
        tail_h = (len(rows) - last) * row_h
        body = []
        if lead_h:
            body.append(Spacer(h=lead_h))
        for i in range(first, last):
            row = rows[i]
            row_fg = row.get('fg', fg)
            body.append(Row([margin()] + [cell(c, v, size, row_fg) for c, v in zip(columns, row.get('cells', []))] + [margin()], id=row.get('id'), h=row_h, gap=gap, align='stretch', bg=selected_bg if row.get('selected') else row.get('bg'), hover={'fill': hover_bg} if row.get('on_click') else None, on_click=row.get('on_click'), on_dbl=row.get('on_dbl'), on_context=row.get('on_context'), radius=4))
        if tail_h:
            body.append(Spacer(h=tail_h))
        kw.setdefault('align', 'stretch')
        super().__init__([header] + body, **kw)
        if virtual is not None and self.w is None and (not self.flex) and (self.min_w is None):
            from .layout import text_width
            total = 2 * pad_x + gap * (len(columns) + 1)
            for col in columns:
                if col.get('w') is not None:
                    total += col['w']
                    continue
                total += min(self.FLEX_MIN_W * 2, max(self.FLEX_MIN_W, text_width(str(col.get('label', '')), header_size)))
            self.min_w = total

class Float(Element):
    """Absolutely-positioned top-layer container (toasts, banners).
    Drawn above everything and occluding image overlays; does NOT grab
    input — content stays clickable underneath elsewhere."""

    def __init__(self, child, x, y, **kw):
        super().__init__(**kw)
        self.child = child
        self.x = x
        self.y = y

class Dialog(Element):
    """Modal dialog: floating container that grabs all input. Clicks
    outside it and ESC emit on_dismiss(); the app closes it by
    re-rendering without the Dialog. No dimmed backdrop (bitmaps render
    above ASS, so a scrim cannot cover posters — see README z-order).

    ``side="left"`` pins it to the page content's margin instead of
    centring it. No current caller; a new dialog should be centred.
    """

    def __init__(self, id, child, on_dismiss=None, side='center', **kw):
        super().__init__(id=id, **kw)
        self.child = child
        self.on_dismiss = on_dismiss
        self.side = side
ICON_TRIGGER = 20

def trigger_box(glyph):
    """The hit box an icon-trigger `Dropdown` builds around a `glyph`-px
    icon, and the smallest interactive target the playback HUD draws.

    A function rather than an expression inside `Dropdown` because the
    HUD's accessibility floor solves it backwards -- "how far may the bar
    shrink before this stops being a reachable target" -- and two copies
    of the arithmetic would drift the day either changes.
    """
    return int(glyph / 1.2 * 1.9)

class Dropdown(Element):
    """``trigger_icon`` replaces the boxed control with a bare Material
    icon (translucent hover wash, no border/arrow/label) — the playback
    HUD's track pickers open their popup from a transparent icon
    button. The popup then sizes to its items (not the trigger) and
    clamps to the screen edges.

    ``popup_w`` lets the open list be wider than the closed control, up to
    that many logical px: the popup takes as much of it as the widest item
    needs, and never less than the control. Unset, the popup matches the
    control, which is the right default when the labels are ones the app
    wrote. It is for the lists whose text comes from *outside* — audio device
    descriptions identify themselves at the end ("… Digital Stereo
    (IEC958)"), so at the control's width every row ellipsizes to the same
    thing. Widening the control instead would leave one row of the settings
    form out of line with all the others."""

    def __init__(self, id, items, selected=0, size=None, icons=None, on_select=None, force=False, ack=None, trigger_icon=None, icon_size=None, trigger_chip=None, popup_w=None, **kw):
        size = theme.size('NORMAL') if size is None else theme.text_size(size)
        if trigger_icon:
            glyph = int(icon_size or ICON_TRIGGER * 1.2)
            box = trigger_box(glyph)
            kw.setdefault('w', box)
            kw.setdefault('h', box)
            if isinstance(kw.get('w'), (int, float)):
                kw.setdefault('min_w', kw['w'])
        super().__init__(id=id, **kw)
        self.items = list(items)
        self.selected = selected
        self.size = size
        self.icons = icons
        self.on_select = on_select
        self.force = force
        self.ack = ack
        self.trigger_icon = trigger_icon
        self.icon_size = icon_size
        self.trigger_chip = trigger_chip
        self.popup_w = popup_w

class Menu(Element):
    """Floating context menu at absolute position (x, y) — out of flow.

    Presence in the tree = open. The renderer reports item choice via
    on_select(index, value) and click-away/ESC via on_dismiss(); the app
    responds by re-rendering without the Menu (the renderer hides it
    instantly on its own for responsiveness).
    """

    def __init__(self, id, items, x, y, size=None, icons=None, on_select=None, on_dismiss=None, **kw):
        size = theme.size('NORMAL') if size is None else theme.text_size(size)
        super().__init__(id=id, **kw)
        self.items = list(items)
        self.x = x
        self.y = y
        self.size = size
        self.icons = icons
        self.on_select = on_select
        self.on_dismiss = on_dismiss

class Scroll(Element):
    """``on_scroll(offset, max)`` fires debounced from the renderer when
    the user scrolls — the hook for windowed/infinite content.

    ``follow=True`` sticks to the end: the container opens scrolled to the
    bottom, and stays pinned there as content grows — but only while the
    user is already at the bottom, so scrolling up to read pauses it. The
    renderer owns this because it is the only side that knows the offset
    and the content height at the same instant; a Python round-trip would
    always be pinning against the *previous* frame's height.

    ``snap`` (logical px, along the scroll axis) declares where the rows of
    a container ARE, so the renderer can quantize to them: it displays and
    hit-tests the offset snapped to the nearest ``snap_off + k*snap``
    boundary. The internal offset stays continuous, so virtualization and
    paging are unaffected — only what's drawn snaps.

    **It is a capability, not a mode**, so declaring it costs nothing on a
    machine that keeps up: the renderer applies it only when a gesture asks
    for frames faster than it can measurably draw them (``snap_round`` /
    ``state.rcost`` in renderer.lua). Quantizing makes consecutive frames
    identical, which is what lets both be skipped.

    ``snaps`` is the same idea for **unequal** breakpoints: an explicit list
    of logical offsets (e.g. the content-y of each home-screen section
    heading). The renderer snaps the display to the nearest one, and a wheel
    notch steps to the adjacent breakpoint. Takes precedence over ``snap``.

    ``offset`` (logical px) is where a container opens **the first time this
    id is seen**, and is ignored on every frame after — it restores a
    position, it does not drive one. Scrolling stays entirely the renderer's,
    which is what keeps a Python round-trip out of the wheel path. Same
    mechanism ``follow`` uses to open at the end, and the same reason it
    lives here: the offset has to be clamped against the content height in
    the frame it is applied to, and only the renderer knows both at once.

    ``cinema_pull=True`` lets a vertical viewport at its top be pulled down
    from noninteractive content. Its temporary negative offset is reported
    through ``on_scroll`` so fixed artwork can expand with the gesture. It
    eases back to zero on release; ordinary scrolling remains clamped.
    """

    def __init__(self, child, axis, scrollbar=False, on_scroll=None, follow=False, snap=None, snap_off=0, snaps=None, offset=None, cinema_pull=False, **kw):
        super().__init__(**kw)
        self.child = child
        self.axis = axis
        self.scrollbar = scrollbar
        self.on_scroll = on_scroll
        self.follow = follow
        self.snap = snap
        self.snap_off = snap_off
        self.snaps = snaps
        self.offset = offset
        self.cinema_pull = bool(cinema_pull and axis == 'y' and not reduced_motion())

class HScroll(Scroll):

    def __init__(self, child, **kw):
        super().__init__(child, 'x', **kw)

class VScroll(Scroll):

    def __init__(self, child, scrollbar=True, **kw):
        super().__init__(child, 'y', scrollbar=scrollbar, **kw)

"""Content-area primitives: the busy and error states, body text, widths.

Pure functions — data in, widget tree out — so every page can reach them
through ``PageContext`` instead of the shell. See
``docs/archive/ARCHITECTURE_TARGET.md`` §1.4 for the test being applied: a component
may need render resources and callbacks, never ``nav``, ``source`` or
``route``.

Moved from ``ViewsMixin`` / the shell. ``CONTENT_PAD`` travels with them
because it is a layout constant, not shell state.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import html
import re
from ...mpvtk.widgets import Box, Busy, Column, Row, Spacer, Text
from .. import theme
CONTENT_PAD = 16

def busy():
    """The loading state: a spinner centred in the content area.

    Spacers rather than pad+align: the Row/Column nesting is what centres it
    on both axes, and it is copied verbatim from the original so the pixels
    do not move.
    """
    return Box([Spacer(), Row([Spacer(), Busy(), Spacer()]), Spacer()], flex=1, direction='column', align='stretch')

def error(msg):
    """A terminal message where content would be — a failed load, an empty
    search box. Not a dialog: the user is already looking here.

    Top-aligned, because it stands in for *content* and content starts at
    the top. ``direction="row"`` makes ``align`` the vertical axis, so this
    used to read ``align="center"`` and floated the line in the middle of
    whatever space was left — under the Live TV tab bar that put "Nothing is
    scheduled to record." halfway down an otherwise empty screen, attached
    to nothing. ``flex=1`` stays: the message still owns the content area,
    it just no longer hovers in it.
    """
    return Box([Text(msg, size='large', color=theme.SUBTLE_FG)], pad=CONTENT_PAD, flex=1, align='start', direction='row')

def wrap_row(items, avail, gap=8, align='center', row_gap=None):
    """``items`` in a Row, broken onto further rows when one will not fit.

    A Row does not wrap: it lays its children out end to end and lets the
    tail run off the window. That is invisible at 1x on a wide window and
    routine at 200%, where a 1280px window is a 640px page (see
    ``mpvtk/scaling.py`` -- view code is logical, so the UI scale is a width
    problem). It is equally reachable at 100% by making the window small,
    and by translation: "Servers & Users" is "Server und Benutzer" in German.

    Measured rather than switched on a width constant, for the same reason
    ``GridPage._fit_bar`` is: what fits depends on these particular items.

    Returns the plain Row when everything fits on one, so the common case
    produces the same tree it always did.
    """
    from ...mpvtk.layout import measure
    one_row = Row(items, gap=gap, align=align)
    if not items or avail <= 0:
        return one_row
    packable = [i for i in items if not (isinstance(i, Spacer) and getattr(i, 'flex', 0))]
    try:
        widths = [measure(i)[0] for i in packable]
    except Exception:
        return one_row
    rows, cur, used = ([], [], 0.0)
    for item, iw in zip(packable, widths):
        need = iw if not cur else used + gap + iw
        if cur and need > avail:
            rows.append(cur)
            cur, used = ([item], iw)
        else:
            cur.append(item)
            used = need
    rows.append(cur)
    if len(rows) == 1:
        return one_row
    return Column([Row(r, gap=gap, align=align) for r in rows], gap=gap if row_gap is None else row_gap, align='start')
BLEED_ATTR = '_bleeds_x'

def bleed(el):
    """Mark ``el`` as a block that runs edge to edge, and return it.

    For content whose own scroll viewport has to reach the window edge — a
    carousel, whose strip is inset from *inside* so a paged tile travels
    out to the edge instead of being clipped at the page margin. A block
    that merely wants to *look* wide does not need this; it can take the
    padded width and be no worse for it.
    """
    setattr(el, BLEED_ATTR, True)
    return el

def split_bleed(blocks, px, gap, align='start'):
    """``blocks`` regrouped so the marked ones escape ``px`` of padding.

    A single Column has one padding for every child, so a full-bleed block
    cannot live in a padded column at all. Runs of ordinary blocks become
    inner columns padded to ``px``; the marked ones become direct children
    of the caller's unpadded outer column. The gap is the same on both
    levels, so a reader cannot tell where a run ended.

    ``align`` is the one thing a caller must not leave to the default by
    accident: it belonged to the column being replaced, and a block that
    used to be stretched to the page width now measures to its content
    instead. That is invisible on a Text and loud on a Table, whose columns
    are shares of whatever width it was given -- every cell ellipsized.
    """
    out, run = ([], [])

    def flush():
        if not run:
            return
        out.append(Column(list(run), pad=(px, 0), gap=gap, align=align))
        run.clear()
    for b in blocks:
        if b is None:
            continue
        if getattr(b, BLEED_ATTR, False):
            flush()
            out.append(b)
        else:
            run.append(b)
    flush()
    return out

def header_body(banner, blocks, pad=CONTENT_PAD, gap=16, full_bleed=False):
    """The scrollable column of a page that opens with a backdrop header.

    ``Column([banner] + blocks, pad=pad, gap=gap)`` -- the padded shape all
    three detail-ish pages used to build by hand -- except that any block
    marked by :func:`bleed`, and the banner itself when ``full_bleed``,
    comes OUT of the horizontal padding.

    The nesting is what makes that possible: a single column has one padding
    for every child, so the escapees and the padded runs between them become
    siblings under an unpadded outer column. ``align="stretch"`` there is
    for those inner columns, which have no width of their own; the banner is
    an Image with an explicit ``w`` and layout leaves a fixed cross size
    alone, so it keeps exactly the width ``banner_box`` gave it.

    The trailing Spacer is the bottom padding the full-bleed path's ``pad=0``
    gives up. There is no top padding to replace, which is the point.

    ``pad`` may be an ``(x, y)`` pair, which is what a page whose body is a
    GRID passes: the grid's own horizontal padding comes from
    ``grid_layout`` and is not CONTENT_PAD (see SeasonPage). Only the y half
    is the one full bleed gives up.
    """
    px, py = pad if isinstance(pad, tuple) else (pad, pad)
    if not full_bleed:
        return Column(split_bleed([banner] + list(blocks), px, gap), pad=(0, py), gap=gap, align='stretch')
    body = split_bleed(list(blocks), px, gap) + [Spacer(h=max(0, py - gap))]
    return Column([banner] + body, gap=gap, align='stretch')

def padded_body(blocks, pad=CONTENT_PAD, gap=16, align='start'):
    """:func:`header_body` for a page that opens with no banner."""
    px, py = pad if isinstance(pad, tuple) else (pad, pad)
    return Column(split_bleed(blocks, px, gap, align), pad=(0, py), gap=gap, align='stretch')

def body_width(w, pad=CONTENT_PAD):
    """Usable text width inside a padded, scrollable content column.

    The window width minus the content padding AND the scrollbar the
    scroll view reserves. Wrapping at ``w - 2*pad`` — the padding alone —
    makes every line 10px wider than the space it actually gets, so
    the tail of each line runs under the scrollbar, and which words land
    there changes with the window size. That is what made resizing look
    like the wrapping was unstable.
    """
    from ...mpvtk.layout import SCROLLBAR_W
    return max(120, w - 2 * pad - SCROLLBAR_W)
PROSE_CH = 88

def prose_w(size, avail):
    """The width explanatory text at ``size`` should wrap at, within
    ``avail``.

    Measured rather than assumed: "n" is a middling glyph and the layout
    engine's own measurement is what the wrap will use, so this stays
    honest when the face changes. Narrower windows keep ``avail`` -- the
    cap is a maximum, never a minimum.
    """
    from ...mpvtk.layout import measure
    return min(avail, measure(Text('n' * PROSE_CH, size=size))[0])
_BREAK = re.compile('<\\s*br\\s*/?\\s*>|<\\s*/\\s*p\\s*>', re.I)
_TAG = re.compile('</?(?:a|b|i|u|s|p|br|em|strong|span|div|font|sup|sub|ul|ol|li|hr|h[1-6]|blockquote)(?:\\s+[a-z-]+\\s*=\\s*(?:"[^"]*"|\'[^\']*\'|[^\\s"\'<>]+))*\\s*/?>', re.I)
_BBCODE = re.compile('\\[/?(?:b|i|u|s|em|strong|quote|spoiler|(?:url|color|size)(?:=[^\\]]*)?)\\]', re.I)

def overview(item):
    """An item's Overview as plain text. Providers leave HTML and BBCode
    in it, and the browser draws text, not markup. Every read of the field
    goes through here (tests/test_overview_markup.py)."""
    text = _BREAK.sub('\n', item.get('Overview') or '')
    text = _BBCODE.sub('', _TAG.sub('', text))
    return html.unescape(text).strip()

def paragraph(text, size, max_w, color=None):
    """Wrapped body text (overviews).

    The layout engine wraps *within* a paragraph, so blank-line breaks
    are handled here. The gap is a full line height: at anything less
    the paragraph break reads as tighter than the wrapped lines around
    it, which looks like a mistake rather than a break.
    """
    from ...mpvtk.layout import LINE_H
    paras = [p.strip() for p in (text or '').replace('\r', '').split('\n') if p.strip()]
    color = color or theme.TEXT_FG
    if len(paras) <= 1:
        return Text(paras[0] if paras else '', size=size, color=color, wrap=True, w=max_w)
    return Column([Text(p, size=size, color=color, wrap=True, w=max_w) for p in paras], gap=round(size * LINE_H), w=max_w)

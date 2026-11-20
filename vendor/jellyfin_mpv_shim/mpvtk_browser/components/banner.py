"""The detail-screen banner compositor.

Moved verbatim from ``TilesMixin``. ``compose_banner`` was a ``classmethod``
purely so it could reach ``cls._wrap_pil``; as module-level functions that
indirection disappears.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from ...mpvtk import pilfont
from ...mpvtk.scaling import px
from . import chrome
from .. import theme

def wrap_pil(draw, text, font, max_w, max_lines=2):
    """Word-wrap ``text`` to ``max_lines``, ellipsizing the last line.
    Falls back to breaking mid-word for a single word too long to fit.

    Measured through ``pilfont.text_length`` so a heading that mixes scripts
    is wrapped at the width it will be drawn at -- the two differ whenever a
    run has to change face (see ``pilfont.runs``).

    Was ``TilesMixin._wrap_pil``.
    """
    words, lines, cur = (text.split(), [], '')
    for word in words:
        trial = (cur + ' ' + word).strip()
        if not cur or pilfont.text_length(draw, trial, font) <= max_w:
            cur = trial
            continue
        lines.append(cur)
        cur = word
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if not lines:
        return [text]
    consumed = len(' '.join(lines).split())
    if consumed < len(words) or pilfont.text_length(draw, lines[-1], font) > max_w:
        last = lines[-1]
        if consumed < len(words):
            last = ' '.join([last] + words[consumed:])
        while last and pilfont.text_length(draw, last + '…', font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + '…'
    return lines
POSTER_H_FRAC = 0.78
POSTER_W_FRAC = 0.25
THUMB_H_FRAC = 0.6
THUMB_RATIO = 1.33
MAX_INSET_ASPECT = 16 / 9

def poster_box(box):
    """``(x, y, max_w, max_h)`` the inset artwork is fitted inside.

    A **bounding** box, not the drawn size: what lands here is a 2:3 poster
    for a film or series and a 16:9 still for an episode, and both are
    drawn at their own shape within these limits. Sized off the banner so
    it scales with the header, and refused when there is no sensible room.
    """
    w, h = box
    max_h = int(h * POSTER_H_FRAC)
    max_w = min(int(w * POSTER_W_FRAC), int(max_h * MAX_INSET_ASPECT))
    if max_w <= 0 or max_h <= 0 or max_w > w // 3:
        return None
    return (_content_x(w), (h - max_h) // 2, max_w, max_h)

def _content_x(w):
    """The banner's left edge, in physical px.

    :data:`chrome.CONTENT_PAD`, not a fraction of the banner: a backdrop
    bleeds to the window edges, but everything baked ON it -- the inset
    poster, the heading, the meta line -- is page content, and the Play
    button lands directly under it. ``w // 40`` put the two in different
    columns (32 against 16 at a 1280 window), which is exactly the size of
    offset the eye picks up on a left edge it can compare against.

    ``w`` is taken and ignored so the call sites read as a measurement of
    the box rather than a constant that happens to be in scope.
    """
    return px(chrome.CONTENT_PAD)

def compose_banner(image, box, title=None, meta=None, context=None, poster=None):
    """Crop ``image`` to the banner box and bake the heading over a
    bottom-up dark gradient.

    Stacked bottom-up: meta, title (wrapped to two lines), then the
    context line above it. Text is sized off the banner height and
    stays small enough that a long episode title reads in full.

    Was ``TilesMixin._compose_banner``.
    """
    from PIL import ImageDraw
    from ...imageutil import TOP_HEAVY, apply_dark_gradient, pil_font, scale_to_cover
    w, h = box
    canvas = scale_to_cover(image.convert('RGBA'), w, h, gravity_y=TOP_HEAVY)
    if not title:
        return canvas
    canvas = apply_dark_gradient(canvas, height_fraction=0.7, max_alpha=215)
    draw = ImageDraw.Draw(canvas)
    margin = _content_x(w)
    bottom = max(px(18), w // 40)
    slot = poster_box(box) if poster is not None else None
    text_left = margin
    if slot is not None:
        right = _paste_poster(canvas, poster, slot)
        text_left = right + max(px(14), w // 60)
    avail = w - text_left - margin
    size = theme.baked_text(max(px(20), min(px(34), h // 6)))
    y = h - bottom
    if meta:
        f = pil_font(int(size * 0.6), text=meta)
        asc, desc = f.getmetrics()
        line = wrap_pil(draw, meta, f, avail, max_lines=1)[0]
        pilfont.draw_text(draw, (text_left, y - asc - desc), line, f, fill=(200, 200, 200, 255))
        y -= asc + desc + px(6)
    f = pil_font(size, bold=True, text=title)
    asc, desc = f.getmetrics()
    for line in reversed(wrap_pil(draw, title, f, avail)):
        pilfont.draw_text(draw, (text_left, y - asc - desc), line, f, fill=(255, 255, 255, 255))
        y -= asc + desc + px(2)
    if context:
        f = pil_font(int(size * 0.62), text=context)
        asc, desc = f.getmetrics()
        line = wrap_pil(draw, context, f, avail, max_lines=1)[0]
        pilfont.draw_text(draw, (text_left, y - asc - desc + px(2)), line, f, fill=(215, 215, 215, 255))
    return canvas

def _paste_poster(canvas, poster, slot):
    """Draw the artwork into ``slot`` at its own shape. Returns its right edge.

    **No letterbox plate and no rounded corners.** The slot has to hold two
    aspect ratios -- a 2:3 poster and a 16:9 episode still -- and the first
    attempt made that one fixed 2:3 box, so a still arrived boxed in black
    with rounded corners, which reads as a poster of a photograph rather
    than as the frame it is [iw]. Fitted inside the bounds instead and
    composed straight onto the backdrop.

    A drop shadow does the separating that the plate was doing. It is drawn
    onto the *canvas* rather than through ``imageutil.with_shadow``, which
    keeps the shadow inside the image's own bounds -- right for a logo with
    margins, and invisible on a full-bleed rectangle, where every edge is
    ink.
    """
    from PIL import Image, ImageFilter
    x, y, max_w, max_h = slot
    baseline = y + max_h
    art = poster.convert('RGBA')
    if art.height and art.width / art.height >= THUMB_RATIO:
        max_h = int(max_h * THUMB_H_FRAC)
    art.thumbnail((max_w, max_h), Image.LANCZOS)
    aw, ah = art.size
    ay = baseline - ah
    span = max(aw, ah)
    blur = max(2.0, span / 25.0)
    drop = max(2, round(span / 40.0))
    shadow = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 190), (x, ay + drop, x + aw, ay + ah + drop))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow)
    canvas.paste(art, (x, ay), art)
    return x + aw

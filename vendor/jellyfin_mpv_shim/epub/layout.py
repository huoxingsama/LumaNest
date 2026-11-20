"""Blocks in, pages out. The whole layout engine, and none of the drawing.

This is where the reader's typography lives: line breaking, justification,
indents, list markers, image fitting and the page break itself. It has no
Pillow drawing in it and no toolkit imports — it measures text through
:class:`Measurer` and emits geometry, which is what makes it testable
without a window and what keeps it out of ``renderer.lua``.

**Pagination is per spine document, and a document always starts a page**,
which is what makes opening a book cheap and what stops "page 4 of 312"
being answerable. **The unit of position is a character offset, not a
page**: every line carries the offset it starts at, so a resize is answered
by re-paginating and finding the page containing it. Pages are derived; the
offset is the state. Both, and what they cost, are ``docs/readers.md`` §4.4.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from .content import HEADING, IMAGE, PARA, RULE
log = logging.getLogger('epub.layout')
DEFAULT_PARA_SPACE = 0.55
DEFAULT_HEADING_SPACE = 1.1
MAX_JUSTIFY_STRETCH = 5.0
_CJK_RANGES = ((4352, 4607), (11904, 12350), (12353, 13311), (13312, 19903), (19968, 40959), (40960, 42191), (44032, 55203), (63744, 64255), (65280, 65376))
_NO_LINE_START = '、。，．・：；？！）」』】〉》”’%,.;:!?)]}'

def _is_cjk(char):
    code = ord(char)
    return any((lo <= code <= hi for lo, hi in _CJK_RANGES))

class ReaderStyle:
    """The knobs that change how a book is set. All in physical pixels."""

    def __init__(self, font_px=21, font_kind='serif', line_spacing=1.5, margin_x=64, margin_y=48, max_measure=34.0, justify=True, paragraph_indent=True):
        self.font_px = font_px
        self.font_kind = font_kind
        self.line_spacing = line_spacing
        self.margin_x = margin_x
        self.margin_y = margin_y
        self.max_measure = max_measure
        self.justify = justify
        self.paragraph_indent = paragraph_indent

    def column(self, width):
        """``(column width, left offset)`` for ``width`` px of drawable area.

        The offset centres the column. **Callers that convert a pointer
        position back into the text must use this offset, not ``margin_x``**
        — see ``ReaderPage._column_point`` and ``docs/readers.md`` §4.7.
        """
        inner = max(1, int(width) - 2 * self.margin_x)
        if self.max_measure:
            inner = min(inner, max(1, int(self.max_measure * self.font_px)))
        return (inner, (int(width) - inner) // 2)

    def key(self):
        return (self.font_px, self.font_kind, round(self.line_spacing, 3), self.margin_x, self.margin_y, round(self.max_measure, 3), self.justify, self.paragraph_indent)

class Measurer:
    """Fonts and string widths for one :class:`ReaderStyle`.

    Widths are cached per (style, string). A chapter is tens of thousands
    of words drawn from a few thousand distinct ones, and re-measuring
    through FreeType for each occurrence is most of the time pagination
    takes.
    """

    def __init__(self, style, script='latin'):
        self.style = style
        self.script = 'latin' if script in ('symbol', 'emoji', '', None) else script
        self._widths = {}
        self._fonts = {}
        self._resolvers = {}
        self._scripts = {}
        self._metrics_cache = {}

    def size_for(self, span_style):
        return max(8, int(round(self.style.font_px * span_style.scale)))

    def _face(self, span_style, script, text=None):
        """``text`` is the run, when there is one, and it decides the face
        for a non-Latin script: #736's rule, whose second site was this
        module reaching `pilfont` through a script name alone.

        **Not cached with ``text`` in play**, because the key cannot hold it
        and a cache without it hands the next run whatever the first run
        needed. `pilfont` memoizes the faces and the per-glyph coverage, so
        the uncached path is a dict lookup rather than a font open. The
        text-free calls -- the book's own base face, and the line-height
        reservation in `_metrics` -- still hit this cache and pay nothing.
        """
        if text is not None:
            from . import fonts
            kind = 'mono' if span_style.mono else self.style.font_kind
            return fonts.face(kind, self.size_for(span_style), span_style.bold, span_style.italic, script, text=text)
        key = (span_style.key(), script)
        hit = self._fonts.get(key)
        if hit is None:
            from . import fonts
            kind = 'mono' if span_style.mono else self.style.font_kind
            hit = fonts.face(kind, self.size_for(span_style), span_style.bold, span_style.italic, script)
            self._fonts[key] = hit
        return hit

    def font(self, span_style):
        """The face for this book's own script — the base a line is set
        in, and the one whose weight and slant the CSS asked for."""
        return self._face(span_style, self.script)

    def faces(self, span_style):
        """A ``script -> face`` resolver for
        :func:`~jellyfin_mpv_shim.mpvtk.pilfont.draw_text`.

        The reader cannot borrow pilfont's own faces: it sets a book in a
        *serif* family it resolved itself, in four weight/slant
        combinations. So it supplies where each run's face comes from and
        pilfont supplies the splitting. Cached per span style, because
        `width` is called tens of thousands of times to paginate a chapter
        and a fresh closure each time is the cache this class exists to be.
        """
        key = span_style.key()
        hit = self._resolvers.get(key)
        if hit is None:

            def hit(script, text=None, _style=span_style):
                return self._face(_style, script, text)
            self._resolvers[key] = hit
        return hit

    def width(self, text, span_style):
        if not text:
            return 0.0
        key = (text, span_style.key())
        hit = self._widths.get(key)
        if hit is None:
            from ..mpvtk import pilfont
            try:
                hit = float(pilfont.length(text, self.font(span_style), faces=self.faces(span_style)))
            except (AttributeError, OSError, ValueError):
                hit = len(text) * self.size_for(span_style) * 0.5
            if len(self._widths) > 80000:
                self._widths.clear()
            self._widths[key] = hit
        return hit

    def _scripts_of(self, text):
        """The set of scripts in ``text``, cached per string.

        `_place_line` asks every token on a line for both its height and
        its ascent, so this runs twice per token over a whole chapter --
        and pagination is most of the time opening a book takes. Bounded
        the same way `_widths` is, and for the same reason.
        """
        hit = self._scripts.get(text)
        if hit is None:
            from ..mpvtk import pilfont
            hit = frozenset((script for script, _chunk in pilfont.runs(text)))
            if len(self._scripts) > 80000:
                self._scripts.clear()
            self._scripts[text] = hit
        return hit

    def _metrics(self, span_style, text=None):
        """``(ascent, descent)`` for a line, over every face it needs.

        ``text`` is what makes this per line rather than per style, and it
        has to be: NotoSansCJK is 25/7 at 21px against DejaVuSerif's 20/5
        (measured), so a line carrying a Japanese word inside an English
        book overlaps the line below it if the band is reserved from the
        serif alone. A line with nothing but the book's own script gets
        exactly the answer it got before, from the same cache entry --
        which is what keeps an all-English chapter paying nothing for this.
        """
        from . import fonts
        scripts = frozenset({self.script})
        if text:
            scripts |= self._scripts_of(text)
        key = (span_style.key(), scripts)
        hit = self._metrics_cache.get(key)
        if hit is None:
            pairs = [fonts.metrics(self._face(span_style, script)) for script in scripts]
            hit = (max((a for a, _d in pairs)), max((d for _a, d in pairs)))
            self._metrics_cache[key] = hit
        return hit

    def line_height(self, span_style, text=None):
        ascent, descent = self._metrics(span_style, text)
        return int(round((ascent + descent) * self.style.line_spacing))

    def ascent(self, span_style, text=None):
        return self._metrics(span_style, text)[0]

class Piece:
    """A run of text placed on a line, ready to draw."""
    __slots__ = ('x', 'text', 'style', 'dy')

    def __init__(self, x, text, style, dy=0):
        self.x = x
        self.text = text
        self.style = style
        self.dy = dy

class Line:
    __slots__ = ('y', 'height', 'ascent', 'pieces', 'char_offset', 'block')

    def __init__(self, y, height, ascent, pieces, char_offset, block=None):
        self.y = y
        self.height = height
        self.ascent = ascent
        self.pieces = pieces
        self.char_offset = char_offset
        self.block = block

    def text(self):
        """The runs on this line, concatenated.

        **Not readable text**: a space is not a run, so words come out
        joined. It is what the line-breaking tests assert against. Anything
        that wants text a person will read wants the *block* — see
        ``book.page_text`` and ``docs/readers.md`` §4.9.
        """
        return ''.join((p.text for p in self.pieces))

class ImageItem:
    __slots__ = ('x', 'y', 'w', 'h', 'src', 'alt', 'char_offset')

    def __init__(self, x, y, w, h, src, alt='', char_offset=0):
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.src = src
        self.alt = alt
        self.char_offset = char_offset

class RuleItem:
    __slots__ = ('x', 'y', 'w', 'char_offset')

    def __init__(self, x, y, w, char_offset=0):
        self.x = x
        self.y = y
        self.w = w
        self.char_offset = char_offset

class Page:
    """One screenful of a spine document."""
    __slots__ = ('spine_index', 'number', 'items', 'start_offset', 'end_offset')

    def __init__(self, spine_index, number, items, start_offset, end_offset):
        self.spine_index = spine_index
        self.number = number
        self.items = items
        self.start_offset = start_offset
        self.end_offset = end_offset

    def __repr__(self):
        return '<Page %d.%d %d items @%d>' % (self.spine_index, self.number, len(self.items), self.start_offset)

class _Flow:
    """Fills pages with items, and knows when to start a new one."""

    def __init__(self, spine_index, width, height):
        self.spine_index = spine_index
        self.width = width
        self.height = height
        self.pages = []
        self._items = []
        self._y = 0
        self._start = 0
        self._end = 0
        self._pending_space = 0

    def space(self, amount):
        """Vertical space before the next item, collapsed CSS-style.

        Collapsing rather than summing is what stops a paragraph's bottom
        margin and the next one's top margin adding up to a blank line the
        author did not ask for — the same rule the web applies, and books
        are styled against the web.
        """
        if self._items:
            self._pending_space = max(self._pending_space, amount)

    def _take(self, height):
        """Reserve ``height`` px, breaking the page first if it will not
        fit. Returns the y to place at."""
        gap = self._pending_space if self._items else 0
        if self._items and self._y + gap + height > self.height:
            self.flush()
            gap = 0
        self._pending_space = 0
        y = self._y + gap
        self._y = y + height
        return y

    def add_line(self, line):
        line.y = self._take(line.height)
        self._place(line, line.char_offset)

    def add_lines(self, lines):
        """Place several lines that may not be split across a page.

        One `_take` for the lot: the group either fits below what is
        already on the page or the page breaks before any of it. Used for
        the lines a drop capital spans, which are a single piece of
        typography however many lines it is.
        """
        if not lines:
            return
        total = sum((line.height for line in lines))
        top = self._take(total)
        for line in lines:
            line.y = top
            top += line.height
            self._place(line, line.char_offset)

    def add_item(self, item, height):
        item.y = self._take(height)
        self._place(item, item.char_offset)

    def _place(self, item, char_offset):
        if not self._items:
            self._start = char_offset
        self._items.append(item)
        self._end = max(self._end, char_offset)

    def break_page(self):
        """Force a page break (``page-break-before``)."""
        if self._items:
            self.flush()

    def flush(self):
        if not self._items:
            return
        self.pages.append(Page(self.spine_index, len(self.pages), self._items, self._start, self._end))
        self._items = []
        self._y = 0
        self._pending_space = 0

    def finish(self):
        self.flush()
        if not self.pages:
            self.pages.append(Page(self.spine_index, 0, [], 0, 0))
        return self.pages

def paginate(blocks, width, height, measurer, image_size=None, spine_index=0):
    """Lay ``blocks`` out into pages of ``width`` x ``height`` pixels.

    ``image_size(src)`` returns an image's ``(w, h)`` in pixels, or None if
    it cannot be read — a picture whose size is unknown is skipped rather
    than guessed at, because a guess that is wrong pushes every page
    boundary after it.
    """
    style = measurer.style
    em = style.font_px
    flow = _Flow(spine_index, width, height)
    previous_after = 0.0
    for block in blocks:
        if block.page_break:
            flow.break_page()
        indent = int(block.indent * em)
        avail = max(em, width - indent - int(block.indent_right * em))
        before, after = _block_space(block, style)
        flow.space(int(max(before, previous_after) * em))
        previous_after = after
        if block.kind == RULE:
            flow.add_item(RuleItem(indent + avail // 8, 0, avail - avail // 4, block.char_offset), max(2, em // 6) * 2)
            continue
        if block.kind == IMAGE:
            _place_image(flow, block, indent, avail, height, em, image_size)
            continue
        lines = _wrap(block, avail, measurer, indent)
        if block.dropcap:
            flow.add_lines(lines[:_dropcap_lines(block, measurer)])
            lines = lines[_dropcap_lines(block, measurer):]
        for line in lines:
            flow.add_line(line)
    return flow.finish()

def _block_space(block, style):
    """``(before, after)`` in ems for one block, honouring its CSS."""
    default = DEFAULT_PARA_SPACE
    if block.kind == HEADING:
        default = DEFAULT_HEADING_SPACE
    elif block.marker:
        default = 0.2
    elif style.paragraph_indent:
        default = 0.0
    before = default if block.space_before is None else block.space_before
    after = default if block.space_after is None else block.space_after
    return (before, after)

def _px(length, avail, em):
    """A declared ``(value, is_fraction)`` in pixels."""
    value, fraction = length
    return value * avail if fraction else value * em

def _image_size(box, natural_w, natural_h, avail, em):
    """How big to draw an image: what the book asked for, or the file.

    Naming one side keeps the aspect ratio; naming both is taken at its
    word (``docs/readers.md`` §4.7). The caps go on last and re-derive the
    other side, so ``max-height: 1.2em`` on a tall icon narrows it rather
    than squashing it.
    """
    width = height = None
    if 'width' in box:
        width = _px(box['width'], avail, em)
    if 'height' in box:
        height = _px(box['height'], avail, em)
    ratio = natural_h / float(natural_w)
    if width is None and height is None:
        width, height = (float(natural_w), float(natural_h))
    elif width is None:
        width = height / ratio
    elif height is None:
        height = width * ratio
    if 'max-width' in box:
        cap = _px(box['max-width'], avail, em)
        if width > cap:
            height *= cap / width
            width = cap
    if 'max-height' in box:
        cap = _px(box['max-height'], avail, em)
        if height > cap:
            width *= cap / height
            height = cap
    return (int(round(width)), int(round(height)))

def _place_image(flow, block, indent, avail, page_height, em, image_size):
    size = image_size(block.src) if image_size else None
    if not size or not size[0] or (not size[1]):
        return
    natural_w, natural_h = size
    width, height = _image_size(block.box, natural_w, natural_h, avail, em)
    if width < 1 or height < 1:
        return
    if width > avail:
        height = max(1, int(height * avail / float(width)))
        width = avail
    if height > page_height:
        scale = page_height / float(height)
        width = int(width * scale)
        height = page_height
    x = indent
    if block.align in ('center', ''):
        x = indent + (avail - width) // 2
    elif block.align == 'right':
        x = indent + avail - width
    flow.add_item(ImageItem(x, 0, width, height, block.src, block.alt, block.char_offset), height)

class _Token:
    __slots__ = ('text', 'style', 'space', 'width', 'offset', 'hard')

    def __init__(self, text, style, space, width, offset, hard=False):
        self.text = text
        self.style = style
        self.space = space
        self.width = width
        self.offset = offset
        self.hard = hard

def _tokenize(block, measurer, spans=None):
    """Spans -> tokens, one per word, space or CJK character.

    ``spans`` overrides the block's own, which is how a drop capital stays
    out of the line breaker: it is not the first word of the first line, it
    is a shape the first few lines are set around.
    """
    tokens = []
    for span in block.spans if spans is None else spans:
        text = span.text
        if not text:
            continue
        base = span.char_offset
        pos = 0
        if block.pre:
            for i, chunk in enumerate(text.split('\n')):
                if i:
                    tokens.append(_Token('', span.style, False, 0.0, base + pos, hard=True))
                    pos += 1
                drawn = chunk.expandtabs(4)
                if drawn:
                    tokens.append(_Token(drawn, span.style, False, measurer.width(drawn, span.style), base + pos))
                pos += len(chunk)
            continue
        for piece, is_space, hard in _split_words(text):
            if hard:
                tokens.append(_Token('', span.style, False, 0.0, base + pos, hard=True))
                pos += 1
                continue
            tokens.append(_Token(piece, span.style, is_space, measurer.width(piece, span.style), base + pos))
            pos += len(piece)
    return tokens

def _split_words(text):
    """``"a b"`` -> ``[("a", False, False), (" ", True, False), …]``.

    CJK gets one token per character, because there are no spaces to break
    at and a paragraph of Japanese would otherwise be one unbreakable
    token as wide as the chapter.
    """
    out = []
    buffer = []
    for char in text:
        if char == '\n':
            if buffer:
                out.append((''.join(buffer), buffer[0].isspace(), False))
                buffer = []
            out.append(('', False, True))
            continue
        if _is_cjk(char):
            if buffer:
                out.append((''.join(buffer), buffer[0].isspace(), False))
                buffer = []
            out.append((char, False, False))
            continue
        if buffer and buffer[0].isspace() != char.isspace():
            out += _hyphen_split(''.join(buffer), buffer[0].isspace())
            buffer = []
        buffer.append(char)
    if buffer:
        out += _hyphen_split(''.join(buffer), buffer[0].isspace())
    return out
_BREAK_AFTER = '-–—/'

def _hyphen_split(word, is_space):
    """``"shrink-wrapped"`` -> ``["shrink-", "wrapped"]``.

    Without this, an already-hyphenated compound is one unbreakable token,
    and a greedy breaker that cannot place it leaves the line before it
    short. On a justified page that short line either stretches visibly or
    (worse) gives up and sets ragged in the middle of a paragraph, which
    reads as a rendering fault rather than as typography. Breaking at a
    hyphen the author already wrote is the one hyphenation that needs no
    dictionary and can never be wrong.

    Both halves must be worth having: splitting "e-mail" after one letter
    saves nothing and looks like a mistake.
    """
    if is_space or len(word) < 6 or (not any((c in word for c in _BREAK_AFTER))):
        return [(word, is_space, False)]
    parts = []
    start = 0
    for i, char in enumerate(word):
        if char in _BREAK_AFTER and i - start >= 2 and (len(word) - i - 1 >= 3):
            parts.append((word[start:i + 1], False, False))
            start = i + 1
    if not parts:
        return [(word, is_space, False)]
    if start < len(word):
        parts.append((word[start:], False, False))
    return parts

def _dropcap_lines(block, measurer):
    """How many lines a drop capital covers.

    From the *measured* height of the letter against the height of a line,
    not from the CSS size: the two are set independently (a 3em capital in
    a book with loose leading covers fewer lines than the same capital set
    solid), and a number picked from the size alone leaves either a band of
    white beside the letter or a letter hanging below the text it belongs
    to. Capped at four, so a stylesheet with an absurd size costs one odd
    capital rather than a page nothing else fits on.
    """
    if not block.spans:
        return 1
    cap = block.spans[0]
    body = block.spans[1].style if len(block.spans) > 1 else cap.style.with_(scale=1.0)
    line_h = max(1, measurer.line_height(body))
    covered = measurer.ascent(cap.style) / float(line_h)
    return max(1, min(int(covered) + (1 if covered % 1 else 0), 4))

def _wrap(block, avail, measurer, indent):
    """Break one text block into placed :class:`Line`\\ s (y unset)."""
    style = measurer.style
    em = style.font_px
    cap = block.dropcap_span()
    tokens = _tokenize(block, measurer, block.spans[1:] if cap is not None else None)
    if cap is not None and (not tokens):
        cap = None
        tokens = _tokenize(block, measurer)
    if not tokens:
        return []
    align = block.align or ('justify' if style.justify else 'left')
    if block.kind == HEADING and (not block.align):
        align = 'left'
    first_indent = block.first_indent
    if first_indent is None:
        first_indent = 0.0 if block.kind == HEADING or block.marker or (not style.paragraph_indent) else 1.2 if not block.indent else 0.0
    marker_style = block.spans[0].style if block.spans else None
    gutter, cap_lines = (0, 0)
    if cap is not None:
        first_indent = 0.0
        gutter = int(measurer.width(cap.text.strip(), cap.style) + em * 0.14)
        cap_lines = _dropcap_lines(block, measurer)

    def start_of(index):
        return gutter if index < cap_lines else 0
    lines = []
    current = []
    width_used = 0.0
    line_start = int(first_indent * em) + start_of(0)
    for token in tokens:
        if token.hard:
            lines.append((current, width_used, line_start, True))
            current, width_used = ([], 0.0)
            line_start = start_of(len(lines))
            continue
        if token.space and (not current):
            continue
        if current and width_used + token.width > avail - line_start and (not token.space):
            if _may_break_before(token, current):
                lines.append((current, width_used, line_start, False))
                current, width_used = ([], 0.0)
                line_start = start_of(len(lines))
        current.append(token)
        width_used += token.width
    if current:
        lines.append((current, width_used, line_start, True))
    out = []
    for tokens_on_line, used, start_x, last in lines:
        while tokens_on_line and tokens_on_line[-1].space:
            used -= tokens_on_line[-1].width
            tokens_on_line.pop()
        if not tokens_on_line:
            blank = block.spans[0].style if block.spans else None
            if blank is not None:
                out.append(Line(0, measurer.line_height(blank), measurer.ascent(blank), [], block.char_offset, block))
            continue
        out.append(_place_line(tokens_on_line, used, start_x + indent, indent + avail, align, last, measurer, block))
    if out and cap is not None:
        _add_dropcap(out[0], cap, indent, measurer)
    if out and block.marker and (marker_style is not None):
        _add_marker(out[0], block.marker, indent, measurer, marker_style)
    return out

def _add_dropcap(line, cap, indent, measurer):
    """Hang the capital in the gutter the lines were set around.

    Its top is aligned with the top of the first line rather than its
    baseline with the last line's: the two are the same thing only when the
    letter happens to be an exact multiple of the leading, and aligning the
    top is the one that never leaves a band of white above the capital.
    """
    dy = measurer.ascent(cap.style) - line.ascent
    line.pieces.insert(0, Piece(indent, cap.text.strip(), cap.style, dy))

def _may_break_before(token, current):
    """Whether a line may end just before ``token``.

    Two rules, both about CJK: a closing bracket or full stop may not start
    a line, and a line may not be broken so early that it is empty.
    """
    if not current:
        return False
    return not (token.text and token.text[0] in _NO_LINE_START)

def _place_line(tokens, used, start_x, limit, align, last, measurer, block=None):
    """Place one line's runs between ``start_x`` and ``limit``.

    ``limit`` is the **absolute** right edge, never the block's measure —
    the two agree only for a block at the left margin, and using the measure
    sets every indented block (list item, quotation, verse) ragged one
    indent short. See ``docs/readers.md`` §4.8.
    """
    height = max((measurer.line_height(t.style, t.text) for t in tokens))
    ascent = max((measurer.ascent(t.style, t.text) for t in tokens))
    slack = max(0.0, limit - start_x - used)
    x = float(start_x)
    stretch = 0.0
    if align == 'right':
        x += slack
    elif align == 'center':
        x += slack / 2.0
    elif align == 'justify' and (not last):
        spaces = [t for t in tokens if t.space]
        if spaces:
            per = slack / len(spaces)
            if per <= MAX_JUSTIFY_STRETCH * max(1.0, sum((t.width for t in spaces)) / len(spaces)):
                stretch = per
    em = measurer.style.font_px
    pieces = []
    for token in tokens:
        if token.space:
            x += token.width + stretch
            continue
        dy = -int(round(token.style.rise * em)) if token.style.rise else 0
        pieces.append(Piece(int(round(x)), token.text, token.style, dy))
        x += token.width
    return Line(0, height, ascent, pieces, tokens[0].offset if tokens else 0, block)

def _add_marker(line, marker, indent, measurer, style):
    """Hang a list marker in the gutter to the left of the first line."""
    width = measurer.width(marker + ' ', style)
    x = max(0, indent - int(width))
    line.pieces.insert(0, Piece(x, marker, style))

"""epub.js's locations index, reimplemented so our position means theirs.

Jellyfin stores an epub's position as ``location / total`` over **epub.js's
locations index** — not "how far through the text you are" in any sense you
could derive from first principles, and any other reading of the number
disagrees with every other client. See ``docs/readers.md`` §4.3.

This is a port of *behaviour*, written against
``node_modules/epubjs/src/locations.js``: no CFIs, no `EpubCFI`, no DOM.
Each linear spine document is cut into runs of ~1024 characters of text and
the runs are counted. The four details that make the numbers agree, and
each of which fails silently when it is wrong:

1. **The counter resets at every section boundary, and each section closes
   its partial tail as a whole location.** This is the big divergence from
   ``chars_read / chars_total``: a book of many short sections inflates its
   total, one long novel barely at all.
2. **A run that starts inside a text node consumes 1025 characters, not
   1024** — ``dist`` is computed before the ``counter === 0`` branch does
   ``pos += 1``. A run continued from the previous node consumes exactly
   ``dist``.
3. **A text node that is entirely whitespace counts zero; any other counts
   its full length, whitespace included.**
4. **``total`` is ``len(locations) - 1``**, which puts the denominator one
   short. It only matters for a short book, where it matters a lot.

Non-linear spine items (``linear="no"`` — covers, footnote documents) are
excluded from the index entirely, as they are in epub.js, which walks only
sections whose ``linear`` is truthy.

**Granularity is absolute, not relative**: ~1024 characters per step in
every book, so a short story steps in 5% and a novel in 0.17%.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import bisect
import logging
log = logging.getLogger('epub.locations')
BREAK_CHARS = 1024

class SectionLocations:
    """Where the location boundaries fall inside one spine document."""
    __slots__ = ('spine_index', 'chars', 'ends', 'first_location')

    def __init__(self, spine_index, chars, ends, first_location=0):
        self.spine_index = spine_index
        self.chars = chars
        self.ends = ends
        self.first_location = first_location

    @property
    def count(self):
        return len(self.ends)

class LocationIndex:
    """The whole book's index: the thing a position is measured against."""

    def __init__(self, sections):
        self.sections = sections
        location = 0
        for section in sections:
            section.first_location = location
            location += section.count
        self.count = location
        self.total = max(0, location - 1)
        self._by_spine = {s.spine_index: s for s in sections}

    def location_of(self, spine_index, char_offset):
        """Global location index for a position in the book.

        ``char_offset`` is in *counted* characters within that spine
        document — the number :mod:`.content` puts on every span, which is
        why the two walks must stay in step.
        """
        section = self._by_spine.get(spine_index)
        if section is None:
            return self._preceding_location(spine_index)
        local = bisect.bisect_right(section.ends, char_offset)
        return section.first_location + min(local, section.count - 1)

    def _preceding_location(self, spine_index):
        best = 0
        for section in self.sections:
            if section.spine_index < spine_index:
                best = section.first_location + section.count - 1
        return best

    def fraction(self, spine_index, char_offset):
        """Position as the fraction Jellyfin stores. 0.0 when unknown."""
        if not self.total:
            return 0.0
        location = self.location_of(spine_index, char_offset)
        return max(0.0, min(location / float(self.total), 1.0))

    def position_of(self, fraction):
        """``(spine_index, char_offset)`` for a stored fraction.

        Rounds **forward**, as jellyfin-web's ``cfiFromPercentage`` does
        (``Math.ceil(total * p)``): reopening a book at or just past where
        you stopped is a re-read of at most a sentence, while rounding back
        can drop you a page behind and, on a book read to the end, never
        let you reach it.
        """
        if not self.sections:
            return (0, 0)
        target = 0
        if self.total:
            import math
            target = int(math.ceil(max(0.0, min(fraction, 1.0)) * self.total))
        target = max(0, min(target, self.count - 1))
        for section in self.sections:
            if target < section.first_location + section.count:
                local = target - section.first_location
                offset = section.ends[local - 1] if local > 0 else 0
                return (section.spine_index, offset)
        last = self.sections[-1]
        return (last.spine_index, last.ends[-1] if last.ends else 0)

    def to_json(self):
        return {'v': 1, 'break': BREAK_CHARS, 'sections': [[s.spine_index, s.chars, s.ends] for s in self.sections]}

    @classmethod
    def from_json(cls, data):
        if not isinstance(data, dict) or data.get('v') != 1:
            raise ValueError('unknown locations index format')
        if data.get('break') != BREAK_CHARS:
            raise ValueError('locations index built with a different break')
        return cls([SectionLocations(spine, chars, list(ends)) for spine, chars, ends in data['sections']])

def count_section(lengths, brk=BREAK_CHARS):
    """Location boundaries for one document, from its text-node lengths.

    ``lengths`` is every text node's length in document order, with
    whitespace-only nodes already dropped (rule 3). Returns
    ``(chars, ends)``.

    This is epub.js's ``Locations.parse`` with the CFI machinery removed:
    the loop below is its loop, including the ``pos += 1`` that makes a
    freshly started run cost one extra character. Do not "simplify" it into
    ``chars // brk`` — that is a different function, off by a location
    every ~1024 characters and by more at every node boundary.
    """
    ends = []
    counter = 0
    consumed = 0
    for length in lengths:
        if length <= 0:
            continue
        base = consumed
        consumed += length
        pos = 0
        dist = brk - counter
        if dist > length:
            counter += length
            continue
        while pos < length:
            dist = brk - counter
            if counter == 0:
                pos += 1
            if pos + dist >= length:
                counter += length - pos
                pos = length
            else:
                pos += dist
                ends.append(base + pos)
                counter = 0
    if consumed:
        ends.append(consumed)
    return (consumed, ends)

def text_node_lengths(markup):
    """Every counted text-node length in a document, in document order.

    Walks ``<body>`` only, because epub.js's ``sprint`` builds its tree
    walker on the body element. Script and style text *is* included: those
    are text nodes in the DOM that walker sees, and matching it is the
    entire point.
    """
    from . import xmlish
    root = xmlish.parse(markup)
    body = root.find('body') or root
    out = []
    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if node.strip():
                out.append(utf16_len(node))
            continue
        for child in reversed(node.children):
            stack.append(child)
    return out

def utf16_len(text):
    """Length as epub.js measures it: **UTF-16 code units**.

    ``node.length`` in the DOM is specified in UTF-16 units, so an emoji or
    a CJK Extension-B ideograph costs two there and one to Python's
    ``len``. A book with a thousand of them counts a thousand short here,
    and the fraction written for a position disagrees with the number
    jellyfin-web would have written for the same place.

    The error largely cancels between a location and the total, so this is
    a small correction — but it is free, and it is the difference between
    a port that agrees with its original and one that nearly does.
    """
    return len(text) + sum((1 for ch in text if ord(ch) > 65535))

def build(package, progress=None):
    """Build the index for a whole book. Reads every linear spine document.

    ``progress(done, total)`` is called after each document, for a UI that
    wants to say what it is doing — this is the one part of opening a book
    that takes a noticeable moment, because it is the only part that has to
    read all of it.
    """
    from .archive import EpubError
    linear = [(i, item) for i, item in enumerate(package.spine) if item.linear]
    sections = []
    for done, (index, _item) in enumerate(linear, 1):
        try:
            lengths = text_node_lengths(package.doc_bytes(index))
        except EpubError:
            log.info('spine %d unreadable; excluded from the index', index, exc_info=True)
            lengths = []
        chars, ends = count_section(lengths)
        if ends:
            sections.append(SectionLocations(index, chars, ends))
        if progress is not None:
            progress(done, len(linear))
    return LocationIndex(sections)

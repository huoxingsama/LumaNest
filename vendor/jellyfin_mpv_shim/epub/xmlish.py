"""One tolerant markup reader, used for every file inside an epub.

An epub is four kinds of markup — `META-INF/container.xml`, the OPF package
document, the EPUB 3 nav document, and the XHTML of the book itself — and
all four are read with :class:`html.parser.HTMLParser`, never with an XML
parser. Two grounds, both in ``docs/readers.md`` §4.1: expat expands
internal entities and this file came off a media server; and real epubs are
not well-formed, while an XML parser is *required* to stop dead on each
malformation.

What this costs: no namespace resolution, no CDATA sections, no XML
validation. Namespaces are handled by matching on the *local* name
(``package``, ``item``, ``itemref``), which is what every epub in the wild
uses anyway and what an epub with a prefixed namespace needs regardless.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import time
from html.parser import HTMLParser
from .errors import EpubError
PARSE_TIMEOUT = 5.0
MAX_DEPTH = 200
_CLOCK_EVERY = 512

class ParseTimeout(EpubError):
    """A document took longer to parse than :data:`PARSE_TIMEOUT`.

    An :class:`~.errors.EpubError` on purpose: every caller that already
    knows how to skip an unreadable document skips this one too, so a
    hostile chapter costs that chapter and not the book.
    """
VOID_TAGS = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'})

class Node:
    """One element. Attributes are lowercased and namespace-stripped."""
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent

    def get(self, name, default=None):
        return self.attrs.get(name, default)

    def find_all(self, tag):
        """Every descendant with this local name, in document order."""
        out = []
        stack = [self]
        while stack:
            node = stack.pop()
            if node is not self and node.tag == tag:
                out.append(node)
            for child in reversed(node.children):
                if isinstance(child, Node):
                    stack.append(child)
        return out

    def find(self, tag):
        for node in self.find_all(tag):
            return node
        return None

    def text(self):
        """All descendant text, concatenated. Whitespace as written."""
        parts = []
        stack = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                parts.append(node)
                continue
            for child in reversed(node.children):
                stack.append(child)
        return ''.join(parts)

    def __repr__(self):
        return '<%s %r %d children>' % (self.tag, self.attrs, len(self.children))

def _local(name):
    """``opf:item`` -> ``item``. See the module docstring on namespaces."""
    return name.rsplit(':', 1)[-1].lower()

class _Builder(HTMLParser):

    def __init__(self, deadline=None):
        super().__init__(convert_charrefs=True)
        self.root = Node('#document')
        self._stack = [self.root]
        self._deadline = deadline
        self._ticks = 0

    def _tick(self):
        """Give up if this document has had its time.

        Checked from the handlers rather than by feeding the parser in
        chunks, which is the obvious implementation and is a trap:
        `HTMLParser` keeps whatever it could not consume in `rawdata` and
        rescans it from the start on the next `feed`, so a document with an
        unterminated tag near the top becomes quadratic in the number of
        chunks. Measured on 800 KB: 53 ms in one call, 401 ms in 64 KB
        chunks, 2419 ms in 8 KB chunks. One `feed` is both faster and
        safer, and this is how it is still interruptible.

        The residue is a document that produces *no* handler calls at all,
        which cannot be interrupted from here. That is bounded rather than
        open-ended: the worst such input found was 627 ms for 2 MB, against
        an entry cap of 32 MB.
        """
        self._ticks += 1
        if self._deadline is None or self._ticks % _CLOCK_EVERY:
            return
        if time.monotonic() > self._deadline:
            raise ParseTimeout('this document took too long to read')

    def handle_starttag(self, tag, attrs):
        self._tick()
        tag = _local(tag)
        node = Node(tag, {_local(k): v if v is not None else '' for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS and len(self._stack) < MAX_DEPTH:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if _local(tag) not in VOID_TAGS:
            self._stack.pop()

    def handle_endtag(self, tag):
        self._tick()
        tag = _local(tag)
        if tag in VOID_TAGS:
            return
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data):
        self._tick()
        if data:
            self._stack[-1].children.append(data)

    def handle_decl(self, decl):
        """``<!DOCTYPE …>``, internal subset and all, as one string.

        Dropped without inspection. This is the hook expat would use to
        define entities; there is nothing here that could expand one.
        """

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

def parse(data, timeout=PARSE_TIMEOUT):
    """Parse markup into a :class:`Node` tree rooted at ``#document``.

    ``data`` may be `bytes` or `str`; bytes are decoded by
    :func:`decode`. Never raises on **malformed** input — that is the whole
    point of this parser — so callers check for the elements they need
    rather than catching. It raises :class:`ParseTimeout` on input that is
    *hostile* rather than merely broken; ``timeout=None`` disables that.
    """
    if isinstance(data, (bytes, bytearray)):
        data = decode(data)
    if '\r' in data:
        data = data.replace('\r\n', '\n').replace('\r', '\n')
    builder = _Builder(None if timeout is None else time.monotonic() + timeout)
    builder.feed(data)
    builder.close()
    return builder.root
_XML_DECL = re.compile(b'<\\?xml[^>]*encoding\\s*=\\s*["\']([\\w.-]+)["\']', re.I)
_META_CHARSET = re.compile(b'<meta[^>]*charset\\s*=\\s*["\']?([\\w.-]+)', re.I)

def decode(data):
    """Bytes -> str, by the encoding the document declares.

    UTF-8 is the epub specification's answer and is what almost every file
    is, but "almost" is doing work: hand-built and converted books ship
    Windows-1252 and Latin-1 regularly. The order is declared encoding, then
    UTF-8, then cp1252 — and cp1252 last because it maps every byte, so it
    can never fail and would mask the two answers that are usually right.
    """
    if data[:3] == b'\xef\xbb\xbf':
        return data[3:].decode('utf-8', 'replace')
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try:
            return data.decode('utf-16')
        except (UnicodeDecodeError, LookupError):
            pass
    head = data[:1024]
    match = _XML_DECL.search(head) or _META_CHARSET.search(head)
    encodings = []
    if match:
        try:
            encodings.append(match.group(1).decode('ascii').lower())
        except UnicodeDecodeError:
            pass
    encodings += ['utf-8', 'cp1252']
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode('utf-8', 'replace')

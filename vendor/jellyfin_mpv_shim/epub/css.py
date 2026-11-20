"""Enough CSS to know a chapter title when it sees one.

Published epubs contain no ``<h1>``: a chapter opener is
``<p class="chaptertitle">`` plus a stylesheet, so without a stylesheet
reader "support headings" delivers headings for hand-written epubs and a
wall of identical paragraphs for the published ones — see
``docs/readers.md`` §4.2.

**What it is not.** There is no cascade in the full sense, no inheritance
engine, no computed-value pass, no layout properties. It answers one
question — *which declarations apply to this element* — for the
declarations this reader can draw, and it answers it with the two rules
that decide almost every real conflict: specificity, then source order.


Supported: type/class/id selectors, compounds (``p.chaptertitle``),
descendant and child combinators, selector lists, ``<style>`` blocks and
linked stylesheets. Ignored: pseudo-classes and elements, attribute
selectors, ``!important``, ``@media`` and every other at-rule, and the
sibling combinators. A rule this parser cannot understand is **dropped, not
guessed** — a mis-parsed selector applies a heading's size to a page of
body text, which is far more damaging than not applying it at all.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import re
log = logging.getLogger('epub.css')
USED_PROPERTIES = frozenset({'font-size', 'font-weight', 'font-style', 'font-family', 'text-align', 'text-decoration', 'display', 'visibility', 'text-indent', 'margin-top', 'margin-bottom', 'page-break-before', 'margin-left', 'margin-right', 'list-style-type', 'font-variant', 'font-variant-caps', 'vertical-align', 'width', 'height', 'max-width', 'max-height'})
MAX_CSS_BYTES = 2 * 1024 * 1024
_COMMENT = re.compile('/\\*.*?\\*/', re.S)
_AT_RULE = re.compile('@[\\w-]+[^{;]*(?:;|\\{)')
_DECL = re.compile('([\\w-]+)\\s*:\\s*([^;]+)')
_COMPOUND = re.compile('\n    (?P<tag>[*\\w-]+)?\n    (?P<rest>(?:[.#][\\w-]+)*)\n    $', re.X)

class Compound:
    """One simple-selector sequence: ``p.chaptertitle#x``."""
    __slots__ = ('tag', 'classes', 'id')

    def __init__(self, tag=None, classes=(), id_=None):
        self.tag = tag
        self.classes = frozenset(classes)
        self.id = id_

    def matches(self, node):
        if self.tag and self.tag != '*' and (node.tag != self.tag):
            return False
        if self.id and node.get('id') != self.id:
            return False
        if self.classes:
            have = set((node.get('class') or '').split())
            if not self.classes <= have:
                return False
        return True

    def specificity(self):
        return (1 if self.id else 0, len(self.classes), 1 if self.tag and self.tag != '*' else 0)

class Rule:
    __slots__ = ('parts', 'decls', 'spec', 'order', 'key')

    def __init__(self, parts, decls, order):
        self.parts = parts
        self.decls = decls
        a = b = c = 0
        for _combinator, compound in parts:
            spec = compound.specificity()
            a, b, c = (a + spec[0], b + spec[1], c + spec[2])
        self.spec = (a, b, c)
        self.order = order
        last = parts[-1][1]
        if last.id:
            self.key = ('#', last.id)
        elif last.classes:
            self.key = ('.', sorted(last.classes)[0])
        elif last.tag and last.tag != '*':
            self.key = ('e', last.tag)
        else:
            self.key = ('*', '')

    def matches(self, node):
        if not self.parts[-1][1].matches(node):
            return False
        current = node.parent
        for combinator, compound in reversed(self.parts[:-1]):
            if combinator == '>':
                if current is None or not compound.matches(current):
                    return False
                current = current.parent
                continue
            while current is not None and (not compound.matches(current)):
                current = current.parent
            if current is None:
                return False
            current = current.parent
        return True

class Stylesheet:
    """Parsed rules, indexed, with one method that matters: :meth:`match`."""

    def __init__(self):
        self._by_id = {}
        self._by_class = {}
        self._by_tag = {}
        self._universal = []
        self._count = 0

    def add(self, text):
        """Parse and append a stylesheet's text. Never raises."""
        try:
            for rule in _parse(text, self._count):
                self._count += 1
                kind, name = rule.key
                if kind == '#':
                    self._by_id.setdefault(name, []).append(rule)
                elif kind == '.':
                    self._by_class.setdefault(name, []).append(rule)
                elif kind == 'e':
                    self._by_tag.setdefault(name, []).append(rule)
                else:
                    self._universal.append(rule)
        except Exception:
            log.debug('stylesheet ignored', exc_info=True)

    def match(self, node):
        """Merged declarations for ``node``, weakest first.

        Returns a plain dict. The inline ``style`` attribute is *not*
        merged here — the content walker applies that afterwards, which is
        the right order and keeps this function about the sheet.
        """
        candidates = []
        node_id = node.get('id')
        if node_id:
            candidates += self._by_id.get(node_id, ())
        classes = (node.get('class') or '').split()
        for cls in classes:
            candidates += self._by_class.get(cls, ())
        candidates += self._by_tag.get(node.tag, ())
        candidates += self._universal
        if not candidates:
            return {}
        out = {}
        for rule in sorted(candidates, key=lambda r: (r.spec, r.order)):
            if rule.matches(node):
                out.update(rule.decls)
        return out

    def __len__(self):
        return self._count

def _parse(text, order_base):
    text = _COMMENT.sub(' ', text)
    rules = []
    i = 0
    order = order_base
    length = len(text)
    while i < length:
        at = _AT_RULE.match(text, i)
        if at:
            i = _skip_block(text, at.end()) if at.group(0).endswith('{') else at.end()
            continue
        brace = text.find('{', i)
        if brace < 0:
            break
        selector_text = text[i:brace]
        end = _skip_block(text, brace + 1)
        body = text[brace + 1:max(brace + 1, end - 1)]
        i = end
        decls = _declarations(body)
        if not decls:
            continue
        for selector in selector_text.split(','):
            parts = _selector(selector)
            if parts:
                rules.append(Rule(parts, decls, order))
                order += 1
    return rules

def _skip_block(text, i):
    """Index just past the ``}`` closing the block that starts at ``i``."""
    depth = 1
    while i < len(text) and depth:
        char = text[i]
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
        i += 1
    return i

def _declarations(body):
    out = {}
    for match in _DECL.finditer(body):
        name = match.group(1).lower()
        value = match.group(2).replace('!important', '').strip().lower()
        if name in SHORTHANDS:
            out.update(SHORTHANDS[name](value))
            continue
        if name not in USED_PROPERTIES:
            continue
        out[name] = value
    return out

def expand_margin(value):
    """``margin: 10px 12.5%`` -> the four longhands this reader reads.

    Books write the shorthand far more often than the longhands — one real
    title's stylesheet uses it 40 times against 32 — so dropping it means
    dropping most of what the book says about its own insets: a pull-quote
    or a verse block sets flush with the prose, which is exactly the
    failure `content._margin_em` exists to prevent.
    """
    parts = value.split()
    if not parts or len(parts) > 4:
        return {}
    if len(parts) == 1:
        top = right = bottom = left = parts[0]
    elif len(parts) == 2:
        top, right = parts
        bottom, left = (top, right)
    elif len(parts) == 3:
        top, right, bottom = parts
        left = right
    else:
        top, right, bottom, left = parts
    return {'margin-top': top, 'margin-right': right, 'margin-bottom': bottom, 'margin-left': left}

def expand_list_style(value):
    """``list-style: none`` -> ``list-style-type: none``.

    Only the type is read. The shorthand's other two slots (position and
    image) say nothing this reader draws, and a keyword it does not know is
    left alone rather than guessed at — same rule as an unparseable
    selector.
    """
    known = {'none', 'disc', 'circle', 'square', 'decimal', 'lower-alpha', 'upper-alpha', 'lower-latin', 'upper-latin', 'lower-roman', 'upper-roman'}
    for part in value.split():
        if part in known:
            return {'list-style-type': part}
    return {}
SHORTHANDS = {'margin': expand_margin, 'list-style': expand_list_style}

def _selector(selector):
    """``"div.story > p.h1"`` -> ``[(" ", Compound), (">", Compound)]``.

    Returns None for anything with a construct this module does not
    implement, so the rule is dropped rather than half-applied.
    """
    selector = selector.strip()
    if not selector or any((c in selector for c in ':[]()+~')):
        return None
    parts = []
    combinator = ' '
    for token in selector.replace('>', ' > ').split():
        if token == '>':
            combinator = '>'
            continue
        compound = _compound(token)
        if compound is None:
            return None
        parts.append((combinator, compound))
        combinator = ' '
    return parts or None

def _compound(token):
    match = _COMPOUND.match(token)
    if match is None:
        return None
    tag = (match.group('tag') or '').lower() or None
    classes = []
    element_id = None
    rest = match.group('rest') or ''
    for piece in re.findall('[.#][\\w-]+', rest):
        if piece[0] == '.':
            classes.append(piece[1:])
        else:
            element_id = piece[1:]
    return Compound(tag, classes, element_id)

def sheet_for(package, doc_href, root, cache):
    """The stylesheet applying to one spine document.

    ``cache`` is a dict owned by the caller, keyed by stylesheet path: a
    book has two or three sheets shared by every chapter, and re-parsing
    them per document is most of the cost of opening one.
    """
    from .archive import EpubError
    sheet = Stylesheet()
    for link in root.find_all('link'):
        rel = (link.get('rel') or '').lower()
        media_type = (link.get('type') or '').lower()
        if 'stylesheet' not in rel and media_type != 'text/css':
            continue
        href = package.resolve(doc_href, link.get('href'))
        if not href:
            continue
        if href not in cache:
            try:
                cache[href] = package.archive.read_text(href, MAX_CSS_BYTES)
            except EpubError:
                log.debug('stylesheet %s unreadable', href, exc_info=True)
                cache[href] = ''
        sheet.add(cache[href])
    for style in root.find_all('style'):
        sheet.add(style.text()[:MAX_CSS_BYTES])
    return sheet
_LENGTH = re.compile('^([+-]?[\\d.]+)\\s*(em|rem|ex|%|px|pt|pc|in|cm|mm)?$')
_ABSOLUTE_EM = {'px': 1 / 16.0, 'pt': 1 / 12.0, 'pc': 1.0, 'in': 6.0, 'cm': 2.3622, 'mm': 0.23622}
_KEYWORD_SIZE = {'xx-small': 0.6, 'x-small': 0.75, 'small': 0.89, 'medium': 1.0, 'large': 1.2, 'x-large': 1.5, 'xx-large': 2.0, 'smaller': 0.83, 'larger': 1.2}

def font_scale(value, parent_scale=1.0):
    """``font-size`` -> a multiple of the reader's body size, or None.

    Relative units multiply the parent's scale (which is what ``em`` and
    ``%`` mean); absolute ones do not, because an author who wrote ``14pt``
    was describing a size, not a relationship.
    """
    if not value:
        return None
    value = value.strip()
    if value in _KEYWORD_SIZE:
        keyword = _KEYWORD_SIZE[value]
        return parent_scale * keyword if value in ('smaller', 'larger') else keyword
    match = _LENGTH.match(value)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    unit = match.group(2) or ''
    if unit in ('em', 'rem', 'ex'):
        factor = number * (0.5 if unit == 'ex' else 1.0)
        return parent_scale * factor if unit != 'rem' else factor
    if unit == '%':
        return parent_scale * number / 100.0
    if unit in _ABSOLUTE_EM:
        return number * _ABSOLUTE_EM[unit]
    return None

def length_em(value):
    """A length in ems of the body size, for indents and margins."""
    scale = font_scale(value, 1.0)
    return scale if scale is not None else None

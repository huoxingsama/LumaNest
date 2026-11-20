"""Script-aware PIL font resolution for baked bitmaps.

**Pillow has no font fallback**: one TrueType face draws the whole string and
anything it lacks renders as tofu (□□□). libass, which draws the ASS half
of the UI, does its own fontconfig fallback — so only text *baked into
bitmaps* (tile captions in mpvtk_browser.strips, the display mirror's title
block) needs any of this. CJK library titles hit it immediately.

So we pick the face per string: scan for the first character outside the
Latin/Cyrillic/Greek range our default face covers, map it to a script, and
load a system font known to cover that script. Everything is cached, and a
miss degrades to the default face (tofu, but never a crash).

Three granularities, because each one was a bug: **per string** (this
module), **per run** of one script (:func:`runs`, so a year beside a Japanese
title is not four boxes) and **per character** (:func:`_peel`, so a Roman
numeral in a Chinese title is not one box -- #740, and mpvtk/GUIDE.md
section 12.9).

Two pseudo-scripts, neither of which is one anywhere else. **"symbol"**,
because a Latin face is not a symbol face (:data:`_SYMBOL_RANGES`). **"emoji"**,
the only one where the *face* is awkward rather than the choice of it: it draws
in its own colours (``embedded_color``) and is very often available at one
fixed pixel size and no other, which is what :data:`_STRIKES` and
:func:`_draw_scaled` are for. A symbol face is not an emoji face either.

The measured evidence behind every table here — face coverage, the RTL
whole-line rule, colour-emoji strikes, shaping and the lookup cost — is in
mpvtk/GUIDE.md section 12.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import os
import unicodedata
log = logging.getLogger('mpvtk.pilfont')
_CANDIDATES = {'latin': ['DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 'NotoSans-Regular.ttf', 'Arial.ttf', 'arial.ttf'], 'cjk': ['NotoSansCJK-Regular.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf', '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf', '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc', '/System/Library/Fonts/Hiragino Sans GB.ttc', '/System/Library/Fonts/PingFang.ttc', 'msgothic.ttc', 'meiryo.ttc', 'YuGothM.ttc', 'msyh.ttc', 'msjh.ttc', 'simsun.ttc', 'malgun.ttf'], 'arabic': ['NotoSansArabic-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf', '/System/Library/Fonts/GeezaPro.ttc', 'FreeSerif.ttf', '/usr/share/fonts/truetype/freefont/FreeSerif.ttf', 'arial.ttf'], 'hebrew': ['LiberationSans-Regular.ttf', '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf', 'DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/System/Library/Fonts/Supplemental/Arial Hebrew.ttc', 'arial.ttf', 'NotoSansHebrew-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf'], 'devanagari': ['NotoSansDevanagari-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf', 'Nirmala.ttf', 'mangal.ttf'], 'thai': ['NotoSansThai-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf', 'LeelawUI.ttf', 'tahoma.ttf', 'FreeSerif.ttf', '/usr/share/fonts/truetype/freefont/FreeSerif.ttf'], 'indic': ['NotoSansBengali-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf', 'NotoSansGurmukhi-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansGurmukhi-Regular.ttf', 'NotoSansGujarati-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansGujarati-Regular.ttf', 'NotoSansOriya-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansOriya-Regular.ttf', 'NotoSansTamil-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf', 'NotoSansTelugu-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf', 'NotoSansKannada-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansKannada-Regular.ttf', 'NotoSansMalayalam-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansMalayalam-Regular.ttf', 'NotoSansSinhala-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf', 'Nirmala.ttf', 'FreeSerif.ttf', '/usr/share/fonts/truetype/freefont/FreeSerif.ttf'], 'lao': ['NotoSansLao-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansLao-Regular.ttf', 'LeelawUI.ttf', 'DejaVuSans.ttf'], 'tibetan': ['NotoSerifTibetan-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSerifTibetan-Regular.ttf', 'himalaya.ttf'], 'myanmar': ['NotoSansMyanmar-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf', 'mmrtext.ttf'], 'georgian': ['NotoSansGeorgian-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansGeorgian-Regular.ttf', 'DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 'sylfaen.ttf'], 'ethiopic': ['NotoSansEthiopic-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansEthiopic-Regular.ttf', 'ebrima.ttf', 'FreeSerif.ttf'], 'cherokee': ['NotoSansCherokee-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansCherokee-Regular.ttf', 'gadugi.ttf', 'FreeSans.ttf'], 'khmer': ['NotoSansKhmer-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansKhmer-Regular.ttf', 'LeelawUI.ttf'], 'mongolian': ['NotoSansMongolian-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansMongolian-Regular.ttf', 'monbaiti.ttf'], 'symbol': ['DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 'NotoSansSymbols2-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf', 'seguisym.ttf', '/System/Library/Fonts/Apple Symbols.ttf', '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'], 'emoji': ['seguiemj.ttf', 'NotoColorEmoji.ttf', '/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf', 'NotoColorEmoji-Regular.ttf', '/System/Library/Fonts/Apple Color Emoji.ttc', 'NotoEmoji-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf', 'Symbola.ttf', '/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf']}
_FALLBACK_SCRIPTS = {'emoji': ('symbol',), 'symbol': ('emoji',)}
_STRIKES = (16, 20, 26, 32, 40, 48, 52, 64, 96, 109, 128, 136, 160)
_BOLD = {'latin': ['DejaVuSans-Bold.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 'NotoSans-Bold.ttf', 'arialbd.ttf'], 'cjk': ['NotoSansCJK-Bold.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc']}
if os.name == 'nt':
    # Match the ASS metric face for both Chinese and Latin UI text. Keep the
    # script coverage fallback for Japanese/Korean characters this face lacks.
    for _script in ('latin', 'cjk'):
        _CANDIDATES[_script] = ['msyh.ttc'] + _CANDIDATES[_script]
        _BOLD[_script] = ['msyhbd.ttc'] + _BOLD[_script]
    _CANDIDATES['latin'] = ['msyh.ttc', 'segoeui.ttf', 'arial.ttf'] + _CANDIDATES['latin']
    _BOLD['latin'] = ['msyhbd.ttc', 'segoeuib.ttf', 'arialbd.ttf'] + _BOLD['latin']
_cache = {}
_chains = {}
_coverage = {}
_notdef = {}
_resolved = {}
_orphans = {}
_NOTDEF_PROBES = ('\U000fffff', '\U0010ffff', '\U000ffffe')
_SYMBOL_RANGES = ((8592, 8703), (8960, 9215), (9632, 9727), (9728, 9983), (9984, 10175), (11008, 11263))
_EMOJI_RANGES = ((8986, 8987), (9193, 9196), (9200, 9200), (9203, 9203), (9725, 9726), (9748, 9749), (9800, 9811), (9855, 9855), (9875, 9875), (9889, 9889), (9898, 9899), (9917, 9918), (9924, 9925), (9934, 9934), (9940, 9940), (9962, 9962), (9970, 9971), (9973, 9973), (9978, 9978), (9981, 9981), (9989, 9989), (9994, 9995), (10024, 10024), (10060, 10060), (10062, 10062), (10067, 10069), (10071, 10071), (10133, 10135), (10160, 10160), (10175, 10175), (11035, 11036), (11088, 11088), (11093, 11093), (126980, 126980), (127183, 127183), (127374, 127374), (127377, 127386), (127462, 127487), (127489, 127490), (127514, 127514), (127535, 127535), (127538, 127546), (127568, 127569), (127744, 128591), (128640, 128767), (128992, 129023), (129280, 129535), (129648, 129791))
_ASTRAL_SYMBOL_RANGES = ((126976, 127231), (128592, 128639), (128768, 128991), (129024, 129279), (129536, 129647), (129792, 130047))

def _codepoints(ranges):
    """A range table, flattened for lookup.

    The tables above stay ranges because that is the form a human can check
    against emoji-data.txt; this is the form the repaint path can afford,
    since `script_of_char` runs per character and `strips._ellipsize`
    re-measures a caption once per character while it trims. Measured at ~70x
    per character (mpvtk/GUIDE.md section 12.5); the three sets together are
    ~3,800 codepoints.
    """
    return frozenset((cp for lo, hi in ranges for cp in range(lo, hi + 1)))
_SYMBOL_CPS = _codepoints(_SYMBOL_RANGES)
_EMOJI_CPS = _codepoints(_EMOJI_RANGES)
_ASTRAL_SYMBOL_CPS = _codepoints(_ASTRAL_SYMBOL_RANGES)

def script_of_char(cp):
    """The script one codepoint needs a face for.

    Latin/Cyrillic/Greek and the punctuation blocks all map to "latin",
    which is the face that covers them. The symbol blocks do not (see
    :data:`_SYMBOL_RANGES`), and neither do the emoji ones (see
    :data:`_EMOJI_RANGES`) -- a symbol face is not an emoji face either.
    """
    if cp < 1424:
        return 'latin'
    if cp <= 1535 or 64285 <= cp <= 64335:
        return 'hebrew'
    if 1536 <= cp <= 1791 or 64336 <= cp <= 65023 or 65136 <= cp <= 65279:
        return 'arabic'
    if 2304 <= cp <= 2431:
        return 'devanagari'
    if 3584 <= cp <= 3711:
        return 'thai'
    if 2432 <= cp <= 6319:
        for lo, hi, script in _BAND_SCRIPTS:
            if lo <= cp <= hi:
                return script
    if cp in _EMOJI_CPS:
        return 'emoji'
    if cp >= 11904:
        return 'symbol' if cp in _ASTRAL_SYMBOL_CPS else 'cjk'
    if cp in _SYMBOL_CPS:
        return 'symbol'
    return 'latin'

def script_of(text):
    """The single script a string is drawn with when it cannot be split:
    the first character outside the Latin face's coverage wins, so
    "進撃の巨人 (2013)" resolves to cjk.

    **Except that RTL outranks everything, wherever it appears.** This
    answer becomes the *whole line's* face when `has_rtl` says so, and
    "first non-Latin wins" handed "進撃の巨人 مسلسل" to a CJK face -- which
    draws the Arabic as boxes, unjoined, in logical order. The trade is
    already decided at :data:`_RTL_RANGES`: *reordered text is a wrong line
    where tofu is only an ugly one*. So the CJK is what becomes tofu here,
    and a line with no RTL in it is untouched.

    **A symbol only wins when there is nothing else in the string.** It is a
    face for the odd glyph, not for words, and this answer is used for two
    things that would be wrong for: the line height a caller reserves
    (`components/banner.py`, `mpvtk_browser/cast.py`), and the face handed to
    ``draw_text`` for a *substring* the symbol need not have survived into.
    One star choosing it would re-typeset whole paragraphs, and for an RTL
    line draw every word as a box (mpvtk/GUIDE.md section 12.4).

    But a string of *nothing but* symbols has no words to protect and still
    has to be drawn by something. `components.placeholder_glyph` answers
    with the first character of a title, so an album named "★" is #713 all
    over again if this says "latin".

    **"emoji" is never the answer, not even for a string of nothing but
    emoji**, and that is deliberate rather than an omission. A colour-emoji
    face is very often bitmap-only -- ``NotoColorEmoji.ttf`` loads at 109px
    and at no other size (measured) -- so the object `font("emoji", 20)`
    returns has a 109px face's metrics, and this answer is used to reserve a
    line's *height* (`components/banner.py`, `mpvtk_browser/cast.py`) and to
    pick the face for a whole book (`epub/fonts.py`). Both would be wrong by
    a factor of five. The emoji face is reached per *run*, inside
    ``draw_text``, which knows to scale what it draws; it is not something a
    caller should be handed.
    """
    saw_symbol = saw_word = False
    first = None
    for ch in text or '':
        script = script_of_char(ord(ch))
        if script in ('hebrew', 'arabic'):
            return script
        if script in ('symbol', 'emoji'):
            saw_symbol = True
        elif script != 'latin':
            if first is None:
                first = script
        elif not ch.isspace():
            saw_word = True
    if first is not None:
        return first
    return 'symbol' if saw_symbol and (not saw_word) else 'latin'
_BAND_SCRIPTS = ((2432, 3583, 'indic'), (3712, 3839, 'lao'), (3840, 4095, 'tibetan'), (4096, 4255, 'myanmar'), (4256, 4351, 'georgian'), (4608, 4991, 'ethiopic'), (5024, 5119, 'cherokee'), (6016, 6143, 'khmer'), (6144, 6319, 'mongolian'))
_JOINERS = frozenset((8205, 65038, 65039, 8419))
_KEYCAP = 8419

def runs(text):
    """``[(script, chunk), ...]`` in order, adjacent same-script chars merged.

    **This is the whole fix for mixed strings.** A face named for a script is
    very often a face for *only* that script, so "進撃の巨人 (2013)" came out
    with the year as four tofu boxes (mpvtk/GUIDE.md section 12.5). Splitting
    draws each run with its own face, which is what libass already does for
    the text we hand it.

    Whitespace is neutral and stays with the run in progress rather than
    starting a Latin one: a space is blank in every face, and splitting on it
    would double the run count of an ordinary sentence for nothing.

    **Except after emoji**, where "blank in every face" stops being true: a
    space is blank in a colour-emoji face but as *wide* as an emoji --
    measured, NotoColorEmoji advances 135.7 of its 109px em for U+0020,
    against DejaVu's 6.4 at 20px -- so a caption with an emoji in the middle
    came out with a four-space hole after it.

    :data:`_JOINERS` is neutral for a harder reason, and unconditionally --
    see there.
    """
    out = []
    for ch in text or '':
        if ord(ch) == _KEYCAP and out:
            chunk = out[-1][1]
            peeled = []
            if chunk and ord(chunk[-1]) == 65039:
                peeled.insert(0, chunk.pop())
            if chunk:
                peeled.insert(0, chunk.pop())
            if not chunk:
                out.pop()
            if out and out[-1][0] == 'emoji':
                out[-1][1].extend(peeled + [ch])
            else:
                out.append(['emoji', peeled + [ch]])
            continue
        if ord(ch) in _JOINERS:
            script = None
        elif ch.isspace():
            script = 'latin' if out and out[-1][0] == 'emoji' else None
        else:
            script = script_of_char(ord(ch))
        if out and (script is None or script == out[-1][0]):
            out[-1][1].append(ch)
        else:
            out.append([script or 'latin', [ch]])
    return [(script, ''.join(chunk)) for script, chunk in out]
_RTL_RANGES = ((1424, 1535), (1536, 1791), (64285, 64335), (64336, 65023), (65136, 65279))

def has_rtl(text):
    return any((lo <= ord(ch) <= hi for ch in text or '' for lo, hi in _RTL_RANGES))

def _strike_order(size):
    """Fixed sizes to try for a face that refused ``size``.

    Bigger first, so what gets drawn is shrunk rather than blown up, and
    the smallest of those, so the shrink is the gentlest available.
    """
    return [s for s in _STRIKES if s > size] + [s for s in reversed(_STRIKES) if s < size]
_HOST_FONT_DIRS = ('/run/host/fonts', '/run/host/local-fonts', '/run/host/user-fonts')
_host_index = None

def _host_fonts():
    global _host_index
    if _host_index is None:
        _host_index = {}
        for directory in _HOST_FONT_DIRS:
            if not os.path.isdir(directory):
                continue
            for root, _dirs, files in os.walk(directory):
                for filename in files:
                    if filename.lower().endswith(('.ttf', '.otf', '.ttc', '.otc')):
                        _host_index.setdefault(filename.lower(), os.path.join(root, filename))
    return _host_index

def _resolutions(name):
    """The paths to try for one candidate: its own name, then the same
    **basename** under the host's font tree.

    A generator because the second half must not be reached on a normal
    host: `_load` only advances it when the name has already failed at
    every size, so the walk in :func:`_host_fonts` is paid by a Flatpak and
    by nobody else.

    The basename is what carries over, for absolute candidates as much as
    bare ones -- ``/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc``
    does not exist inside the sandbox, and the file it names does.
    """
    if name.startswith(_FC_PREFIX):
        found = _fontconfig()['match'].get(name[len(_FC_PREFIX):])
        if found:
            yield found
        return
    yield name
    base = os.path.basename(name).lower()
    alt = _host_fonts().get(base)
    if alt and alt != name:
        yield alt
    alt2 = _fontconfig()['files'].get(base)
    if alt2 and alt2 not in (name, alt):
        yield alt2
_FC_LANGS = {'latin': ('',), 'cjk': ('ja', 'zh-cn', 'zh-tw', 'ko'), 'arabic': ('ar',), 'hebrew': ('he',), 'devanagari': ('hi',), 'thai': ('th',), 'indic': ('bn', 'pa', 'gu', 'or', 'ta', 'te', 'kn', 'ml', 'si'), 'lao': ('lo',), 'tibetan': ('bo',), 'myanmar': ('my',), 'georgian': ('ka',), 'ethiopic': ('am',), 'cherokee': ('chr',), 'khmer': ('km',), 'mongolian': ('mn-mn',)}
_FC_PREFIX = 'fontconfig:'

def _fc_query(script, bold, lang):
    if script == 'emoji':
        return 'emoji'
    return 'sans-serif' + (':lang=' + lang if lang else '') + (':weight=bold' if bold else '')

def _fc_names(script, bold):
    """Sentinel candidates `_resolutions` answers from fontconfig."""
    langs = ('',) if script == 'emoji' else _FC_LANGS.get(script, ())
    names = [_FC_PREFIX + _fc_query(script, b, lang) for b in ((True, False) if bold else (False,)) for lang in langs]
    return list(dict.fromkeys(names))

def _fontconfig():
    from . import fontconfig
    return fontconfig.lookup([_fc_query(s, b, lang) for s in list(_FC_LANGS) + ['emoji'] for b in (False, True) for lang in (('',) if s == 'emoji' else _FC_LANGS[s])])

def _load(names, size, strikes=False):
    """``(font, name, native)``. ``native`` is the size it actually opened
    at, which is ``size`` for every scalable face and the strike for a
    bitmap one."""
    from ..win_fribidi import preload
    preload()
    from PIL import ImageFont
    for name in names:
        for candidate in _resolutions(name):
            for want in [size] + (_strike_order(size) if strikes else []):
                try:
                    return (ImageFont.truetype(candidate, want), candidate, want)
                except (OSError, IOError):
                    continue
    return (None, None, size)

def _notdef_refs(fnt, name):
    """What ``fnt`` renders for a codepoint that cannot exist.

    Memoized per (name, open size) because the *bitmap* is size-dependent
    even though the coverage verdict below is not: three renders cost 105 us
    on Linux and 19 us on Windows (measured), which is worth paying once per
    face rather than once per batch of characters.
    """
    key = (name, getattr(fnt, 'size', None))
    hit = _notdef.get(key)
    if hit is None:
        hit = []
        for probe in _NOTDEF_PROBES:
            mask = fnt.getmask(probe, mode='L')
            hit.append((mask.size, bytes(mask)))
        _notdef[key] = hit
    return hit

def _draws(fnt, name, ch):
    """Whether ``fnt`` has a glyph for ``ch``, by rendering it and comparing
    against that face's own no-glyph mark.

    **A render comparison because Pillow exposes no cmap.** A
    ``FreeTypeFont`` offers ``getmask``, ``getbbox``, ``getlength`` and
    ``getname`` and nothing that maps a character to a glyph id, so the
    alternative is a fontTools dependency for a check this cheap.

    Measured 2026-09-08: 45 us per codepoint on Linux, 7.5 us on Windows,
    paid once per (face, codepoint) for the session. **The verdict is the
    same at 8px and at 96px on every face tried**, which is why the memo is
    keyed by the face's name and shared across every size it is opened at.

    Two faces measured here answer in ways that look wrong and are not.
    ``simsun.ttc`` renders a *blank* no-glyph mark, and
    ``NotoColorEmoji.ttf`` renders one identical to its space -- both are
    harmless because the only glyphs that render blank are whitespace, and
    :func:`_covers` skips it. Do not "fix" this by requiring the mark to be
    non-blank; that assertion fails on a stock Debian box.
    """
    key = (name, ch)
    hit = _coverage.get(key)
    if hit is None:
        try:
            mask = fnt.getmask(ch, mode='L')
            hit = (mask.size, bytes(mask)) not in _notdef_refs(fnt, name)
        except (OSError, ValueError, AttributeError):
            hit = True
        _coverage[key] = hit
    return hit

def _covers(fnt, name, text, script):
    """Whether ``fnt`` can draw the part of ``text`` that chose ``script``,
    or **all** of it when ``script`` is None.

    **Only that part, and that is the whole design of this check.** Asking
    a face to cover the whole *string* would quietly overturn two decisions
    this module made on measured evidence and wrote down:
    ``NotoSansArabic-Regular.ttf`` is kept first for Arabic although it has
    no ASCII at all (:data:`_CANDIDATES`, and the comment above it says
    why), and the Hebrew list is ordered the opposite way round for
    precisely the neutrals an RTL line cannot split off. Requiring only the
    selecting script's codepoints leaves both answering exactly as they did,
    while a Japanese face stops being handed a Simplified Chinese title
    (#736).

    Everything else in the string is somebody else's run: ``runs`` splits a
    mixed title and each piece resolves its own face.

    **Except for an RTL line, which has no other run to fall to** -- there
    the whole line is one draw call, so ``script`` is passed as None and
    every codepoint is required. `font` asks for that first and falls back
    to the script-only question, so the stricter answer is a preference and
    never a way to end up with no face at all.
    """
    for ch in text or '':
        if ch.isspace():
            continue
        if script is not None and script_of_char(ord(ch)) != script:
            continue
        if not _draws(fnt, name, ch):
            return False
    return True

def _opened(script, size, bold, index):
    """``(name, face)`` at ``index`` in this script's candidate order, or
    None past the end. Candidates are opened lazily and kept, so the common
    case is still one ``ImageFont.truetype`` per (script, size, weight).

    Measured 2026-09-08 on the Windows VM, where the walk is longest: a
    Korean title reaches ``malgun.ttf`` on the fourth candidate for 1.81 ms
    all in, once per chain.
    """
    key = (script, size, bool(bold))
    state = _chains.get(key)
    if state is None:
        names = []
        if bold:
            names += _BOLD.get(script, [])
        names += _CANDIDATES.get(script, [])
        names += _fc_names(script, bold)
        for other in _FALLBACK_SCRIPTS.get(script, ()):
            names += _CANDIDATES.get(other, [])
        if script != 'latin':
            if bold:
                names += _BOLD['latin']
            names += _CANDIDATES['latin']
            names += _fc_names('latin', bold)
        state = {'names': names, 'next': 0, 'faces': []}
        _chains[key] = state
    while len(state['faces']) <= index and state['next'] < len(state['names']):
        name = state['names'][state['next']]
        state['next'] += 1
        fnt, got, native = _load([name], size, strikes=script == 'emoji')
        if fnt is not None:
            _stamp(fnt, script, size, bold, native)
            state['faces'].append((got, fnt))
    if index < len(state['faces']):
        return state['faces'][index]
    return None

def _stamp(fnt, script, size, bold, native):
    """What it was asked for, so ``draw_text`` can ask for the same in
    another script. ``size`` is on ``FreeTypeFont`` already but the weight is
    not, and the bitmap default has neither. The script rides along too, so
    a face can be asked whether it is the right one for the run in hand --
    see :func:`_run_face`."""
    try:
        fnt._jms_size, fnt._jms_bold = (size, bool(bold))
        fnt._jms_script = script
        fnt._jms_native = native
    except AttributeError:
        pass

def default_face(size):
    """Pillow's own face, at ``size``. Without the size it is 10px whatever
    was asked for, which is what made #786's titles tiny."""
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

def font_for(text, size, bold=False):
    """A PIL font able to render ``text`` at ``size``. Falls back to the Latin
    face (and finally Pillow's bitmap default) when nothing better is
    installed."""
    return font(script_of(text), size, bold, text=text, whole=has_rtl(text))

def _pick(script, size, bold, text, require):
    """First face in candidate order satisfying ``require`` (a script name,
    or None for "every codepoint in ``text``"). None if the chain runs out."""
    index = 0
    while True:
        entry = _opened(script, size, bold, index)
        if entry is None:
            return None
        name, fnt = entry
        if name is None or _covers(fnt, name, text, require):
            return fnt
        index += 1

def font(script, size, bold=False, text=None, whole=False):
    """A PIL face for ``script``, able to draw ``text`` if it is given.

    **"The first candidate that opens" is not the same question as "the
    first candidate that works", and the difference is #736.** A stock
    Windows 10 resolves ``msgothic.ttc`` for every CJK string, and MS Gothic
    is a *Japanese* face: measured, it is missing four of the eight
    codepoints in that issue's own title, so half a Simplified Chinese
    library drew as tofu. No ordering of :data:`_CANDIDATES` fixes that,
    because no face on that host covers all four CJK languages -- msyh and
    simsun have the Chinese and no Hangul, malgun has the Hangul and little
    Han. So ``text`` decides, and the list only expresses preference.

    Without ``text`` this answers exactly what it always did, from the same
    one-lookup cache: the first candidate that opens. That is the answer for
    a caller who wants a face's *metrics* rather than its glyphs
    (`components/banner.py` reserving a line), and it is the fallback when
    nothing in the chain covers the string -- tofu, but never a crash.
    """
    key = (script, size, bool(bold))
    primary = _cache.get(key)
    if primary is None:
        entry = _opened(script, size, bold, 0)
        if entry is None:
            fnt = default_face(size)
            _stamp(fnt, script, size, bold, size)
            entry = (None, fnt)
        name, primary = entry
        if _resolved.get((script, bool(bold))) != name:
            _resolved[script, bool(bold)] = name
            if name is None and script != 'latin':
                log.info('no font found for script %r; text may not render', script)
        _cache[key] = primary
    if not text:
        return primary
    if whole:
        got = _pick(script, size, bold, text, None)
        if got is not None:
            return got
    got = _pick(script, size, bold, text, script)
    return got if got is not None else primary

def _scale_of(fnt):
    """How much of its natural size a face's output has to be shrunk to.

    1.0 for every scalable face, so every path below is a no-op for all of
    them. It is not 1.0 for a bitmap-strike colour-emoji face, which draws
    and measures at its strike and nothing else: see :data:`_STRIKES`.
    """
    native = getattr(fnt, '_jms_native', None)
    want = getattr(fnt, '_jms_size', None)
    if not native or not want or native == want:
        return 1.0
    return float(want) / float(native)

def metrics(fnt):
    """``(ascent, descent)`` in the pixels a face actually draws in.

    **The one place a face's metrics may be read from.** A bitmap-strike
    emoji face reports its strike's metrics -- 101 and 27, for a face being
    used at 20px -- so a caller reserving a line from ``getmetrics()``
    directly reserves five lines. Tolerant of Pillow's bitmap default,
    which has no ``getmetrics`` at all.
    """
    try:
        ascent, descent = fnt.getmetrics()
    except AttributeError:
        size = getattr(fnt, 'size', 11)
        return (int(size * 0.8), int(size * 0.2))
    scale = _scale_of(fnt)
    if scale == 1.0:
        return (ascent, descent)
    return (int(round(ascent * scale)), int(round(descent * scale)))

def _same_size(fnt, script, text=None):
    """The face for ``script`` at the size and weight ``fnt`` was made with.

    Callers hold a font, not a (size, bold) pair -- they need its metrics for
    line spacing -- so the pair rides along on the object, stamped by
    ``font()``. A font from anywhere else falls back to its own ``size`` and
    regular weight, which is right for every caller here and wrong for
    nobody: the worst case is a bold run drawn regular.
    """
    return font(script, getattr(fnt, '_jms_size', getattr(fnt, 'size', 12)), getattr(fnt, '_jms_bold', False), text=text, whole=has_rtl(text))

def _run_face(fnt, script, text=None):
    """The face to draw a run of ``script`` with, given the font a caller
    chose — possibly for a longer string this run was wrapped out of.

    ``fnt`` itself whenever it is already that script, or when it carries no
    stamp at all: a caller who built a face by hand gets exactly the face it
    passed, which is what keeps a single-run string drawn byte for byte as
    it was before any of this existed.

    **Single-run only.** The multi-run paths use :func:`_same_size`, which
    resolves a real face per script unconditionally. For an unstamped font the
    two must not agree: "leave the caller's face alone" is right for a string
    that face can draw and disastrous for the mixed string this whole path
    exists to handle, where it puts the tofu straight back.

    For a stamped font the two agree **as long as the caller resolved the face
    for the text it goes on to draw** -- every caller here does, and the ones
    that ellipsize or wrap draw a subset plus a Latin "…", which is its own
    run. That is the contract now, not an identity: since `font()` picks by
    coverage, the same (script, size, weight) can legitimately answer with two
    different faces, so a face resolved for one string and reused for another
    the first face cannot draw would be returned unchanged. No guard is added
    for it, because a guard here would be a second place deciding what
    `font()` already decides -- if a caller ever needs that, it should pass
    the text it draws.
    """
    stamped = getattr(fnt, '_jms_script', None)
    if stamped is None or stamped == script:
        return fnt
    return _same_size(fnt, script, text)

def _face_pickers(fnt, faces):
    """``(single, per_run)`` — how to turn a script name into a face.

    ``faces`` is the seam the epub reader hangs off: its faces are serif
    families of its own, resolved per (kind, size, weight, slant), so it
    supplies a resolver rather than borrowing this module's. **A supplied
    resolver is authoritative for both**, which is the difference that
    matters: `_run_face`'s "leave an unstamped caller's face exactly as it
    was passed" rule is a promise to the *browser*, whose fonts all come
    from `font()` and so carry a stamp. The reader's Latin faces do not,
    and honouring the rule there would hand a CJK run a serif face --
    which is the bug the reader was routed through here to fix.
    """
    if faces is not None:
        return (faces, faces)
    return (lambda script, text=None: _run_face(fnt, script, text), lambda script, text=None: _same_size(fnt, script, text))
_ORPHAN_SCRIPTS = ('symbol', 'cjk')

def _peelable(ch):
    """Whether a character the run's face lacks may be moved to another.

    **Category C is excluded and it is not a formality**: measured on
    Debian, `NotoSansSymbols2` draws a *picture* for every C0/C1 control
    and for U+007F, so a peel that took the first face with a glyph would
    turn a stray U+0085 in a title into a visible one -- and a zero-width
    space into something with width. GUIDE section 12.4 names that set as
    the one nothing may rescue. Whitespace is left alone for the reason
    :func:`_covers` skips it: it is blank in every face, and moving it
    would split a run for no glyph.
    """
    return not ch.isspace() and unicodedata.category(ch)[0] != 'C'

def _attaches(ch):
    """Whether a character joins what is beside it, so it can never be
    peeled off on its own **or left behind when its base moves**.

    Shaping does not cross a run boundary (GUIDE section 12.3), and the peel
    is a run boundary. Measured on the Windows VM: Segoe UI Symbol has no
    U+FE0F, so without this the variation selector was peeled to the emoji
    face and "☂️" was drawn as two pieces -- the exact split
    :data:`_JOINERS` exists to prevent, arriving one level down. The
    combining marks are here for the same reason and for the other
    direction: an orphaned base takes its marks with it, or Devanagari and
    niqqud stack onto the wrong glyph.
    """
    return ord(ch) in _JOINERS or unicodedata.category(ch)[0] == 'M'

def _face_draws(fnt, ch):
    """:func:`_draws` for a face somebody already resolved.

    The name is only a memo key, and it comes off the object here because
    there is no candidate list in hand. Pillow's bitmap default is the one
    face reachable without a ``path``; every other one is a file.
    """
    return _draws(fnt, getattr(fnt, 'path', None), ch)

def _orphan_face(ch, script, size, bold, prefer=()):
    """A face for one character its run's face cannot draw, or None.

    ``prefer`` is the faces the *rest of this string* resolved, and it is
    asked first because a character with no script of its own belongs with
    the text around it: U+2161 in a Chinese title is East Asian wide, and
    taking it from the face already drawing the Han beside it is both the
    right width and the right weight. Falling to :data:`_ORPHAN_SCRIPTS`
    after that is what covers the character standing on its own.

    The run's own chain is asked before either, because `font` rejected a
    candidate for failing *somewhere else in the run* -- a face further
    down the same list may have exactly this glyph.
    """
    for face in prefer:
        if _face_draws(face, ch):
            return face
    key = (ch, script, size, bool(bold))
    if key not in _orphans:
        got = None
        for other in (script,) + tuple((s for s in _ORPHAN_SCRIPTS if s != script)):
            got = _pick(other, size, bold, ch, None)
            if got is not None:
                break
        _orphans[key] = got
    return _orphans[key]

def _peel(chunk, face, script, prefer):
    """``[(chunk, face), ...]`` for one run, splitting off the characters
    ``face`` cannot draw.

    **This is the fallback Pillow does not have** (#740). A run's face is
    chosen for the whole run, so one codepoint the chain cannot supply used
    to make one box; here that character alone moves to a face that has it
    and everything around it is drawn exactly as before. A character
    nothing on the host draws stays put -- tofu, as it was.
    """
    size = getattr(face, '_jms_size', getattr(face, 'size', 12))
    bold = getattr(face, '_jms_bold', False)
    out = []
    for ch in chunk:
        if out and _attaches(ch):
            out[-1][0].append(ch)
            continue
        use = face
        if _peelable(ch) and (not _face_draws(face, ch)):
            use = _orphan_face(ch, script, size, bold, prefer) or face
        if out and out[-1][1] is use:
            out[-1][0].append(ch)
        else:
            out.append(([ch], use))
    return [(''.join(chars), used) for chars, used in out]

def _split(text, fnt, faces):
    """``(pieces, one_face_or_None)`` — the one place face resolution
    happens, so measuring and drawing cannot disagree about it. ``pieces``
    is ``[(script, chunk, face), ...]`` in order; a non-None second element
    means the whole string is drawn with that face in a single call.

    A single **emoji** run does not take the bypass. It has to reach the
    per-run resolver (`_run_face` would hand back the caller's own face,
    which is the face that cannot draw it) and, on a bitmap-strike face,
    the scaling that only the run loop does.

    **An RTL line is never peeled**: Pillow reorders bidi within one draw
    call and cannot across several, so a missing glyph there stays tofu
    rather than becoming a reordered line (GUIDE section 12.1).
    """
    parts = runs(text)
    single, per_run = _face_pickers(fnt, faces)
    if has_rtl(text):
        script = script_of(text)
        face = single(script, text)
        return ([(script, text, face)], face)
    resolver = single if len(parts) == 1 and parts[0][0] != 'emoji' else per_run
    chosen = [resolver(script, chunk) for script, chunk in parts]
    pieces = []
    for (script, chunk), face in zip(parts, chosen):
        prefer = [other for other in chosen if other is not face]
        for piece, used in _peel(chunk, face, script, prefer):
            pieces.append((script, piece, used))
    if len(pieces) == 1 and pieces[0][0] != 'emoji':
        return (pieces, pieces[0][2])
    return (pieces, None)

def _measure(text, fnt, faces, measure):
    if not text:
        return 0.0
    pieces, whole = _split(text, fnt, faces)
    if whole is not None:
        return measure(text, whole) * _scale_of(whole)
    total = 0.0
    for _script, chunk, face in pieces:
        total += measure(chunk, face) * _scale_of(face)
    return total

def text_length(draw, text, fnt, faces=None):
    """``draw.textlength`` for a string that may need more than one face.

    Measuring has to agree with drawing or a caption is ellipsized against a
    width it is not drawn at, so this and ``draw_text`` split identically.
    """
    return _measure(text, fnt, faces, lambda chunk, face: draw.textlength(chunk, font=face))

def length(text, fnt, faces=None):
    """:func:`text_length` for a caller that has no ``ImageDraw``.

    The epub reader measures a chapter to paginate it long before there is
    anything to draw on, and building a 1x1 image per measurement is the
    thing its width cache exists to avoid. ``draw.textlength`` is
    ``font.getlength`` with the draw's ``fontmode`` passed through, and
    every surface here is antialiased, which is that default.
    """
    return _measure(text, fnt, faces, lambda chunk, face: face.getlength(chunk))

def _draw_scaled(draw, x, baseline, chunk, face, scale, fill):
    """Draw one run with a face that only exists at another pixel size.

    Rendered at the face's own size into a scratch bitmap, shrunk, and
    composited -- the only route for a CBDT face with one fixed strike
    (mpvtk/GUIDE.md section 12.2).

    Two things here look wrong and are not. **Not premultiplied** before the
    resize: the glyphs are already antialiased into transparent *black*, so
    there is no halo to remove and premultiplying without dividing back out
    darkens every edge. And **the alpha rides in the mask, not the source**,
    or a transparent plate squares it and the run comes out thin.
    """
    from PIL import Image, ImageDraw
    target = getattr(draw, 'im', None)
    if target is None:
        return
    ascent, descent = face.getmetrics()
    pad = max(1, int(round(ascent * 0.25)))
    advance = draw.textlength(chunk, font=face)
    w = max(1, int(advance) + 2 * pad)
    h = max(1, ascent + descent + 2 * pad)
    scratch = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(scratch).text((pad, pad + ascent), chunk, font=face, fill=fill if fill is not None else (255, 255, 255, 255), anchor='ls', embedded_color=True)
    small = scratch.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.LANCZOS)
    body = small.convert('RGB')
    if draw.mode != 'RGB':
        body = body.convert(draw.mode)
    ox = int(round(x - pad * scale))
    oy = int(round(baseline - (pad + ascent) * scale))
    target.paste(body.im, (ox, oy, ox + small.width, oy + small.height), small.getchannel('A').im)

def draw_text(draw, xy, text, fnt, fill=None, anchor=None, faces=None):
    """``draw.text`` with a face per script run.

    **A single-run string takes the original path**, byte for byte: one
    ``draw.text`` with the font it was given. That is almost every string
    this app draws, and it means the change can only alter the strings that
    were broken. The one exception is a run of emoji — see :func:`_split`.

    ``faces`` overrides where a run's face comes from; the epub reader
    passes its own resolver. See :func:`_face_pickers`.

    Multi-run strings are drawn run by run along a shared baseline. The
    baseline is the point: PIL's default vertical anchor is the *ascender*,
    and two faces do not share one, so anchoring each run that way would
    stagger them. The tallest ascent in the line decides where the baseline
    sits, and every run is drawn from it.

    So a mixed line can sit a little lower than the band its caller
    reserved, which reserves from ``script_of``'s face and not from the
    tallest run: on Windows the symbol face is (22, 6) against Arial's
    (19, 5) at 20px, so one star pushes the line down about 3px. That is
    the accepted cost of *not* letting a symbol choose the whole string's
    face — the alternative re-typesets every wrapped line of a paragraph in
    Segoe UI Symbol. Every caller here draws into a margin that absorbs it
    (`banner.py` has 18px below the meta line, `cast.py` a per-line gap),
    and no tile caption carries a symbol.
    """
    if not text:
        return
    pieces, whole = _split(text, fnt, faces)
    if whole is not None:
        draw.text(xy, text, font=whole, fill=fill, anchor=anchor)
        return
    fonts = [face for _script, _chunk, face in pieces]
    scales = [_scale_of(f) for f in fonts]
    ascent = max((metrics(f)[0] for f in fonts))
    descent = max((metrics(f)[1] for f in fonts))
    x, y = xy
    horizontal, vertical = ((anchor or 'la')[0], (anchor or 'la')[1])
    if horizontal == 'm':
        x -= text_length(draw, text, fnt, faces) / 2.0
    elif horizontal == 'r':
        x -= text_length(draw, text, fnt, faces)
    if vertical == 'a':
        baseline = y + ascent
    elif vertical == 'm':
        baseline = y + (ascent - descent) / 2.0
    elif vertical == 'd':
        baseline = y - descent
    else:
        baseline = y
    for (script, chunk, face), scale in zip(pieces, scales):
        if scale != 1.0:
            _draw_scaled(draw, x, baseline, chunk, face, scale, fill)
        else:
            draw.text((x, baseline), chunk, font=face, fill=fill, anchor='ls', embedded_color=script == 'emoji' and draw.mode in ('RGB', 'RGBA'))
        x += draw.textlength(chunk, font=face) * scale

def clear_cache():
    global _host_index
    _host_index = None
    _cache.clear()
    _chains.clear()
    _coverage.clear()
    _orphans.clear()
    _notdef.clear()
    _resolved.clear()

def _env_extra():
    """Allow an explicit override for exotic setups (a single font path).

    It goes in front of **every** list, emoji included, so setting it turns
    emoji back into whatever that one face draws for them. That is the
    contract of "use this font" and is no worse than the answer before
    there was an emoji bucket -- but it is why a host with this set will
    not show the colour ones.
    """
    path = os.environ.get('JELLYFIN_MPV_SHIM_UI_FONT')
    if path:
        for names in list(_CANDIDATES.values()) + list(_BOLD.values()):
            names.insert(0, path)
_env_extra()

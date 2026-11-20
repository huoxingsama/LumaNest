"""The toolkit's design tokens and type scale.

Colours and sizes are semantic tokens, not literals; an embedding app calls
:func:`set_tokens` (or :func:`set_accent`) once at startup and every widget
and renderer-drawn control follows. Values are bare ``"rrggbb"``, matching
every other colour field in mpvtk, and are served through the module
``__getattr__`` rather than as globals so a theme can never redefine the
functions here.

Why the token set is shaped this way -- semantic-not-literal, the two
surfaces (app chrome vs over-video), tier names rather than numbers, `Px`
and the exactly-once multiplier, and the floor-not-a-scale argument -- is in
`mpvtk/GUIDE.md` section 7.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
DEFAULT_ACCENT = '7aa2f7'
TOKENS = {'ON_SURFACE': 'eeeeee', 'ON_SURFACE_STRONG': 'ffffff', 'ON_SURFACE_MUTED': 'aaaaaa', 'ON_SURFACE_FAINT': '777777', 'CONTROL_BG': '333333', 'CONTROL_HOVER': '4a4a4a', 'CONTROL_SUNKEN': '2a2a2a', 'OUTLINE': '444444', 'OUTLINE_STRONG': '555555', 'POPUP_BG': '222222', 'OVERLAY_BG': '111111', 'SCROLLBAR_THUMB': '666666', 'SCROLLBAR_THUMB_ACTIVE': 'bbbbbb', 'SELECTION': '3d59a1', 'ACCENT': DEFAULT_ACCENT, 'ACCENT_HOVER': None, 'ACCENT_SOFT': None, 'ON_ACCENT': None, 'ACCENT_ON_VIDEO': None, 'SCRIM': '000000', 'CHIP_BG': '202020', 'CHIP_BG_HOVER': '3a3a3a', 'CHIP_FG': 'ffffff'}
TYPE_SCALE = {'MICRO': 0.7, 'CAPTION': 0.8, 'SMALL': 0.88, 'NORMAL': 1.0, 'LARGE': 1.12, 'TITLE': 1.3, 'HEADING': 1.42, 'PAGE': 1.53, 'HERO': 1.7}
DEFAULT_BASE_SIZE = 17
_base_size = DEFAULT_BASE_SIZE
_min_size = 0
_text_factor = 1.0

class Px(int):
    """A size that has already been resolved by :func:`text_size`.

    The user's multiplier is applied **exactly once**, and this is the
    marker that says so -- composite widgets resolve their own size and hand
    the number to a child, which would otherwise resolve it again. An int
    subclass rather than a flag, so it stores and compares like the number
    it replaces; **arithmetic returns a plain int**, because a derived size
    is a new size. See GUIDE.md section 7.
    """
    __slots__ = ()

def set_type_scale(base=None, minimum=None, factor=None):
    """Set the base size every tier derives from. ``None`` restores stock.

    Wholesale like :func:`set_tokens`, so a theme that says nothing about
    type gets the default back rather than the last theme's. ``minimum``
    floors every size the toolkit resolves and ``factor`` multiplies it; see
    ``_min_size`` and ``_text_factor``.
    """
    global _base_size, _min_size, _text_factor
    try:
        value = float(DEFAULT_BASE_SIZE if base is None else base)
    except (TypeError, ValueError):
        value = float(DEFAULT_BASE_SIZE)
    _base_size = value if value > 0 else float(DEFAULT_BASE_SIZE)
    try:
        floor = float(0 if minimum is None else minimum)
    except (TypeError, ValueError):
        floor = 0.0
    try:
        mult = float(1.0 if factor is None else factor)
    except (TypeError, ValueError):
        mult = 1.0
    _text_factor = mult if mult > 0 else 1.0
    _biggest = _base_size * _text_factor * max(TYPE_SCALE.values())
    _min_size = min(max(floor, 0.0), _biggest)
    return _base_size

def size_of_tier(tier):
    """The px size for a named tier. Unknown tiers are an error, not a
    silent default -- a typo'd tier would otherwise render as body text
    and look almost right."""
    try:
        ratio = TYPE_SCALE[str(tier).upper()]
    except KeyError:
        raise KeyError('unknown type tier %r. Tiers are %s' % (tier, ', '.join(TYPE_SCALE))) from None
    return Px(max(int(round(_base_size * _text_factor * ratio)), int(_min_size), 1))
size = size_of_tier

def text_size(size):
    """A size as the toolkit will actually render it.

    Accepts a **tier name** (``"caption"``) or a number; the name is the
    preferred spelling at a call site (GUIDE.md section 7). Numbers are
    multiplied and floored HERE, where a widget resolves its size and
    before layout measures it -- flooring at the logical-to-physical
    boundary instead would draw bigger text inside boxes measured for the
    smaller kind.
    """
    if isinstance(size, Px):
        return size
    if isinstance(size, str):
        return size_of_tier(size)
    try:
        return Px(max(int(round(float(size) * _text_factor)), int(_min_size), 1))
    except (TypeError, ValueError):
        return Px(max(int(round(_base_size * _text_factor)), 1))

def text_factor():
    """The user's text multiplier, for code that resolves its own sizes --
    which is the renderer, and nothing else."""
    return _text_factor

def min_size():
    return _min_size
_ALIASES = {'HOVER': 'ACCENT_HOVER', 'SOFT': 'ACCENT_SOFT'}
_tokens = {}
GLOW = False

def _rgb(hexstr):
    h = (hexstr or '').lstrip('#')
    if len(h) != 6:
        raise ValueError('expected rrggbb, got %r' % (hexstr,))
    return tuple((int(h[i:i + 2], 16) for i in (0, 2, 4)))

def _hex(rgb):
    return '%02x%02x%02x' % tuple((max(0, min(255, int(round(c)))) for c in rgb))

def lighten(hexstr, amount=0.18):
    """Move a colour toward white by ``amount`` (0..1)."""
    r, g, b = _rgb(hexstr)
    return _hex((r + (255 - r) * amount, g + (255 - g) * amount, b + (255 - b) * amount))

def darken(hexstr, amount=0.72):
    """Move a colour toward black by ``amount`` (0..1)."""
    r, g, b = _rgb(hexstr)
    f = 1.0 - amount
    return _hex((r * f, g * f, b * f))

def readable_on(hexstr):
    """Black or white, whichever reads better on ``hexstr``. Uses relative
    luminance rather than a naive average — a saturated blue is much darker
    than its mean channel suggests."""
    r, g, b = (c / 255.0 for c in _rgb(hexstr))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return '101010' if lum > 0.6 else 'ffffff'

def set_tokens(glow=None, **tokens):
    """Replace the token set. Unnamed tokens fall back to their stock value.

    Replaced wholesale rather than updated in place, which is what makes a
    **runtime** theme swap safe: a token the new theme does not mention resets
    to the default instead of keeping the old theme's value. Callers pass
    whatever subset they have opinions about.

    Names are case-insensitive so an app can pass the lowercase wire names
    straight back in.
    """
    global GLOW
    resolved = {k: v for k, v in TOKENS.items() if v is not None}
    for key, value in tokens.items():
        name = key.upper()
        name = _ALIASES.get(name, name)
        if name not in TOKENS:
            raise KeyError('unknown mpvtk theme token %r' % (key,))
        if value is not None:
            resolved[name] = str(value).lstrip('#')
    accent = resolved['ACCENT']
    resolved.setdefault('ACCENT_HOVER', lighten(accent))
    resolved.setdefault('ACCENT_SOFT', darken(accent))
    resolved.setdefault('ON_ACCENT', readable_on(accent))
    resolved.setdefault('ACCENT_ON_VIDEO', resolved['ACCENT'])
    for name in ('ACCENT_HOVER', 'ACCENT_SOFT', 'ON_ACCENT', 'ACCENT_ON_VIDEO'):
        if name not in resolved:
            resolved[name] = None
    _tokens.clear()
    _tokens.update(resolved)
    if glow is not None:
        GLOW = bool(glow)
    return palette()

def set_accent(accent, hover=None, soft=None, on_accent=None, glow=None):
    """Set just the accent, leaving every other token alone.

    The original entry point, and still the right one for an app whose only
    opinion is its brand colour. ``hover``/``soft``/``on_accent`` default to
    sensible derivations.
    """
    keep = {k: v for k, v in _tokens.items() if k not in ('ACCENT', 'ACCENT_HOVER', 'ACCENT_SOFT', 'ON_ACCENT', 'ACCENT_ON_VIDEO')}
    return set_tokens(glow=glow, ACCENT=accent or DEFAULT_ACCENT, ACCENT_HOVER=hover, ACCENT_SOFT=soft, ON_ACCENT=on_accent, **keep)

def palette():
    """The resolved tokens as the renderer wants them: lowercase keys, plus
    the glow flag. This is the ``mpvtk-theme`` message payload."""
    out = {k.lower(): v for k, v in _tokens.items()}
    out['glow'] = GLOW
    return out

def __getattr__(name):
    """Serve tokens as module attributes (PEP 562).

    Only reached when normal lookup fails, so the functions above always win
    and no caller can shadow them by naming a token after one.
    """
    key = _ALIASES.get(name, name)
    if key in _tokens:
        return _tokens[key]
    raise AttributeError('module %r has no attribute %r. Tokens are %s' % (__name__, name, ', '.join(sorted(_tokens)))) from None

def __dir__():
    return sorted(list(globals()) + list(_tokens) + list(_ALIASES))
set_tokens()

"""Selectable UI themes for the mpvtk browser: the schema, and the loader.

A theme is a plain data bundle — the colour palette (see ``theme.py``), the mpv
browse ``background-color``, a glow toggle, and a handful of size and layout
choices. ``theme.apply(name)`` makes one active at startup; ``player`` /
``strips`` / ``tile_renderer`` / the renderer read the rest.

Themes are **JSON files**, resolved the way shader packs are: the ones shipped
in the package are the built-ins, and a file of the same name under the user's
config directory (``<config>/themes/<id>.json``) shadows the built-in entirely.
So a user can add themes, and can retheme a shipped one, without touching the
install.

:data:`DEFAULT` is the exception, and deliberately so. It lives here in Python
because it is three things at once:

* the **fallback** — whatever else fails to load, this always resolves;
* the **schema** — a theme file may only set keys that appear here, and its
  values are coerced to the types found here;
* the **base** — every theme is merged *over* this, so a theme file only states
  what it changes. ``{"name": "Red", "palette": {"ACCENT": "cc2222"}}`` is a
  complete, valid theme.

That last property is what makes the merge safe. There is no such thing as a
half-applied theme: an absent key is the default's value, never the previously
active theme's.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import json
import logging
import os
log = logging.getLogger('mpvtk_browser.themes')
THEME_DIR = 'themes'
DEFAULT = {'name': 'LumaNest', 'palette': {'WINDOW_BG': '12151b', 'CARD_BG': '1c2129', 'PANEL_BG': '1c2129', 'PLACEHOLDER_BG': '252b34', 'BUTTON_BG': '262c35', 'BUTTON_ACTIVE': '353c46', 'ENTRY_BG': '171c23', 'BORDER': '343b46', 'TEXT_FG': 'f4f1eb', 'SUBTLE_FG': 'b3b8c1', 'ACCENT': 'e6e2da', 'ACCENT_HOVER': 'faf8f5', 'ACCENT_SOFT': '343a43', 'ACCENT_FG': '202228', 'FAV_RED': 'e26b79', 'OK_GREEN': '87c9a8', 'WARN_AMBER': 'e5c07b', 'PROGRESS_TRACK': '343b46', 'WATCHED_GREEN': '87c9a8'}, 'browse_bg': '#12151b', 'glow': False, 'rounded': True, 'accent_buttons': False, 'badge_shadow': True, 'arrow_mode': 'header', 'arrow_bg': '262c35', 'arrow_alpha': 180, 'hud_accent': 'e6e2da', 'window_gradient': None, 'topbar_gradient': None, 'poster_scale': 1.0, 'base_size': None, 'heading_size': None, 'tile_landscape': (240, 135), 'tile_title_size': None, 'tile_sub_size': None}
_COLOUR_KEYS = ('arrow_bg', 'hud_accent')
_GRADIENT_KEYS = ('window_gradient', 'topbar_gradient')
_ENUMS = {'arrow_mode': ('header', 'overlay')}

def _is_hex(value):
    h = str(value).lstrip('#')
    if len(h) != 6:
        return False
    try:
        int(h, 16)
    except ValueError:
        return False
    return True

def _stops(value):
    """Validate a gradient stop list, or None if it is not one.

    Sorted on the way out and de-duplicated by position: the renderer walks
    the stops in order and a zero-length segment would divide by nothing.
    """
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    out = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        pos, colour = item
        if not isinstance(pos, (int, float)) or not 0.0 <= pos <= 1.0:
            return None
        if not _is_hex(colour):
            return None
        out.append((float(pos), str(colour).lstrip('#')))
    out.sort(key=lambda s: s[0])
    deduped = [out[0]]
    for pos, colour in out[1:]:
        if pos - deduped[-1][0] < 1e-06:
            deduped[-1] = (pos, colour)
        else:
            deduped.append((pos, colour))
    return deduped if len(deduped) >= 2 else None

def _coerce(key, value, default, where):
    """One value from a theme file, checked against the default it replaces.

    Returns ``(ok, value)``. A rejected value is *dropped*, not fatal: the
    default stands and the rest of the theme still applies. A theme file is
    user-editable, so one bad colour must not cost you the whole look — and a
    silent drop would be worse, hence the log line.
    """

    def bad(why):
        log.warning('theme %s: ignoring %s (%s)', where, key, why)
        return (False, default)
    if key in _ENUMS:
        if value not in _ENUMS[key]:
            return bad('expected one of %s' % (', '.join(_ENUMS[key]),))
        return (True, value)
    if key in _COLOUR_KEYS:
        if value is None:
            return (True, None)
        if not _is_hex(value):
            return bad('expected a "rrggbb" colour')
        return (True, str(value).lstrip('#'))
    if key in _GRADIENT_KEYS:
        if value is None:
            return (True, None)
        stops = _stops(value)
        if stops is None:
            return bad('expected [[position, "rrggbb"], ...] with positions 0-1')
        return (True, stops)
    if key == 'tile_landscape':
        try:
            w, h = value
            return (True, (int(w), int(h)))
        except (TypeError, ValueError):
            return bad('expected [width, height]')
    if key == 'browse_bg':
        if not _is_hex(value):
            return bad('expected a "#rrggbb" colour')
        return (True, '#' + str(value).lstrip('#'))
    if default is None:
        if value is None:
            return (True, None)
        try:
            return (True, int(value))
        except (TypeError, ValueError):
            return bad('expected a number or null')
    if isinstance(default, bool):
        if not isinstance(value, bool):
            return bad('expected true or false')
        return (True, value)
    for kind in (int, float, str):
        if isinstance(default, kind):
            try:
                return (True, kind(value))
            except (TypeError, ValueError):
                return bad('expected %s' % kind.__name__)
    return bad('unsupported value')

def resolve(data, where='<memory>'):
    """Merge one theme file's contents over :data:`DEFAULT` and return a theme.

    Unknown keys are dropped with a warning rather than accepted, which is the
    whole reason this exists: the old code copied a theme's palette straight
    into the ``theme`` module's globals, so a typo silently defined a *new*
    global while the real one kept its old value — invisible, and the theme
    just looked subtly wrong. There is nowhere to put a typo now.
    """
    out = dict(DEFAULT)
    out['palette'] = dict(DEFAULT['palette'])
    if not isinstance(data, dict):
        log.warning('theme %s: expected a JSON object, ignoring', where)
        return out
    for key, value in data.items():
        if key == 'palette':
            if not isinstance(value, dict):
                log.warning('theme %s: palette is not an object', where)
                continue
            for ckey, colour in value.items():
                if ckey not in DEFAULT['palette']:
                    log.warning('theme %s: unknown palette colour %s', where, ckey)
                    continue
                if not _is_hex(colour):
                    log.warning('theme %s: palette %s is not "rrggbb"', where, ckey)
                    continue
                out['palette'][ckey] = str(colour).lstrip('#')
            continue
        if key not in DEFAULT:
            log.warning('theme %s: unknown key %s', where, key)
            continue
        ok, coerced = _coerce(key, value, DEFAULT[key], where)
        if ok:
            out[key] = coerced
    return out

def theme_dirs():
    """``(builtin, user)`` theme directories, either possibly None.

    Imported lazily: this module is pulled in by ``theme``, which nearly every
    view imports, and neither of these is needed to answer "what is the default
    palette" — which is all that matters until a theme is actually applied.
    """
    builtin = user = None
    try:
        from ..utils import get_resource
        builtin = get_resource(THEME_DIR)
    except Exception:
        log.debug('no built-in theme directory', exc_info=True)
    try:
        from .. import conffile
        from ..constants import APP_NAME
        user = conffile.get_dir(APP_NAME, THEME_DIR)
    except Exception:
        log.debug('no user theme directory', exc_info=True)
    return (builtin, user)

def _scan(directory, into):
    if not directory or not os.path.isdir(directory):
        return
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        log.warning('could not read theme directory %s', directory, exc_info=True)
        return
    for filename in names:
        if not filename.lower().endswith('.json'):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            log.warning('could not load theme %s', path, exc_info=True)
            continue
        theme_id = os.path.splitext(filename)[0].lower()
        resolved = resolve(data, where=path)
        if not (isinstance(data, dict) and data.get('name')):
            resolved['name'] = theme_id
        into[theme_id] = resolved

def load(force=False):
    """The theme registry: ``{id: theme}``, built-ins then user overrides.

    Cached — themes take effect at startup, so re-reading the directories per
    frame would buy nothing. ``force`` re-reads, for the settings screen and
    for tests.
    """
    global _cache
    if _cache is not None and (not force):
        return _cache
    themes = {'default': resolve({}, where='<built-in>')}
    builtin, user = theme_dirs()
    _scan(builtin, themes)
    _scan(user, themes)
    _cache = themes
    return themes
_cache = None

def get(name):
    """A theme by id, falling back to ``default`` for unknown/None."""
    themes = load()
    fallback = themes.get('default') or resolve({})
    return themes.get((name or 'default').lower(), fallback)

def choices(force=False):
    """``(label, id)`` pairs for the settings dropdown, ``default`` first and
    the rest alphabetical — the list is user-extensible now, so it needs an
    order that does not depend on directory listing order.

    ``(label, value)``, matching ``config.LABELED_ENUMS``, because that is
    what the settings form consumes."""
    themes = load(force=force)
    rest = sorted((k for k in themes if k != 'default'), key=lambda k: themes[k].get('name', k).lower())
    return [(themes[k].get('name', k), k) for k in ['default'] + rest if k in themes]

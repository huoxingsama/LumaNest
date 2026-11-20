"""The window furniture around whatever page is showing.

The top bar, the offline/update banner, the download bar and the toast.
Function-per-piece taking the browser, mirroring ``hud.py`` — these are large
widget trees that read a lot of shell state and own none of it, so a class
would only give them a second ``self`` to confuse with the first.

Not to be confused with ``components/chrome.py``, which is the *content
area's* primitives (the busy and error states). This is the frame; that is
what goes inside it.

``build()`` stays on the browser: assembling the window from these pieces,
the page, the dialogs and the now-playing bar is the shell's actual job. So
does everything with state or a thread behind it — ``set_status``,
``_arm_toast_clear``, ``set_download_status``, ``_poll_download_status``,
``notify_update`` — because the toast timer and the download-bar poller are
registered in the browser's daemon table by attribute name.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import time
from ..i18n import _, _p
from ..mpvtk.layout import natural_size
from ..mpvtk.widgets import Box, Button, Dropdown, Float, Gradient, Icon, Menu, Progress, Row, Spacer, Stack, Text, TextBox
from . import theme
from .components import server_icon
log = logging.getLogger('mpvtk_browser.chrome')

def toast_node(b, w, h):
    """Status messages as a floating toast, on every screen.

    ``status`` used to be rendered in exactly one place — the Settings
    tab — while being written from fourteen, including _edit_call's
    "the change could not be applied". So a rejected Add to Playlist
    from the home screen wrote its error to a field that never reached
    a pixel, which is the very thing _edit_call exists to prevent."""
    if not b.status:
        return None
    left = b.TOAST_SECS - (time.time() - (b._status_at or 0))
    if left <= 0:
        b.status = ''
        return None
    b._arm_toast_clear(left)
    tw = min(max(320, len(b.status) * 9), max(360, w - 80))
    return Float(Box([Text(b.status, size='normal', wrap=True, w=tw - 32)], pad=16, bg=theme.CARD_BG, radius=10, border=theme.BORDER, align='stretch'), x=(w - tw) / 2, y=max(20, h - 140), w=tw)

def _may_syncplay(b):
    """Whether to offer this user SyncPlay at all, on the current server.

    Fails open in every direction — a source without the method (the
    offline one declares it, but a test double may not), no server yet, or
    an error asking — because the cost of being wrong that way is the
    button being there, which is where it was before.
    """
    from ..user_policy import NO_SYNCPLAY
    source, server = (getattr(b, 'source', None), getattr(b, 'server', None))
    if source is None or server is None:
        return True
    ask = getattr(source, 'syncplay_access', None)
    if ask is None:
        return True
    try:
        return ask(server) != NO_SYNCPLAY
    except Exception:
        return True

def chrome(b, w, immersive=False):
    from .components.cinema_chrome import render
    servers, users = chrome_lists(b)
    probe = chrome_bar(b, compact=False, probe=True, servers=servers, users=users, immersive=True)
    compact = natural_size(probe)[0] + b.TITLE_MIN_W > w
    bar = chrome_bar(b, compact=compact, servers=servers, users=users, immersive=True)
    return render(b, bar, w)

def chrome_lists(b):
    """The switcher's two lists, read once per frame (see ``chrome``).

    Servers come from the **credential list** rather than from the source
    while there is a live source to come back to. A server that did not answer
    is not in the source, so it used to vanish from the switcher entirely: the
    machine looked like it had one server, and there was nowhere to press to
    get the other one back. Listing it greyed, with the reconnect behind
    selecting it, is what makes that recoverable without going to Settings.

    Offline too (D3 case 3, ruled 2026-09-26): picking a server there is how
    you come back, and it reconnects or says why. The offline source's own
    list is not a fallback then -- the Offline entry stands for it.
    """
    servers = None
    if b.controller is not None:
        try:
            servers = b.controller.switcher_servers()
        except Exception:
            log.debug('switcher_servers failed', exc_info=True)
            servers = None
    if not servers and b._offline:
        servers = []
    elif not servers:
        try:
            servers = b.source.servers()
        except Exception:
            servers = []
    return (servers, b._users())

def _has_downloads(b):
    """Whether the Offline entry is offered. Asked of the gateway, which
    keeps it cheap enough for every frame (a catalog count)."""
    ask = getattr(b.controller, 'has_downloads', None)
    if ask is None:
        return False
    try:
        return bool(ask())
    except Exception:
        log.debug('has_downloads failed', exc_info=True)
        return False

def _server_label(sv):
    """A switcher entry's text. Offline ones say so in words as well as by
    their icon: the icon is the half that survives a narrow bar, since the
    label ellipsizes from the end and takes "(offline)" with it, but the icon
    is a glyph nobody has to have learned and the word is what makes it one
    the first time."""
    name = sv.get('name') or '?'
    if sv.get('connected', True):
        return name
    return _('%s (needs reconnect)') % name
WINDOW_CONTROL_W = theme.NAV_HEIGHT

def window_controls(b, prefix='win', icon_size=16, w=WINDOW_CONTROL_W, fg=None, gap=4):
    """Minimize / maximize / close, for a window the desktop drew no title
    bar for. See ``WindowMixin.window_controls_wanted``.

    Shared by the library's top bar and the playback HUD's, parameterized
    rather than written twice: they are the same three buttons doing the
    same three things, and the failure mode of a second copy is a window
    you can close from one screen and not the other. ``prefix`` keeps the
    node ids distinct, since a scene may not have two nodes with one id.

    Only the close button departs from the shared hover: on every desktop
    it is the one that is coloured, and it is the only one here whose
    misclick costs anything.
    """
    if not b.window_controls:
        return []

    def ctl(icon, node_id, cb, tip, hover=None):
        return Button('', id='%s-%s' % (prefix, node_id), icon=icon, size=icon_size,
                      icon_size=icon_size, w=w, h=w, pad=0, radius=w / 2,
                      justify='center', fg=fg, tip=tip, on_click=cb, flat=True,
                      hover=hover or {'fill': theme.BUTTON_ACTIVE, 'circle': True})
    return [Spacer(w=gap), ctl('minimize', 'min', b.minimize_window, _('Minimize')), ctl('filter_none' if b.maximized else 'crop_square', 'max', b.toggle_maximized, _('Restore') if b.maximized else _('Maximize')), ctl('close', 'close', b.close_window, _('Close'), hover={'fill': theme.FAV_RED, 'circle': True})]
RESIZE_GRIP = 22

def resize_grip(b, w, h):
    """The bottom-right corner, draggable to resize the window.

    Only where the desktop drew no frame to resize by, and never while
    maximized (there the geometry write would silently un-maximize instead
    -- see the renderer's on_mouse_down).

    On Wayland this is usually dead weight in the best way: mpv implements
    its own edge-resize zone when ``border`` is false, and takes the press
    in the compositor's protocol before it can reach a script. The grip is
    for everywhere else -- and it is what the dots on screen are for
    either way, since mpv's zone is invisible.

    A ``Float`` so the corner is the *window's* corner, not the corner of
    whatever the page happens to end in; and last in the scene, so it is
    over the now-playing bar rather than under it. The dots are ordinary
    Boxes with no interaction of their own, which is what leaves the hit
    rect underneath them the topmost thing ``node_at`` will return.
    """
    if not b.window_controls or b.maximized or (not w) or (not h):
        return None
    dots = [Box([], w=3, h=3, bg=theme.SUBTLE_FG, radius=1, anchor='se', dx=-3, dy=-3), Box([], w=3, h=3, bg=theme.SUBTLE_FG, radius=1, anchor='se', dx=-9, dy=-3), Box([], w=3, h=3, bg=theme.SUBTLE_FG, radius=1, anchor='se', dx=-3, dy=-9)]
    return Float(Box([Stack(dots, w=RESIZE_GRIP, h=RESIZE_GRIP)], id='win-resize', w=RESIZE_GRIP, h=RESIZE_GRIP, window_resize='se', tip=_('Resize')), x=w - RESIZE_GRIP, y=h - RESIZE_GRIP)

def chrome_bar(b, compact, probe=False, servers=None, users=None, immersive=False):
    title = '' if probe else b.route.get('bar_title') or b.route.get('title') or _('Home')
    accent_style = dict(theme.chrome_button_style(), size='normal', icon_size=20,
                        h=theme.NAV_HEIGHT, pad=(16, 0), gap=theme.NAV_GAP,
                        radius=theme.NAV_HEIGHT / 2, justify='center')

    def nav_button(label, node_id, icon, cb, on_context=None):
        return Button('' if compact else label, id=node_id, icon=icon, on_click=cb,
                      tip=label if compact else None, on_context=on_context,
                      **(dict(accent_style, w=theme.NAV_HEIGHT, pad=0) if compact else accent_style))
    left = []
    if b._nav.can_go_back:
        left.append(nav_button(_('Back'), 'nav-back', 'arrow_back', b.go_back, on_context=lambda x, y: open_history_menu(b, x, y)))
    left.append(nav_button(_('Home'), 'nav-home', 'home', b.go_home, on_context=None if b._nav.can_go_back or not b._nav.forward else lambda x, y: open_history_menu(b, x, y)))
    if not b._offline and (b.route or {}).get('kind') == 'home':
        left.append(nav_button(_('Favorites'), 'nav-favorites', 'favorite', lambda: b.navigate({'kind': 'favorites', 'server': b.server, 'title': _('Favorites')})))
    right = []
    if servers is None:
        servers = chrome_lists(b)[0]
    labels = [_server_label(s) for s in servers]
    icons = [server_icon(s) for s in servers]
    offline_at = None
    if b._offline or _has_downloads(b):
        offline_at = len(labels)
        labels.append(_p('server switcher entry', 'Offline'))
        icons.append('folder')
    if len(labels) > 1:
        if b._offline and offline_at is not None:
            cur = offline_at
        else:
            cur = next((i for i, s in enumerate(servers) if s['uuid'] == b.server), 0)

        def chosen(i, _v):
            if i == offline_at:
                b.go_offline()
            else:
                b._switch_server(servers[i]['uuid'])
        right.append(Dropdown('nav-server', labels, selected=cur, min_w=110, tip=_('Server'), force=True, ack=b._server_pick_ack, max_w=170 if compact else 300, popup_w=460, icons=icons, on_select=chosen, size='normal', h=theme.NAV_HEIGHT, radius=theme.NAV_HEIGHT / 2))
    if users is None:
        users = b._users()
    if len(users) > 1:
        cur = next((i for i, u in enumerate(users) if u.get('active')), 0)
        right.append(Dropdown('nav-user', [u.get('name', '?') for u in users], selected=cur, min_w=100, tip=_('User'), max_w=190 if compact else 280, force=True, popup_w=380, icons=['lock' if u.get('locked') else 'person' for u in users], on_select=lambda i, v: b._switch_user(users[i]), size='normal', h=theme.NAV_HEIGHT, radius=theme.NAV_HEIGHT / 2))
    from .components.cinema_chrome import open_search
    right += [Button('', id='nav-search-go', icon='search', tip=_('Search'),
                     on_click=lambda: open_search(b), **dict(accent_style, w=theme.NAV_HEIGHT, pad=0)),
              nav_button(_('Settings'), 'nav-settings', 'settings', b._open_settings)]
    if _may_syncplay(b):
        right.insert(-1, nav_button(_('SyncPlay'), 'nav-syncplay', 'groups', b._open_syncplay))
    controls = window_controls(b, icon_size=18, gap=0)
    if controls:
        right.append(Row(controls[1:], gap=theme.NAV_GAP, h=theme.NAV_HEIGHT, align='center'))
    middle = [Text(title, id='nav-page-title', size='normal', flex=1, min_w=0)] if b.route.get('kind') != 'home' else [Spacer()]
    bar = Row(left + middle + right, pad=(16, 8), gap=theme.NAV_GAP, align='center',
              h=theme.NAV_BAR_HEIGHT, bg=None if immersive or theme.topbar_gradient() else theme.PANEL_BG,
              window_drag=b.window_controls)
    stops = None if immersive else theme.topbar_gradient()
    if not stops:
        return bar
    return Stack([Gradient(stops=stops, axis='x', h=60), bar], h=60)
HISTORY_MAX = 12

def history_entries(b):
    """The page stack as (label, icon, depth) rows, root first.

    Ordered as a stack rather than as two "recent first" lists, because
    that is the question the menu answers: where am I, and what is on
    either side of me. ``depth`` is the 1-based stack position a pick
    rewinds to; the pages ahead carry the depth they would occupy, so
    picking one is that many forward steps.
    """
    stack = list(b._nav.stack)
    ahead = list(reversed(b._nav.forward))
    here = len(stack)

    def label(route):
        return route.get('title') or _('Home')
    rows = [(label(r), 'chevron_left', i + 1) for i, r in enumerate(stack)]
    rows[-1] = (label(stack[-1]), 'check', here)
    rows += [(label(r), 'chevron_right', here + i + 1) for i, r in enumerate(ahead)]
    if len(rows) <= HISTORY_MAX:
        return rows
    lo = min(max(0, here - 1 - HISTORY_MAX // 2), len(rows) - HISTORY_MAX)
    return rows[lo:lo + HISTORY_MAX]

def open_history_menu(b, x, y):
    b._menu = {'kind': 'history', 'x': x, 'y': y}
    b.invalidate()

def history_menu_node(b):
    m = b._menu
    entries = history_entries(b)

    def pick(index, _value):
        b._close_menu()
        if not 0 <= index < len(entries):
            return
        depth = entries[index][2]
        if depth < b._nav.depth:
            b.go_back_to(depth)
        elif depth > b._nav.depth:
            b.go_forward_to(depth)
    return Menu('historymenu', [e[0] for e in entries], m['x'], m['y'], icons=[e[1] for e in entries], on_select=pick, on_dismiss=b._close_menu)

def banner(b):
    if getattr(b, '_restart_keys', None):
        cfg = b._config()
        names = sorted((cfg.label_for(k) for k in b._restart_keys))
        row = [Text(_('Restart to apply: %s') % ', '.join(names), size='normal'), Spacer()]
        if b.can_restart():
            row.append(Button(_('Restart Now'), id='banner-restart', on_click=b._restart_now))
        row.append(Button(_p('Restart banner', 'Later'), id='banner-restart-dismiss', on_click=b._dismiss_restart))
        return Row(row, pad=10, gap=10, align='center', h=48, bg=theme.ACCENT_SOFT)
    if b._update:
        return Row([Text((_('Update available: %s — run “flatpak update”') if b._update.get('flatpak') else _('Update available: %s')) % b._update['version'], size='normal'), Spacer(), Button(_('Details…'), id='banner-open', on_click=lambda: b._open_url(b._update['url'])), Button(_p('update banner', 'Later'), id='banner-dismiss', on_click=b._dismiss_update), Button(_('Ignore'), id='banner-ignore', tip=_('Do not mention this version again'), on_click=b._ignore_update)], pad=10, gap=10, align='center', h=48, bg=theme.ACCENT_SOFT)
    if b._unreachable and (not b._offline):
        row = [Text(_("The server isn't answering."), size='normal'), Spacer(), Button(_('Retry'), id='banner-unreachable-retry', on_click=b.retry_unreachable)]
        if _has_downloads(b):
            row.append(Button(_('Go Offline'), id='banner-unreachable-offline', on_click=b.go_offline))
        return Row(row, pad=10, gap=10, align='center', h=48, bg=theme.mix(theme.WARN_AMBER, theme.WINDOW_BG, 0.72))
    if b._offline and (not b._offline_chosen):
        return Row([Text(_("Offline — showing what's available."), size='normal'), Spacer(), Button(_('Configure Servers'), id='banner-servers', on_click=b.show_login), Button(_('Retry'), id='banner-retry', on_click=b._retry_connect)], pad=10, gap=10, align='center', h=48, bg=theme.mix(theme.WARN_AMBER, theme.WINDOW_BG, 0.72))
    return None

def download_bar(b):
    """A persistent bar while downloads are outstanding, with a way into
    the manager. Downloads are otherwise completely invisible once the
    confirm dialog closes."""
    st = b._dl_status
    if not st or not st.get('pending'):
        return None
    name = st.get('name') or ''
    pct = st.get('percent')
    left = (_('Downloading %(name)s — %(n)d remaining') if name else _('Downloading — %(n)d remaining')) % {'name': name, 'n': st['pending']}
    row = [Icon('file_download', 20), Text(left, size='normal')]
    if pct is not None:
        row.append(Progress(pct / 100.0, w=160))
        row.append(Text('%d%%' % pct, size='small', w=48, color=theme.SUBTLE_FG))
    row += [Spacer(), Button(_('View Downloads'), id='dlbar-view', on_click=lambda: b.open_settings('downloads'))]
    return Row(row, pad=10, gap=10, align='center', h=44, bg=theme.PANEL_BG)

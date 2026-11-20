"""Login, the lock screen, and user switching.

Covers the login form and Quick Connect, the startup-PIN lock gate, user
switching with its PIN prompt, and per-user PIN setup.

State on ``self``: ``_login`` / ``_login_error`` mirror the login form
(the renderer owns the live text; we mirror so Connect can read all three
fields at once), and ``_pin`` / ``_pin_error`` / ``_locked`` drive the lock
gate. Reads ``self.source`` and ``self.server``; leaves the screen via
core's ``navigate`` / ``nav_stack``.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from ..constants import REAUTH_WRONG_SERVER
from ..i18n import _, _p
from ..mpvtk.widgets import Box, Busy, Button, Checkbox, Column, Dialog, Icon, Row, Spacer, Text, TextBox, VScroll
from . import theme
from .components import chrome, server_icon
log = logging.getLogger('mpvtk_browser.auth')

class AuthMixin:
    _busy = staticmethod(chrome.busy)
    ROUTES = {'connecting': (None, '_render_connecting'), 'locked': (None, '_render_locked'), 'login': (None, '_render_login')}

    def _users(self):
        """Local users for the switcher: ``[{id, name, locked, active}]``."""
        if self.controller is None:
            return []
        try:
            return list(self.controller.list_users() or [])
        except Exception:
            log.debug('list_users failed', exc_info=True)
            return []

    def _switch_user(self, user):
        user = next((u for u in self._users() if u.get('id') == user.get('id')), None)
        if user is None or user.get('active'):
            return
        if user.get('locked'):
            self._ask_pin(user)
        else:
            self._do_switch_user(user, None)

    def _ask_pin(self, user):
        state = {'pin': '', 'error': None}

        def build():
            rows = [Text(_('Switch to %s') % user.get('name', ''), size='title', bold=True)]
            if state['error']:
                rows.append(Text(state['error'], size='small', color=theme.FAV_RED))
            rows += [TextBox('switch-pin', placeholder=_('PIN'), mask=True, w=240, on_change=lambda v: state.__setitem__('pin', v), on_submit=lambda v: submit()), self._dialog_buttons([Button(_('Cancel'), id='switch-cancel', on_click=self._close_dialog), Button(_('Switch'), id='switch-ok', on_click=submit)])]
            return Dialog('switchpin', self._dialog_shell('switchpin', rows), on_dismiss=self._close_dialog)

        def submit():
            self._do_switch_user(user, state['pin'], on_bad_pin=lambda: (state.__setitem__('error', _('Incorrect PIN.')), self._show_dialog(build)))
        self._show_dialog(build)

    def _do_switch_user(self, user, pin, on_bad_pin=None):
        ep = self._epoch
        self._switch_gen += 1
        gen = self._switch_gen
        if self._switch_from is None:
            self._switch_from = next((u.get('id') for u in self._users() if u.get('active')), None)
            self.invalidate()

        def work():
            return self.controller.switch_user(user.get('id'), pin)

        def let_go():
            if gen == self._switch_gen:
                self._switch_from = None

        def done(source):
            if gen != self._switch_gen:
                return
            if source is False:
                if on_bad_pin is not None:
                    on_bad_pin()
                return
            self._close_dialog()
            if source is None:
                self._locked = False
                if self._profile_has_servers():
                    self.show_connecting()
                    self.connect_failed()
                else:
                    self.show_login()
                return
            self._locked = False
            self.set_source(source)
        self.run_async(work, done, ep, always=let_go)

    def _open_pin_setup(self, u):
        state = {'cur': '', 'new': '', 'confirm': '', 'startup': bool(u.get('require_startup')), 'error': None}

        def build():
            rows = [Text(_('Set PIN for %s') % u.get('name', ''), size='title', bold=True)]
            if state['error']:
                rows.append(Text(state['error'], size='small', color=theme.FAV_RED))
            if u.get('locked'):
                rows.append(self._pin_field(_('Current PIN'), 'ps-cur', state, 'cur', on_submit=save))
            rows += [self._pin_field(_('New PIN'), 'ps-new', state, 'new', on_submit=save), self._pin_field(_p('form label', 'Confirm'), 'ps-confirm', state, 'confirm', on_submit=save), Checkbox(_('Require this PIN at startup'), state['startup'], id='ps-startup', on_toggle=lambda: (state.__setitem__('startup', not state['startup']), self._show_dialog(build))), Row([Button(_('Remove PIN'), id='ps-remove', on_click=lambda: save(remove=True)) if u.get('locked') else Spacer(h=0), Spacer(), Button(_('Cancel'), id='ps-cancel', on_click=self._close_dialog), Button(_('Save'), id='ps-ok', on_click=save)], gap=10, align='center', justify='end')]
            return Dialog('pinsetup', self._dialog_shell('pinsetup', rows, w=460), on_dismiss=self._close_dialog)

        def save(remove=False):
            if self.controller is None:
                return self._close_dialog()
            if u.get('locked'):
                try:
                    ok = self.controller.unlock_user(u.get('id'), state['cur'])
                except Exception:
                    log.debug('pin verify failed', exc_info=True)
                    ok = False
                if not ok:
                    state['error'] = _('Current PIN is incorrect.')
                    return self._show_dialog(build)
            if not remove and (not state['new']):
                state['error'] = _('Enter a new PIN.')
                return self._show_dialog(build)
            if not remove and state['new'] != state['confirm']:
                state['error'] = _("The PINs don't match.")
                return self._show_dialog(build)
            try:
                ok = self.controller.set_user_pin(u.get('id'), None if remove else state['new'], require_startup=state['startup'])
            except Exception:
                log.error('set_user_pin failed', exc_info=True)
                ok = False
            if not ok:
                state['error'] = _('The PIN could not be removed.') if remove else _('The PIN could not be saved.')
                return self._show_dialog(build)
            self._close_dialog()
            self._after_users_changed()
        self._show_dialog(build)

    @staticmethod
    def _pin_field(label, node_id, state, key, on_submit=None):
        return Row([Text(label, w=140, size='normal', color=theme.SUBTLE_FG), TextBox(node_id, placeholder=label, mask=True, w=200, on_change=lambda v: state.__setitem__(key, v), on_submit=None if on_submit is None else lambda v: on_submit())], gap=10, align='center')

    def show_login(self, reauth=None):
        """Show the add-server / login screen.

        Only resets the nav stack when there is nowhere to go back *to*: with
        servers already connected this is "add another", and cancelling has
        to return you to the library rather than trapping you on the form.

        ``reauth`` is a saved server whose token the server no longer accepts
        -- ``{"uuid", "name", "address", "username"}``. The same form, because
        it is the same three fields and the same two ways to fill them, but
        the login is sent as a **replacement** for that server rather than as
        a new one: see ``ClientManager.reauthenticate`` for why the identity
        has to survive. Without this the only way back into a signed-out
        server was to remove it and add it again, which silently orphaned
        every download made from it.
        """
        route = {'kind': 'login', 'title': _('Sign In Again') if reauth else _('Add Server')}
        if reauth:
            route['_reauth'] = dict(reauth)
            self._login['server'] = reauth.get('address') or ''
            self._login['user'] = reauth.get('username') or ''
            self._login['pass'] = ''
            self._login_error = None
        else:
            self._login = {'server': '', 'user': '', 'pass': ''}
            self._login_error = None
        self._discovered = []
        if self.server is None and (not reauth):
            self.navigate(route, reset=True)
        else:
            self.navigate(route)
        if not reauth:
            self._scan_for_servers()

    def _render_login(self, route, size):

        def field(fid, ph, key, mask=False):
            return Row([Text(ph, w=140, size='normal', color=theme.SUBTLE_FG), TextBox(fid, text=self._login[key], placeholder=ph, mask=mask, w=360, on_change=lambda v, k=key: self._login.__setitem__(k, v), on_submit=lambda v: self._do_login())], gap=12, align='center')
        qc = route.get('_qc')
        reauth = route.get('_reauth')
        if reauth:
            rows = [Text(_('Sign In Again'), size='hero', bold=True), Text(_('Your saved login for %s is no longer accepted. Signing in again keeps its downloads and settings.') % (reauth.get('name') or reauth.get('address') or ''), size='small', color=theme.SUBTLE_FG, wrap=True, w=460)]
        else:
            rows = [Text(_('Connect to Jellyfin'), size='hero', bold=True)]
        if self._login_error:
            rows.append(Text(self._login_error, size='small', color=theme.FAV_RED))
        known = []
        if self.controller is not None and (not qc) and (not reauth):
            try:
                known = self.controller.known_servers() or []
            except Exception:
                known = []
        servers = []
        if not qc and (not reauth):
            servers += self._discovered_rows(route)
        if known:
            servers.append(Text(_('Previously added servers'), size='small', color=theme.SUBTLE_FG))
            for i, k in enumerate(known):
                addr = k.get('address', '')
                servers.append(Row([Icon(server_icon(k), 16, color=theme.SUBTLE_FG), Text(k.get('name') or addr, size='normal', flex=1), Button(_('Use'), id='login-known-%d' % i, size='small', on_click=lambda a=addr: self._use_known_server(a)), Button(_('Quick Connect'), id='login-known-qc-%d' % i, size='small', icon='phonelink_lock', on_click=lambda a=addr: self._quick_connect_to(route, a))], id='login-known-row-%d' % i, pad=8, gap=10, radius=6, align='center', bg=theme.PANEL_BG))
        rest = []
        if qc:
            rest += [Text(_('Quick Connect'), size='large', bold=True), Text(_('Enter this code in the Jellyfin app or web client:'), size='small', color=theme.SUBTLE_FG, wrap=True, w=460), Text(qc.get('code') or _('Requesting…'), size=44, bold=True, align='center'), Text(qc.get('status') or '', size='small', color=theme.SUBTLE_FG, align='center'), self._dialog_buttons([Button(_('Cancel'), id='login-qc-cancel', on_click=lambda: self._cancel_quick_connect(route))])]
        else:
            rest += [field('login-server', _('Server URL'), 'server'), field('login-user', _('Username'), 'user'), field('login-pass', _('Password'), 'pass', mask=True), Row([Button(_('Use Quick Connect'), id='login-qc', icon='phonelink_lock', on_click=lambda: self._start_quick_connect(route)), Spacer(), Button(_('Cancel'), id='login-cancel', on_click=self.go_back) if len(self.nav_stack) > 1 else Spacer(h=0), Button(_('Sign In') if reauth else _('Connect'), id='login-connect', on_click=self._do_login)], gap=10, align='center')]
        if servers:
            rows.append(self._server_box(servers, rows + rest, size))
        form = Column(rows + rest, pad=self.LOGIN_PAD, gap=self.LOGIN_GAP, bg=theme.CARD_BG, radius=12, border=theme.BORDER, w=self.LOGIN_W, align='stretch')
        return Box([Spacer(), Row([Spacer(), form, Spacer()]), Spacer()], flex=1, direction='column', align='stretch', gap=10)
    LOGIN_W = 560
    LOGIN_PAD = 28
    LOGIN_GAP = 16

    def _server_box(self, servers, others, size):
        """The saved and discovered servers, scrolling on their own.

        Both lists used to sit in the form directly, above the fields, so
        each server pushed Server URL and Connect further down. At 1280x720,
        three of each was enough to put Connect below the window. This box is
        as tall as its rows when they fit and capped at whatever the window
        leaves after the rest of the form when they do not, so the form
        looks the same with a few servers and stays usable with eighty.
        """
        from ..mpvtk.layout import measure_h
        inner_w = self.LOGIN_W - 2 * self.LOGIN_PAD
        body = Column(servers, gap=self.LOGIN_GAP, align='stretch')
        natural = measure_h(body, inner_w)
        spent = measure_h(Column(others, pad=self.LOGIN_PAD, gap=self.LOGIN_GAP, w=self.LOGIN_W, align='stretch'), self.LOGIN_W)
        room = size[1] - spent - self.LOGIN_GAP - 2 * 24
        cap = max(120, room)
        if natural <= cap:
            return body
        return VScroll(body, id='login-servers', h=cap)

    def _scan_for_servers(self):
        """Look for servers on this network, in the background.

        A second of broadcast on the loop thread would freeze the form for a
        second, and the answer is worth nothing if the user cannot type while
        it arrives.

        Nothing is selected and nothing is signed into: the replies are not
        authenticated -- anything on the network can answer with any name and
        any address -- so this offers addresses and the user chooses one.
        """
        if self.controller is None:
            return
        scan = getattr(self.controller, 'discover_servers', None)
        if scan is None:
            return
        ep = self._epoch

        def done(found):
            self._discovered = list(found or [])
            if self._discovered:
                self.invalidate()
        self.run_async(lambda: scan(), done, ep)

    def _discovered_rows(self, route):
        """The "Servers on your network" block, or [] when nothing answered.

        **The address is shown, not only the name.** A discovery reply is
        unauthenticated, so the name is whatever the answering machine chose
        to call itself and the address is the only part a user can judge --
        see the apiclient's `discovery` module, which says the same thing to
        its own callers.
        """
        found = [s for s in self._discovered or [] if s.get('Address')]
        if not found:
            return []
        rows = [Text(_('Servers on your network'), size='small', color=theme.SUBTLE_FG)]
        for i, server in enumerate(found):
            address = server.get('Address') or ''
            name = server.get('Name') or address
            rows.append(Row([Icon('lan', 16, color=theme.SUBTLE_FG), Column([Text(name, size='normal'), Text(address, size='small', color=theme.SUBTLE_FG)], gap=2, flex=1), Button(_('Use'), id='login-found-%d' % i, size='small', on_click=lambda a=address: self._use_known_server(a))], id='login-found-row-%d' % i, pad=8, gap=10, radius=6, align='center', bg=theme.PANEL_BG))
        return rows

    def _use_known_server(self, address):
        self._login['server'] = address
        self.invalidate()

    def _quick_connect_to(self, route, address):
        """Fill the URL and start Quick Connect against it in one step."""
        self._login['server'] = address
        self._start_quick_connect(route)

    def _start_quick_connect(self, route):
        server = (self._login.get('server') or '').strip()
        if not server:
            self._login_error = _('Enter the server URL first.')
            self.invalidate()
            return
        if self.controller is None:
            return
        self._login_error = None
        qc = route['_qc'] = {'code': None, 'status': _('Contacting the server…'), 'cancelled': False}
        self.invalidate()
        ep = self._epoch
        before = self._known_server_uuids()
        owner = self._switch_from

        def on_code(code):
            if not qc['cancelled']:
                qc['code'] = code
                qc['status'] = _('Waiting for approval…')
                self.invalidate()
        reauth = route.get('_reauth')

        def cancelled():
            return qc['cancelled']

        def work():
            if reauth:
                return self.controller.reauthenticate_quick_connect(reauth['uuid'], on_code, cancelled, address=server)
            if owner is not None:
                return (self.controller.quick_connect(server, on_code, cancelled, owner_id=owner), None)
            return (self.controller.quick_connect(server, on_code, cancelled), None)

        def done(result):
            ok, reason = result
            if qc['cancelled']:
                return
            if route.get('_qc') is qc:
                route.pop('_qc', None)
            if ok:
                self._login_error = None
                self._after_login(before, landing=reauth['uuid'] if reauth else None)
            else:
                self._login_error = self._login_failure(reason, _('Quick Connect was not approved.'))
        self.run_async(work, done, ep)

    def _cancel_quick_connect(self, route):
        qc = route.get('_qc')
        if qc is not None:
            qc['cancelled'] = True
        route.pop('_qc', None)
        self.invalidate()

    @staticmethod
    def _login_failure(reason, fallback):
        """The error line under the form.

        Only a reason the user would act on differently earns its own
        string. A wrong address is that: the generic line asks them to check
        their details, and someone whose password is right retypes it
        instead of looking at the field that is actually wrong.
        """
        if reason == REAUTH_WRONG_SERVER:
            return _('That address is a different server. To sign in to it, add it as a new server instead.')
        return fallback

    def _do_login(self):
        if self.controller is None:
            return
        info = dict(self._login)
        reauth = (self.route or {}).get('_reauth')
        self._login_error = _('Connecting…')
        self.invalidate()
        ep = self._epoch
        before = self._known_server_uuids()
        owner = self._switch_from

        def work():
            if reauth:
                return self.controller.reauthenticate(reauth['uuid'], info['user'], info['pass'], address=info['server'])
            if owner is not None:
                return (self.controller.add_server(info['server'], info['user'], info['pass'], owner_id=owner), None)
            return (self.controller.add_server(info['server'], info['user'], info['pass']), None)

        def done(result):
            ok, reason = result
            if ok:
                self._login_error = None
                self._login['pass'] = ''
                self._after_login(before, landing=reauth['uuid'] if reauth else None)
            else:
                generic = _('Could not sign in. Please check your details.') if reauth else _('Could not connect. Please check your details.')
                self._login_error = self._login_failure(reason, generic)
        self.run_async(work, done, ep)

    def _known_server_uuids(self):
        """The servers the current source has, before a login adds one."""
        try:
            return {s.get('uuid') for s in self.source.servers() or []}
        except Exception:
            return set()

    def _after_login(self, before=None, landing=None):
        """Land on the server that was just added, not the last one browsed.

        `set_source` with no uuid asks `_pick_server`, whose fallback is
        `get_last_server()` -- right for a reconnect and wrong here, because
        adding a server IS a request to look at it. Adding one from the
        server manager dropped you back on the previous server's home, which
        reads as the login having done nothing.

        Identified by difference rather than by plumbing a uuid back through
        `add_server` -> `clientManager.login` -> `_finalize_login`: this form
        can also re-authenticate a server already in the list, which keeps its
        uuid (`replacing_uuid`), and there the honest answer is "nothing new" --
        exactly what an empty difference says. `before=None` keeps the old behaviour for any caller that did
        not sample it.

        ``landing`` names the server outright, for the one case the
        difference cannot answer: re-authenticating adds nothing, so the
        difference is empty by construction and the fallback would land on
        whichever server was browsed last rather than the one just signed
        back into.
        """
        source = None
        if self.controller is not None:
            try:
                source = self.controller.rebuild_source()
            except Exception:
                log.warning('rebuild_source failed', exc_info=True)
        if source is None:
            return
        added = landing
        if added is None and before is not None:
            try:
                added = next((s.get('uuid') for s in source.servers() or [] if s.get('uuid') not in before), None)
            except Exception:
                log.debug('could not identify the new server', exc_info=True)
        self.set_source(source, server_uuid=added)

    def show_locked(self):
        """Show the startup-PIN unlock gate.

        Idempotent: re-locking an already-locked UI must not wipe a PIN the
        user is halfway through typing (the tray can fire show/hide at any
        moment)."""
        if self._locked:
            return
        self._locked = True
        self._pin['pin'] = ''
        self._pin_error = None
        self.navigate({'kind': 'locked', 'title': _('Locked')}, reset=True)

    def maybe_relock(self):
        """Re-gate the UI behind the startup PIN when the window is released
        or re-surfaced. Unlocking once must not leave the client open for the
        rest of the process's life — closing to the tray and re-raising
        re-prompts, matching the Tk browser."""
        if self.controller is None:
            return
        try:
            if self.controller.needs_unlock():
                self.show_locked()
        except Exception:
            log.debug('relock check failed', exc_info=True)

    def show_connecting(self):
        """Come up on the connecting screen while the network settles.

        The browser used to open straight onto an empty home route, which
        rendered as a bare ``_busy()`` spinner: no explanation, and — with a
        server that never answers — no way out short of killing the app.
        Tk had this screen; mpvtk had the route name in ``CHROME_FREE`` and
        nothing behind it."""
        self.navigate({'kind': 'connecting'}, reset=True)

    def _render_connecting(self, route, size):
        rows = [Text(_('Connecting to your server…'), size='page', bold=True), Spacer(h=8), Row([Spacer(), Busy(), Spacer()])]
        if route.get('_connect_error'):
            rows.append(Spacer(h=8))
            rows.append(Text(route['_connect_error'], size='small', color=theme.FAV_RED))
        actions = []
        if self._have_downloads():
            actions.append(Button(_('Work Offline'), id='conn-offline', icon='file_download', on_click=self._work_offline))
        if route.get('_connect_error'):
            actions.append(Button(_('Retry'), id='conn-retry', icon='refresh', on_click=self._retry_connect))
            actions.append(Button(_('Sign In'), id='conn-login', icon='person', on_click=self.show_login))
        if actions:
            rows.append(Spacer(h=14))
            rows.append(Row([Spacer()] + actions + [Spacer()], gap=10))
        card = Column(rows, pad=32, gap=8, bg=theme.CARD_BG, radius=12, border=theme.BORDER, w=480, align='stretch')
        return Box([Spacer(), Row([Spacer(), card, Spacer()]), Spacer()], flex=1, direction='column', align='stretch')

    def _profile_has_servers(self):
        """Has the ACTIVE profile a saved login? Not `known_servers`, which
        answers for every local profile (it is there so a new one need not
        retype an address): a profile would be offered Retry on servers it
        never signed in to."""
        if self.controller is None:
            return False
        try:
            return bool(self.controller.list_servers())
        except Exception:
            log.debug('list_servers failed', exc_info=True)
            return False

    def _have_downloads(self):
        if self.controller is None:
            return False
        try:
            return bool(self.controller.has_downloads())
        except Exception:
            log.debug('has_downloads failed', exc_info=True)
            return False

    def _work_offline(self):
        """Give up on the connect and browse the catalog instead.

        Does not touch the ``work_offline`` *setting* — this is a one-shot
        escape from a slow startup, not a preference change. If the connect
        lands later, set_source swaps the live source in over the top."""
        if self.controller is None:
            return
        ep = self._epoch

        def work():
            return self.controller.offline_source()

        def done(source):
            if source is None:
                self.set_status(_('There is nothing downloaded to browse.'))
                self.invalidate()
                return
            self.set_source(source)
        self.run_async(work, done, ep)

    def connect_failed(self, message=None):
        """The startup connect gave up. Called from the connect thread."""
        if self.route.get('kind') != 'connecting':
            return
        self.route['_connect_error'] = message or _("Couldn't reach your saved server(s).")
        self.invalidate()

    def _render_locked(self, route, size):
        """Startup PIN gate.

        A full page rather than a modal, and it offers the other local users
        — a locked user must not be able to lock the whole client out, which
        is what a bare PIN prompt with no way past it amounts to."""
        users = [u for u in self._users() if not u.get('active')]
        active = next((u.get('name') for u in self._users() if u.get('active')), None)
        rows = [Text(_('Enter your PIN'), size='hero', bold=True), Text(_('%s is locked.') % active if active else '', size='normal', color=theme.SUBTLE_FG)]
        if self._pin_error:
            rows.append(Text(self._pin_error, size='small', color=theme.FAV_RED))
        rows += [TextBox('lock-pin', text='', placeholder=_('PIN'), mask=True, w=260, on_change=lambda v: self._pin.__setitem__('pin', v), on_submit=lambda v: self._do_unlock()), Row([Button(_('Unlock'), id='lock-unlock', icon='lock', on_click=self._do_unlock)], gap=10, justify='end')]
        if users:
            rows.append(Spacer(h=6))
            rows.append(Text(_('Or switch to another user'), size='small', color=theme.SUBTLE_FG))
            for i, u in enumerate(users):
                rows.append(Row([Icon('lock' if u.get('locked') else 'person', 18), Text(u.get('name', '?'), size='normal', flex=1), Button(_('Switch'), id='lock-switch-%d' % i, size='small', on_click=lambda u=u: self._switch_user(u))], id='lock-user-%d' % i, pad=8, gap=10, radius=6, align='center', bg=theme.PANEL_BG, hover={'fill': theme.BUTTON_BG}))
        form = Column(rows, pad=28, gap=14, bg=theme.CARD_BG, radius=12, border=theme.BORDER, w=460, align='stretch')
        return Box([Spacer(), Row([Spacer(), form, Spacer()]), Spacer()], flex=1, direction='column', align='stretch')

    def _do_unlock(self):
        if self.controller is None:
            return
        pin = self._pin.get('pin', '')
        ep = self._epoch

        def work():
            if not self.controller.unlock(pin):
                return False
            return self.controller.connect_and_rebuild()

        def done(source):
            if source is False:
                self._pin_error = _('Incorrect PIN.')
                return
            self._pin_error = None
            self._pin['pin'] = ''
            if source is None:
                self._locked = False
                if self._profile_has_servers():
                    self.show_connecting()
                    self.connect_failed()
                else:
                    self.show_login()
                return
            self._locked = False
            self.set_source(source)
        self.run_async(work, done, ep)

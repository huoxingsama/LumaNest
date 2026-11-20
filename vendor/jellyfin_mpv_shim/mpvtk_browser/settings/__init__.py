"""The Settings route.

Seven tabs -- general, browse, playback, home screen, servers & users,
downloads, logs. This module is the frame: the route entry, the tab bar, and
the dispatch to whichever tab is selected. Each tab is a mixin in its own
module, composed into ``SettingsMixin`` below.

**The first three are one renderer, not three.** They are the config form,
split across tabs by ``config.TAB_SECTIONS``; ``_settings_form`` reads the
tab off the route. They were a single page until it reached eleven groups
and about a hundred controls in one scroll.

The home-screen tab is the odd one out: unlike every other setting here, its
contents live on the *server* (DisplayPreferences, shared with
jellyfin-web), so it loads and saves asynchronously rather than through the
config module. It absorbed the old "Display" tab, which held one checkbox
out of the same document governing two home-screen rows.

State on ``self``: ``_config_obj`` (the settings accessor; None means the
real config module, tests inject a fake), ``_sync_path`` (download-folder
field mirror), ``_dl_thread`` (the downloads poller) and ``_log_thread``
(the log tail), both started via core's ``_start_daemon``. The pollers run
on foreign threads, so they write then call ``invalidate()``; they exit on
``_shutdown_evt``, which is only ever set at shutdown, or as soon as the
user leaves the tab they belong to.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from ...i18n import _
from ...mpvtk.widgets import Button, Column, Dialog, Row, Spacer, Text, TextBox
from .. import theme
from ..components import chrome, controls
from .base import SettingsBase
from .downloads import DownloadsTabMixin
from .general import GeneralTabMixin
from .home import HomeTabMixin
from .logs import LogsTabMixin
from .servers import ServersTabMixin

class SettingsMixin(GeneralTabMixin, HomeTabMixin, ServersTabMixin, DownloadsTabMixin, LogsTabMixin, SettingsBase):
    """The Settings route, composed from one mixin per tab.

    Inheritance rather than composition for the same reason the
    gateway and the player mixins use it: these all read and write the
    same browser state, and an owned object would need a
    back-reference to every piece of it.
    """
    ROUTES = {'settings': (None, '_render_settings')}
    SETTINGS_TABS = ('general', 'browse', 'playback', 'home', 'servers', 'downloads', 'logs')
    TAB_RENDERERS = {'general': '_settings_form', 'browse': '_settings_form', 'playback': '_settings_form', 'home': '_settings_home', 'servers': '_settings_servers', 'downloads': '_settings_downloads', 'logs': '_settings_logs'}

    def _open_settings(self):
        if self._locked:
            return
        width = min(440, max(260, (getattr(self, '_size', None) or (640, 480))[0] - 48))

        def player_settings():
            self._close_dialog()
            self.open_settings('playback')

        def build():
            return Dialog('settings-entry', Column([
                Text('设置', size='title', bold=True),
                Text('系统设置管理媒体库、来源、缓存、网络与自动化。', wrap=True),
                Button('系统设置', id='open-system-settings', icon='settings',
                       on_click=self._open_system_settings, radius=20),
                Button('播放器设置', id='open-player-settings', icon='play_arrow',
                       on_click=player_settings, radius=20),
                Button(_('Cancel'), id='settings-entry-cancel',
                       on_click=self._close_dialog, radius=20),
            ], w=width, pad=20, gap=16, bg=theme.PANEL_BG, radius=20),
                on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _open_system_settings(self):
        if self._locked:
            return
        from personal_cinema.runtime_paths import application_home
        from personal_cinema.settings_launcher import open_settings
        root = application_home()
        self._close_dialog()
        self.set_status('正在打开系统设置…')
        self.run_async(lambda: open_settings(root),
                       lambda result: self.set_status(result['message']), self._epoch,
                       on_error=lambda _error: self.set_status('系统设置暂时无法打开，请通过桌面入口重试。'))

    def open_settings(self, tab='general'):
        """Open Settings on ``tab``. Public: the tray's Configure Servers /
        Show Console entries route here — which is why it has to respect the
        lock gate: the logs and server list are behind the PIN too."""
        if self._locked:
            return
        if self.route.get('kind') == 'settings':
            self.route['_tab'] = tab
            self.invalidate()
            return
        self.navigate({'kind': 'settings', 'server': self.server, 'title': '播放器设置', '_tab': tab})

    def tab_labels(self):
        """Tab -> display name.

        A method rather than a module constant so the strings are resolved
        when the page is drawn. Every ``_()`` at module scope is frozen at
        import, and this module is imported well before ``i18n.configure``
        on some paths -- config.py's tables get away with it because they
        are imported later, and copying that here would silently pin the
        tab bar to English. The search results name their tab from this
        too, which is why it is not a local any more.
        """
        return {'general': _('General'), 'browse': _('Browse'), 'playback': _('Playback'), 'home': _('Home Screen'), 'servers': _('Servers & Users'), 'downloads': _('Downloads'), 'logs': _('Logs')}

    def _render_settings(self, route, size):
        tab = route.get('_tab', 'general')
        labels = self.tab_labels()
        tab_style = theme.chrome_button_style()

        def tab_button(t):
            return controls.tab_btn(labels[t], 'stab-' + t, tab == t, lambda t=t: self._set_settings_tab(route, t), style=tab_style)
        pad = chrome.CONTENT_PAD
        tabs = chrome.wrap_row([tab_button(t) for t in self.SETTINGS_TABS], (size[0] if size else 0) - 2 * pad, gap=8)
        body = getattr(self, self.TAB_RENDERERS.get(tab, '_settings_form'))(route, size)
        head = [Row([tabs], pad=pad)]
        box = self._search_box_row(route, tab, pad)
        if box is not None:
            head.append(box)
        return Column(head + [body], flex=1, align='stretch')

    def _search_box_row(self, route, tab, pad):
        """The settings search field, or None on a tab it cannot search.

        Only the three schema-driven tabs, because that is what
        ``config.search`` walks: the other four are their own screens (the
        server list, the download manager, the log tail) or live on the
        server (the home screen), and none of them is made of config keys.
        A box that searched nothing on four of seven tabs would be worse
        than no box.
        """
        if tab not in getattr(self._config(), 'FORM_TABS', ()):
            return None
        return Row([TextBox('set-search-box', text=route.get('_q', ''), placeholder=_('Search settings…'), w=320, force=not route.get('_q'), on_change=lambda v: self._set_settings_query(route, v), on_submit=lambda v: self._set_settings_query(route, v)), Spacer()], pad=pad, gap=8, align='center')

    def _set_settings_query(self, route, value):
        """Live filtering, on every keystroke.

        Affordable because the form is already rebuilt per keystroke -- see
        ``_dynamic_enum``, which is written around that fact -- and because
        searching narrows: the result is nearly always smaller than the tab
        it replaced.
        """
        if route.get('_q', '') == (value or ''):
            return
        route['_q'] = value or ''
        self.invalidate()

    def _set_settings_tab(self, route, tab):
        route['_tab'] = tab
        route.pop('_q', None)
        for key in ('_home_layout', '_home_error', '_home_loading', '_display_prefs'):
            route.pop(key, None)
        self.status = ''
        self.invalidate()

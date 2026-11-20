"""MpvtkBrowser -- the app shell: route stack, async data loading, and the
``build(size)`` that turns the current route into an mpvtk widget tree.

Runs in the main process next to ``playerManager`` and attaches its UI to the
player's mpv window via ``mpvtk.MpvtkApp.attach`` (see ``mpvtk/GUIDE.md``).

This module is the *core*: ``__init__``, the nav stack, the epoch and
``run_async``, ``_load_route``, ``build``/``_render_route``, the chrome, the
browse<->playback lifecycle and HUD glue, and ``shutdown``. Around it are the
Pages (``pages/``, where a route should go) and the mixins (what has not been
converted yet, plus the app-wide surfaces that are not routes). The class
composition, the Page/ROUTES fallback and the mixin partition rule are in
docs/browser-shell.md section 1.

**Adding a view means adding a Page**: subclass ``pages.base.Page``, give it a
``kind``, register it in ``pages/PAGES``.

Three invariants hold the whole thing together, and each has a test:

**The thread contract.** Renderer event handlers and ``build()`` run on the loop
thread. ``on_playstate``, ``notify_update``, ``set_download_status``,
``display_item`` and ``on_downloads_changed`` are called from foreign threads, as
are the pool workers behind ``run_async`` -- everything they touch must be
write-then-``invalidate()``, never a direct scene change.

**Epoch discipline.** ``_epoch`` and ``_lock`` live *only* here. Dispatchers read
``ep = self._epoch`` on the loop thread and hand it to ``run_async``, which drops
the result if navigation has moved on. Caching an ``ep`` and passing it across a
module boundary reads fine and is subtly wrong.

**``_lock`` protects writers from each other, not from the reader.** ``build()``
reads route data unlocked. That is safe only because every writer ends with
``invalidate()``, so a torn read is a one-frame glitch the next build heals.
Don't "fix" it by locking ``build()``.

A widget tree is a snapshot, so a handler must read mutable state *inside*
itself, and a handler that changes state must ask for a repaint -- a Checkbox
cannot update itself. Both have shipped as bugs; see docs/browser-shell.md
section 3.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import threading
import time
from types import SimpleNamespace
from ..i18n import _
from ..mpvtk.layout import natural_size
from ..mpvtk.rawimage import cache_dir
from ..utils import memory_is_tight
from ..mpvtk.widgets import Box, Busy, Button, Column, Dropdown, Float, Gradient, Icon, Progress, Row, Spacer, Stack, Text, TextBox
from . import theme
from . import navigator
from .async_runner import AsyncRunner
from .navigator import Navigator
from .scroll_state import ScrollState
from .tile_renderer import TileRenderer
from .item_actions import ItemActions
from .hud_control import HudController
from .load_feedback import LoadFeedback
from . import window_chrome
from . import pagination
from .pagination import Paginator
from .pages import PAGES
from .pages.base import PageContext
from .hud import build_hud
from ..books import AUDIOBOOK_TYPE, BOOK_TYPE
from .repository import BOOKS_COLLECTION, FOLDER_TYPES, LIVE_TV_COLLECTION, LIVE_TYPES, PHOTO_TYPE, PLAYABLE_TYPES, SERIES_TYPES
from .strips import BANNER_GEOM, LANDSCAPE_GEOM, POSTER_GEOM, SQUARE_GEOM, StripStore, TileGeom
from .dialogs import DialogsMixin
from .livetv_dialogs import LiveTvDialogsMixin
from .auth import AuthMixin
from .settings import SettingsMixin
from .music import MusicMixin
from .views import ViewsMixin, SORTS
from .tiles import TilesMixin
from .cast import CastMixin
log = logging.getLogger('mpvtk_browser.app')

def now_id_of(state):
    """The playing item's id, if the payload carries one."""
    return (state or {}).get('id')
CHROME_FREE = {'login', 'locked', 'connecting', 'cast', 'reader', 'comic'}
NO_NOW_PLAYING = {'login', 'locked', 'connecting'}
LIVE_KINDS = {'livetv', 'channel', 'program'}
USERDATA_KINDS = {'home', 'series', 'season', 'detail', 'audiobook', 'book'}
_UNKNOWN = object()
LIVE_POLL_SECS = 120

def _is_unreachable(exc):
    """Whether a failed fetch means the server did not answer -- refused,
    timed out, unreachable -- rather than that it answered with an error."""
    if getattr(exc, 'status', None) in ('ServerUnreachable', 'ReadTimeout'):
        return True
    import requests
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))

class MpvtkBrowser(DialogsMixin, LiveTvDialogsMixin, AuthMixin, SettingsMixin, MusicMixin, ViewsMixin, TilesMixin, CastMixin):
    CONTENT_PAD = 16
    PAGINATION_BAR_H = pagination.PAGINATION_BAR_H
    CHROME_H = 60
    BANNER_H = 48
    DLBAR_H = 44
    PAGE_MAX = pagination.PAGE_MAX
    PAGEABLE_KINDS = {'grid', 'list', 'person', 'music'}
    LONG_JOB_SHUTDOWN_WAIT = 20.0

    def __init__(self, app, source, strips=None, thumbs=None, server_uuid=None, geom=None, controller=None, config=None):
        from ..conf import settings as _settings
        self._apply_theme(getattr(_settings, 'theme', 'default'))
        self.app = app
        self.source = source
        self._config_obj = config
        self.controller = controller
        self._browsing = True
        self._minimized = False
        self._now_playing = None
        self._userdata_servers = set()
        self._np_thread = None
        self._np_scrub = None
        self._shutdown_evt = threading.Event()
        self._poller_lock = threading.Lock()
        self.load = LoadFeedback(get_controller=lambda: self.controller, invalidate=lambda: self.invalidate(), status=lambda msg: self.set_status(msg), is_browsing=lambda: self._browsing, enter_browse=lambda: self.enter_browse(), hand_off=lambda: self._yield(), arm=lambda: self._arm_spinner())
        self.hud = HudController(get_app=lambda: self.app, get_controller=lambda: self.controller, invalidate=lambda: self.invalidate(), ctl=lambda fn: self._ctl(fn), start_ticker=lambda: self._start_np_ticker(), is_browsing=lambda: self._browsing)
        self._cast = {'idle': True}
        self._cast_entry = None
        self._cast_backdrop = None
        self._cast_backdrop_key = None
        self._cast_size = None
        self._cast_lock = threading.Lock()
        self.headless = bool(self._cfg_headless())
        self.set_app(app)
        self._dl_thread = None
        self._log_thread = None
        self._userdata_thread = None
        self._userdata_lock = threading.Lock()
        self._long_thread = None
        self._dl_status = None
        self._dlbar_thread = None
        self._nav_mode = False
        self._menu = None
        self._update = None
        self._offline = False
        self._offline_chosen = False
        self._unreachable = False
        self._refresh_dispatch = threading.local()
        self._server_pick_ack = 0
        self._restart_keys = set()
        self._can_restart = None
        self._screen_seq = 0
        self._load_seq = 0
        self._shed_seq = 0
        self._live_page = None
        self._csd = False
        self._maximized = False
        self._dialog = None
        self._dl = None
        self._timer_dlg = None
        self._guide_dlg = None
        self._login = {'server': '', 'user': '', 'pass': ''}
        self._login_error = None
        self._discovered = []
        self._search_box = {'term': ''}
        self._newuser = {'name': ''}
        self._pin = {'pin': ''}
        self._pin_error = None
        self._locked = False
        self._switch_gen = 0
        self._switch_from = None
        self._geom_pinned = geom
        self._derive_cover_size()
        self.strips = strips or StripStore(cache_dir=cache_dir('mpvtk-browser-'), geom=self.geom)
        self.strips.set_notify(self.invalidate_art)
        self.thumbs = thumbs
        if self.thumbs is not None:
            self.thumbs.set_notify(self.invalidate_art)
        servers = []
        try:
            servers = source.servers()
        except Exception:
            log.warning('could not enumerate servers', exc_info=True)
        self.server = self._pick_server(servers, server_uuid)
        self._async = AsyncRunner(invalidate=self.invalidate)
        self._scroll = ScrollState(lambda: self.invalidate())
        self.tiles = TileRenderer(art=self, scroll=self._scroll, on_open=lambda item: self._open_item(item), nav_mode=lambda: self._nav_mode, get_app=lambda: self.app, on_play=lambda item: self._play_tile(item), can_play=lambda item: self._tile_playable(item))
        self.tiles.on_context = lambda *a, **k: self._open_tile_menu(*a, **k)
        self._pages = Paginator(run=self._async, content_h=lambda route, size: self._content_h(route, size), is_current=lambda route: route is self.route, status=lambda msg: self.set_status(msg), invalidate=lambda: self.invalidate(), enabled=pagination.enabled_from_settings, cols=lambda w, geom: self.tiles.cols(w, geom), set_enabled=lambda v: self._config().set_setting('paginated', v), forget=lambda *ids: self._scroll.forget(*ids))
        self._dialogs = SimpleNamespace(confirm=lambda *a, **k: self._confirm(*a, **k), message=lambda *a, **k: self._message(*a, **k), add_to=lambda item, server=None: self._open_add_to(item, server), open_download=lambda item: self._open_download(item), timer_editor=lambda server, timer, series=False, on_change=None: self._open_timer_editor(server, timer, series, on_change), guide_settings=lambda server, prefs, categories, on_save=None: self._open_guide_settings(server, prefs, categories, on_save), view_settings=lambda current, on_set, paginated=None: self.view_settings(current, on_set, paginated), open_book_progress=lambda item, server=None: self._open_book_progress(item, server), media_info=lambda item, server=None: self._open_media_info(item, server), filter_panel=lambda get_vals, get_filters, get_count, on_set, on_toggle, on_clear, collection_type=None: self.filter_panel(get_vals, get_filters, get_count, on_set, on_toggle, on_clear, collection_type=collection_type))
        self._actions = ItemActions(services=self, run=self._async, dialogs=self._dialogs, on_launch=lambda audio, title: self._start(audio=audio, title=title), on_downloads_changed=lambda: self._refresh_downloaded())
        self._requested = set()
        self._img_retry = {}
        self._edit_apis_ok = None
        self._addto_build = None
        self._addcol_name = None
        self._addto_ids = None
        self._addto_explicit_ids = None
        self._bkprog = None
        self._bkprog_build = None
        self._status_at = 0.0
        self._toast_timer = None
        self._spinner_timer = None
        self._livetv_poll = None
        self._sync_path = {}
        self.status = ''
        self._size = None
        self._nav = Navigator(self._default_route, is_headless=lambda: self.headless, is_locked=lambda: self._locked)
        self._load_route(self.route)

    def _default_route(self):
        """Where the browser lands when it has nowhere specific to go.

        Handed to the Navigator as a callback rather than a value, because
        every stack-emptying path backfills through it and it must reflect
        the headless flag *at that moment*. That is what makes
        ``Navigator.replace`` safe to leave unfiltered.

        This used to carry a warning that every direct ``nav_stack``
        assignment must come through here — a successful connect once put a
        headless box on the library because ``set_source`` reset the stack
        itself and the refusal never ran. The stack is now private to the
        Navigator, so the warning is an invariant instead
        (``tests/test_source_invariants.py``).
        """
        if self._locked:
            return {'kind': 'locked', 'title': _('Locked')}
        if self.headless:
            return {'kind': 'cast'}
        return {'kind': 'home', 'server': self.server}

    @property
    def route(self):
        return self._nav.route

    @property
    def nav_stack(self):
        """The live route stack. Read freely; every WRITE goes through the
        Navigator, which is what makes the headless lockdown hold."""
        return self._nav.stack

    @nav_stack.setter
    def nav_stack(self, routes):
        self._nav.replace(routes)
    HEADLESS_ROUTES = navigator.HEADLESS_ROUTES
    LOCKED_ROUTES = navigator.LOCKED_ROUTES

    def navigate(self, route, reset=False, force=False):
        """Go to ``route``, then load and repaint it.

        Every way into the library ends up here — a tile click, the tray's
        "Show Library Browser", a remote's GoHome, the now-playing bar's
        Queue button, a DisplayContent from a phone — which is what lets the
        headless lockdown mean something rather than hiding one entry point
        and leaving five others open. The refusal itself is the Navigator's
        (see navigator.py); this method's job is that a refusal skips the
        load and the repaint too.

        ``force`` is for the screens headless itself needs to reach.
        """
        self._park_scroll()
        if not self._nav.push(route, reset=reset, force=force):
            return
        self.tiles.set_hover(None)
        self._reset_scroll()
        self._screen_seq += 1
        self._bump_epoch()
        self._load_route(route)
        focus = getattr(self.app, 'focus', None)
        if focus is not None:
            try:
                focus()
            except Exception:
                log.debug('autofocus request failed', exc_info=True)
        self.invalidate()

    def _poll_live_tv(self, route):
        """Re-read a Live TV route every ``LIVE_POLL_SECS`` while it is up.

        Started from the render path, like the logs tail and the downloads
        list, and ``restartable`` for the same reason: the thread only
        notices the route has changed on its next tick, so leaving and
        coming straight back would otherwise find the slot still taken and
        leave nobody polling.
        """

        def tick():
            while not self._shutdown_evt.wait(LIVE_POLL_SECS):
                if self.route is not route or not self._browsing:
                    break
                self.refresh_live_tv()
        self._start_daemon('_livetv_poll', 'mpvtk-livetv', tick, restartable=True)

    def _refresh_current(self, kinds, server=None):
        """Re-read the current route in place, if it is one of ``kinds``.

        **The one implementation of "refresh what is on screen".** There were
        two, and they had drifted: the Live TV one took a `_refreshing` marker
        and the Home one did not, so only one of them was safe against a
        scroll paging in against a list about to be replaced. Widening either
        gate would have made a third copy -- see docs/browser-shell.md
        section 4.

        Every rule here has a reason and none of them is optional:

        * a **load, not a reload** -- the epoch stays put, nothing in flight
          is cancelled, and the loaders write in place, so a refresh nobody
          asked for does not blink a spinner over what is being read;
        * **deferred, never forced** while a menu or dialog is up: yanking a
          list out from under someone mid-interaction is worse than being one
          refresh stale, and the next event is seconds away;
        * ``_refreshing`` is set here and cleared by ``_route_async``'s
          ``always`` however the load ends -- `pagination` reads it, so a
          page-in cannot append to a list this is about to replace;
        * ``self.server is None`` is refused because ``_load_route`` would
          not dispatch, and then nothing would ever clear the marker.

        ``server`` filters by the server the event came FROM. Home is exempt:
        it is assembled from every logged-in server, so an event from any of
        them can change it. Everything else belongs to one server, and
        re-fetching a Series page on server B because something happened on
        server A is at best a wasted round trip and at worst an unrelated
        event driving the screen into the offline fallback.

        Reached from the websocket thread and from the Live TV poller, so it
        must be safe off the loop thread and cheap when it does not apply.
        Returns True if a load was dispatched.
        """
        route = self.route
        if route.get('kind') not in kinds or not self._browsing:
            return False
        if self.source is None or self.server is None:
            return False
        if server is not None and route.get('kind') != 'home' and ((route.get('server') or self.server) != server):
            return False
        if self._menu is not None or self._dialog is not None:
            return False
        if route.get('_loading') or route.get('_refreshing'):
            again = route.get('_refresh_again')
            if again is not None and again[1] != server:
                server = None
            route['_refresh_again'] = (kinds, server)
            return False
        route['_refreshing'] = True
        self._refresh_dispatch.active = True
        try:
            self._load_route(route)
        finally:
            self._refresh_dispatch.active = False
        return True

    def refresh_live_tv(self, _client=None):
        """Re-fetch the Live TV screen, if that is what is showing.

        Reached from the websocket thread (a timer created or cancelled,
        possibly by another client entirely) and from the poller above.

        Not filtered by server: a timer event carries no route of its own and
        the guide is the screen it would affect, so the old unfiltered
        behaviour is kept deliberately rather than by omission.
        """
        self._refresh_current(LIVE_KINDS)
    USERDATA_DEBOUNCE = 3.0

    def refresh_userdata(self, server=None, now=False):
        """Re-read the current screen when watched/favourite state changes.

        **Every screen that shows it, not only Home.** Home's Continue
        Watching and Next Up were the reported case (#560) and the gate was
        written for them; a series page's per-season "N episodes remaining"
        is the same field from the same event, and it went stale until the
        app was restarted (#722). `UnplayedItemCount` is computed by the
        server on the PARENT, so there is nothing to patch client-side --
        re-reading is the only honest answer, and a client-side aggregate
        would be a second authority on it.

        ``now`` skips the debounce, for the caller that is not a burst:
        coming back from playback (see enter_browse). ``server`` is the
        server the event came from; see ``_refresh_current``.
        """
        if now:
            self._refresh_current(USERDATA_KINDS, server)
            return

        def tick():
            while True:
                self._shutdown_evt.wait(self.USERDATA_DEBOUNCE)
                with self._userdata_lock:
                    pending, self._userdata_servers = (self._userdata_servers, set())
                    if not pending or self._shutdown_evt.is_set():
                        self._userdata_thread = None
                        return
                try:
                    self._refresh_current(USERDATA_KINDS, self._userdata_target(pending))
                except Exception:
                    log.error('userdata refresh failed', exc_info=True)
        if self.route.get('kind') not in USERDATA_KINDS or not self._browsing:
            return
        with self._userdata_lock:
            self._userdata_servers.add(server)
            if self._userdata_thread is not None:
                return
            self._userdata_thread = threading.Thread(target=tick, name='mpvtk-userdata', daemon=True)
            self._userdata_thread.start()

    def _userdata_target(self, pending):
        """Which server to hand `_refresh_current` for a coalesced burst.

        One value, because the route on screen belongs to one server and
        Home is exempt from the filter -- so refreshing once per server
        would re-read Home several times for nothing.

        `None` wins outright: it means an event that could not be
        attributed, which refreshes unfiltered, and swallowing it behind a
        filtered one would lose the very event we cannot reason about.
        Otherwise prefer the route's own server when the burst contains it;
        failing that any of them, which the filter then declines exactly as
        a single event would have.
        """
        if not pending or None in pending:
            return None
        route_server = self.route.get('server') or self.server
        if route_server in pending:
            return route_server
        return next(iter(pending))

    def _reload_route(self, route):
        """Re-run a route's loader in place: the data changed, the screen did
        not. A sort or filter change, the collections toggle. Distinct from
        navigate(), which pushes a new screen, and from go_back()."""
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _cfg_headless(self):
        """Read the headless flag. Tolerates a config object without it (the
        test fakes hand-build a small schema)."""
        cfg = self._config_obj
        if cfg is None:
            try:
                from ..conf import settings
                return getattr(settings, 'headless', False)
            except Exception:
                return False
        try:
            return bool((cfg.get_settings() or {}).get('headless', False))
        except Exception:
            return False

    def go_home(self):
        """Go to the home screen, reusing the one the stack already holds.

        The route dict *is* the page cache, so pushing a fresh one means
        ``chrome.busy()`` until a whole home fetch lands -- on the screen the
        user almost always already has. Reuse is safe rather than stale because
        Home re-reads itself in place. Scroll comes back with the route.

        Pressing Home while already on Home still goes the whole way round: the
        epoch and ``_screen_seq`` both move, so the caches shed and the rows
        re-read. That is deliberate -- it is the one gesture that means "reload
        this". **Do not "fix" it by skipping the bump when the route is
        unchanged.**

        The server is part of the match: switching servers pushes its own home
        (``_switch_server``) and must not land on the previous one's rows. A
        stack with no usable home falls through to a fresh route, which is also
        what keeps headless refusing. See docs/browser-shell.md section 5.
        """
        for route in reversed(self.nav_stack):
            if route.get('kind') == 'home' and route.get('server') == self.server:
                from .cinema_featured import refresh_on_home
                route['_cinema_shuffle_due'] = refresh_on_home()
                self.navigate(route, reset=True)
                return
        self.navigate({'kind': 'home', 'server': self.server}, reset=True)

    def go_back(self):
        self._park_scroll()
        left = self._nav.pop()
        if left is not None:
            self._land_back([left])

    def _land_back(self, left):
        """Settle on whatever a back move landed on. ``left`` is the routes
        it left, nearest first — one for a Back press, several for a jump
        through the history menu.

        Shared so the two cannot diverge: the menu's jump used to reload
        only Home, so jumping past the playlist editor showed the pre-edit
        membership as fresh while pressing Back the same number of times
        refetched it.
        """
        self._reset_scroll()
        self._screen_seq += 1
        self._bump_epoch()
        if self.route.get('kind') == 'home':
            self._load_route(self.route)
        elif any(((r or {}).get('kind') == 'playlist_edit' for r in left)) and self.route.get('kind') in ('playlist', 'grid'):
            self._refetch_underneath(self.route)
        elif any(((r or {}).get('_deleted') for r in left)):
            self._refetch_underneath(self.route)
        elif any(((r or {}).get('kind') in ('reader', 'comic') for r in left)) and self.route.get('kind') == 'book':
            self.route.pop('_data', None)
            self._load_route(self.route)
        elif self.route.get('_data') is None and (not self.route.get('_items')) and (not self.route.get('_error')) and (not self.route.get('_loading')):
            self._refetch_underneath(self.route)
        self.invalidate()

    def _refetch_underneath(self, route):
        """Drop a route's cached result set and load it again.

        The paginator goes with the items. `ensure()` only rebuilds `_pages`
        when the page SIZE changes, so a cache left behind is served straight
        back -- the deleted tile still drawn, from a page dict, with `_items`
        correctly gone. Every other place that replaces a result set resets it
        (`_retry_route`, `after_playlist_deleted`, `grid.py`); `_land_back`
        was the one that did not, which is the same one-step-of-two the test
        for it made: it asserted `_items` and nothing else.
        """
        route.pop('_data', None)
        route.pop('_items', None)
        route.pop('_loading', None)
        self._pages.reset(route)
        self._load_route(route)

    def go_forward(self):
        """Return to a page ``go_back`` left. Mouse-only (the thumb button),
        by design: it exists so an accidental Back is cheap to undo, which
        does not earn a permanent arrow in the top bar. The history menu on
        the Back button is where it becomes visible.

        No stale-while-revalidate counterpart to go_back's Home reload: a
        page reached by going forward was on screen moments ago, and the one
        case that reloads on the way back — leaving the playlist editor —
        cannot be ahead of you, because editing pushes rather than pops.
        """
        self._park_scroll()
        self._land_forward(self._nav.unpop())

    def go_forward_to(self, depth):
        """Jump forward to the page ``depth`` deep — the history menu's
        pick on the other side of the current page."""
        self._park_scroll()
        self._land_forward(self._nav.fast_forward(depth))

    def _land_forward(self, route):
        """Settle on a route the forward stack gave back. None means
        nothing moved: an empty stack, or the headless lockdown."""
        if route is None:
            return
        self._reset_scroll()
        self._screen_seq += 1
        self._bump_epoch()
        if route.get('_data') is None and (not route.get('_items')):
            route.pop('_loading', None)
            self._load_route(route)
        self.invalidate()

    def go_back_to(self, depth):
        """Jump back to the page ``depth`` deep in the stack (1 is the root)
        — the history menu's pick. Everything skipped goes onto the forward
        stack, and lands exactly as pressing Back that many times would."""
        self._park_scroll()
        left = self._nav.rewind_to(depth)
        if not left:
            return
        self._land_back(left)

    def after_playlist_deleted(self, playlist_id):
        """Drop every route pointing at a now-deleted playlist and reload
        whatever is left showing.

        A playlist page keys its id as ``item_id``; only ``parent_id`` was
        checked, so nothing was ever pruned and deleting a playlist left the
        user sitting on its now-dead page. The route we land on also has to
        re-fetch, or the grid we came from still lists the playlist."""
        self._nav.prune(lambda r: playlist_id not in (r.get('item_id'), r.get('parent_id')))
        route = self.route
        route.pop('_data', None)
        route.pop('_items', None)
        route.pop('_loading', None)
        self._pages.reset(route)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def display_item(self, server_uuid, item_id):
        if self.headless:
            self.display_cast_item(server_uuid, item_id)
            return
        return self._display_item(server_uuid, item_id)

    def _display_item(self, server_uuid, item_id):
        """Open an item's page because a remote asked us to (Jellyfin's
        DisplayContent — "show me this" from a phone or web client).

        This is the browsable counterpart to the legacy kiosk mirror: the
        remote picks the page, then its arrows drive the same spatial
        navigation the keyboard uses.

        Two things it deliberately does NOT do. It never starts playback —
        jellyfin-web emits DisplayContent as you *browse* on the phone, so a
        cast track has to open its album, not play it. And it never
        interrupts playback for the same reason: browsing on the phone while
        something plays here would otherwise stop the video. The page is
        simply waiting when playback ends."""
        if self._locked:
            return
        if server_uuid and server_uuid != self.server:
            self.server = server_uuid
        ep = self._epoch

        def work():
            return self.source.get_item(server_uuid or self.server, item_id)

        def done(item):
            if not item:
                return
            self._display_route(item)
            from ..conf import settings
            if self._minimized and (not settings.display_mirror_summon):
                return
            if self._minimized or self._browsing:
                self.enter_browse()
                if self.controller is not None and settings.display_mirror_summon:
                    self._safe(lambda c: c.raise_window())
        self.run_async(work, done, ep)

    def _display_route(self, item):
        """Navigate to an item's *page*. Same dispatch as a click, except
        that types a click would play resolve to the page they belong to."""
        if item.get('Type') == 'Audio':
            album = item.get('AlbumId')
            if album:
                self.navigate({'kind': 'album', 'server': self.route.get('server') or self.server, 'item_id': album, 'title': item.get('Album') or ''})
            else:
                log.debug('DisplayContent for a track with no album; ignoring')
            return
        self._open_item(item)

    def on_nav_command(self, name):
        """Remote menu commands that map onto real pages here (GoHome /
        GoToSettings), or onto the chrome (GoToSearch). Returns True when
        handled; the OSD menu has none of them, so for every other path
        settings still just opens the menu."""
        if self.headless:
            return False
        if name == 'search':
            if not self._browsing:
                return False
            from .components.cinema_chrome import open_search
            open_search(self)
            return True
        if name == 'settings':
            self._open_settings()
            return True
        if name == 'home':
            self.go_home()
            if self._now_playing is not None or self.hud.state is not None:
                self._ctl(lambda c: c.stop())
            return True
        return False

    def on_back(self):
        """BACK / ESC from a remote or the keyboard. Returns True when it
        consumed the press, so the player can fall back to its own handling
        (leaving fullscreen) at the root of the stack."""
        if self._dialog is not None:
            self._close_dialog()
            return True
        if self._menu is not None:
            self._close_menu()
            return True
        if self._nav.can_go_back:
            self.go_back()
            return True
        return False

    def _on_mouse_forward(self):
        """The mouse's forward button, from the renderer.

        Guarded where ESC is *layered*. Back peels one layer at a time
        because ESC means "out of this"; forward means "the page I left",
        and there is no sense in which a dialog or a context menu is
        between you and it — navigating underneath one would leave it
        floating over a page it was never opened from. So an open overlay
        makes this a no-op rather than something to close first.

        The renderer's mouse group is also live while the playback HUD is
        summoned, where the library is not on screen at all.
        """
        if not self._browsing or self._dialog is not None or self._menu is not None:
            return
        self.go_forward()

    def _on_nav_mode(self, active):
        """Renderer 'nav' event: keyboard/remote engaged or the mouse
        took over. Repaint so modality-dependent chrome (carousel
        arrows) follows."""
        if active != self._nav_mode:
            self._nav_mode = active
            self.invalidate()

    @property
    def _epoch(self):
        return self._async.epoch

    @property
    def _pool(self):
        return self._async.pool

    @_pool.setter
    def _pool(self, value):
        self._async.pool = value

    def _on_scene_pushed(self):
        """A scene reached the renderer (MpvtkApp.on_scene_pushed).

        The strip cache frees bitmaps mpv may be compositing, so it needs to
        count *pushes* and not builds -- a build can run twice for one push
        and can raise without pushing at all. See StripStore.on_scene_pushed.
        """
        if self.strips is not None:
            self.strips.on_scene_pushed()
        if self.thumbs is not None:
            self.thumbs.on_scene_pushed()

    def _bump_epoch(self):
        """Invalidate every in-flight async result. Returns the new epoch."""
        return self._async.bump()

    def _shed_caches_on_screen_change(self):
        """Drop the decoded artwork of the screen just left.

        Decoded images are the most expensive thing held per picture -- a 4K
        backdrop is 33 MB decoded against ~400 KB on the wire -- and they exist
        only to composite tile strips. Once the screen is behind you they have
        nothing left to do.

        **Observed here, not done in navigate().** navigate() is reachable from
        mpv's event thread and the websocket, and this cache has no lock; a
        counter bumped there and read here turns a cross-thread call into a
        loop-thread observation.

        **Keyed on _screen_seq, not the async epoch** -- four things bump the
        epoch without leaving the screen, and shedding on those re-decoded the
        page the user was still looking at. See docs/browser-shell.md
        section 6.
        """
        seq = self._screen_seq
        if seq == self._shed_seq:
            return
        self._shed_seq = seq
        tight = memory_is_tight()
        if tight and self.thumbs is not None:
            self.thumbs.trim_memory()
        if self.strips is not None:
            self.strips.set_memory_pressure(tight)
            if tight:
                self.strips.trim_soon()

    def _apply_theme(self, name):
        """Make a theme current: palette, toolkit tokens, mpv's browse
        background. Everything here is idempotent and safe to repeat, which
        is what :meth:`set_theme` relies on."""
        self._theme_cfg = theme.apply(name)
        try:
            from .. import player_window as _pw
            _pw.set_browse_bg(self._theme_cfg['browse_bg'])
        except Exception:
            log.debug('could not set the browse background', exc_info=True)
        theme.apply_to_toolkit(glow=self._theme_cfg.get('glow', False))
        log.info('theme: %s (accent %s, glow %s)', self._theme_cfg.get('name', '?'), theme.ACCENT, self._theme_cfg.get('glow', False))
        return self._theme_cfg

    def set_theme(self, name):
        """Change theme without restarting.

        Three things happen beyond re-applying the palette: the renderer needs
        the new tokens pushed (it draws text fields, dropdowns, scrollbars and
        tooltips itself), mpv's own ``background-color`` is a property rather
        than something the scene paints, and every composited strip has the old
        colours baked in, so the strip store is **retagged, not cleared** (see
        StripStore.tag).

        Tile *geometry* is deliberately not re-derived here -- see
        apply_cover_size and docs/browser-shell.md section 9.
        """
        cfg = self._apply_theme(name)
        if self.app is not None:
            try:
                self.app.push_theme()
            except Exception:
                log.debug('could not push the theme to the renderer', exc_info=True)
        if self.strips is not None:
            self.strips.set_theme_tag(cfg.get('name', name))
        try:
            from .. import player as _player
            player = getattr(_player, 'playerManager', None)
            if player is not None:
                player.refresh_browse_bg()
        except Exception:
            log.debug('could not repaint the mpv background', exc_info=True)
        self.invalidate()
        return cfg

    def _derive_cover_size(self):
        """(Re)compute the four tile geometries from the theme and the Cover
        Size setting.

        Split out of __init__ so the setting can be changed without a
        restart: everything downstream reads ``art.geom*`` at render time,
        and strip bitmaps are keyed on the geometry's own dimensions
        (StripStore._key), so nothing cached at the old size can be served
        at the new one.
        """
        import dataclasses
        from ..conf import settings as _settings
        cs = getattr(_settings, 'poster_scale', None) or self._theme_cfg.get('poster_scale', 1.0)
        lw, lh = self._theme_cfg.get('tile_landscape', (LANDSCAPE_GEOM.tile_w, LANDSCAPE_GEOM.tile_h))
        self.geom = self._geom_pinned or POSTER_GEOM.scaled(cs)
        if (lw, lh) == (LANDSCAPE_GEOM.tile_w, LANDSCAPE_GEOM.tile_h):
            wide = LANDSCAPE_GEOM
        else:
            wide = TileGeom(tile_w=lw, tile_h=lh, caption_h=LANDSCAPE_GEOM.caption_h)
        self.geom_wide = wide.scaled(cs)
        self.geom_square = SQUARE_GEOM.scaled(cs)
        self.geom_banner = BANNER_GEOM.scaled(cs)
        tts = self._theme_cfg.get('tile_title_size')
        tss = self._theme_cfg.get('tile_sub_size')
        if tts or tss:

            def caption(g):
                return dataclasses.replace(g, title_size=tts or g.title_size, sub_size=tss or g.sub_size)
            self.geom = caption(self.geom)
            self.geom_wide = caption(self.geom_wide)
            self.geom_square = caption(self.geom_square)
        from ..conf import settings
        try:
            factor = float(getattr(settings, 'ui_text_scale', 1.0) or 1.0)
        except (TypeError, ValueError):
            factor = 1.0
        try:
            floor = int(getattr(settings, 'ui_text_min', 0) or 0)
        except (TypeError, ValueError):
            floor = 0
        if factor != 1.0 or floor:
            for name in ('geom', 'geom_wide', 'geom_square', 'geom_banner'):
                setattr(self, name, getattr(self, name).with_text_scale(factor if factor > 0 else 1.0, floor))

    def apply_cover_size(self):
        """Adopt a changed Cover Size without a restart.

        Theme-driven geometry stays restart-only (see set_theme); a control
        LABELLED Cover Size is the opposite case -- seeing it happen is the
        whole point, which is why it sat behind a restart and nobody could tell
        what the values meant (#616).

        Two things must be cleared for the **whole stack**, not just the current
        route, or going back lands on a stale one: parked scroll offsets (pixel
        positions into a list whose row pitch just changed) and every route's
        parked ``_grid_shape``, which GridPage resolves once per route on
        purpose. See docs/browser-shell.md section 9.
        """
        self._derive_cover_size()
        if self.strips is not None:
            self.strips.geom = self.geom
        for route in self.nav_stack:
            route.pop('_grid_shape', None)
            route.pop(self._scroll.PARK_KEY, None)
        self._scroll.reset()
        self.invalidate()

    def apply_logo_legibility(self):
        """Adopt a changed "Make logos more legible" without a restart.

        The plate behind a transparent logo is baked into the composited
        strip, so retag the store exactly as a theme change does -- the rows
        recomposite as they are next drawn, and the old bitmaps age out
        through the LRU rather than being freed under a running compositor.

        Nothing else moves: the geometry, the scroll offsets and the grid
        shapes are all unaffected by which colour goes behind a logo.
        """
        if self.strips is not None:
            self.strips.retag()
        self.invalidate()

    def invalidate(self):
        if self.app is not None:
            self.app.invalidate()

    def invalidate_art(self):
        """Repaint because a picture arrived.

        Throttled and coalesced by the app loop (MpvtkApp.ART_RENDER_INTERVAL)
        rather than drawn on the spot: a grid asks for a hundred thumbnails at
        once and the decode pool answers one at a time, so this is called in
        hundreds and each call would otherwise lay out the whole screen to
        change the picture on one tile."""
        if self.app is not None:
            self.app.invalidate(soon=True)

    def run_async(self, work, on_done, epoch, on_error=None, always=None):
        """Run ``work()`` off the loop thread; apply ``on_done(result)`` only
        if the epoch still matches (the user hasn't navigated away).

        The contract — why ``on_error`` is not epoch-gated, why ``always``
        exists, why every callback is individually guarded — lives with the
        implementation in ``async_runner.py``. Two callers rely on the
        "``on_error`` runs regardless of epoch" clause and guard the live
        screen themselves by testing ``route is self.route``: ``_route_async``
        before the offline fallback (``set_source`` discards the nav stack)
        and ``_page_more`` before its toast. ``_edit_call``'s toast is
        deliberately unguarded — the user pressed a button and the server
        refused, so they should be told wherever they now are.
        """
        self._async.run(work, on_done, epoch, on_error=on_error, always=always)
    LOAD_ID_KEY = '_load_id'

    def _next_load_id(self):
        self._load_seq += 1
        return self._load_seq

    def _route_async(self, route, work, on_done, ep):
        """run_async for a route's data, recording a failure on the route so
        the view can say so and offer a retry instead of spinning."""
        load_id = self._next_load_id()
        route[self.LOAD_ID_KEY] = load_id
        background = getattr(getattr(self, '_refresh_dispatch', None), 'active', False)
        patch = route.get('_stop_patch')
        believed = patch if patch is not None and patch['delivered'] else None

        def landed(data):
            if route.get(self.LOAD_ID_KEY) != load_id:
                return
            if self._unreachable and (not self._offline):
                self._unreachable = False
                self.invalidate()
            on_done(data)
            if route.get('_stop_patch') is not None and route.get('_stop_patch') is not believed:
                self._apply_stop_patch(route)

        def failed(exc):
            route.pop('_loading', None)
            log.info('route %r failed to load: %s', route.get('kind'), exc)
            if route.get(self.LOAD_ID_KEY) != load_id:
                return
            route['_error'] = _('Failed to load. Check the connection.')
            if route is self.route and (not self._offline) and (not background) and _is_unreachable(exc):
                self._unreachable = True
                self.invalidate()
            if route is self.route and (not background) and _is_unreachable(exc):
                self._offline_fallback(route)

        def settled():
            route.pop('_refreshing', None)
            again = route.pop('_refresh_again', None)
            if again is not None and route is self.route:
                self._refresh_current(*again)
        self.run_async(work, landed, ep, on_error=failed, always=settled)
    PAGE_SLOP = pagination.PAGE_SLOP

    def _paginated(self):
        return self._pages.enabled()

    def _content_h(self, route, size):
        """Vertical space the route content actually gets — the window minus
        the chrome and bars that sit above/below it in ``build``. Mirrors
        build()'s own conditions so a paginated page can size itself to fit.

        Stays here: only the shell knows which of its bars are up."""
        h = size[1]
        if route.get('kind') not in CHROME_FREE:
            from .components.cinema_chrome import active
            if not (active(self) and window_chrome.banner(self) is None and (window_chrome.download_bar(self) is None)):
                h -= self.CHROME_H
            if window_chrome.banner(self) is not None:
                h -= self.BANNER_H
            if self._dl_status and self._dl_status.get('pending'):
                h -= self.DLBAR_H
        h -= self.PAGINATION_BAR_H
        if self._now_playing is not None and route.get('kind') not in NO_NOW_PLAYING:
            from .music import now_playing_bar_h
            h -= now_playing_bar_h(self._now_playing, size[0])
        return max(1, h)

    def _page_count(self, route, ps):
        return self._pages.page_count(route, ps)

    def _ensure_page(self, route, ps, fetch, seed=None):
        return self._pages.ensure(route, ps, fetch, seed)

    def _reset_pagination(self, route):
        self._pages.reset(route)

    def _page_go(self, route, page):
        self._pages.go(route, page)

    def _page_jump(self, route, text):
        self._pages.jump(route, text)

    def _pagination_bar(self, route, w):
        """`Page [n] of N     |◀ ◀ ▶ ▶|` — the bottom bar that replaces the
        scrollbar in paginated mode. None unless paginated, on a pageable
        route, and a page count is known (set by _ensure_page this frame)."""
        if not self._paginated() or route.get('kind') not in self.PAGEABLE_KINDS:
            return None
        npages = route.get('_npages')
        if not npages:
            return None
        cur = route.get('_page') or 0

        def nav(icon, node_id, target, tip):
            return Button('', id=node_id, icon=icon, w=32, h=32, pad=0, justify='center', tip=tip, on_click=lambda: self._page_go(route, target))
        return Row([Text(_('Page'), size='small', color=theme.SUBTLE_FG), TextBox('pg-jump', text=str(cur + 1), w=64, force=True, on_submit=lambda s: self._page_jump(route, s), on_commit=lambda s: self._page_jump(route, s)), Text(_('of %d') % npages, size='small', color=theme.SUBTLE_FG), Spacer(), nav('first_page', 'pg-first', 0, _('First page')), nav('chevron_left', 'pg-prev', cur - 1, _('Previous page')), nav('chevron_right', 'pg-next', cur + 1, _('Next page')), nav('last_page', 'pg-last', npages - 1, _('Last page'))], pad=8, gap=8, align='center', h=self.PAGINATION_BAR_H, bg=theme.PANEL_BG)

    def _offline_fallback(self, route):
        """A failed *home* load with downloads present drops to the offline
        library, as the Tk browser does — otherwise the first thing a user
        sees with the server down is an error where their downloads are."""
        if route.get('kind') != 'home' or self._offline:
            return
        if self.controller is None:
            return
        try:
            source = self.controller.offline_source()
        except Exception:
            log.debug('offline fallback failed', exc_info=True)
            return
        if source is not None:
            log.info('server unreachable; falling back to the downloads')
            self.set_source(source)

    def _retry_route(self, route):
        route.pop('_error', None)
        route.pop('_data', None)
        route.pop('_items', None)
        route.pop('_loading', None)
        self._pages.reset(route)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()
    _ROUTES_CACHE = None

    @classmethod
    def _routes(cls):
        """The merged kind -> (loader, renderer) table.

        Each mixin declares the kinds it owns in its own ROUTES, so a view is
        added in one place next to the code that draws it. Reading
        ``self.ROUTES`` would only ever see the first mixin in the MRO, so
        walk it and merge; a kind claimed twice is a bug, not a silent
        override (see tests/test_mpvtk_browser_mixins.py).
        """
        if cls.__dict__.get('_ROUTES_CACHE') is None:
            merged = {}
            for base in cls.__mro__:
                for kind, pair in (base.__dict__.get('ROUTES') or {}).items():
                    merged.setdefault(kind, pair)
            cls._ROUTES_CACHE = merged
        return cls._ROUTES_CACHE
    SCROLL_STEP = ScrollState.STEP

    def _offset(self, scroll_id):
        return self._scroll.offset(scroll_id)

    def _on_scroll(self, scroll_id, offset, maximum, then=None):
        self._scroll.on_scroll(scroll_id, offset, maximum, then)

    def _reset_scroll(self):
        self._scroll.reset()

    def _park_scroll(self):
        """Stash the current screen's scroll offsets on its route dict, so
        coming back to it lands where it was left. No-op with no route (the
        first navigate of the session).

        **Refuses to park while the browser is not on screen**, and that is not
        an optimisation: a yielded scene holds no containers, so the live read
        answers None and ``park`` falls through to a fallback that holds only
        watched containers -- writing that *partial* snapshot over the complete
        one ``_yield`` saved on the way into playback. Reachable via a remote's
        GoHome, and on every mpv < 0.36. See docs/browser-shell.md section 7.

        ``_park_on_leaving_browse`` is the one caller that runs at the boundary,
        which is why it is invoked before ``_yield`` clears the flag.
        """
        route = self.route
        if route is not None and self._browsing:
            self._scroll.park(route, self.app)

    def _art_context(self):
        """Render resources a page may use. A namespace rather than the
        browser, so a page cannot reach past it into the shell's state.

        ``tiles`` and ``scroll`` are the two services step 6b extracted, and
        they are here rather than as their own ``PageContext`` fields because
        both answer questions about *what the renderer is drawing* — tile
        geometry and where a container is scrolled to. Reaching them through
        ``art`` is also what keeps the context at nine fields, which the
        contract test caps.
        """
        from types import SimpleNamespace
        return SimpleNamespace(strips=self.strips, thumbs=self.thumbs, geom=self.geom, geom_wide=self.geom_wide, geom_square=self.geom_square, geom_banner=self.geom_banner, tiles=self.tiles, scroll=self._scroll, pages=self._pages, set_picture_pan=lambda cfg=None: self._set_picture_pan(cfg), node_rect=lambda node_id: self.app.node_rect(node_id) if self.app is not None else None)

    def _page_context(self):
        """Build the dependency bundle handed to every page.

        Rebuilt per call rather than cached: `source` and `server` are swapped
        by set_source at arbitrary moments (a reconnect, a user switch), and a
        page holding a stale source would browse a server the user has left.
        """
        return PageContext(source=self.source, server=self.server, nav=SimpleNamespace(navigate=lambda route, **kw: self.navigate(route, **kw), go_back=lambda: self.go_back(), reload=lambda route: self._reload_route(route), load=lambda route: self._load_route(route), is_current=lambda route: route is self.route, after_playlist_deleted=lambda pid: self.after_playlist_deleted(pid)), run=self._async, art=self._art_context(), player=self.controller, actions=self._actions, dialogs=self._dialogs, status=self.set_status, invalidate=self.invalidate, shell=self)
    PAGE_OBJ_KEY = '_page_obj'

    def _release_page_grabs(self):
        """Give back everything a page took hold of outside its own scene.

        Called when the browser hands the window over — to playback, or to
        being minimized. ``build()`` returns before ``_retire_page`` once
        ``_browsing`` is False, so a page that yields is never retired and
        none of this would be dropped otherwise.

        All three outlive the page in a way the user feels. A key claim
        keeps SPACE bound to the reader, so pause is dead and instead turns
        the page and writes the new position to the server. A pan model
        makes a wheel notch pan the *playing video* inside the comic's
        clamp. And the picture's zoom and pan are global mpv options, so
        the film plays at whatever the last comic page was set to.
        """
        self._drop_key_claims()
        self._set_picture_pan(None)
        route = self.route
        if route.get('_showing'):
            route['_showing'] = False
        if self.controller is not None:
            try:
                self.controller.reset_picture_view()
            except Exception:
                log.debug('could not reset the picture view', exc_info=True)

    def _drop_key_claims(self):
        """Give every claimed key back to mpv.

        Its own method because `_yield` calls it TWICE, and the second call
        is the point: see the note there.
        """
        claim = getattr(self.app, 'claim_keys', None)
        if claim is None:
            return
        try:
            claim(())
        except Exception:
            log.debug('could not drop the key claim', exc_info=True)

    def _retire_page(self, route):
        """Tell the page we just stopped drawing that it is off screen.

        On the loop thread, from build(), rather than in navigate(): that
        is reachable from mpv's event thread and from the websocket (a
        remote's GoHome, a phone's DisplayContent), and what a page does
        here may touch the player. Observed rather than pushed, the same
        way _shed_caches_on_screen_change is, and for the same reason.

        The comparison is by page *object*, so a route re-entered later
        gets its close() and its next load() in the right order.
        """
        page = self._page_for(route)
        previous = getattr(self, '_live_page', None)
        if previous is page:
            return
        self._live_page = page
        if getattr(page, 'kind', None) != 'comic':
            self._set_picture_pan(None)
        if previous is None:
            return
        try:
            previous.close()
        except Exception:
            log.warning('page close failed', exc_info=True)

    def _drop_abandoned_sync_path(self):
        """Forget a download-folder path typed into a field that is no longer
        on screen.

        The renderer drops a textbox's text when its node leaves the scene, so
        a field revisited later draws the **stored** setting again -- while
        this dict went on holding whatever was typed at it, for the life of
        the browser. The Move button reads the dict in preference to the value
        the field is showing (deliberately: an emptied field is a real request
        for the default folder, which `get("path") or val` cannot express), so
        the two together moved the store to a path nobody could see. This is
        the app's one Tier-1 destructive text field; `docs/do-not-fix.md` F42
        is the report.

        Mirroring the renderer's own prune rather than clearing on a
        navigation is what makes it complete: a tab change, a search that
        filters the row out, a yield to playback and leaving Settings
        altogether are four different events and one fact, which is that the
        row was not drawn. The row stamps itself when it is.
        """
        if not self._sync_path.pop('drawn', False):
            self._sync_path.pop('path', None)

    def _claim_page_keys(self, route):
        """Push this route's key claim to the renderer (see
        ``MpvtkApp.claim_keys``).

        Driven by a ``claimed_keys`` attribute on the Page rather than by
        the shell knowing which kinds want keys — and read every frame, so
        a claim cannot outlive the page that made it. Almost every page
        claims nothing, and ``claim_keys`` is a no-op when the set has not
        changed.
        """
        claim = getattr(self.app, 'claim_keys', None)
        if claim is None:
            return
        page = self._page_for(route) if self._browsing else None
        keys = tuple(getattr(page, 'claimed_keys', ()) or ())
        keys += self._shell_claimed_keys()
        if not self._browsing:
            keys = ()
        try:
            claim(keys)
        except Exception:
            log.debug('key claim failed', exc_info=True)
    SHELL_VOLUME_KEYS = {'9': -2.0, '0': 2.0}

    def _shell_claimed_keys(self):
        """Keys the SHELL takes, on top of whatever the page claimed.

        Only while audio is playing, because that is the whole of the case:
        `browse_block_keys` swallows mpv's own `9`/`0`/`m` along with every
        other printable key, and music is the one thing you can be listening
        to *while looking at the library*. Claiming them back means the
        now-playing bar's slider tracks the change and the server hears
        about it, which mpv's own binding did neither of.

        Gated on the setting as well: someone who turned the block off asked
        for their own keyboard back, and taking three keys out of it anyway
        would be the same interception with a smaller footprint.
        """
        from ..conf import settings
        if not self._browsing or self._now_playing is None:
            return ()
        if not settings.browse_block_keys:
            return ()
        return tuple(self.SHELL_VOLUME_KEYS) + ('m', 'SPACE')

    def _set_picture_pan(self, config=None):
        """Push (or drop) the renderer's gesture model for a picture."""
        setter = getattr(self.app, 'set_picture_pan', None)
        if setter is None:
            return
        try:
            setter(config)
        except Exception:
            log.debug('could not set the pan model', exc_info=True)

    def _on_picture_gesture(self, kind, evt):
        """A wheel or drag gesture over a displayed picture. Loop thread.

        Node-less, so it cannot go through the handler registry: the
        picture is mpv's video, not something in the scene. Handed to
        whichever page put it there, which is the only thing that knows
        what "past the bottom" means.
        """
        page = self._page_for(self.route)
        handler = getattr(page, 'on_picture_gesture', None)
        if handler is None:
            return
        try:
            handler(kind, evt)
        except Exception:
            log.warning('page picture gesture failed', exc_info=True)

    def _shell_key(self, key):
        """Handle a key the SHELL claimed, and say whether it did.

        Only for keys the current page did not claim by name -- see
        `_on_claimed_key`. The two claims are unioned into one list, so the
        renderer delivers both with nothing to say whose a key was; the
        caller resolves that, once, rather than each page defending itself.

        The state is read HERE and not captured at claim time. A claim is
        installed from a frame and the press arrives later, so between the
        two the music can have stopped -- the standing footgun of this
        shell, and the reason there is a test named for it.
        """
        if self._now_playing is None or self.controller is None:
            return False
        step = self.SHELL_VOLUME_KEYS.get(key)
        try:
            if step is not None:
                self.controller.adjust_volume(step)
                return True
            if key == 'm':
                self.controller.toggle_mute()
                return True
            if key == 'SPACE':
                self.controller.toggle_pause()
                return True
        except Exception:
            log.debug('shell volume key failed', exc_info=True)
            return True
        return False

    def _on_claimed_key(self, key):
        """A key this route claimed. Handed to the page, on the loop thread."""
        page = self._page_for(self.route)
        if key not in (getattr(page, 'claimed_keys', ()) or ()):
            if self._shell_key(key):
                return
        handler = getattr(page, 'on_key', None)
        if handler is None:
            return
        try:
            handler(key)
        except Exception:
            log.warning('page key handler failed', exc_info=True)

    def _page_for(self, route):
        """The Page serving ``route``, or None if its kind is still a mixin.

        Cached on the route dict so load() and render() share one instance and
        a page can keep state on itself rather than in the route.
        """
        cls = PAGES.get(route.get('kind'))
        if cls is None:
            return None
        page = route.get(self.PAGE_OBJ_KEY)
        if page is None or type(page) is not cls:
            page = cls(self._page_context(), route)
            route[self.PAGE_OBJ_KEY] = page
        else:
            page.ctx = self._page_context()
        return page

    def _load_route(self, route, epoch=None):
        """Dispatch to the route kind's loader, if it has one.

        Kinds are declared in each mixin's ROUTES table alongside their
        renderer, so adding a view is one edit in one place.

        The epoch is re-read here rather than threaded down from the
        ``_bump_epoch()`` every caller performs immediately above. **That is
        deliberate and it is not the race it looks like**: re-reading yields the
        newest epoch, so a loader can never capture one already superseded,
        whereas threading the value down lets an interloping bump strand the
        view spinning with no retry. ``TestNavigationSurvivesAConcurrentBump``
        pins it; the reasoning is in docs/browser-shell.md section 2.

        ``epoch`` therefore exists only for callers that genuinely have their
        own (none today). Leave it None.
        """
        if self.server is None:
            return
        route.pop('_error', None)
        ep = self._epoch if epoch is None else epoch
        mark_stale = getattr(self.source, 'mark_stale', None)
        if mark_stale is not None:
            mark_stale()
        page = self._page_for(route)
        if page is not None:
            page.load(ep)
            return
        loader = (self._routes().get(route['kind']) or (None, None))[0]
        if loader is not None:
            getattr(self, loader)(route, ep)

    def _edit_call(self, fn, on_ok=None, on_error=None, error=None):
        """A mutating edit whose failure the user must see.

        _client_call swallows: an "Add to playlist" the server rejected
        looked exactly like one that worked. ``on_error`` undoes whatever
        the view already showed optimistically — leaving a rejected change
        on screen is worse than never showing it."""
        ep = self._epoch
        msg = error or _('The change could not be applied.')

        def work():
            fn(self.controller)

        def done(_ok):
            if on_ok is not None:
                on_ok()

        def failed(_exc):
            if on_error is not None:
                on_error()
            self.set_status(msg)
        self.run_async(work, done, ep, on_error=failed)

    def _client_call(self, fn):
        """Run a client-mutating action (watched/favorite) off the loop
        thread so a slow server never stalls the UI."""
        if self.controller is None:
            return
        self._pool.submit(lambda: self._safe(fn))

    def _safe(self, fn):
        try:
            fn(self.controller)
        except Exception:
            log.warning('client action failed', exc_info=True)

    def _open_item(self, item):
        t = item.get('Type')
        server = self.route.get('server') or self.server
        base = {'server': server, 'item_id': item.get('Id'), 'title': item.get('Name', '')}
        if t == 'MusicAlbum':
            self.navigate(dict(base, kind='album'))
        elif t == 'MusicArtist':
            self.navigate(dict(base, kind='artist'))
        elif t == 'MusicGenre':
            self.navigate(dict(base, kind='music_genre', parent_id=self.route.get('parent_id')))
        elif t == 'Playlist':
            self.navigate(dict(base, kind='playlist'))
        elif t == 'Audio':
            self._play_list([item.get('Id')], server, audio=True)
        elif t == AUDIOBOOK_TYPE:
            self.navigate(dict(base, kind='audiobook'))
        elif t == BOOK_TYPE:
            self.navigate(dict(base, kind='book'))
        elif t == 'Studio':
            self.navigate(dict(base, kind='byname', list={'type': 'items', 'studio_ids': item.get('Id')}))
        elif t == 'Genre':
            self.navigate(dict(base, kind='byname', list={'type': 'items', 'genre_ids': item.get('Id')}))
        elif t == PHOTO_TYPE:
            self._play_photo(item, server)
        elif item.get('CollectionType') == LIVE_TV_COLLECTION:
            self.navigate(dict(base, kind='livetv', parent_id=item.get('Id'), title=item.get('Name') or _('Live TV')))
        elif t == 'Program':
            self.navigate(dict(base, kind='program', channel_id=item.get('ChannelId'), _seed=item))
        elif t in LIVE_TYPES:
            self.navigate(dict(base, kind='channel', item_id=item.get('ChannelId') or item.get('Id'), _seed=item))
        elif item.get('CollectionType') == 'music':
            self.navigate(dict(base, kind='music', parent_id=item.get('Id')))
        elif t in SERIES_TYPES:
            self.navigate(dict(base, kind='series'))
        elif t == 'Season':
            self.navigate(dict(base, kind='season', series_id=item.get('SeriesId'), bar_title=item.get('SeriesName') or None))
        elif t in PLAYABLE_TYPES:
            self.navigate(dict(base, kind='detail'))
        elif t in ('Person', 'Actor', 'Director', 'Writer'):
            self.navigate(dict(base, kind='person', person_id=item.get('Id')))
        elif t in FOLDER_TYPES or item.get('CollectionType'):
            ctype = item.get('CollectionType')
            if ctype is None and self.route.get('collection_type') == BOOKS_COLLECTION:
                ctype = BOOKS_COLLECTION
            self.navigate(dict(base, kind='books' if ctype == BOOKS_COLLECTION else 'grid', parent_id=item.get('Id'), parent_type=t, collection_type=ctype))
        else:
            self.set_status(_('Selected: %s') % item.get('Name', ''))
            self.invalidate()

    def _set_renderer_active(self, active):
        """Suspend/resume the in-mpv renderer. Pushing an empty scene is not
        enough to yield to the OSC — the renderer's forced mouse/wheel
        bindings keep swallowing the clicks until it is suspended."""
        if self.app is not None and hasattr(self.app, 'set_active'):
            try:
                self.app.set_active(active)
            except Exception:
                log.debug('set_active failed', exc_info=True)

    def set_app(self, app):
        """Point the browser at a (possibly fresh) MpvtkApp and wire the
        callbacks. mpv re-creation attaches a brand-new app per handle —
        without re-wiring here its nav/HUD events would go nowhere (the
        old app object kept the handlers)."""
        self.app = app
        self.hud.reset()
        if app is None:
            return
        if hasattr(app, 'on_nav'):
            app.on_nav = self._on_nav_mode
        if hasattr(app, 'on_hud'):
            app.on_hud = self.hud.on_hud
        if hasattr(app, 'on_hud_skip'):
            app.on_hud_skip = self.hud.on_skip
        if hasattr(app, 'on_pause'):
            app.on_pause = lambda: self._ctl(lambda c: c.toggle_pause())
        if hasattr(app, 'on_clipboard_error'):
            app.on_clipboard_error = self._on_clipboard_error
        if hasattr(app, 'on_forward'):
            app.on_forward = self._on_mouse_forward
        if hasattr(app, 'on_key'):
            app.on_key = self._on_claimed_key
        if hasattr(app, 'on_gamepad_seek'):
            app.on_gamepad_seek = lambda d: self._ctl(lambda c: c.kb_seek(d))
        if hasattr(app, 'on_gamepad_nav'):
            app.on_gamepad_nav = lambda a: self._ctl(lambda c: c.remote_action(a))
        app.on_picture_gesture = self._on_picture_gesture
        if hasattr(app, 'on_scene_pushed'):
            app.on_scene_pushed = self._on_scene_pushed

    def reassert_window_state(self):
        """Re-assert window ownership on a FRESH renderer (which starts
        active): browse takes the window back; a video in flight
        re-enters attached-but-idle HUD mode; otherwise get fully out
        of the way (lua OSC / minimized).

        `hud.state` alone is not "a video is in flight". It is cleared only
        by a `stopped` push, and a queue advance does not always make one --
        the player suppresses the incidental stopped pushes a load produces
        -- so after a film it holds that film's playstate for the whole of
        the track that followed, and mpv being re-created under a playing
        track put the renderer into HUD mode over the library, auto-hide and
        all.

        Asked here rather than fixed by clearing `hud.state` when a track
        starts, which is what the first attempt did: a start is not atomic
        from this side, so late audio pushes (the now-playing ticker sends
        one a second) arrive *after* `_start(audio=False)` has yielded the
        window, and a destructive write on that path tore the HUD down off
        the video that had just begun -- no player controls at all. A read
        cannot race anything.
        """
        if self._browsing:
            self._set_renderer_active(True)
        elif self.hud.available() and self._now_playing is None and (self.hud.state is not None):
            try:
                self.hud.engage()
            except Exception:
                log.debug('set_hud failed', exc_info=True)
        else:
            self._set_renderer_active(False)

    def _tell_controller(self, name):
        """Fire one of the browse/playback transition callbacks, and survive
        it raising. Returns its result, or None if it raised.

        These three run in the MIDDLE of a transition -- ``_yield`` has
        already cleared ``_browsing`` and still has to engage the HUD;
        ``enter_browse`` still has to refresh Home and re-activate the
        renderer. An exception out of the controller therefore did not fail
        the callback, it abandoned the transition half-applied, and the
        browser stayed in a state nothing would put right until the next one.

        Found the hard way: gateway.on_browse_leave read a setting #615 had
        retired, so every single browse -> video transition raised
        AttributeError and skipped the HUD engage that follows it.
        """
        if self.controller is None:
            return
        fn = getattr(self.controller, name, None)
        if fn is None:
            return
        try:
            return fn()
        except Exception:
            log.exception('controller.%s failed', name)
            return None

    def _park_on_leaving_browse(self):
        """Park the scroll offsets on the way out of the browser.

        Yielding to playback pushes an EMPTY scene, and the renderer drops
        the state of every container that left the scene. Unlike a
        navigation, coming back is a rebuild of the *same* route — nothing
        pushes or pops — so ``navigate``'s park never runs and there was
        nothing left for ``Page.parked_scroll`` to restore. Press play on
        something near the end of a library and the library came back at the
        top; whether it came back *blank* on the way depended on whether the
        live read still had the old offsets when the window was built.

        The ``_browsing`` guard this needs lives in ``_park_scroll`` now, so
        every caller gets it: a video start parks here and then reaches
        ``_yield`` once playback reports in, by which time the containers
        have left the scene and a park would write a partial snapshot over
        this one.
        """
        self._park_scroll()

    def _yield(self):
        self._park_on_leaving_browse()
        self._browsing = False
        self._release_page_grabs()
        self._tell_controller('on_browse_leave')
        if self.hud.available():
            try:
                self.hud.engage(reset=True)
            except Exception:
                log.debug('set_hud failed', exc_info=True)
        else:
            self._set_renderer_active(False)
        self._drop_key_claims()
        self.invalidate()

    def _start(self, audio, title=''):
        """Prepare to start playback. Video yields the whole window to the
        video + OSC; audio has no picture, so we stay in browse and show the
        now-playing bar instead (playing would-be background over audio would
        stop it)."""
        self.load.error = None
        if audio:
            self._now_playing = self._now_playing or {'title': _('Loading…')}
            self.invalidate()
        else:
            self._park_on_leaving_browse()
            self._browsing = False
            self.load.begin(title)
            self.invalidate()
    SPINNER_DELAY = LoadFeedback.SPINNER_DELAY

    def _arm_spinner(self):
        """Repaint once the grace period is up — nothing else would.

        The load holds no ticker: if it finishes first, load.clear() repaints
        and this wakes to find nothing to do. The waiting policy (why a loop
        rather than a flat sleep) is LoadFeedback.due_in; the thread slot and
        the shutdown event are the shell's, which is why this stayed here.
        """

        def show():
            while not self._shutdown_evt.is_set():
                due_in = self.load.due_in()
                if due_in is None:
                    break
                if due_in <= 0:
                    break
                self._shutdown_evt.wait(due_in)
            with self._poller_lock:
                self._spinner_timer = None
            self.invalidate()
        self._start_daemon('_spinner_timer', 'mpvtk-spinner', show)

    def open_hud_menu(self):
        """kb_menu during playback. Forwarded: playerManager holds a
        reference to this bound method (see ui.py)."""
        return self.hud.open_menu()

    def _play_async(self, work):
        """Start playback off the loop thread.

        ``work()`` receives the controller and runs on a pool worker.

        Starting playback is seconds of work: the controller builds a
        ``Media``, asks the server for PlaybackInfo, then loads the file into
        mpv under the player's own lock. Called straight from a click handler
        that ran on the loop thread, so the UI dispatched no events and drew
        no frames until playback began — click a movie and the browser froze.
        The episode path was worse: it ran inside a ``run_async`` ``on_done``,
        which holds ``_lock``, so every other worker's callback and any
        ``navigate()`` queued up behind it too.

        Deliberately NOT epoch-gated: the user pressed Play, and navigating
        elsewhere while it starts is not a reason to cancel it.
        """
        if self.controller is None:
            return

        def failed(_exc):
            self.set_status(_('Playback could not be started.'))
            self.invalidate()
        self.run_async(lambda: work(self.controller), lambda _r: None, self._epoch, on_error=failed)

    def _play(self, item, server, offset_ticks=None, srcid=None, aid=None, sid=None):
        self._actions.play(item, server, offset_ticks=offset_ticks, srcid=srcid, aid=aid, sid=sid)

    def _play_list(self, ids, server, start_index=0, audio=False, items=None):
        self._actions.play_list(ids, server, start_index, audio, items)

    def _play_photo(self, item, server):
        """Open a photo, with the rest of its album queued behind it.

        The album is whatever the grid this was clicked in has LOADED -- no
        fetch, and it matches what the user can see. Since #617 that is a
        window rather than everything walked past, so a photo opened deep in
        a large album queues its neighbourhood rather than the whole folder;
        the holes are skipped like any other consumer skips them. Falls back
        to the one photo when the route has no list (a search result, say),
        which still opens the picture.
        """
        items = [i for i in self.route.get('_items') or [] if i and i.get('Type') == PHOTO_TYPE and i.get('Id')]
        ids = [i['Id'] for i in items]
        try:
            start = ids.index(item.get('Id'))
        except ValueError:
            ids, items, start = ([item.get('Id')], [item], 0)
        self._play_list(ids, server, start, items=items)

    def start_background_work(self):
        """Kick off the pollers that keep the chrome honest (download status)
        and the one-shot startup update check. Called once the browser is
        live; separate from __init__ so tests don't spawn threads."""
        self._poll_download_status()
        if self.controller is not None:
            self._pool.submit(lambda: self._safe(lambda c: c.check_updates()))

    def enter_browse(self):
        """Show the browser: take the window + hide the OSC, then render.
        mpvtk-active yes also drops the renderer out of HUD mode."""
        if self.headless and self.route.get('kind') not in self.HEADLESS_ROUTES:
            self.show_cast()
        self._browsing = True
        self._minimized = False
        self.hud.shown = False
        if self._tell_controller('on_browse_enter') is False:
            self._browsing = False
            if self.hud.available():
                try:
                    self.hud.engage(reset=True)
                except Exception:
                    log.debug('set_hud failed', exc_info=True)
            else:
                self._set_renderer_active(False)
            self._drop_key_claims()
            self.invalidate()
            return
        if not self._browsing:
            return
        self.refresh_userdata(now=True)
        self._set_renderer_active(True)
        self.invalidate()

    def minimize(self):
        """Release the window entirely — the app keeps running in the tray as
        a cast target. This is the player's "playback_abort yes, force_window
        no" state; there is no separate window to hide, so minimizing *is*
        dropping force_window with nothing playing."""
        self._park_on_leaving_browse()
        self._minimized = True
        self._browsing = False
        self._release_page_grabs()
        self.hud.shown = False
        self._set_renderer_active(False)
        self._tell_controller('on_minimize')

    @property
    def minimized(self):
        return self._minimized

    def on_stopped_at(self, stop):
        """The position a stop reported, for the page showing that item.

        A detail or audiobook page left open says Resume from its own copy
        of the item. Re-reading it is not enough on its own: coming back to
        the library re-reads at once, BEFORE the stop report has reached the
        server (it is queued), and that read said the pre-stop position --
        measured, Resume at 61 s for a stop at 244 s. So the page is patched
        from what the app reported, and the patch is re-applied to any load
        that started before the server had the report (`_route_async`). The
        player calls this twice: at the stop, and with ``delivered`` once the
        report is through, which is when a re-read can be believed.

        Called by the player, possibly under its lock: patch and repaint,
        nothing that blocks or calls back into it.
        """
        route = self.route
        if not stop or stop.get('ticks') is None or route.get('kind') not in ('detail', 'audiobook') or (route.get('item_id') != stop.get('id')) or (stop.get('server') is not None and (route.get('server') or self.server) != stop.get('server')):
            return
        patch = route.get('_stop_patch')
        if stop.get('delivered') and patch is not None and (not patch['delivered']):
            patch['delivered'] = True
            self.refresh_userdata(now=True)
            return
        route['_stop_patch'] = {'ticks': stop['ticks'], 'finished': bool(stop.get('finished')), 'delivered': bool(stop.get('delivered'))}
        self._apply_stop_patch(route)

    def _apply_stop_patch(self, route):
        patch = route.get('_stop_patch')
        data = route.get('_data')
        item = (data or {}).get('item') if route.get('kind') == 'detail' else data
        if patch is None or not isinstance(item, dict):
            return
        ud = dict(item.get('UserData') or {})
        if patch['finished']:
            ud['Played'], ud['PlaybackPositionTicks'] = (True, 0)
        else:
            ud['PlaybackPositionTicks'] = int(patch['ticks'])
        item['UserData'] = ud
        self.invalidate()

    def on_playstate(self, state):
        """Registered as playerManager.on_playstate. Drives browse/playback
        state and the now-playing bar. Audio keeps the browser visible (bar +
        browsing); video stays yielded to the picture + OSC."""
        if state and (not state.get('stopped')) and (not self._still_playing(state)):
            return
        if not (state or {}).get('stopped'):
            self.load.clear()
        self._sync_queue_highlight(state)
        now_id = (state or {}).get('id')
        track_changed = now_id != (self._now_playing or {}).get('id')
        if not state or state.get('stopped') or track_changed:
            self._np_scrub = None
        if self.headless:
            self._follow_cast_to_playback(state, track_changed)
        if not state or state.get('stopped'):
            self._now_playing = None
            self.hud.state = None
            self.hud.shown = False
            self.hud.menu = None
            if self.load.error is not None:
                self.invalidate()
                return
            if self.load.starting is not None:
                self.invalidate()
                return
            if self._minimized:
                self.minimize()
            else:
                self.enter_browse()
            return
        if state.get('is_audio'):
            self._now_playing = state
            if not self._browsing:
                self.enter_browse()
            else:
                self.invalidate()
            self._start_np_ticker()
        else:
            self._now_playing = None
            self.hud.state = state
            if self._browsing:
                self._yield()
                if self._held_item() is None:
                    self.hud.state = None
                    self.enter_browse()
                    return
            else:
                self.invalidate()
            if not self._browsing and self.hud.available():
                try:
                    self.hud.engage()
                    self.app.set_hud_skip(state.get('skip_label') or '')
                except Exception:
                    log.debug('hud sync failed', exc_info=True)

    def _still_playing(self, state):
        """Whether the player still holds the item ``state`` describes.

        Playstates arrive from several threads unordered: the 1 s ticker's
        snapshot of a video could land after that video's stop had put the
        library back, and yielding to it left the library blank after HUD
        Back. The player clears the item before it reports a stop, so asking
        before the yield and again after leaves the last word with the truth.
        Not a lock across on_playstate: that deadlocks a playback start
        (on_browse_leave -> enable_osc waits on the player's _lock).
        """
        held = self._held_item()
        return held is _UNKNOWN or held == state.get('id')

    def _held_item(self):
        """The player's item id, None when it holds nothing, or `_UNKNOWN`
        when there is nothing to ask (tests, embedders) or asking failed."""
        ask = getattr(self.controller, 'playing_item_id', None)
        if ask is None:
            return _UNKNOWN
        try:
            return ask()
        except Exception:
            return _UNKNOWN

    def _sync_queue_highlight(self, state):
        """Keep the queue view on the player's queue.

        The highlight comes off the playstate's id, with no refetch. The
        rows are re-read when the playstate's ``queue_rev`` differs from the
        one they were read at -- the player moves it on every queue write,
        including a Play Next from a phone or a SyncPlay update while the
        same item plays -- and on a stop."""
        route = self.route
        if route.get('kind') != 'queue':
            return
        data = route.get('_data')
        if not data:
            return
        new = None if not state or state.get('stopped') else state.get('id')
        if data.get('current_id') != new:
            data['current_id'] = new
            self.invalidate()
        if new is None or state.get('queue_rev') != data.get('queue_rev'):
            self._refresh_current({'queue'})

    def _start_daemon(self, attr, name, body, restartable=False):
        """Run ``body`` on a daemon thread, at most one per ``attr``.

        **The check and the assignment have to be atomic.** Callers are
        reachable from the loop thread *and* from foreign ones
        (``on_playstate``, ``on_downloads_changed``), so two could both see None
        and both start a thread -- and a doubled poller is only a wasted refresh
        today, which is exactly why it would go unnoticed.

        ``attr`` is cleared when the thread exits. Returns True if this call
        started the thread, False if one was already running -- callers driven
        by a *user action* should say so rather than appear to do nothing.

        ``restartable=True`` makes a departing thread ``invalidate()`` once it
        has released the slot, closing the window in which a poller exits just
        after a re-entering view was refused one and leaves the panel frozen.
        Opt-in, because ``_arm_toast_clear`` releases its slot early by design.
        See docs/browser-shell.md section 8.
        """
        with self._poller_lock:
            if getattr(self, attr) is not None:
                return False

            def run():
                try:
                    body()
                finally:
                    released = False
                    with self._poller_lock:
                        if getattr(self, attr) is thread:
                            setattr(self, attr, None)
                            released = True
                    if released and restartable:
                        self.invalidate()
            thread = threading.Thread(target=run, daemon=True, name=name)
            setattr(self, attr, thread)
        thread.start()
        return True

    def _run_long(self, work, name):
        """Run a job that can take minutes, off the pool.

        The pool has four workers and serves every route load and every
        client mutation. A job that holds one for minutes — relocating the
        download store copies the whole thing, possibly across drives —
        starves browsing, and a handful of them would stop it outright.
        Long jobs get their own thread instead.

        One at a time, and False if one is already running."""

        def run():
            try:
                work()
            except Exception:
                log.error('long job %r failed', name, exc_info=True)
        return self._start_daemon('_long_thread', name, run)

    def _start_np_ticker(self):
        """Keep the now-playing bar's clock at 1s.

        The timeline thread only pushes state every 5s (it also talks to the
        server, so speeding it up is not free). While the bar is on screen we
        ask the player for a fresh snapshot once a second instead; the thread
        exits as soon as the bar goes away."""
        if self.controller is None:
            return

        def tick():
            while not self._shutdown_evt.wait(1.0):
                bar = self._now_playing is not None and self._browsing
                if not bar and (not self.hud.shown):
                    break
                try:
                    self.controller.refresh_playstate()
                except Exception:
                    log.debug('playstate refresh failed', exc_info=True)
        self._start_daemon('_np_thread', 'mpvtk-np-tick', tick)

    def _publish_auth_origins(self):
        """Tell the thumbnail store which token belongs to which server.

        Images are fetched on the store's own session, so this is how they
        authenticate by header rather than by query string. Called on every
        source swap, which is also what revokes a signed-out server's token
        -- the store replaces its map wholesale.
        """
        origins = {}
        get = getattr(self.source, 'auth_origins', None)
        if get is not None:
            try:
                origins = get()
            except Exception:
                log.debug('could not publish auth origins', exc_info=True)
        try:
            self.thumbs.set_auth(origins)
        except Exception:
            log.debug('thumbnail store would not take auth origins', exc_info=True)

    def set_source(self, source, server_uuid=None, keep_place=False):
        """Swap in a live data source once servers connect (the browser opens
        immediately on a spinner and populates when the network settles).

        A catalog-backed source raises the offline banner: every path that
        can land offline goes through here, so deriving the banner from the
        source is what keeps the two from drifting apart.

        ``keep_place=True`` refreshes in place instead of resetting to Home.
        Use it for anything that is not a deliberate user action. A *reconnect*
        arrives from the websocket redial loop, the cast-recovery path and the
        periodic health check — i.e. at arbitrary moments mid-session — and
        resetting the nav stack there threw the user out of whatever they were
        reading, with no interaction on their part, every time a flaky server
        bounced.
        """
        from .repository import OfflineLibrarySource
        self.set_offline(isinstance(source, OfflineLibrarySource))
        if not self._offline:
            self._offline_chosen = False
        self._unreachable = False
        self.source = source
        self._publish_auth_origins()
        try:
            servers = source.servers()
        except Exception:
            servers = []
        server = self._pick_server(servers, server_uuid)
        known = {s.get('uuid') for s in servers}
        stay = keep_place and self.nav_stack and (self.server == server) and all((r.get('server') in known or r.get('server') is None for r in self.nav_stack))
        self.server = server
        if not stay:
            self._nav.reset_to_default()
        self._bump_epoch()
        self._load_route(self.route)
        self._refresh_downloaded()
        if self.route.get('kind') == 'cast' and (self._cast or {}).get('idle'):
            self.show_cast_idle()
        self.invalidate()

    def _follow_cast_to_playback(self, state, track_changed):
        """Keep the headless cast screen showing the current track.

        Stopping goes back to "Ready to cast" rather than leaving the last
        thing played on screen, which reads as though it is still playing.
        """
        if not state or state.get('stopped'):
            if not (self._cast or {}).get('idle'):
                self.show_cast_idle()
            return
        if not (state.get('is_audio') and track_changed and now_id_of(state)):
            return
        server = state.get('server_uuid') or self.server
        if server is not None:
            self.display_cast_item(server, now_id_of(state))

    def on_downloads_changed(self):
        """Sync-manager push: the catalog changed. Runs on the download
        worker's thread, so it only schedules the refresh."""
        try:
            self._refresh_downloaded()
        except Exception:
            log.debug('download refresh failed', exc_info=True)

    def _refresh_downloaded(self):
        """Refresh the downloaded-id sets for tile badges (from the sync db)."""
        if self.controller is None:
            return

        def work():
            try:
                self.tiles.set_downloaded(*self.controller.downloaded_ids(self.server))
            except Exception:
                log.debug('downloaded-id refresh failed', exc_info=True)
            try:
                self._actions.flush_pending_reads()
            except Exception:
                log.debug('pending book reads failed', exc_info=True)
            try:
                page = self._page_for(self.route)
                refresh = getattr(page, 'refresh_download_state', None)
                if refresh is not None:
                    refresh()
            except Exception:
                log.debug('page download-state refresh failed', exc_info=True)
            self.invalidate()
        self._pool.submit(work)

    def build(self, size):
        w, h = size
        self._size = size
        self._scroll.refresh(self.app, self.route)
        if self.load.error is not None:
            return self.load.error_scene(size)
        if self.load.starting is not None and (not self._browsing) and self.load.spinner_due():
            return self.load.loading_scene(size)
        if not self._browsing:
            self._drop_abandoned_sync_path()
            if self.hud.shown:
                return build_hud(self, size)
            return Column([], w=w, h=h)
        if self.thumbs is not None:
            self.thumbs.pump()
        self._shed_caches_on_screen_change()
        route = self.route
        if route['kind'] in LIVE_KINDS:
            self._poll_live_tv(route)
        content = self._render_route(route, size)
        hero_wait = route.get('kind') in {'home', 'detail', 'series', 'season'} and (route.get('_cinema_hero_pending') or (route.get('_loading') and route.get('_data') is None))
        wait_key = (id(route), self._screen_seq)
        now = time.monotonic()
        if hero_wait and (not route.get('_error')) and (self._dialog is None):
            waiting = getattr(self, '_cinema_scene_wait', None)
            if waiting is None or waiting[0] != wait_key:
                waiting = (wait_key, now)
                self._cinema_scene_wait = waiting
            if getattr(self.app, '_nodes', None) and now - waiting[1] < 5.0:
                if getattr(self, '_cinema_wait_timer', None) != wait_key:
                    self._cinema_wait_timer = wait_key
                    timer = threading.Timer(5.05, self.invalidate)
                    timer.daemon = True
                    timer.start()
                return None
        else:
            self._cinema_scene_wait = None
            self._cinema_wait_timer = None
        self._claim_page_keys(route)
        self._retire_page(route)
        self._drop_abandoned_sync_path()
        children = []
        immersive_header = None
        if route['kind'] not in CHROME_FREE:
            banner = window_chrome.banner(self)
            dlbar = window_chrome.download_bar(self)
            from .components.cinema_chrome import active
            immersive = active(self) and banner is None and (dlbar is None)
            if immersive:
                immersive_header = window_chrome.chrome(self, w, immersive=True)
            else:
                children.append(window_chrome.chrome(self, w))
            if banner is not None:
                children.append(banner)
            if dlbar is not None:
                children.append(dlbar)
        children.append(Stack([content, immersive_header], flex=1) if immersive_header is not None else content)
        pbar = self._pagination_bar(route, w)
        if pbar is not None:
            children.append(pbar)
        if self._now_playing is not None and route['kind'] not in NO_NOW_PLAYING:
            children.append(self._now_playing_bar(w))
        if self._menu is not None:
            menu = window_chrome.history_menu_node(self) if self._menu.get('kind') == 'history' else self._tile_menu_node()
            if menu is not None:
                children.append(menu)
        if self._dialog is not None:
            children.append(self._dialog())
        toast = window_chrome.toast_node(self, w, h)
        if toast is not None:
            children.append(toast)
        grip = window_chrome.resize_grip(self, w, h)
        if grip is not None:
            children.append(grip)
        page = Column(children, w=w, h=h, align='stretch')
        if self.route.get('_showing'):
            return page
        stops = theme.window_gradient()
        if route.get('_cinema_immersive'):
            back = Box(bg=route.get('_cinema_ambient') or theme.WINDOW_BG, w=w, h=h)
        elif stops:
            back = Gradient(stops=stops, axis='y', w=w, h=h)
        elif self._wants_opaque_backdrop():
            back = Box(bg=theme.WINDOW_BG, w=w, h=h)
        else:
            return page
        return Stack([back, page], w=w, h=h)

    def _wants_opaque_backdrop(self):
        """Whether the library has to paint its own window background.

        Normally it does not: mpv's ``background-color`` is the window, and
        one flat fill costs nothing to composite. Under "Custom OSC" that is
        not enough. The controls are then a script we never see, it draws an
        idle screen while the browse window sits there with nothing loaded,
        and that screen is OSD *over* the VO background -- so no colour we
        give mpv can hide it. Ours is a bitmap, which composites above OSD,
        and is the only thing that can. (Asking the script to stop drawing
        works too and we do ask, but only a fork that honours
        ``osc-idlescreen`` hears it, and one that has already drawn may have
        no path left that wipes it.) docs/mpv-backends.md section 12.

        Through `resolve_osc_style`, never the raw setting, because that can
        hold a legacy alias. Not through the player, which would cost this
        module an import of `player` -- and importing that builds its
        singleton and opens an mpv window.
        """
        from ..mpv_options import resolve_osc_style
        try:
            return resolve_osc_style() == 'custom'
        except Exception:
            return False
    TOAST_SECS = 6.0

    def _arm_toast_clear(self, delay):
        """Repaint once the toast has expired — nothing else would."""

        def clear():
            self._shutdown_evt.wait(delay)
            with self._poller_lock:
                self._toast_timer = None
            self.invalidate()
        self._start_daemon('_toast_timer', 'mpvtk-toast', clear)

    def set_status(self, text):
        """Show a transient message. Use this rather than assigning to
        ``status``, so the toast's timer starts."""
        self.status = text or ''
        self._status_at = time.time()
        self.invalidate()

    def clear_status_if(self, text):
        """Take the toast down early, but only if it is still ours.

        For a message that *reports something in progress*: TOAST_SECS is
        six, and on a local server "Downloading X…" outlives the download
        it is about and sits over the first page of the book (#2).

        Conditional on the text, because six seconds is long enough for
        something else to have replaced it -- an error, a finished
        download, a queued item -- and a reader clearing that would be
        worse than the stale toast it was fixing.
        """
        if text and self.status == text:
            self.set_status('')
    TITLE_MIN_W = 260

    def _pick_server(self, servers, server_uuid=None):
        """Choose which server the library opens on.

        An explicit request wins; then the server this user last browsed, if
        it is still in the list (it may have been removed, or be down this
        launch); then the first one. That fallback is not a preference —
        server order is connection order, which sorts by network locality, so
        without the remembered value the default silently changes between
        launches on a multi-server setup.
        """
        if not servers:
            return None
        known = {s.get('uuid') for s in servers}
        if server_uuid:
            if server_uuid in known:
                return server_uuid
            log.info('server %r is not in this source; picking another', server_uuid)
        getter = getattr(self.controller, 'get_last_server', None)
        if getter is not None:
            try:
                last = getter()
            except Exception:
                log.debug('could not read last server', exc_info=True)
            else:
                if last and last in known:
                    return last
        return servers[0]['uuid']

    def _remember_server(self, uuid):
        """Persist the browsed server. Best-effort — losing a preference must
        never break navigation."""
        setter = getattr(self.controller, 'set_last_server', None)
        if setter is None:
            return
        try:
            setter(uuid)
        except Exception:
            log.debug('could not persist last server', exc_info=True)

    def _switch_server(self, uuid):
        if uuid == self.server:
            return
        old = self.server
        if not self._server_is_browsable(uuid):

            def arrived():
                self._leave_syncplay_on(old)
                self._remember_server(uuid)
            self.reconnect_server(uuid, on_success=arrived, on_refused=self.refuse_server_pick)
            return
        self._leave_syncplay_on(old)
        self.server = uuid
        self._remember_server(uuid)
        self.navigate({'kind': 'home', 'server': uuid}, reset=True)

    def refuse_server_pick(self):
        self._server_pick_ack += 1
        self.invalidate()

    def retry_unreachable(self):
        """The unreachable banner's Retry: ask again for what failed. If the
        server still does not answer, the failure raises the banner again."""
        self._unreachable = False
        self._retry_route(self.route)
        self.invalidate()

    @property
    def offline_chosen(self):
        return self._offline_chosen

    def go_offline(self):
        """The server switcher's Offline entry (D3 case 2): browse the
        downloads, with every server left as it is. Stays until a server is
        picked -- see ``_offline_chosen``."""
        if self._offline or self.controller is None:
            return
        old = self.server

        def done(source):
            if source is None:
                self.set_status(_('Nothing is downloaded to browse offline.'))
                self.refuse_server_pick()
                return
            self._leave_syncplay_on(old)
            self.set_source(source)
            self._offline_chosen = True
        self.run_async(self.controller.offline_source, done, self._epoch)

    def _leave_syncplay_on(self, uuid):
        """Leave the SyncPlay group held on ``uuid``, if there is one.

        A group belongs to the server it was joined on, and this UI only ever
        talks to the selected one — so leaving that server means leaving the
        group, or it stays joined with no way to reach it from here.

        One function because there are now two ways off a server: the direct
        switch, and the reconnect that picking an offline entry performs. The
        second arrived without this and left the group standing, which is the
        multi-site shape this repo keeps producing.
        """
        if not uuid or self.controller is None:
            return
        try:
            if self.controller.sync_active():
                self._client_call(lambda c: c.sync_leave(uuid))
        except Exception:
            log.debug('syncplay leave on server switch failed', exc_info=True)

    def _server_is_browsable(self, uuid):
        """Does the live source hold this server?

        The source, not the credential list: a saved server is browsable
        exactly when the source has a connection for it, and that is the
        thing every route load is about to ask. Fails **open** -- a source
        that will not answer gets the old behaviour rather than a reconnect
        the user did not ask for.
        """
        try:
            return any((s.get('uuid') == uuid for s in self.source.servers() or []))
        except Exception:
            log.debug("could not list the source's servers", exc_info=True)
            return True

    def _open_queue(self):
        self.navigate({'kind': 'queue', 'server': self.server, 'title': _('Queue')})

    def _render_route(self, route, size):
        page = self._page_for(route)
        renderer = (self._routes().get(route['kind']) or (None, None))[1]
        if page is None and renderer is None:
            return self._busy()
        if route.get('_error') and route.get('_data') is None and (not route.get('_items')):
            return self._error_retry(route)
        if page is not None:
            return page.render(size)
        return getattr(self, renderer)(route, size)

    def _error_retry(self, route):
        return Box([Spacer(), Row([Spacer(), Text(route['_error'], size='large', color=theme.SUBTLE_FG), Spacer()]), Row([Spacer(), Button(_('Retry'), id='route-retry', icon='refresh', on_click=lambda: self._retry_route(route)), Spacer()]), Spacer()], flex=1, direction='column', align='stretch', gap=14)

    def notify_update(self, version, url):
        """Registered as playerManager.notify_update: show the update notice
        as a browser banner (mirrors the Tk browser / CLI-OSD split).

        ``flatpak`` decides the wording only -- the link stays either way.
        Asked once here rather than in the banner builder, which runs on
        every repaint while the notice is up.
        """
        from ..hostinfo import flatpak_managed
        self._update = {'version': version, 'url': url, 'flatpak': flatpak_managed()}
        self.invalidate()

    def resend_hud_config(self):
        """Re-send the renderer's HUD token. Called when SyncPlay is joined
        or left, because the token carries whether the renderer's own pause
        paths hand over to Python -- a group joined mid-playback would
        otherwise keep the local `cycle pause`, which in a group is a desync
        rather than a pause.

        Called directly from whichever thread SyncPlay is on, as
        ``notify_syncplay`` beside it is: ``engage`` ends in an mpv command,
        which is thread-safe, and deferring it would let a group be joined
        and a click land before the renderer had been told.

        **Guarded exactly as every other engage() call site is.** engage()
        is not a re-send, it is set_hud(True), and the renderer treats that
        as a MODE CHANGE when the HUD is not already up: ui_suspend() drops
        the nav keys, the mouse and the wheel bindings. Ungated, leaving a
        group from the library -- which is what pressing stop in a group
        does -- froze the library with no way back. And for a user on a lua
        OSC it switched the mpvtk summon layer on over their OSC.
        Un-engaged, the flag does not matter: none of the renderer's pause
        paths are live, and the next engage() sends the current value."""
        try:
            if self.hud is not None and (not self._browsing) and self.hud.available() and (self.hud.state is not None):
                self.hud.engage()
        except Exception:
            log.debug('could not re-send the HUD config', exc_info=True)

    def notify_syncplay(self, message):
        """Registered as playerManager.notify_syncplay: SyncPlay's messages
        on the status line rather than the MPV OSD.

        Dropped while video owns the window: the browser is yielded, so there
        is nothing on screen to draw a status line on, and a toast queued now
        would appear minutes later when the library came back. The log has
        them either way."""
        if not self._browsing:
            return
        self.set_status(message)

    def syncplay_menu_reachable(self):
        """Registered as playerManager.syncplay_menu_reachable: whether
        stopping playback lands somewhere the SyncPlay menu can be opened,
        which is what decides between halting the group and leaving it.

        Headless has one page and it is chrome-free; ``_minimized`` is the
        cast that never opened the library, and stopping it puts the window
        away rather than showing the browser. In both the SyncPlay button in
        the top bar is unreachable, so a halted group would be one nobody
        could get out of."""
        return not self.headless and (not self._minimized)

    @property
    def window_controls(self):
        """Whether the top bar is standing in for a title bar this frame."""
        return self._csd

    @property
    def maximized(self):
        """Live only while ``window_controls`` is on — it is read to pick the
        maximize button's glyph and nothing else consults it."""
        return self._maximized

    def refresh_window_controls(self):
        """Re-take the window-chrome snapshot.

        Called at startup and from ``playerManager.on_decorations_changed``.
        It has to be a push, not a poll: on Wayland the answer arrives in a
        compositor configure event, so a session's first frames can be drawn
        before mpv knows, and fullscreen/maximize change it again later.
        """
        ask = getattr(self.controller, 'window_chrome_state', None)
        if ask is None:
            return
        try:
            state = ask() or {}
        except Exception:
            log.debug('could not ask about window decorations', exc_info=True)
            return
        wanted = bool(state.get('controls'))
        maximized = bool(state.get('maximized'))
        if (wanted, maximized) != (self._csd, self._maximized):
            self._csd = wanted
            self._maximized = maximized
            self.invalidate()

    def close_window(self):
        """The title bar's close button. Deliberately the same path as mpv's
        own close (CLOSE_WIN), so ``close_to_tray`` and the no-tray safeguard
        decide what closing means here exactly as they do there -- a second
        close button with its own idea of "close" is how the two drift."""
        self._tell_controller('close_window')

    def minimize_window(self):
        """Iconify. Not ``minimize()``, which is this app's own tray-minimize
        (it tears the window down and keeps running headless) -- the title
        bar's button has to mean what it means in every other window."""
        self._tell_controller('minimize_window')

    def toggle_maximized(self):
        self._tell_controller('toggle_window_maximized')

    @property
    def offline(self):
        """Whether the browser is on the offline source. Read-only;
        ``set_offline`` is the single writer, and services read it live
        through here rather than reaching for ``_offline``."""
        return self._offline

    def set_offline(self, offline):
        offline = bool(offline)
        if offline != self._offline:
            self._offline = offline
            self.invalidate()

    def set_download_status(self, status):
        """``{"pending": int, "name": str, "percent": int|None}`` — pushed by
        the sync manager's progress hook."""
        if status == self._dl_status:
            return
        self._dl_status = status
        self.invalidate()

    def _poll_download_status(self):
        """Keep the status bar current. The sync manager has no push hook the
        browser can subscribe to, so poll it — cheaply, and only while there
        is something to report or the browser is on screen."""
        if self.controller is None:
            return

        def tick():
            while not self._shutdown_evt.wait(2.0):
                if not self._browsing:
                    continue
                try:
                    st = self.controller.download_status()
                except Exception:
                    break
                self.set_download_status(st)
        self._start_daemon('_dlbar_thread', 'mpvtk-dlbar', tick)

    def _dismiss_update(self):
        self._update = None
        self.invalidate()

    def _ignore_update(self):
        """"Ignore": do not raise this version again, in this run or any
        later one.

        The alternative people reach for is turning the update check off
        entirely, which is a much bigger answer to "not this one" -- and one
        they never revisit. `has_notified` only covers the current run.
        """
        version = (self._update or {}).get('version')
        self._dismiss_update()
        if not version:
            return
        try:
            from . import config as cfg
            cfg.set_setting('update_skip_version', version)
        except Exception:
            log.warning('could not remember the skipped version', exc_info=True)

    def note_restart_needed(self, key):
        """Remember that ``key`` will not do anything until a restart."""
        self._restart_keys.add(key)
        self.invalidate()

    def _dismiss_restart(self):
        """Put the banner away without restarting.

        The settings are still saved and still pending -- this only stops
        saying so, which is why the button is "Later" rather than
        "Dismiss". Changing another restart-required setting afterwards
        raises it again, naming that one: the user is being told about a
        new decision, not nagged about the one they answered.
        """
        self._restart_keys = set()
        self.invalidate()

    def can_restart(self):
        """Whether to offer a Restart button rather than only a notice.

        Asked at draw time and answered by the controller, so a stand-in
        without the method (and a machine where the launch cannot be
        reconstructed) both fall back to the notice. False is the safe
        answer: a button that takes the app away and does not bring it back
        is worse than no button.
        """
        if self._can_restart is not None:
            return self._can_restart
        can = getattr(self.controller, 'can_restart', None) if self.controller else None
        if can is None:
            return False
        try:
            self._can_restart = bool(can())
        except Exception:
            log.debug('could not ask whether a restart is possible', exc_info=True)
            self._can_restart = False
        return self._can_restart

    def _restart_now(self):
        """Restart the app. The banner's button.

        The keys are NOT cleared here. If the restart fails to start, the
        settings are still pending and the banner still has something true
        to say -- clearing first would have left the user with a working app,
        an unapplied setting and nothing on screen about either.
        """
        ok = False
        if self.controller is not None:
            restart = getattr(self.controller, 'restart_app', None)
            if restart is not None:
                try:
                    ok = bool(restart(set(self._restart_keys)))
                except Exception:
                    log.exception('could not restart')
        if not ok:
            self.set_status(_('Could not restart automatically — quit and start the app again to apply your changes.'))
            self.invalidate()

    def _open_url(self, url):
        if self.controller is not None and url:
            self._safe(lambda c: c.open_url(url))
        self._dismiss_update()

    def _retry_connect(self):
        """Offline banner → Retry. A reconnect that works has to swap the
        source in, or the banner clears while the catalog is still what's
        being browsed."""
        if self.controller is None:
            return
        ep = self._epoch

        def work():
            try:
                return self.controller.retry_connect()
            except Exception:
                log.warning('retry connect failed', exc_info=True)
                return None

        def done(source):
            if source is not None:
                self.set_source(source)
                return
            self.set_status(_("Still can't reach the server."))
            if self.route.get('kind') == 'connecting':
                self.route['_connect_error'] = _("Still can't reach the server.")
            self.invalidate()
        self.run_async(work, done, ep)

    def run(self):
        """Block the calling thread driving the app loop (spawned-app / demo
        use). For the shared-window integration this runs on a dedicated
        thread next to playerManager — see 0.2/0.5 wiring."""
        self.app.run(self.build)

    def shutdown(self, free_bitmaps=True):
        """Stop background work, and let the live page go.

        The page that is on screen has never been retired — retirement is
        observed from ``build()``, which stops running — so anything it
        took hold of is still held. For the comic reader that is a
        directory of extracted pages, which nothing else will delete:
        quitting inside a comic left them behind every time.

        ``free_bitmaps=False`` keeps the composited tile buffers alive. On
        libmpv those are read BY ADDRESS by mpv every frame it composites, so
        they may only be released once mpv is genuinely dead — the caller
        knows that, this does not. See mpvtk_browser.ui.stop().
        """
        page, self._live_page = (self._live_page, None)
        if page is not None:
            try:
                page.close()
            except Exception:
                log.debug('page close on shutdown failed', exc_info=True)
        self._shutdown_evt.set()
        self._async.shutdown(wait=False, cancel_futures=True)
        long_thread = self._long_thread
        if long_thread is not None and long_thread.is_alive():
            log.info('waiting for a long job to finish before shutdown')
            long_thread.join(timeout=self.LONG_JOB_SHUTDOWN_WAIT)
            if long_thread.is_alive():
                log.warning('long job still running at shutdown; it may be left incomplete')
        if self.thumbs is not None:
            self.thumbs.shutdown()
        self.strips.shutdown()
        if free_bitmaps:
            self.strips.clear()

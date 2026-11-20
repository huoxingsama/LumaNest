"""mpvtk browser as a ``user_interface`` — the in-process launcher.

Exposes the same small surface ``mpv_shim.main`` expects (``start``,
``login_servers``, ``stop``, ``open_player_menu``, ``stop_callback``) as
``cli_mgr``, but instead of a separate window in a child process it
attaches the mpvtk UI to the player's own mpv window (main process, next
to ``playerManager``).

Lifecycle: open the window immediately on a spinner (``enter_browse`` →
``force_window`` + OSC off), connect to servers in the background, then
swap in the live data source. A tile click on a playable item yields the
window to playback + the OSC; when playback stops (``on_playstate``) the
browser takes the window back.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Optional, cast
from ..clients import clientManager
from ..conf import settings
from ..i18n import _
from .gateway import PlayerGateway, _collect_servers, _saved_servers_exist
if TYPE_CHECKING:
    from ..mpvtk.app import MpvtkApp
    from .app import MpvtkBrowser
log = logging.getLogger('mpvtk_browser.ui')

class UserInterface:

    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop_callback = None
        self.gui_ready = None
        self._app: Optional['MpvtkApp'] = None
        self._browser: Optional['MpvtkBrowser'] = None
        self._thread: Optional[threading.Thread] = None
        self._tray = None
        self._detaching = False

    def start(self):
        from ..tray import TrayManager
        self._tray = TrayManager({'show': self.activate, 'show_preferences': lambda: self._open_settings('servers'), 'show_console': lambda: self._open_settings('logs'), 'open_config': self._open_config_folder, 'quit': self._quit})
        self._tray.start()

    def activate(self):
        """Surface the UI: leave playback, show the browser, raise the window.

        Also what SingleInstance calls when the app is launched a second time
        (mpv_shim wires ``single.on_activate`` to this)."""
        from ..player import playerManager
        if self._browser is not None:
            self._browser.maybe_relock()
            self._browser.enter_browse()
        try:
            playerManager.raise_window()
        except Exception:
            log.debug('could not raise the player window', exc_info=True)

    def on_second_launch(self):
        """The app was launched again (SingleInstance). Over a playing
        video that means "show me the app", and the video IS the app then:
        raise the window and leave the film alone [iw, 2026-09-27] --
        entering browse brought the library up for a moment and the film's
        next playstate took the window straight back. Otherwise, activate.
        """
        from ..player import playerManager
        try:
            watching = playerManager.get_video() is not None and (not playerManager._library_showing())
        except Exception:
            watching = False
        if not watching:
            return self.activate()
        try:
            playerManager.raise_window()
        except Exception:
            log.debug('could not raise the player window', exc_info=True)

    def _open_settings(self, tab):
        if self._browser is None:
            return
        self.activate()
        self._browser.open_settings(tab)

    @staticmethod
    def _server_uuid(client):
        """Which connected server a websocket event came from, or None.

        Identity, not equality: `clientManager.clients` is keyed by uuid and
        the values are the `JellyfinClient` objects the events are raised
        on.
        """
        if client is None:
            return None
        return next((u for u, c in clientManager.clients.items() if c is client), None)

    def _display_content(self, client, item_id):
        """Route a remote's DisplayContent to the browser, resolving which
        connected server it came from."""
        if self._browser is None:
            return
        self._browser.display_item(self._server_uuid(client), item_id)

    def _live_tv_changed(self, _client=None):
        """Forward a timer websocket event to the browser.

        Not filtered by server here: the browser refreshes whatever Live TV
        route is on screen, and a timer event only ever comes from a server
        this client is logged into. Guarding on identity would mean the
        common single-server case paid for a lookup to reach the same
        answer.
        """
        if self._browser is None:
            return
        self._browser.refresh_live_tv()

    def _user_data_changed(self, client=None):
        """Forward a UserDataChanged websocket event to the browser.

        **Filtered by server, unlike the timer events above**, and the
        difference is which screens answer. A timer event only ever affects
        Live TV, which is one screen. This one now re-reads a series or a
        season as well as Home (#722), and those belong to ONE server -- so
        in a two-server session, activity on A would otherwise re-fetch a
        Series page on B: a wasted round trip at best, and at worst an
        unrelated event driving the screen into the offline fallback when B
        is unreachable. Home is exempt inside `_refresh_current`, because it
        is assembled from every logged-in server.

        A client we cannot resolve gives None, which refreshes as before --
        no worse than the unfiltered behaviour this replaces.
        """
        if self._browser is None:
            return
        self._browser.refresh_userdata(self._server_uuid(client))

    def _open_config_folder(self):
        PlayerGateway().open_config_folder()

    def _quit(self):
        self.quit_app()

    def stop_tray(self):
        """Terminate the tray child. Idempotent, and safe to call twice.

        Public because the shutdown is not the only caller any more: the
        tray lives in its own process and is stopped in the sixth of seven
        shutdown steps, so a step that wedges before then leaves it running
        -- and since a restart now spawns a replacement from the forced
        exit, the user would get a second tray icon beside a dead one whose
        menu goes to a queue nobody reads. `mpv_shim`'s final action calls
        this first for that reason.
        """
        tray, self._tray = (self._tray, None)
        if tray is None:
            return
        try:
            tray.stop()
        except Exception:
            log.debug('could not stop the tray', exc_info=True)

    def quit_app(self):
        """Shut the application down, from outside the tray.

        The public name for ``_quit``. The settings screen's Restart needs
        exactly this shutdown -- the relaunch is armed first and happens at
        the end of it (restart.py) -- and reaching in for a private method
        from another module is how that stops working the day this class is
        rearranged.

        **Returns whether a shutdown was actually started.** The caller that
        armed a restart has to know: a quit that silently did nothing would
        leave the flag set, and the user's next ordinary quit would then
        relaunch the app out of nowhere. It is not a theoretical state --
        `stop_callback` is wired by `mpv_shim.main` a few lines after the UI
        is started, and anything driving this class outside that (a test, an
        embedding) may never wire it at all.
        """
        if self.stop_callback is None:
            log.error('Asked to quit with no shutdown callback wired; nothing to do.')
            return False
        self.stop_callback()
        return True

    def _can_run_windowless(self):
        """True if the app may keep running with no window on screen.

        Either the tray can bring it back, or the user set allow_background
        and accepted that `jellyfin-mpv-shim stop` is the way out. Anything
        else would leave a process nobody can see or reach.

        Cast-target mode is the exception: there is no library to come back
        to, and staying reachable *over the network* is the entire job. On
        those machines exiting on window close is the failure, not the
        safeguard — close_to_tray=False is how you ask for the app to quit.
        """
        if settings.headless:
            return True
        if self._tray is not None and self._tray.available:
            return True
        return bool(settings.allow_background)

    def _may_start_minimized(self):
        """True if we may come up with no window on screen.

        `--minimized` on the command line counts on its own: it is a decision
        made for this launch, in a terminal, by someone who can see the log
        line telling them how to get the window back. The config key is not
        self-authorizing in the same way — it may have been set on a machine
        that had a tray at the time — so that path still wants a tray or
        allow_background.

        Either way there is a way back: launching the app again asks the
        running copy to surface its window (see single_instance.py).
        """
        if self._can_run_windowless():
            return True
        from ..args import get_args
        if get_args().start_minimized:
            log.info('Started minimized with no system tray. Run jellyfin-mpv-shim again to show the window, or jellyfin-mpv-shim stop to exit.')
            return True
        return False

    def on_window_closed(self):
        """The user closed the mpv window.

        With one shared window, closing it means "minimize to tray" — the app
        stays alive as a cast target. But that is only safe if there *is* a
        tray: without one the app would keep running with no way to reach or
        quit it, so we exit instead — unless allow_background says the user
        asked for exactly that and knows how to stop it."""
        if not settings.close_to_tray:
            self._quit()
            return
        if not self._can_run_windowless():
            from ..tray import tray_unavailable_advice
            log.info('Window closed and no system tray is available; exiting rather than becoming unreachable. %s', tray_unavailable_advice())
            self._quit()
            return
        if self._browser is not None:
            self._browser.maybe_relock()
            self._browser.minimize()
        PlayerGateway().stop_for_close()

    def login_servers(self):
        from ..player import playerManager, is_using_ext_mpv
        from ..mpvtk.app import MpvtkApp
        from ..mpvtk.rawimage import MemoryStore, cache_dir
        from .app import MpvtkBrowser
        from .repository import LibrarySource
        from .strips import StripStore
        from .thumbnails import ThumbnailStore, disk_cache
        clientManager.load_credentials()
        app = MpvtkApp.attach(playerManager.get_mpv(), ext=is_using_ext_mpv)
        self._app = app
        strips = StripStore(mem_store=MemoryStore()) if app.in_process else StripStore(cache_dir=cache_dir('mpvtk-browser-'))
        app.scene_check = strips.dead_srcs
        thumb_dir, thumb_mb = disk_cache()
        thumbs = ThumbnailStore(thumb_dir, verify_ssl=not settings.ignore_ssl_cert, max_mem_mb=settings.library_image_cache_mb, max_disk_mb=thumb_mb)
        source = LibrarySource([], clientManager.device_id, settings.player_name, not settings.ignore_ssl_cert)
        browser = MpvtkBrowser(app, source, strips=strips, thumbs=thumbs, controller=PlayerGateway())
        self._browser = browser
        playerManager.mpvtk_active = True
        playerManager.on_playstate = browser.on_playstate
        playerManager.on_stopped_at = browser.on_stopped_at
        playerManager.on_load_start = browser.load.on_load_start
        playerManager.on_load_error = browser.load.on_load_error
        playerManager.notify_update = browser.notify_update
        playerManager.notify_syncplay = browser.notify_syncplay
        playerManager.on_syncplay_change = browser.resend_hud_config
        playerManager.syncplay_menu_reachable = browser.syncplay_menu_reachable
        playerManager.on_window_closed = self.on_window_closed
        playerManager.on_decorations_changed = browser.refresh_window_controls
        browser.refresh_window_controls()
        clientManager.on_server_connected = self._on_server_connected
        clientManager.on_servers_changed = browser.invalidate
        PlayerGateway().on_downloads_changed(browser.on_downloads_changed)
        playerManager.on_mpv_gone = self.on_mpv_gone
        playerManager.on_mpv_terminated = self.on_mpv_terminated
        playerManager.on_mpv_recreated = self.on_mpv_recreated
        playerManager.on_hud_menu = self._browser.open_hud_menu
        if settings.start_minimized and self._tray is not None and (not settings.allow_background):
            self._tray.ready.wait(5)
        if settings.start_minimized and self._may_start_minimized():
            browser.minimize()
        else:
            if settings.start_minimized:
                from ..tray import tray_unavailable_advice
                log.info('start_minimized ignored: no system tray to restore the window from. %s', tray_unavailable_advice())
            browser.enter_browse()
        if browser.headless:
            browser.show_cast()
            try:
                playerManager.set_fullscreen(True)
            except Exception:
                log.debug('headless fullscreen failed', exc_info=True)
        self._thread = threading.Thread(target=self._run, daemon=True, name='mpvtk-browser')
        self._thread.start()
        browser.start_background_work()
        playerManager.on_nav_back = browser.on_back
        playerManager.on_nav_command = browser.on_nav_command
        from ..event_handler import eventHandler
        eventHandler.display_content = self._display_content
        eventHandler.live_tv_changed = self._live_tv_changed
        eventHandler.user_data_changed = self._user_data_changed
        from ..users import userManager
        try:
            locked = userManager.startup_needs_unlock()
        except Exception:
            locked = False
        if locked:
            browser.show_locked()
        else:
            browser.show_connecting()
            threading.Thread(target=self._connect, daemon=True, name='mpvtk-connect').start()

    def on_mpv_gone(self):
        """The mpv handle is no longer ours (idle-quit or a lost connection).

        Stop the render loop and detach. Deliberately does NOT free the
        composited tile bitmaps: on libmpv those are in-process buffers mpv
        reads BY ADDRESS every frame it composites, and mpv is still being
        terminated on another thread at this point. Freeing here released
        memory out from under a live compositor — a segfault on quit. That
        happens in on_mpv_terminated instead."""
        self._detaching = True
        app, self._app = (self._app, None)
        if app is not None:
            app.quit()
        self._join_render_loop()
        if self._browser is not None:
            self._browser.app = None

    def on_mpv_terminated(self):
        """mpv is really dead — now every picture in RAM can go.

        This is the one moment the app can drop the lot. Everything else is
        a partial: the strip cache can only shed what the live scene is not
        using, because on libmpv those buffers are read BY ADDRESS by a
        compositor that is still running, and the decoded-image cache sheds
        a screen at a time as you navigate. With no mpv there is no scene,
        no compositor and nothing on screen, so both go to zero.

        Holding them past this point would both leak and defeat the memory
        saving that quitting mpv while minimized is FOR -- the app is now a
        cast target with no window, and a cast target does not need a
        library's artwork. Freeing any earlier crashes. See
        playerManager.on_mpv_terminated."""
        if self._browser is None:
            return
        try:
            self._browser.strips.clear()
        except Exception:
            log.debug('clearing the tile cache failed', exc_info=True)
        try:
            self._browser.forget_cast_bitmap()
        except Exception:
            log.debug('could not reset the cast bitmap', exc_info=True)
        try:
            if self._browser.thumbs is not None:
                self._browser.thumbs.trim_memory(0)
        except Exception:
            log.debug('clearing the decoded image cache failed', exc_info=True)

    def on_mpv_recreated(self):
        """A fresh mpv handle exists — attach a new renderer to it.

        mpvtk binds its event callbacks and loads renderer.lua at attach time,
        so the app object is per-handle; the browser keeps all of its state
        (routes, data, caches) and simply gets pointed at the new one."""
        from ..player import playerManager, is_using_ext_mpv
        from ..mpvtk.app import MpvtkApp
        if self._browser is None:
            return
        try:
            app = MpvtkApp.attach(playerManager.get_mpv(), ext=is_using_ext_mpv)
        except Exception:
            log.error('could not re-attach the mpvtk UI to the new mpv', exc_info=True)
            return
        if not self._join_render_loop():
            return
        self._app = app
        self._browser.set_app(app)
        self._detaching = False
        self._thread = threading.Thread(target=self._run, daemon=True, name='mpvtk-browser')
        self._thread.start()
        self._browser.reassert_window_state()
        self._browser.invalidate()
    RENDER_LOOP_JOIN = 2.0

    def _join_render_loop(self):
        """Stop tracking the render loop once it has actually exited.

        Returns True if it is gone. A survivor is kept in ``_thread`` so the
        next attach joins it again instead of racing it."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._thread = None
            return True
        if thread is threading.current_thread():
            log.debug('detaching from inside the render loop; not joining self')
            self._thread = None
            return True
        thread.join(timeout=self.RENDER_LOOP_JOIN)
        if thread.is_alive():
            log.warning('mpvtk render loop did not stop within %.0fs; not starting another alongside it', self.RENDER_LOOP_JOIN)
            return False
        self._thread = None
        return True

    def _run(self):
        from ..player import _mpv_errors
        app = cast('MpvtkApp', self._app)
        browser = cast('MpvtkBrowser', self._browser)
        try:
            app.run(browser.build)
        except _mpv_errors:
            log.info('mpvtk browser loop ended: mpv is gone')
        except Exception:
            log.error('mpvtk browser loop crashed', exc_info=True)
        finally:
            if not self._detaching and self.stop_callback is not None:
                self.stop_callback()

    def _on_server_connected(self, *_a):
        """A server came up after startup — rebuild so it appears.

        keep_place: this fires from the websocket redial loop, the
        cast-recovery path and the periodic health check, so it lands at
        arbitrary moments mid-session. Resetting to Home threw the user out
        of whatever they were reading every time a flaky server bounced."""
        if self._browser is None:
            return
        if getattr(self._browser, 'offline_chosen', False):
            self._browser.invalidate()
            return
        try:
            source = PlayerGateway().rebuild_source()
        except Exception:
            log.debug('rebuild after connect failed', exc_info=True)
            return
        if source is not None:
            self._browser.set_source(source, server_uuid=self._browser.server, keep_place=True)

    def _connect(self):
        from .repository import LibrarySource
        if not settings.work_offline:
            try:
                clientManager.connect_all()
            except Exception:
                log.error('mpvtk browser connect failed', exc_info=True)
        servers = _collect_servers()
        if self._browser is None:
            return
        if not servers:
            offline = PlayerGateway().offline_source()
            if offline is not None:
                self._browser.set_source(offline)
                return
            if _saved_servers_exist():
                log.warning('mpvtk browser: no servers connected')
                self._browser.connect_failed()
                return
            log.warning('mpvtk browser: no servers configured; showing login')
            self._browser.show_login()
            return
        source = LibrarySource(servers, clientManager.device_id, settings.player_name, not settings.ignore_ssl_cert)
        self._browser.set_source(source)

    def stop(self):
        from ..player import playerManager
        self.stop_tray()
        playerManager.mpvtk_active = False
        app, self._app = (self._app, None)
        if app is not None:
            app.quit()
            self._join_render_loop()
        if self._browser is not None:
            self._browser.shutdown(free_bitmaps=False)
user_interface = UserInterface()

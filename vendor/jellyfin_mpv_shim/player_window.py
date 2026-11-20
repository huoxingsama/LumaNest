"""The mpv window: who owns it, how big it is, and when it exists at all.

One window is shared by video playback and the in-window library browser, and
most of the difficulty here comes from that. The browser needs a window with
no file loaded; playback needs the same window back, at the size the user left
it, without mpv helpfully resizing it to each video.

**Three calls leave this module, and they are real coupling to the player's
lifecycle.** ``set_browse_window`` and ``force_window`` can call ``_reopen_mpv``
-- taking the window back may mean building a new mpv, because a version that
cannot give up its window on request has to be restarted instead (see
``runtime_force_window_works``). Both take ``_lock`` that way, and so does
``set_browse_window``'s call to ``idle_quit``.

**Fullscreen is here and it is ``_lock``-synchronized**, unlike everything
else in this module. It is a window property, so it belongs here, but the
lock discipline is the transport one -- worth knowing before this module is
reasoned about as a whole.

Before editing this file, read ``docs/mpv-backends.md``.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import sys
import threading
from typing import TYPE_CHECKING, Any, Optional
from .conf import settings
from .utils import get_resource, synchronous
log = logging.getLogger('player')
wlog = logging.getLogger('window')

def _caller(depth=2):
    """`module.function:line` of the code that asked for a transition.

    Which caller it was *is* the finding when a window re-opens on its own —
    the state alone cannot distinguish the browser re-entering browse mode
    from playback re-arming the window. Frames are cheap here: these
    transitions happen a handful of times per session, not per frame.
    """
    try:
        f = sys._getframe(depth)
        return '%s.%s:%d' % (f.f_globals.get('__name__', '?'), f.f_code.co_name, f.f_lineno)
    except Exception:
        return '?'
BROWSE_BG_HEX = '#141414'

def set_browse_bg(hexstr):
    """Set the browser's window background (an ``"#rrggbb"`` string)."""
    global BROWSE_BG_HEX
    if hexstr:
        BROWSE_BG_HEX = hexstr
    return BROWSE_BG_HEX
MPV_DEFAULT_BACKGROUND = 'tiles'
MPV_DEFAULT_BACKGROUND_HEX = '#000000'

class WindowMixin:
    """Window ownership, geometry and fullscreen for ``PlayerManager``."""
    if TYPE_CHECKING:
        _player: Any
        _video: Any
        _mpv_alive: bool
        _loading: bool
        _runtime_force_window: bool
        mpvtk_active: bool
        _geometry_armed: Optional[str]
        _showing_browse_bg: bool
        _browse_bg_deferred: bool
        _colorspace_hint: Any
        _colorspace_hint_suspended: bool
        fullscreen_disable: bool
        on_decorations_changed: Any

        def _reopen_mpv(self) -> bool:
            ...

        def _library_showing(self) -> bool:
            ...

        def _current_is_audio(self) -> bool:
            ...

        def stop(self, leave_group: bool=...) -> None:
            ...

        def idle_quit(self, reason: str=...) -> None:
            ...

        def _handle_mpv_disconnect(self) -> None:
            ...

        def upd_player_hide(self) -> None:
            ...
    _COLORSPACE_HINT = 'target_colorspace_hint'
    _colorspace_hint_lock = threading.Lock()

    def clear_media_title(self):
        """Put the window title back to the app's own, playback being over.

        The ``title`` option expands ``${media-title}`` live (see
        mpv_options), which is what lets the title follow playback without
        anything pushing updates. But ``force-media-title`` -- what
        ``_play_media`` sets -- is an *option*, not a per-file property: it
        outlives the file it was set for. Measured against mpv 0.40: after
        ``stop`` unloads the file, ``media-title`` still answers with it. So
        the window went on naming the last thing watched for the rest of the
        session, over a library screen (#647).

        **Only after the file is gone.** With one still loaded, clearing
        this does not empty ``media-title`` -- it falls back to the file's
        own metadata title, or failing that its path, which for us is a
        stream URL. Every caller therefore clears after its ``stop``
        command, which both backends complete before returning.
        """
        from .player import _mpv_errors
        if not self._mpv_alive:
            return
        try:
            self._player.force_media_title = ''
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            log.debug('could not clear the window title', exc_info=True)

    def suspend_colorspace_hint(self):
        """Let the window's colorspace follow the display while the UI owns it.

        mpv only revisits the swapchain's colorspace *while a video frame
        exists*, so on Windows an idle window keeps whatever hint the last
        file set: the library UI goes on encoding as PQ against a display
        that has since gone back to SDR (#605). Parking
        ``target-colorspace-hint`` at ``no`` takes mpv's other branch, whose
        NULL maps to sRGB — which is what this UI actually is.

        Only the browse window needs it, and only while nothing is playing:
        suspending over live video would cost that video its HDR passthrough.
        The OSD menu's window is torn down on close, taking the stale hint
        with it.

        Never raises, and does nothing at all on an mpv without the option
        (built without gpu-next, or too old) — the read is how we find out.
        Derivation: ``docs/mpv-backends.md`` §11.
        """
        with self._colorspace_hint_lock:
            if getattr(self, '_colorspace_hint_suspended', False):
                return
            if not self._mpv_alive:
                return
            try:
                saved = getattr(self._player, self._COLORSPACE_HINT)
                setattr(self._player, self._COLORSPACE_HINT, 'no')
            except Exception:
                wlog.debug('this mpv will not take %s', self._COLORSPACE_HINT, exc_info=True)
                return
            self._colorspace_hint = saved
            self._colorspace_hint_suspended = True
        wlog.info('colorspace hint parked at no (was %r) <- %s', saved, _caller())

    def resume_colorspace_hint(self):
        """Give the user's ``--target-colorspace-hint`` back before playback.

        Called from ``_play_media`` rather than from ``browse_yield``, which
        would read as the natural counterpart to ``set_browse_window``: yield
        is a browser gateway hook, so it misses every other way playback
        starts (a cast, the remote, the OSD menu). Playing HDR with the hint
        still parked would be a worse bug than the one being worked around,
        so the restore hangs off the one path all of them share.

        The flag is cleared only once mpv has taken the value, so a write that
        fails is retried by the next start rather than silently left parked.
        """
        with self._colorspace_hint_lock:
            if not getattr(self, '_colorspace_hint_suspended', False):
                return
            if not self._mpv_alive:
                return
            try:
                setattr(self._player, self._COLORSPACE_HINT, self._colorspace_hint)
            except Exception:
                wlog.debug('could not restore %s', self._COLORSPACE_HINT, exc_info=True)
                return
            self._colorspace_hint_suspended = False
        wlog.info('colorspace hint restored to %r <- %s', self._colorspace_hint, _caller())

    def _set_force_window(self, value):
        """Single authority for force_window.

        With the in-window UI there is one window and it must survive
        everything except being minimized: stopping playback, the OSC's close
        button, the end of a queue and closing the OSD menu all used to drop
        it, which showed as the window vanishing and being rebuilt. So while
        ``mpvtk_active`` (the browser owns the window — true even while
        yielded to playback, false only once minimized) force_window never
        goes False. The minimize path clears mpvtk_active first, which is
        what lets it through."""
        if not value and self.mpvtk_active:
            wlog.info('force_window=False SUPPRESSED (in-window UI owns it) <- %s', _caller())
            return
        wlog.info('force_window=%s <- %s', value, _caller())
        if not value:
            self._save_window_geometry()
            size = self._reopen_window_size()
        self._player.force_window = value
        if not value:
            self._rearm_window_geometry(size)

    def _reopen_window_size(self):
        """The size the next window should open at, read while this one lives.

        Separate from _save_window_geometry, which persists to settings and
        is gated on remember_window_size: reopening from the tray mid-session
        should land where the user left the window whether or not they asked
        for it to be remembered across launches. Falls back to the configured
        size when the live size is unreadable or the window is maximized.
        """
        width = height = 0
        try:
            if not self._player.window_maximized:
                width = int(self._player.osd_width or 0)
                height = int(self._player.osd_height or 0)
        except Exception:
            pass
        if width < 320 or height < 240:
            width = max(320, int(settings.window_width or 1280))
            height = max(240, int(settings.window_height or 720))
        return (width, height)

    def _sync_window_geometry(self):
        """Re-arm the geometry option at the window's *current* size.

        The obvious move here — clearing geometry so it cannot be re-applied —
        is what caused the window to jump on playback. Writing the option at
        runtime is not bookkeeping: every VO treats it as a resize command
        (w32_common and x11_common un-maximize the window first and force a
        reset to the computed size; wayland_common calls set_geometry with
        resize=true). With the option cleared, that computed size is whatever
        mpv currently has, i.e. the video's native size — or 960x540 while the
        browser is idle, since that is the dummy size mpv gives a forced
        window. So a maximized window un-maximized itself and shrank to the
        video, and playing from an empty browser landed on mpv's default size.

        Arming the live size instead keeps X11's re-apply on reconfig a no-op
        without ever commanding a resize. While maximized or fullscreen there
        is no floating size to record, and the write itself would un-maximize,
        so leave the last armed value alone.
        """
        try:
            if self._player.fullscreen or self._player.window_maximized:
                return
            width = int(self._player.osd_width or 0)
            height = int(self._player.osd_height or 0)
        except Exception:
            log.debug('Could not read the window size', exc_info=True)
            return
        if width < 320 or height < 240:
            return
        self._rearm_window_geometry((width, height))

    def _rearm_window_geometry(self, size):
        """Write the geometry option, if it isn't already what we want.

        Skipping the redundant write matters: see _sync_window_geometry for
        what a write does to a live window.
        """
        want = '%dx%d' % size
        if want == self._geometry_armed:
            return
        try:
            self._player.geometry = want
            self._geometry_armed = want
        except Exception:
            log.debug('Could not re-arm the window geometry', exc_info=True)

    @synchronous('_lock')
    def toggle_fullscreen(self):
        self.set_fullscreen(not self._player.fs, persist=True)

    def _apply_browse_fullscreen(self):
        """Put the browse window at the size ``browser_fullscreen`` asks for.

        Browsing is a desktop-UI activity: only go fullscreen if the user
        explicitly asked for a fullscreen browser. ``settings.fullscreen``
        still applies when playback starts.

        headless is a kiosk: the cast screen IS the display, so it stays
        fullscreen throughout. Without this, stopping playback dropped a
        cast-target box back to a window whenever ``browser_fullscreen`` was
        off — and ``browser_fullscreen`` is about the library, which headless
        does not even have.

        `_library_showing()`, not `not self._video`: music keeps `_video` set
        and keeps the library up, so the OFF direction never ran while a
        track played -- ticking the box went fullscreen and unticking it did
        nothing. The old spelling was coherent when `set_fullscreen`
        persisted a music-time toggle as the VIDEO preference; fixing that
        is what made it stale.

        Called on every browse transition and, since #729, whenever the
        setting itself is written — one decision in one place, because two
        copies of it would disagree the first time either moved.
        """
        if settings.browser_fullscreen or settings.headless:
            self._player.fs = True
        elif self._library_showing():
            self._player.fs = False

    @synchronous('_lock')
    def apply_browser_fullscreen(self):
        """Live-apply ``browser_fullscreen`` from the settings page (#729).

        The setting used to wait for the next browse transition, which for
        someone sitting in Settings is indistinguishable from a control that
        does nothing.

        **Not `set_fullscreen()`**, which is the other plausible way to write
        this and is wrong: that one also records `fullscreen_disable`, a
        *user intent* flag read at the next playback start — so turning the
        library's fullscreen off would quietly turn off auto-fullscreen for
        the next video too.
        """
        from .player import _mpv_errors
        if not self._mpv_alive:
            return
        if not self._library_showing():
            return
        try:
            self._apply_browse_fullscreen()
        except _mpv_errors:
            self._handle_mpv_disconnect()

    @synchronous('_lock')
    def set_fullscreen(self, enabled: bool, persist: bool=False):
        """``persist`` remembers the choice, for toggles the *user* made (a
        key, the OSC button, a remote command) as opposed to ones the app
        makes for its own reasons — the update notice dropping out of
        fullscreen, or the browser opening windowed.

        Which key it lands in depends on what's on screen: browsing writes
        browser_fullscreen, playback writes fullscreen. They're separate
        settings precisely because people want different answers for the
        two."""
        self._player.fs = enabled
        if not persist:
            return
        library = self._library_showing()
        if not (library and self.mpvtk_active):
            self.fullscreen_disable = not enabled
        key = 'browser_fullscreen' if library else 'fullscreen'
        if getattr(settings, key) == enabled:
            return
        setattr(settings, key, enabled)
        try:
            settings.save()
        except Exception:
            log.error('Could not persist %s', key, exc_info=True)

    def _save_window_geometry(self):
        """Remember the window size for the next launch.

        Size only: mpv exposes the *actual* window size as osd-width/height,
        but never its position — the `geometry` property reads back the
        option we set, not where the user dragged the window. So there is
        nothing to persist for position without platform-specific code.

        Never raises: this runs on the shutdown path, where mpv may already
        be gone, and failing to remember a window size must not stop the
        app exiting.
        """
        if not settings.remember_window_size:
            return
        try:
            maximized = bool(self._player.window_maximized)
            if not maximized:
                width = int(self._player.osd_width or 0)
                height = int(self._player.osd_height or 0)
                if width >= 320 and height >= 240:
                    settings.window_width = width
                    settings.window_height = height
            settings.window_maximized = maximized
            settings.save()
        except Exception:
            log.debug('Could not save the window geometry', exc_info=True)

    def _suspend_shaders_for_still(self):
        """Take the shader profile off for a picture. Best effort.

        Shared by the comic reader and by photo playback (which reaches it
        through _play_media, not here) so the two cannot drift about what
        counts as a still.
        """
        try:
            profiles = self.menu.profile_manager if self.menu else None
            if profiles is not None:
                profiles.suspend_for_still()
        except Exception:
            wlog.debug('could not suspend the shader profile', exc_info=True)

    def _resume_shaders_after_still(self):
        """Put back whatever :meth:`_suspend_shaders_for_still` took off."""
        try:
            profiles = self.menu.profile_manager if self.menu else None
            if profiles is not None:
                profiles.resume_after_still()
        except Exception:
            wlog.debug('could not restore the shader profile', exc_info=True)

    def show_picture(self, path):
        """Display a local image file in the browse window.

        The comic reader's page, which is *played* rather than drawn --
        docs/readers.md section 5 has the measurement behind that.

        **Here rather than in the gateway** because these are the same
        three properties ``set_browse_window`` owns — the browse background
        is itself "force_window with nothing loaded", and a picture is that
        state with something loaded. Splitting them would leave two places
        setting keep_open and image_display_duration with no idea about
        each other, which is how the backdrop and the picture would take
        turns overwriting one another.

        ``_showing_browse_bg`` goes False for exactly that reason: the flag
        means "the window is empty and painted", which stops being true
        here, and a later ``set_browse_window(True)`` has to know to stop
        the picture before claiming it again.
        """
        if not self._mpv_alive:
            return False
        if self._video is not None:
            if not self._current_is_audio():
                return False
            self.stop()
        from .player import _mpv_errors
        try:
            self._player.keepaspect = True
            self._suspend_shaders_for_still()
            self._player.image_display_duration = 'inf'
            self._player.keep_open = True
            self._sync_window_geometry()
            self._player.command('loadfile', path, 'replace')
            self._showing_browse_bg = False
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return False
        return True

    def reset_picture_view(self):
        """Put video-zoom and video-pan back, whatever is playing.

        **Split out of clear_picture, and guarded differently on purpose.**
        Zoom and pan are global mpv options: a comic leaves them at (say)
        2.67x and panned, and the next thing loaded inherits them.
        clear_picture bails when ``_video`` is set, which is exactly the
        case that matters — playback starting is how the comic stops being
        on screen — so a reset that shared its guard could never run when
        it was needed, and every film after a comic played zoomed until a
        comic was opened and closed again with nothing playing.

        **keepaspect is not one of those, and putting it here stretched
        every video.** It is not the picture's property: it is the window's,
        owned by set_browse_window (off, so the library resizes freely) and
        browse_yield (on, so video keeps its shape), and show_picture
        borrows it for as long as a page is up. This runs from
        ``_release_page_grabs`` on *every* browse -> video handoff, through
        ``run_action`` — which is documented to DEFER to the action thread
        whenever the player lock is busy, and it is busy for the whole of a
        playback start. browse_yield is a direct call. So the two ran in the
        order the lock decided, and whenever the reset landed second the
        film played stretched to the window with its aspect ignored — with
        no comic anywhere in the session, because this path does not need
        one. Hence the guard: while a video is playing or opening, the
        window's shape belongs to browse_yield and this must not touch it.

        ``_loading`` is half of that guard for the same reason it is in
        set_browse_window: ``_video`` is not assigned until the open
        succeeds, so during a start the obvious test says "nothing is
        playing" and lets exactly the harmful write through.

        Safe to call at any time: with no picture and no video these are
        already at their defaults, and setting them again costs nothing.
        """
        if not self._mpv_alive:
            return
        from .player import _mpv_errors
        try:
            self._player.video_zoom = 0.0
            self._player.video_pan_x = 0.0
            self._player.video_pan_y = 0.0
            if self._video is None and (not self._loading):
                self._player.keepaspect = False
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            wlog.debug('could not reset the picture view', exc_info=True)

    def clear_picture(self):
        """Take the picture down and go back to the painted browse window.

        Through ``set_browse_window`` rather than by stopping directly:
        that method is what decides what an empty window looks like, and
        reproducing half of it here is how the two drift.
        """
        self.reset_picture_view()
        self._resume_shaders_after_still()
        if not self._mpv_alive or self._video is not None:
            return
        if self.mpvtk_active:
            self.set_browse_window(True)

    def set_picture_view(self, zoom=None, pan_x=None, pan_y=None):
        """Move the displayed picture: mpv's three view properties.

        ``zoom`` is mpv's own unit — a log2 exponent on top of the scale
        that fits the picture in the window — so 0 is "fits" and 1 is twice
        that. ``mpvtk_browser.gateway.picture`` turns a reading mode into
        one.
        """
        if not self._mpv_alive:
            return
        from .player import _mpv_errors
        try:
            if zoom is not None:
                self._player.video_zoom = float(zoom)
            if pan_x is not None:
                self._player.video_pan_x = float(pan_x)
            if pan_y is not None:
                self._player.video_pan_y = float(pan_y)
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            wlog.debug('could not set the picture view', exc_info=True)

    def set_browse_window(self, enabled: bool):
        """Persistent window for the in-window mpvtk browser.

        With the in-window UI there is exactly one window, and its state is
        the product of two mpv properties:

            state                          playback_abort  force_window
            library browser                yes             yes
            media playing                  no              yes
            "minimized" (tray only)        yes             no
            cast to, library not open      no              no

        ``set_browse_window(True)`` is the first row; ``False`` drops to the
        third (or leaves the fourth alone, since it won't touch the window
        while something is playing). Minimizing is therefore not a
        window-manager action — it's releasing force_window with nothing to
        play, which is also why the app stays a usable cast target while
        minimized.

        Differs from force_window() (used by the OSD menu) in two ways the
        browser needs: no Jellyfin logo splash (the browser paints its own
        background) and free resizing (keepaspect-window=no, like the mpvtk
        demo) so the window doesn't snap to a media aspect ratio.

        Returns False when it declined: mpv had to be built, and a video
        started while this waited for it. None otherwise."""
        from .player import _mpv_errors
        wlog.info('browse=%s (alive=%d video=%d loading=%d mpvtk=%d bg=%d) <- %s', 'on' if enabled else 'off', bool(getattr(self, '_mpv_alive', False)), getattr(self, '_video', None) is not None, bool(getattr(self, '_loading', False)), bool(getattr(self, 'mpvtk_active', False)), bool(getattr(self, '_showing_browse_bg', False)), _caller())
        if not self._mpv_alive:
            if not enabled:
                return
            wlog.info('browse=on with no mpv: building a new one')
            if not self._reopen_mpv():
                return
            if not self._library_showing():
                wlog.info('browse=on declined: a video started meanwhile <- %s', _caller())
                return False
        release_failed = False
        try:
            if enabled:
                try:
                    self._player.keepaspect = False
                except Exception:
                    pass
                if self._video is None and (not self._loading):
                    self.suspend_colorspace_hint()
                self._player.background = 'color'
                self._player.background_color = BROWSE_BG_HEX
                self._player.force_window = True
                self._player.keep_open = True
                self._player.image_display_duration = 'inf'
                if self._video is None and (not self._showing_browse_bg):
                    if self._loading:
                        self._browse_bg_deferred = True
                    else:
                        self._player.command('stop')
                        self._showing_browse_bg = True
                        self._browse_bg_deferred = False
                self._apply_browse_fullscreen()
            else:
                try:
                    self._player.keepaspect = True
                except Exception:
                    pass
                self._showing_browse_bg = False
                self._player.image_display_duration = 1
                self._player.keep_open = False
                if not self._video and (not self._loading):
                    self._set_force_window(False)
                    self._player.command('stop')
                    release_failed = not self._runtime_force_window
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        if release_failed:
            wlog.info('releasing the window by QUITTING mpv (this mpv cannot drop force-window at runtime)')
            self.idle_quit(reason='minimized')

    def refresh_browse_bg(self):
        """Re-apply ``BROWSE_BG_HEX`` to the live mpv window.

        The colour behind the browser is an mpv *property*, not something the
        scene paints — nothing in the UI covers the whole window, so a theme
        change that only redrew the scene would leave the old background
        showing around it. Only meaningful while the browse window is up; a
        no-op otherwise, and never fatal, because a theme change must not be
        able to take the player down with it.

        `_showing_browse_bg` alone was the wrong gate. It means "we issued a
        stop and the window is painted with nothing loaded", which is never
        true during music -- a track is loaded -- yet the window is still
        painted by `background-color`, because these are global vo options
        that survive the file change (see set_browse_window). So a theme
        changed while music played left the old colour on screen until the
        music stopped.
        """
        try:
            if self._player is not None and (getattr(self, '_showing_browse_bg', False) or self._library_showing()):
                self._player.background_color = BROWSE_BG_HEX
        except Exception:
            wlog.debug('could not repaint the browse background', exc_info=True)
    _CSD_PROPS = ('border', 'title_bar')

    def _read_decorations(self):
        """True if the window has a server-drawn title bar, False if it does
        not, None if mpv could not be asked.

        Both properties have to be true: win32 can drop the title bar while
        keeping a resizable border, and a window with no title bar is one
        with nothing to drag or close by, which is the whole question.
        """
        if not self._mpv_alive or self._player is None:
            return None
        seen = False
        for prop in self._CSD_PROPS:
            try:
                value = getattr(self._player, prop)
            except Exception:
                continue
            if value is None:
                continue
            seen = True
            if not value:
                return False
        return True if seen else None

    def window_controls_wanted(self):
        """Whether the UI should draw its own title bar right now."""
        mode = (settings.window_controls or 'auto').lower()
        if mode == 'never':
            return False
        if not settings.window_controls_fullscreen:
            try:
                if self._player is not None and self._player.fullscreen:
                    return False
            except Exception:
                pass
        if mode == 'always':
            return True
        return self._read_decorations() is False

    def window_chrome_state(self):
        """``{"controls": bool, "maximized": bool}`` — everything the UI's
        own title bar has to know, in one call.

        One call because these are mpv property reads, and on the jsonipc
        backend each one is an IPC round trip: the UI must be able to take a
        snapshot on a change event rather than reading them per frame.
        """
        maximized = False
        try:
            maximized = bool(self._player.window_maximized)
        except Exception:
            pass
        return {'controls': bool(self.window_controls_wanted()), 'maximized': maximized}

    def _on_border_change(self, _name, _value):
        """Something the UI's own title bar is drawn from changed —
        `border`, `window-maximized` or `fullscreen`. On Wayland the first of
        those is mpv reporting what the compositor granted, not us setting
        anything. Runs on mpv's event thread, so it only notifies; the UI
        re-reads and rebuilds on its own thread."""
        handler = self.on_decorations_changed
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log.debug('on_decorations_changed handler failed', exc_info=True)

    def begin_window_drag(self):
        """Start an interactive window move (mpv >= 0.38).

        Normally issued from the renderer, which is holding the mouse button
        down at the time; this is the fallback path for callers that are not.
        """
        return self._window_command('begin-vo-dragging')

    def toggle_window_maximized(self):
        if not self._mpv_alive:
            return False
        try:
            self._player.window_maximized = not self._player.window_maximized
            return True
        except Exception:
            wlog.debug('could not toggle window-maximized', exc_info=True)
            return False

    def minimize_window(self):
        """Iconify via the window manager — NOT the app's own "minimize",
        which tears the window down and lives on in the tray. This is the
        title bar's button and it has to mean what that button means
        everywhere else."""
        if not self._mpv_alive:
            return False
        try:
            self._player.window_minimized = True
            return True
        except Exception:
            wlog.debug('could not minimize the window', exc_info=True)
            return False

    def _window_command(self, *args):
        from .player import _mpv_errors
        if not self._mpv_alive:
            return False
        try:
            self._player.command(*args)
            return True
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return False
        except Exception:
            wlog.debug('window command %r failed', args, exc_info=True)
            return False

    def raise_window(self):
        """Best-effort "bring the player window forward" — the tray's Show
        action and a second app launch both need it. Windows has a real API
        for this via pywin32; elsewhere the most we can portably do is
        un-minimize, since raising is the window manager's call."""
        from .player import win_utils
        if not self._mpv_alive:
            return
        if win_utils is not None:
            try:
                win_utils.raise_mpv()
                return
            except Exception:
                log.debug('win_utils.raise_mpv failed', exc_info=True)
        try:
            self._player.window_minimized = False
        except Exception:
            log.debug('could not un-minimize the player window', exc_info=True)

    def browse_yield(self):
        """Hand the window from the in-window browser back to playback.

        Undoes only the parts of set_browse_window() that would harm video —
        the stretched aspect, the browse background and the non-fullscreen
        browse window. It must not touch force_window/keep_open: playback is
        still starting up here and tearing the window down would kill it."""
        from .player import _mpv_errors
        if not self._mpv_alive:
            return
        self._showing_browse_bg = False
        try:
            self._player.keepaspect = True
            if settings.fullscreen and (not self.fullscreen_disable):
                self._player.fs = True
            try:
                self._player.background = MPV_DEFAULT_BACKGROUND
                self._player.background_color = MPV_DEFAULT_BACKGROUND_HEX
            except Exception:
                pass
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            log.debug('browse_yield failed', exc_info=True)

    def force_window(self, enabled: bool):
        from .player import _mpv_errors
        if not self._mpv_alive:
            if not enabled:
                return
            wlog.info('menu window with no mpv: building a new one <- %s', _caller())
            if not self._reopen_mpv():
                return
            try:
                if not self._player.playback_abort:
                    return
            except _mpv_errors:
                self._handle_mpv_disconnect()
                return
        try:
            if enabled:
                self._player.force_window = True
                self._player.keep_open = True
                self._player.image_display_duration = 'inf'
                self._player.play(get_resource('logo.png'))
                if settings.fullscreen:
                    self._player.fs = True
            else:
                self._player.image_display_duration = 1
                self._player.keep_open = False
                if not self._video:
                    self._set_force_window(False)
                    self._player.command('stop')
                elif self._player.playback_abort:
                    self._set_force_window(False)
                    self._player.play('')
                else:
                    self.upd_player_hide()
        except _mpv_errors:
            self._handle_mpv_disconnect()

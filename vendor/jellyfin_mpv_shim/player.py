from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import functools
import logging
import os
import re
import sys
import time
import json
import random
from threading import RLock, Lock, Thread, Event
from queue import Queue, Empty as queue_empty
from collections import deque
from typing import TYPE_CHECKING, Optional
from . import conffile
import threading
from .utils import same_origin, synchronous, Timer, get_resource, item_is_audio
from .media import segment_labels
from . import mpv_guard
from .mpv_events import observe as observe_property
from .mpv_events import wait_property
from .player_audio import AudioMixin
from .player_reporting import ReportingMixin
from . import player_window
from .player_window import WindowMixin, wlog
from .mpv_options import REPLACES_OSC, build_mpv_options, mpv_scripts, osc_script_opts, resolve_osc_style
from .session_reporter import SessionReporter
from . import conf
from .conf import settings
from .menu import OSDMenu
from .osc_bridge import OscBridge
from .constants import APP_NAME
from .syncplay import SyncPlayManager
from .update_check import UpdateChecker
from .i18n import _
if TYPE_CHECKING:
    from .media import Video as Video_type
log = logging.getLogger('player')
mpv_log = logging.getLogger('mpv')
discord_presence = False
if settings.discord_presence:
    try:
        from .rich_presence import register_join_event, send_presence, clear_presence
        discord_presence = True
    except Exception:
        log.error('Could not enable Discord Rich Presence.', exc_info=True)
python_mpv_available = True
is_using_ext_mpv = False
if not settings.mpv_ext:
    try:
        import mpv
        log.info('Using libmpv playback backend.')
    except OSError:
        log.warning('Could not load libmpv.', exc_info=True)
        python_mpv_available = False
if settings.mpv_ext or not python_mpv_available:
    import python_mpv_jsonipc as mpv
    log.info('Using external mpv playback backend.')
    is_using_ext_mpv = True

def _disarm_sdl_signal_handlers():
    """Keep SDL's signal handlers out of the *external* mpv's child process.

    mpv's gamepad support is SDL2, and ``SDL_Init`` takes any SIGINT/SIGTERM
    handler still at ``SIG_DFL`` unless this says otherwise. The external
    backend's child mpv has none of its own -- jsonipc spawns it with
    ``terminal=no``, which skips the handler install -- so SDL takes its
    SIGTERM and ``MPVProcess.stop()`` is swallowed, orphaning the window.

    **Called unconditionally.** ``input-gamepad`` is an ordinary mpv option,
    so the user's own ``mpv.conf`` can start the SDL thread with the option
    absent from anything we built; there is no way to ask mpv in time, so the
    only correct gate is no gate. A blank value counts as unset
    (``SDL_GetHintBoolean`` returns the default for ``""``); ``"0"`` is
    honoured and logged.

    Why this is the layer under ``mpv_shim._claim_sigterm`` rather than the
    fix, and the process-wide ``os.environ`` side effect that was accepted to
    get it: docs/mpv-backends.md section 4.
    """
    if os.environ.get('SDL_NO_SIGNAL_HANDLERS'):
        if os.environ['SDL_NO_SIGNAL_HANDLERS'] not in ('1', 'true', 'yes'):
            log.warning('SDL_NO_SIGNAL_HANDLERS=%r in the environment, so SDL keeps its own signal handlers. With game controller input and an external mpv, that means this application cannot stop its mpv process on the way out.', os.environ['SDL_NO_SIGNAL_HANDLERS'])
        return
    os.environ['SDL_NO_SIGNAL_HANDLERS'] = '1'

def _rejected_option(error):
    """The mpv option an exception blames, as an mpv_options key, or None.

    Both backends can name it and neither does so the same way -- libmpv puts
    it in an ``AttributeError``'s third arg, jsonipc >= 1.3.0 exposes
    ``bad_option``. (That version is the shim's floor: older ones flattened
    every start failure into "MPV process retry limit reached." after spending
    the whole retry budget.) See docs/mpv-backends.md section 2.

    Returns the *underscored* form, because that is how the option appears in
    the dict handed to ``mpv.MPV``.
    """
    bad = getattr(error, 'bad_option', None)
    if isinstance(bad, str) and bad:
        return bad.replace('-', '_')
    args = getattr(error, 'args', ())
    if len(args) >= 3 and isinstance(args[2], tuple) and (len(args[2]) >= 2):
        name = args[2][1]
        if isinstance(name, bytes):
            return name.decode('utf-8', 'replace').replace('-', '_')
        if isinstance(name, str):
            return name.replace('-', '_')
    return None

def _reap_orphaned_core(error):
    """Destroy the mpv core a failed ``mpv.MPV(...)`` left running.

    libmpv only, and not defensive: python-mpv initializes in a ``finally``,
    so an option this build lacks raises *after* the core is up. What is left
    is not a stale handle but a **running mpv** -- window, scripts, threads --
    that nothing stops before the retry's mpv is up, giving two VOs on one
    display and a crash in a thread that is not Python's. Both option retries
    below depend on this reaping. See docs/mpv-backends.md section 2.

    The instance is recovered from the traceback because the constructor never
    returned it, and identified by holding a live handle rather than by class
    (the external backend has no core here to reap). Read out of ``__dict__``
    directly: libmpv's ``__getattr__`` turns an unknown attribute into a
    property read on a core in no state to answer one.
    """
    tb = getattr(error, '__traceback__', None)
    while tb is not None:
        owner = tb.tb_frame.f_locals.get('self')
        handle = getattr(owner, '__dict__', {}).get('handle')
        stop = getattr(owner, 'terminate', None)
        if handle is not None and callable(stop):
            try:
                stop()
            except Exception:
                log.debug('could not stop the mpv left by a failed construction', exc_info=True)
            return
        tb = tb.tb_next

def _explain_missing_mpv():
    """Say why there is no mpv, before the traceback says something else.

    The external backend starts an mpv binary, so its failure to find one is
    reported by ``subprocess`` as a missing file -- accurate, and misleading
    whenever this backend was not the user's choice. Two different situations
    arrive here and they want opposite advice, and the one thing neither can
    be told from the traceback is which of them it is.
    """
    if not is_using_ext_mpv:
        return
    if settings.mpv_ext:
        log.error('Could not start mpv. The external mpv backend is selected, so an mpv binary has to be on PATH or named by the mpv_ext_path setting.')
    else:
        log.error('Could not start mpv. libmpv would not load (see the earlier warning for why), and the external mpv backend it fell back to found no mpv binary to run. This build ships libmpv, so the earlier warning is the failure worth reporting.')
_mpv_errors = (BrokenPipeError,)
if hasattr(mpv, 'ShutdownError'):
    _mpv_errors = (BrokenPipeError, mpv.ShutdownError)
else:
    _mpv_errors = (BrokenPipeError, TimeoutError)
IPC_TEARDOWN_TIMEOUT = 5

def bound_ipc_replies(seconds=IPC_TEARDOWN_TIMEOUT):
    """Stop waiting minutes for replies from an mpv that is going away.

    ``TIMEOUT`` is a module global read at each wait, so lowering it takes
    effect for calls already in flight as well as later ones. Idempotent,
    and never raised back to the caller: this runs on teardown paths where
    failing to tighten a timeout must not become the thing that breaks the
    shutdown.
    """
    if not is_using_ext_mpv:
        return
    try:
        if mpv.TIMEOUT > seconds:
            log.debug('Bounding mpv IPC reply wait to %ss for teardown.', seconds)
            mpv.TIMEOUT = seconds
    except Exception:
        log.debug('Could not bound the mpv IPC reply wait.', exc_info=True)

def _source_height(video):
    """The video height of what is about to play, or None.

    Read off the **MediaSource**, not the item: an item with several
    versions has one height per version, and the one being played is the
    one that decides whether hardware decoding is worth its risk. A photo,
    an audio track and anything the server did not probe all answer None,
    which every caller treats as "software" -- see mpv_options.hwdec_for.
    """
    try:
        streams = (getattr(video, 'media_source', None) or {}).get('MediaStreams') or []
        for stream in streams:
            if stream.get('Type') == 'Video' and stream.get('Height'):
                return int(stream['Height'])
    except Exception:
        log.debug('could not read the source height', exc_info=True)
    return None

def runtime_force_window_works(version):
    """Whether this mpv acts on a force-window change made while idle.

    Every mpv *stores* the property; only 0.41+ creates or destroys the video
    output for it. Older builds decide at startup, so the window must be asked
    for on the command line (see ``_init_mpv``) and releasing it later does
    nothing.

    An unreadable version is treated as old: assuming old costs a fallback
    that works everywhere, assuming new costs a window that will not go away.
    Why the browser is the first caller to need this: docs/mpv-backends.md
    section 3.
    """
    m = re.search('(\\d+)\\.(\\d+)', version or '')
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (0, 41)
BUILTIN_OSC = '@osc.lua'

def osc_preview_api_works(version):
    """Whether mpv's own OSC can drive our seek previews.

    mpv 0.41 added the **OSC Preview API** (``DOCS/man/osc.rst``): the OSC
    publishes ``user-data/osc/draw-preview`` -- ``{x, y, w, h, hover-sec,
    ass}`` -- and any thumbnailer script draws into it, clearing when the
    property goes nil.

    That is the hook `trickplay-osc.lua` was forked to work around; its own
    header says "sadly there is no way to do this without forking the entire
    OSC script", and as of 0.41 there is. So on a new enough mpv we load the
    STOCK OSC and answer its requests, and the fork is only for older ones --
    Debian and others still ship 0.40.

    Same threshold as `runtime_force_window_works` and deliberately its own
    predicate: they are two different questions that happen to share an
    answer today, and folding them would make a backport of either a silent
    lie about the other.

    An unreadable version is treated as old, which costs a fork that works
    everywhere (measured on 0.41+) rather than an OSC with no previews.
    """
    m = re.search('(\\d+)\\.(\\d+)', version or '')
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (0, 41)
SUBTITLE_POS = {'top': 0, 'bottom': 100, 'middle': 80}
mpv_log_levels = {'fatal': mpv_log.error, 'error': mpv_log.error, 'warn': mpv_log.warning, 'info': mpv_log.info}
_recent_mpv_errors = deque(maxlen=8)
_stream_failed = threading.Event()
_STREAM_FAILURE_LINES = (('curl', re.compile('Transferred a partial file|^transfer failed|retrying \\(#\\d+\\) from')), ('ffmpeg', re.compile('^https?: (Stream ends prematurely|Will reconnect)')), ('ffmpeg/demuxer', re.compile('^hls: Failed to open segment')))

def is_stream_failure(prefix: str, text: str) -> bool:
    """Whether one mpv log line says the playing stream failed partway."""
    for want, pattern in _STREAM_FAILURE_LINES:
        if prefix == want and pattern.search(text.strip()):
            return True
    return False

def stream_failed() -> bool:
    """Whether the current file's stream failed partway (see above)."""
    return _stream_failed.is_set()

def clear_mpv_errors():
    """Drop stale errors so a failed load can't report the previous file's."""
    _recent_mpv_errors.clear()
    _stream_failed.clear()

def last_mpv_error():
    """The most recent error line mpv logged, or None."""
    try:
        return _recent_mpv_errors[-1]
    except IndexError:
        return None
MPV_NOISE = 'noise'

def mpv_loglevel_for(level: str) -> str:
    """What to hand mpv. Every value is one of mpv's own except `noise`."""
    return 'debug' if level == MPV_NOISE else level
_MPV_NOISE_ARGS = re.compile('^Run command: script-message, flags=\\d+, args=\\[args="mpvtk-(?:scene|metrics)"')

def _is_mpv_noise(prefix: str, text: str) -> bool:
    if prefix.startswith('vo/gpu-next'):
        return True
    return prefix == 'cplayer' and _MPV_NOISE_ARGS.match(text) is not None

def mpv_log_handler(level: str, prefix: str, text: str):
    if level not in ('fatal', 'error', 'warn') and settings.mpv_log_level != MPV_NOISE and _is_mpv_noise(prefix, text):
        return
    message = '{0}: {1}'.format(prefix, text)
    if level in ('fatal', 'error'):
        _recent_mpv_errors.append(message.strip())
    if level in ('fatal', 'error', 'warn') and is_stream_failure(prefix, text):
        _stream_failed.set()
    if level in mpv_log_levels:
        mpv_log_levels[level](message)
    else:
        mpv_log.debug(message)
_END_FILE_REASONS = {0: 'eof', 2: 'stop', 3: 'quit', 4: 'error', 5: 'redirect'}

def _decode_reason(value):
    """Coerce an end-file reason to a lowercase string, or None if unreadable.

    The value's shape depends on the backend and the python-mpv release: a
    str, bytes, an int, or a Reason enum. Returning None (rather than
    guessing) matters — the caller only acts on a confident "error", so an
    unrecognized shape must degrade to the timeout path, never to aborting a
    load that is actually fine.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8', 'replace').lower()
        except Exception:
            return None
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _END_FILE_REASONS.get(value)
    name = getattr(value, 'name', None)
    if isinstance(name, str):
        return name.lower()
    return _decode_reason(getattr(value, 'value', None))

def end_file_info(event):
    """(reason, detail) for an mpv end-file event, normalized across backends.

    libmpv delivers an event object whose layout has shifted across
    python-mpv releases; external mpv delivers a plain dict. Never raises:
    this runs on mpv's event thread, where an exception would take out every
    other observer with it.
    """
    reason = detail = None
    try:
        data = event.as_dict() if hasattr(event, 'as_dict') else event
        if isinstance(data, dict):
            inner = data.get('event')
            if not isinstance(inner, dict):
                inner = data
            reason = _decode_reason(inner.get('reason', data.get('reason')))
            detail = inner.get('file_error') or inner.get('error') or data.get('file_error') or data.get('error')
        else:
            payload = getattr(event, 'data', None)
            reason = _decode_reason(getattr(payload, 'reason', None))
            detail = getattr(payload, 'error', None)
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', 'replace')
        elif not isinstance(detail, str):
            detail = None
    except Exception:
        log.debug('Could not decode end-file event.', exc_info=True)
        return (None, None)
    return (reason, detail)
win_utils = None
if sys.platform.startswith('win32') or sys.platform.startswith('cygwin'):
    try:
        from . import win_utils
    except ModuleNotFoundError:
        log.warning('win_utils is not available.')

def _item_is_audio(video):
    """Whether ``video`` is an audio item, from its metadata.

    Takes the video rather than reading ``self._video`` so it can be asked
    during a start, before the player has adopted the new item. Metadata, not
    mpv's track list, so it answers before a byte has been demuxed.
    """
    item = getattr(video, 'item', None) or {} if video is not None else {}
    return item_is_audio(item)

def _rank_stream(prev_source, prev_index, streams, stream_type):
    """Find the stream in `streams` best matching the previously-selected one
    (jellyfin-web heuristic): +2 language, +2 display title, +1 relative index,
    +1 codec; a match needs >= 3. Returns the matching stream Index or None."""
    prev_streams = [s for s in prev_source.get('MediaStreams') or [] if s.get('Type') == stream_type]
    prev_stream = next((s for s in prev_streams if s.get('Index') == prev_index), None)
    if prev_stream is None:
        return None
    prev_rel = prev_streams.index(prev_stream)
    best_score, best_index = (0, None)
    for rel, stream in enumerate((s for s in streams if s.get('Type') == stream_type)):
        score = 0
        if prev_stream.get('Codec') and prev_stream.get('Codec') == stream.get('Codec'):
            score += 1
        if prev_rel == rel:
            score += 1
        title = prev_stream.get('DisplayTitle')
        if title and title == stream.get('DisplayTitle'):
            score += 2
        lang = prev_stream.get('Language')
        if lang and lang != 'und' and (lang == stream.get('Language')):
            score += 2
        if score > best_score and score >= 3:
            best_score, best_index = (score, stream.get('Index'))
    return best_index

def chapter_target(chapters, pos, direction):
    """Where a previous/next-chapter jump from ``pos`` lands, or None when
    there is nowhere to go.

    ``chapters`` is a list of dicts with a ``time`` in seconds, in order.

    Back restarts the chapter you are in unless you are in its first couple of
    seconds; forward has no grace at all and takes the next boundary strictly
    ahead. Rationale for the asymmetry: docs/mpv-backends.md section 3.

    **The answer is clamped to 0** (#614). A matroska chapter can start at a
    slightly negative timestamp, and mpv reads a negative ABSOLUTE seek as the
    END of the file -- so "previous chapter" hit EOF and the EOF observer
    advanced the queue. ``seek()`` refuses one as well, because the chapter
    PICKER hands it the very same value.

    **Both directions can answer None, and the caller must not seek.** A
    button that quietly restarts the file is worse than one that declines.

    One definition, because two callers jump for different reasons: the HUD's
    chapter buttons and the mouse's back/forward buttons (mouse_chapter_nav).
    """
    if direction < 0:
        target = None
        for ch in chapters:
            if ch['time'] < pos - 2.0:
                target = ch['time']
        return None if target is None else max(0.0, target)
    for ch in chapters:
        if ch['time'] > pos:
            return ch['time']
    return None
_UNSET = object()
_LIFECYCLE_LOCK = threading.Lock()
_STOP_OWED_LOCK = threading.Lock()

def _shutdown_seen(handle):
    """Read through `__dict__`: on libmpv an unset attribute is a property
    read, which raises on a dead core (docs/mpv-backends.md §1)."""
    return getattr(handle, '__dict__', {}).get('_jms_shutdown_seen', False)

def _set_shutdown_seen(handle):
    """Hold `_LIFECYCLE_LOCK`."""
    try:
        handle._jms_shutdown_seen = True
    except Exception:
        pass

def _mark_own_close(handle):
    """Before we terminate mpv ourselves: mark the handle as already shut
    down, with the same once-per-handle flag _on_shutdown_event sets, so the
    "shutdown" event and _on_ipc_closed that our close causes do not run the
    outside-shutdown path (a second terminate, the teardown, stop_cmd)."""
    with _LIFECYCLE_LOCK:
        _set_shutdown_seen(handle)

class MpvShutDownDuringBuild(RuntimeError):
    """The mpv being built shut down before the build published it."""

class PlayerManager(AudioMixin, ReportingMixin, WindowMixin):
    """
    The underlying player is thread safe, however, locks are used in this
    class to prevent concurrent control events hitting the player, which
    violates assumptions.
    """
    _mpv_build = None
    _mpv_building = None
    _mpv_build_failures = 0
    _mpv_build_error = None
    _mpv_exiting = False
    on_reopen_failed = None

    def __init__(self):
        self._video = None
        self.timeline_trigger = None
        self.action_trigger = None
        self._track_memory = None
        self.external_subtitles = {}
        self._pending_subs = {}
        self.should_send_timeline = False
        self.start_time = None
        self.url = None
        self.evt_queue = Queue()
        self._audio_configured = False
        self._audio_snapshot = None
        self._device_snapshot = None
        self._audio_lock = RLock()
        self._lock = RLock()
        self._tl_lock = RLock()
        self._finished_lock = Lock()
        self.last_update = Timer()
        self._jf_settings = None
        self.pause_ignore = None
        self._key_claims = {}
        self._key_actions = {}
        self._swept = None
        self._swept_ptr = None
        self._lua_works = None
        self._gamepad_works = None
        self._osc_style_override = None
        self._osc_suppressed = False
        self._lua_probe = None
        self.do_not_handle_pause = False
        self._last_offline_record = float('-inf')
        self.last_seek = None
        self.warned_about_transcode = False
        self.fullscreen_disable = False
        self._geometry_armed = None
        self.update_check = UpdateChecker(self)
        self.menu = None
        self.syncplay = None
        self.osc_bridge = OscBridge(self)
        self.is_in_intro = False
        self.playback_time_before_seek = None
        self._last_ui_seek_time = 0.0
        self.trickplay = None
        self._hud_skip = None
        self._osc_script_loaded = False
        self._mpv_alive = False
        self._terminate_thread = None
        self._play_epoch = 0
        self._reporter = SessionReporter()
        self._load_failed = Event()
        self._load_completed = Event()
        self._load_error_detail = None
        self._load_generation = 0
        self._loading = False
        self._load_cancelled = False
        self._browse_bg_deferred = False
        self._start_in_progress = False
        self._trickplay_pending = False
        self._failed_playback = None
        self.on_load_start = None
        self.on_load_error = None
        self._reached_eof = False
        self._last_playback_position = 0
        self._stall_position = None
        self._stall_since = 0.0
        self._last_intro_msg_time = 0.0
        self.on_playstate = None
        self.on_stopped_at = None
        self.mpvtk_active = False
        self._stats_shown = False
        self._deinterlace_override = None
        self._aspect_override = None
        self._aspect_pristine = _UNSET
        self._hud_generation = 0
        self._render_pristine = {}
        self._render_written = set()
        self._no_deinterlace_auto = False
        self._showing_browse_bg = False
        self.on_window_closed = None
        self.on_mpv_gone = None
        self.on_mpv_terminated = None
        self.on_mpv_recreated = None
        self.on_reopen_failed = None
        self.on_nav_back = None
        self.on_nav_command = None
        self.on_decorations_changed = None
        self.on_hud_menu = None
        self.notify_update = None
        self.on_syncplay_change = None
        self.notify_syncplay = None
        self.syncplay_menu_reachable = None
        self.repeat_mode = 'none'
        self._session_ready = Event()
        self._session_ready.set()
        self._init_mpv()

    def _teardown_player(self):
        """Release process-scoped resources of the current mpv instance before
        a re-open (crash recovery / idle-quit). Safe to call before the first
        init — everything is None then.

        The trickplay worker is a *non-daemon* thread that would otherwise be
        leaked on every re-open (and block process exit); stopping it here is
        the fix. The mpv process itself is terminated by the disconnect path or
        the idle-quit path, not here."""
        term = self._terminate_thread
        if term is not None and term.is_alive():
            term.join(timeout=10)
            if term.is_alive():
                log.warning('Previous mpv terminate did not finish in time.')
        self._terminate_thread = None
        while True:
            try:
                self.evt_queue.get_nowait()
            except queue_empty:
                break
        if self.trickplay is not None:
            try:
                self.trickplay.stop(join=False)
            except Exception:
                log.debug('Stopping previous trickplay failed', exc_info=True)
            self.trickplay = None

    def _effective_osc_style(self):
        """The OSC style this mpv should be built with.

        `resolve_osc_style()` answers from settings, which know nothing about
        the machine -- so a fallback that discovered something about *this*
        mpv (no lua, no Pillow) has to be applied on top, and **has to keep
        being applied**: `_init_mpv` runs again on every re-creation, and
        re-resolving from settings there undoes the whole fallback (see
        docs/mpv-backends.md section 8).

        Its answer feeds `mpv_scripts` and `build_mpv_options` too, because
        there is no point handing lua scripts to an mpv known not to run them.
        """
        if self._osc_style_override is not None:
            return self._osc_style_override
        return resolve_osc_style()

    def _construct_mpv(self, mpv_options, **kwargs):
        """``mpv.MPV(...)``, retried without ``--osc`` if mpv has no such
        option -- which means it was built without lua.

        **A build without lua does not ignore ``--osc``, it refuses to
        start**, which made the whole lua fallback unreachable. The two
        backends report entirely different errors and none of it has to be
        parsed, because ``--osc`` is the only lua-gated option the shim sets:
        failing with it and succeeding without it *is* the answer.

        Recorded rather than rediscovered (`lua_works` then skips its probe),
        and **only in this direction** -- mpv having ``--osc`` says lua was
        compiled in, not that it runs. Measurements and the retry-budget cost
        of re-learning: docs/mpv-backends.md section 2.
        """
        from .mpv_options import GAMEPAD_OPTION, OSC_OPTION
        options = dict(mpv_options)
        if self._gamepad_works is False:
            options.pop(GAMEPAD_OPTION, None)
        if self._lua_works is False:
            options.pop(OSC_OPTION, None)
        _disarm_sdl_signal_handlers()
        dropped_gamepad = False
        dropped_osc = False
        guarded = mpv_guard.guarded(mpv.MPV)
        while True:
            try:
                player = guarded(**kwargs, **options)
            except FileNotFoundError:
                _explain_missing_mpv()
                raise
            except Exception as error:
                _reap_orphaned_core(error)
                if _rejected_option(error) == GAMEPAD_OPTION and GAMEPAD_OPTION in options:
                    del options[GAMEPAD_OPTION]
                    dropped_gamepad = True
                    continue
                if OSC_OPTION in options and (not dropped_osc):
                    del options[OSC_OPTION]
                    dropped_osc = True
                    continue
                raise
            if dropped_gamepad:
                log.warning('This mpv has no --%s option, so it was built without SDL2 gamepad support. Game controller input is unavailable; nothing else is affected.', GAMEPAD_OPTION.replace('_', '-'))
                self._gamepad_works = False
            if dropped_osc:
                log.warning('This mpv has no --%s option, so it was built without lua. The library browser, the playback HUD and the on-screen controls all need it; falling back to the command line and the OSD menu.', OSC_OPTION)
                self._lua_works = False
            mpv_guard.arm(player)
            return player

    def _init_mpv(self, caller=None):
        """Build mpv. Returns whether this was a re-open. `caller` is who
        asked, for the log; a re-open passes its own."""
        reopen = getattr(self, '_player', None) is not None
        wlog.info('CREATE mpv (%s) <- %s', 're-open' if reopen else 'first', caller or player_window._caller())
        self._teardown_player()
        osc_style = self._effective_osc_style()
        trickplay_started = False
        if settings.thumbnail_enable:
            try:
                from .trickplay import TrickPlay
                self.trickplay = TrickPlay(self)
                self.trickplay.start()
                trickplay_started = True
            except Exception:
                log.error('Could not enable trickplay.', exc_info=True)
        self._osc_style_resolved = osc_style
        self._osc_script_loaded = osc_style == 'mpv'
        self._osc_suppressed = False
        conffile.get_dir(APP_NAME, 'scripts')
        conffile.get_dir(APP_NAME, 'fonts')
        conffile.get(APP_NAME, 'input.conf', True)
        conffile.get(APP_NAME, 'mpv.conf', True)
        browser_wants_window = self.mpvtk_active if reopen else not settings.start_minimized
        mpv_options = build_mpv_options(osc_style, mpv_scripts(osc_style, trickplay_started), is_using_ext_mpv, browser_wants_window)
        self._geometry_armed = mpv_options['geometry']
        player = self._construct_mpv(mpv_options, input_default_bindings=True, input_vo_keyboard=True, input_media_keys=settings.media_keys, log_handler=mpv_log_handler, loglevel=mpv_loglevel_for(settings.mpv_log_level))
        with _LIFECYCLE_LOCK:
            self._player = player
            self._mpv_building = player
        try:
            self._configure_new_mpv(osc_style)
        except BaseException:
            self._abandon_build()
            raise
        return reopen

    def _abandon_build(self):
        """A build failed past `_construct_mpv`: the new mpv is running and
        nothing drives it. Terminate it the way `idle_quit` does, minus
        `on_mpv_gone` (no UI was attached to it yet), so the next re-open
        joins the terminate rather than building on top of it."""
        wlog.error('mpv build failed; terminating the half-built mpv')
        player = self._player
        try:
            self._teardown_player()
        except Exception:
            log.debug('teardown after a failed build failed', exc_info=True)
        with _LIFECYCLE_LOCK:
            _set_shutdown_seen(player)
            self._mpv_building = None
        self._mark_mpv_dead()
        self._terminate_thread = Thread(target=self._terminate_mpv, args=(player,), daemon=True)
        self._terminate_thread.start()

    def _configure_new_mpv(self, osc_style):
        """Everything `_init_mpv` does to a handle once it exists."""
        if is_using_ext_mpv:
            self._player.quit_callback = functools.partial(self._on_ipc_closed, self._player)
        self._render_written = set()
        self._aspect_pristine = _UNSET
        self._snapshot_render_pristine()
        try:
            self._runtime_force_window = runtime_force_window_works(self._player.mpv_version)
        except Exception:
            log.debug('could not read the mpv version', exc_info=True)
            self._runtime_force_window = False
        if not self._runtime_force_window:
            log.info('This mpv cannot give up its window on request (needs 0.41+); minimizing will quit mpv instead.')
        try:
            for _opt in osc_script_opts(osc_style):
                self._player.command('change-list', 'script-opts', 'append', _opt)
        except Exception:
            log.debug('could not pre-set the OSC script options', exc_info=True)
        if osc_style == 'mpv':
            self._load_classic_osc()
        if self.menu is None:
            self.menu = OSDMenu(self, self._player)
        else:
            self.menu.update_player(self._player)
        if self.syncplay is None:
            self.syncplay = SyncPlayManager(self)
            if discord_presence:
                try:
                    register_join_event(self.syncplay.discord_join_group)
                except Exception:
                    log.error('Could not register Discord join callback.', exc_info=True)
        try:
            if settings.screenshot_dir is not None:
                if hasattr(self._player, 'screenshot_directory'):
                    self._player.screenshot_directory = settings.screenshot_dir
                else:
                    log.warning("This mpv version doesn't support setting the screenshot directory.")
        except Exception:
            log.error('Could not set the screenshot directory.', exc_info=True)
        try:
            if hasattr(self._player, 'resume_playback'):
                self._player.resume_playback = False
        except Exception:
            log.error('Could not turn off resume_playback.', exc_info=True)
        self._audio_configured = False
        self._audio_snapshot = None
        self._device_snapshot = None
        self._stats_shown = False
        self._no_deinterlace_auto = False
        self._colorspace_hint_suspended = False
        try:
            self.apply_audio_settings()
        except Exception:
            log.error('Could not apply audio settings at startup.', exc_info=True)
        self._bind_mpv_handlers()
        self._showing_browse_bg = False
        if settings.enable_gui:
            for prop in ('keepaspect_window', 'auto_window_resize'):
                try:
                    setattr(self._player, prop, False)
                except Exception:
                    log.debug('%s unsupported by this mpv', prop, exc_info=True)
        self._publish_mpv(self._player)

    def _publish_mpv(self, player):
        """The one place `_mpv_alive` goes True. A handle that shut down
        while it was being built is a failed build, not a live mpv."""
        with _LIFECYCLE_LOCK:
            if _shutdown_seen(player):
                raise MpvShutDownDuringBuild('mpv shut down before its build finished')
            self._mpv_building = None
            self._mpv_alive = True

    def _mark_mpv_dead(self, player=None):
        """The one place `_mpv_alive` goes False. With `player`, only while
        it is still the current handle: a superseded handle's late terminate
        must not mark its successor dead. Returns whether it flipped."""
        with _LIFECYCLE_LOCK:
            if player is not None and player is not self._player:
                return False
            was_alive = self._mpv_alive
            self._mpv_alive = False
            return was_alive

    def _reopen_mpv(self):
        """Re-create a dead mpv. Every re-open comes through here, never
        straight to `_init_mpv` (#828).

        A caller that finds a build in progress waits for it on `_lock` and
        returns True on the mpv it built; if that build failed, it gets the
        failure instead of building another. Returns False only to a call
        from inside this thread's own build. docs/mpv-backends.md section 8.
        """
        caller = player_window._caller()
        failures = self._mpv_build_failures
        term = self._terminate_thread
        if term is not None and term is not threading.current_thread() and (not self._mpv_alive):
            term.join(timeout=10)
        with self._lock:
            if self._mpv_alive:
                return True
            if self._mpv_build is not None:
                wlog.error('mpv re-open asked for from inside its own build <- %s', caller)
                return False
            if self._mpv_build_failures != failures:
                raise RuntimeError('mpv re-open failed on another thread') from self._mpv_build_error
            if self._mpv_exiting:
                raise RuntimeError('mpv could not be re-created; the app is shutting down') from self._mpv_build_error
            self._mpv_build = threading.current_thread()
            try:
                reopen = self._init_mpv(caller=caller)
            except BaseException as error:
                self._mpv_build_error = error
                self._mpv_build_failures += 1
                self._reopen_failed(error)
                raise
            finally:
                self._mpv_build = None
        if reopen and self.on_mpv_recreated is not None:
            try:
                self.on_mpv_recreated()
            except Exception:
                log.error('on_mpv_recreated handler failed', exc_info=True)
        return True

    def _reopen_failed(self, error):
        """mpv could not be re-created: shut the app down, orderly, with no
        retry (jsonipc's launch already retries). Every UI goes through mpv,
        so without one the app is a cast target and a tray icon that cannot
        show anything. docs/mpv-backends.md section 8."""
        with _LIFECYCLE_LOCK:
            self._mpv_exiting = True
        log.error('mpv could not be re-created; shutting down', exc_info=error)
        hook = self.on_reopen_failed
        if hook is None:
            return
        try:
            hook(error)
        except Exception:
            log.error('on_reopen_failed handler failed', exc_info=True)

    def _bind_key(self, key, func):
        """Bind one configurable key, ignoring the ones the user cleared.

        Was the `keypress` decorator factory; the emptiness check is the
        whole of it, and an unset keybind is a supported configuration.

        Empty string as well as None: the settings are Optional now, so a
        documented `null` really does arrive as None, but a config written
        under the old typing holds the string "None" and somebody clearing
        the field in a text editor leaves "". All three mean the same thing
        and none of them is a key.
        """
        if key and key != 'None':
            self._player.on_key_press(key)(func)

    def _observe(self, prop, handler):
        """Register a property observer on either backend.

        Thin by design: the dispatch (and the bound-method hazard it exists
        to dodge) lives in ``mpv_events.observe``, so it can be checked
        against a real mpv without importing this module -- which would
        build a whole player as a side effect. Returns that function's
        token; nothing here unregisters, because mpv is torn down whole.
        """
        return observe_property(self._player, prop, handler)

    def _load_classic_osc(self):
        """Load the OSC for ``osc_style`` "mpv": the stock one where it can
        drive our previews, our fork where it cannot.

        `trickplay-osc.lua` is a fork of mpv 0.38's `osc.lua` whose only
        purpose is seek-bar thumbnails -- its whole patch set is six
        thumbfast blocks and one compatibility fix. mpv 0.41 made that
        unnecessary by publishing the OSC Preview API, so a modern mpv gets
        its own OSC (upstream fixes, upstream look) and `thumbfast.lua`
        answers the property instead.

        The fork stays for older builds, and it is also the fallback if the
        builtin name is ever refused: `@osc.lua` is mpv's internal spelling
        rather than a documented one, so the failure it could produce -- an
        OSC style with no OSC at all -- is caught rather than shipped.
        """
        if osc_preview_api_works(getattr(self._player, 'mpv_version', '')):
            try:
                self._player.command('load-script', BUILTIN_OSC)
                log.info("Using mpv's own OSC; previews go through its Preview API.")
                return
            except Exception:
                log.warning('mpv refused %s; falling back to the bundled OSC.', BUILTIN_OSC, exc_info=True)
        else:
            log.info('This mpv has no OSC Preview API (needs 0.41+); using the bundled OSC so seek previews still work.')
        try:
            self._player.command('load-script', get_resource('trickplay-osc.lua'))
        except Exception:
            log.error("Could not load an OSC for the 'mpv' style.", exc_info=True)

    def _bind_mpv_handlers(self):
        """Attach every key binding and event handler to the current mpv."""
        p = self._player
        self._bind_key(settings.kb_stop, self._on_kb_stop)
        p.on_key_press('STOP')(self._on_stop_key)
        p.on_key_press('CLOSE_WIN')(self._on_close_win)
        self._bind_key(settings.kb_prev, self._on_prev_key)
        self._bind_key(settings.kb_next, self._on_next_key)
        p.on_key_press('PREV')(self._on_media_prev)
        p.on_key_press('XF86_PREV')(self._on_media_prev)
        p.on_key_press('NEXT')(self._on_media_next)
        p.on_key_press('XF86_NEXT')(self._on_media_next)
        self._bind_key(settings.kb_watched, self._on_watched_key)
        self._bind_key(settings.kb_unwatched, self._on_unwatched_key)
        self._bind_key(settings.kb_menu, self._on_menu_key)
        self._bind_key(settings.kb_menu_esc, self._on_menu_esc)
        self._bind_key(settings.kb_nav_back, self._on_nav_back_key)
        self._bind_key(settings.kb_menu_ok, self._on_menu_ok)
        self._bind_key(settings.kb_pause, self._on_pause_key)
        if settings.kb_fullscreen != type(settings).kb_fullscreen:
            self._bind_key(settings.kb_fullscreen, self._on_fullscreen_key)
        self._bind_key(settings.kb_kill_shader, self._on_kill_shader_key)
        from . import keysweep
        self._key_claims['fullscreen'] = {keysweep.FULLSCREEN}
        if self._seek_is_ours():
            self._key_claims['seek'] = {keysweep.SEEK}
        with self._lock:
            self._refresh_key_section()
        p.on_key_press('i')(self._on_stats_oneshot)
        p.on_key_press('I')(self._on_stats_toggle)
        if settings.mouse_chapter_nav:
            p.on_key_press('MBTN_BACK')(self._on_chapter_prev_key)
            p.on_key_press('MBTN_FORWARD')(self._on_chapter_next_key)
        self._observe('border', self._on_border_change)
        self._observe('window-maximized', self._on_border_change)
        self._observe('fullscreen', self._on_border_change)
        self._observe('eof-reached', self._on_eof_reached)
        self._observe('playback-abort', self._on_playback_abort)
        self._observe('seeking', self._on_seeking)
        self._observe('pause', self._on_pause_change)
        self._observe('paused-for-cache', self._on_cache_pause)
        self._observe('mute', self._on_volume_change)
        self._observe('volume', self._on_volume_change)
        p.event_callback('file-loaded')(self._on_file_loaded)
        self._observe('current-tracks/audio/codec', self._on_audio_codec_change)
        p.event_callback('end-file')(self._on_end_file)
        p.event_callback('shutdown')(functools.partial(self._on_shutdown_event, handle=p))
        p.event_callback('client-message')(self._on_client_message)

    def _on_kb_stop(self):
        log.info('handle_stop triggered')
        if self.mpvtk_active:
            self.put_task(self.stop_to_browser)
        else:
            self.put_task(self.stop_and_close)

    def _on_stop_key(self):
        log.info('handle_stop triggered')
        self.put_task(self.stop_and_close)

    def _on_close_win(self):
        log.info('handle_close_win triggered')
        handler = self.on_window_closed
        if self.mpvtk_active and handler is not None:
            self.put_task(handler)
        else:
            self.put_task(self.stop_and_close)

    def _on_prev_key(self):
        self.put_task(self.play_prev)

    def _on_chapter_prev_key(self):
        self.put_task(self.chapter_seek, -1)

    def _on_chapter_next_key(self):
        self.put_task(self.chapter_seek, 1)

    def _on_next_key(self):
        self.put_task(self.play_next)

    def _on_media_prev(self):
        if settings.media_key_seek:
            seektime, _x = self.get_seek_times()
            self.seek(seektime)
        else:
            self.put_task(self.play_prev)

    def _on_media_next(self):
        if settings.media_key_seek:
            if self.is_in_intro and settings.skip_intro_on_seek:
                self.skip_intro()
            else:
                _x, seektime = self.get_seek_times()
                self.seek(seektime)
        else:
            self.put_task(self.play_next)

    def _on_watched_key(self):
        self.put_task(self.watched_skip)

    def _on_unwatched_key(self):
        self.put_task(self.unwatched_quit)

    def _on_menu_key(self):
        if self._library_has_input():
            try:
                self._player.command('keypress', 'MENU')
            except Exception:
                log.debug('context menu keypress failed', exc_info=True)
            return
        self.toggle_settings_menu()

    def toggle_settings_menu(self):
        """The player's own settings menu, toggled.

        Two callers with one meaning: the kb_menu key, and a remote whose
        cog or hamburger found no page to open (menu_action). Which surface
        that is depends on the OSC — the HUD's gear menu under mpvtk, the
        OSD menu otherwise — and deciding it twice is how they would drift.
        """
        if self.do_not_handle_pause:
            self._player.show_text(_('Please wait, loading...'), 1000, 1)
            return
        if getattr(self, '_osc_style_resolved', None) == 'mpvtk':
            if self._video_on_screen() and self.on_hud_menu is not None:
                try:
                    self.on_hud_menu()
                except Exception:
                    log.debug('hud menu open failed', exc_info=True)
            return
        if not self.menu.is_menu_shown:
            self.menu.show_menu()
        else:
            self.menu.hide_menu()

    def _on_menu_esc(self):
        if self.menu.is_menu_shown:
            self.menu.menu_action('back')
        elif self._nav_back():
            pass
        else:
            self._player.command('set', 'fullscreen', 'no')
            if not (self.mpvtk_active and self._library_showing()):
                self.fullscreen_disable = True

    def _on_nav_back_key(self):
        """"Back", and *only* back -- no fullscreen fallback.

        `_on_menu_esc` above is ESC's handler and ends by leaving fullscreen,
        which is the half this exists to be free of: at the library ROOT
        `_nav_back` declines (it wants `mpvtk_active` and `_library_showing`),
        so the fallback fires and ESC drops fullscreen as well. Users who map
        a remote button to a literal ESC get that too (#762).

        **ESC is deliberately unchanged.** Plenty of people expect ESC to
        leave fullscreen, so the answer is a second, unbound key rather than a
        new meaning for the one everybody has. `_bind_key(None, ...)` is
        already a no-op, which is what makes "new and unbound" cost nothing.

        Not reached by the mouse thumb buttons or a multimedia Back key, which
        go through the renderer's `mpvtk_thumb` section and still carry the
        fullscreen fallback at the library root. Re-routing those would change
        what the thumb buttons do for people using them today, and the ruling
        was about leaving ESC alone.
        """
        if self.menu.is_menu_shown:
            self.menu.menu_action('back')
        else:
            self._nav_back()

    def _on_menu_ok(self):
        """ENTER: confirm the OSD menu, and nothing else.

        It must not *open* it: ``menu_action("ok")`` on a hidden menu is
        ``show_menu()``, and under mpvtk the OSD menu draws as mpv OSD text,
        so it lands under the overlay bitmaps and takes the arrow keys with
        it -- the same thing ``toggle_settings_menu`` refuses above.

        Swallowed rather than left to mpv, which binds ENTER to
        ``playlist-next``: our mpv playlist holds one file, so what that does
        depends on ``keep-open`` and is not a behaviour to inherit by
        accident. Part of #16, except that here we mean nothing by the key at
        all.
        """
        if self.menu.is_menu_shown:
            self.menu.menu_action('ok')

    def _on_pause_key(self):
        if self.menu.is_menu_shown:
            self.menu.menu_action('ok')
        else:
            self.toggle_pause()

    def _on_fullscreen_key(self):
        self.toggle_fullscreen()

    def _on_kill_shader_key(self):
        from .shared_playback import kill_shaders
        if kill_shaders(self):
            return
        if self.menu is not None and self.menu.profile_manager is not None:
            self.menu.profile_manager.suppressed = True
        if settings.shader_pack_remember:
            settings.shader_pack_profile = None
            settings.save()
        if self.menu.profile_manager is not None:
            self.menu.profile_manager.unload_profile()

    def _stats_key(self, oneshot):
        if self.mpvtk_active and self._video is not None and (not self._current_is_audio()):
            self.put_task(self.toggle_stats)
        elif not self.mpvtk_active:
            self._player.command('script-binding', 'stats/display-stats' if oneshot else 'stats/display-stats-toggle')

    def _on_stats_oneshot(self):
        self._stats_key(True)

    def _on_stats_toggle(self):
        self._stats_key(False)

    def _on_eof_reached(self, _name, reached_end: bool):
        if self._video and reached_end:
            self._reached_eof = True
            self._queue_finished()

    def _on_playback_abort(self, _name, value: bool):
        if self._video and value and (not self._video.parent.has_next):
            self._queue_finished()

    def _on_seeking(self, _name, value: bool):
        if self.do_not_handle_pause:
            return
        if value:
            self.playback_time_before_seek = self._player.playback_time
        else:
            landed = self._player.playback_time if getattr(self, '_mpv_alive', False) else None
            if landed is not None:
                self._last_playback_position = landed
            if settings.skip_intro_on_seek and time.time() - self._last_ui_seek_time > 2.0 and self.is_in_intro and (self.playback_time_before_seek is not None) and (self._player.playback_time is not None) and (self._player.playback_time > self.playback_time_before_seek):
                self.skip_intro()
        if self.syncplay.is_enabled():
            play_time = self._player.playback_time
            if play_time is not None and self.last_seek is not None and (abs(self.last_seek - play_time) > 10):
                self.syncplay.seek_request(play_time)
            else:
                log.info('SyncPlay Buffering: {0}'.format(value))
                if value:
                    self.syncplay.on_buffer()
                else:
                    self.syncplay.on_buffer_done()

    def _on_pause_change(self, _name, value: bool):
        if self.do_not_handle_pause:
            return
        if not self._player.playback_abort:
            self.timeline_handle()
        if value != self.pause_ignore and self._video:
            if self.syncplay.is_enabled():
                if value:
                    self.syncplay.pause_request()
                else:
                    self.syncplay.play_request()
                    self.set_paused(True, True)

    def _on_cache_pause(self, _name, value: bool):
        """mpv stalled waiting for the demuxer cache, or recovered.

        SyncPlay has a Buffer request for exactly this: the group pauses for a
        member that has stalled, and resumes when it reports Ready. We only
        ever raised it from the `seeking` property, so a cache underrun --
        the common case, and the one the feature exists for -- was never
        reported. This client simply fell behind and was then yanked back by
        SkipToSync.

        on_buffer debounces by min_buffer_thresh_ms, so a brief stall costs
        nothing. Loads are excluded by do_not_handle_pause, which is set for
        the whole of a playback start; without that every file would announce
        itself as buffering while it filled its cache.
        """
        if self.do_not_handle_pause or not self.syncplay.is_enabled():
            return
        if value:
            self.syncplay.on_buffer()
        else:
            self.syncplay.on_buffer_done()

    def _on_volume_change(self, _name, _value):
        """Volume or mute moved -- tell the UI now rather than on the tick.

        The HUD's mute icon and volume bar read the playstate snapshot, and
        nothing pushed one when either changed, so the button showed the old
        icon for up to a second after the audio stopped (#618).

        push_playstate() directly, not timeline_handle(): the timeline thread
        also POSTs progress to the server, and a volume nudge is not worth a
        request.

        Observing mpv rather than patching state at our own button is what
        makes mpv's OWN bindings -- `m`, the wheel, a script -- move it too.
        """
        self.push_playstate()

    def _on_file_loaded(self, _event):
        if self._loading:
            self._load_completed.set()
        self.put_task(self.apply_audio_filters)

    def _on_audio_codec_change(self, _name, _value):
        self.put_task(self.apply_audio_filters)

    def _on_end_file(self, event):
        generation = self._load_generation
        if not self._loading:
            return
        reason, detail = end_file_info(event)
        if reason != 'error':
            return
        if generation != self._load_generation or not self._loading:
            return
        log.error('mpv reported a load error: %s', detail or 'no detail')
        self._load_error_detail = detail
        self._load_failed.set()

    def _on_shutdown_event(self, event, handle=None):
        if handle is None:
            handle = self._player
        with _LIFECYCLE_LOCK:
            if _shutdown_seen(handle):
                return
            _set_shutdown_seen(handle)
            if handle is not self._player:
                return
            building = handle is self._mpv_building
        if building:
            log.warning('mpv shut down while it was being built')
            return
        log.info('mpv shutdown event received')
        self.should_send_timeline = False
        self.put_task(self._handle_mpv_shutdown)
        self._terminate_thread = Thread(target=self._terminate_mpv, args=(handle,), daemon=True)
        self._terminate_thread.start()
    _MPV_EQUIVALENT_SEEK = {'use_web_seek': False}

    def _seek_is_ours(self):
        """Whether the shim's seek does something mpv's cannot.

        Four things can: a changed distance, an exact-seek flag,
        jellyfin-web's variable seek, and skip-intro-on-seek. None of them
        and seeking is mpv's outright -- the defaults here are `seek 60` /
        `seek -60` / `seek 5` / `seek -5`, character for character what mpv
        binds, so the old arrow bindings existed to reimplement mpv's
        arrows exactly.

        Any of them and the keys are *claimed* rather than bound: the claim
        follows whatever currently means seek, so somebody who moved seeking
        onto other keys gets their features there too, which four fixed
        bindings never gave them.
        """
        return any((getattr(settings, key, default) != default for key, default in self._MPV_EQUIVALENT_SEEK.items()))

    def set_osc_style(self, style: str):
        """Override the resolved OSC style for this mpv instance.

        One caller: the no-lua fallback. Every OSC the shim can offer is a
        lua script, so when lua is unavailable there is no style to resolve
        *to* -- and leaving the resolved value at "mpvtk" is what kept the
        OSD menu unreachable, since ``toggle_settings_menu`` refuses it
        under that style alone.
        """
        self._osc_style_override = style
        self._osc_style_resolved = style
        self._osc_script_loaded = False

    def lua_works(self, timeout: float=2.0):
        """Whether this mpv can run a lua script at all. Cached.

        **Everything the shim draws needs this** -- the browser and the HUD
        are renderer.lua, the stock OSC is lua, mouse.lua is lua.

        A probe, not a capability string: ``mpv-configuration`` does not
        mention lua on every build, and ``load-script`` on a script that
        cannot run **raises nothing on either backend**. Only a script
        reporting back is proof, which also catches lua that loads and then
        errors. Costs ~10 ms when lua works, and the full timeout once when it
        does not. See docs/mpv-backends.md section 2.
        """
        if self._lua_works is not None:
            return self._lua_works
        answered = threading.Event()
        self._lua_probe = answered
        try:
            self._player.command('load-script', get_resource('lua_probe.lua'))
            answered.wait(timeout)
        except Exception:
            log.debug('lua probe could not be loaded', exc_info=True)
        finally:
            self._lua_probe = None
        self._lua_works = answered.is_set()
        if not self._lua_works:
            log.warning('This MPV cannot run lua scripts. The library browser, the playback HUD and the on-screen controls all need it; falling back to the command line and the OSD menu.')
        return self._lua_works
    SECTION_FLAGS = 'allow-hide-cursor+allow-vo-dragging'
    MENU_SECTION = 'jms_menu'
    MENU_MESSAGE = 'jms-menu'
    _MENU_KEYS = (('kb_menu_left', 'left'), ('kb_menu_right', 'right'), ('kb_menu_up', 'up'), ('kb_menu_down', 'down'))

    def claim_menu_keys(self, wanted):
        """Give the arrows to the OSD menu for as long as it is on screen.

        Installed on show and torn down on hide, which is what lets them
        belong to mpv the rest of the time with no conditional behaviour
        for the user to notice -- the same lifecycle the renderer runs for
        its own mouse sections.

        Unconditional since the arrows stopped being bound in Python: this
        is the only thing that gives the menu its navigation, so a gate here
        is a menu that cannot be driven.
        """
        try:
            if not wanted:
                self._player.command('disable-section', self.MENU_SECTION)
                return
            lines = '\n'.join(('%s script-message %s %s' % (getattr(settings, key), self.MENU_MESSAGE, act) for key, act in self._MENU_KEYS if getattr(settings, key, None)))
            self._player.command('define-section', self.MENU_SECTION, lines, 'force')
            self._player.command('enable-section', self.MENU_SECTION, self.SECTION_FLAGS)
        except Exception:
            log.debug('could not update the menu key section', exc_info=True)
    KEY_SECTION = 'jms_keys'
    KEY_MESSAGE = 'jms-key'

    def claim_keys(self, owner, semantics=None):
        """Take, or give back, every key that currently means one of
        ``semantics``. ``None`` releases ``owner``'s claim.

        Owners are independent and the section is the union, so SyncPlay
        joining a group does not disturb the standing fullscreen claim.
        """
        with self._lock:
            if semantics:
                self._key_claims[owner] = set(semantics)
            else:
                self._key_claims.pop(owner, None)
            self._refresh_key_section()

    def _swept_keys(self):
        """The sweep, done ONCE and cached.

        Cached because it must not see our own section: the lines we install
        are non-weak, so a re-sweep would find `script-message jms-key ...`
        winning every claimed key, classify it as nothing, and quietly drop
        the claim on the next refresh. Bindings do not change at runtime
        anyway -- mpv reads input.conf at startup.
        """
        if self._swept is None:
            from . import keysweep
            try:
                bindings = self._player.input_bindings
            except Exception:
                log.debug('could not read input-bindings', exc_info=True)
                bindings = []
            self._swept = keysweep.sweep(bindings, {keysweep.PAUSE, keysweep.SEEK, keysweep.FULLSCREEN})
            log.debug('key sweep: %s', self._swept)
        return self._swept

    def _swept_pointers(self):
        """Pointer keys meaning seek, cached alongside the sweep and for the
        same reason -- see :meth:`_swept_keys`."""
        if self._swept_ptr is None:
            from . import keysweep
            try:
                bindings = self._player.input_bindings
            except Exception:
                bindings = []
            self._swept_ptr = keysweep.pointer_keys(bindings, {keysweep.SEEK})
        return self._swept_ptr

    def _refresh_key_section(self):
        """Rebuild and re-enable the section for the current claims. Callers
        hold ``_lock``."""
        from . import keysweep
        wanted = set()
        for owned in self._key_claims.values():
            wanted |= owned
        claims = [c for c in self._swept_keys() if c[1] in wanted]
        self._key_actions = {key: (semantic, arg) for key, semantic, arg in claims}
        suppress = [] if keysweep.SEEK not in wanted else self._swept_pointers()
        try:
            if not claims and (not suppress):
                self._player.command('disable-section', self.KEY_SECTION)
                return
            self._player.command('define-section', self.KEY_SECTION, keysweep.section_lines(claims, self.KEY_MESSAGE, suppress), 'force')
            self._player.command('enable-section', self.KEY_SECTION, self.SECTION_FLAGS)
        except Exception:
            log.debug('could not update the key section', exc_info=True)

    def _on_claimed_key(self, semantic, key):
        """A claimed key was pressed: carry out what the user had bound,
        through the operation that knows about SyncPlay and about
        remembering the choice."""
        found = self._key_actions.get(key)
        if found is None:
            return
        _semantic, arg = found
        if semantic == 'pause':
            if arg is None:
                self.toggle_pause()
            else:
                self.set_paused(bool(arg))
        elif semantic == 'seek':
            amount, exact = arg
            if settings.use_web_seek:
                back, forward = self.get_seek_times()
                amount = forward if amount > 0 else back
            self.seek(amount, exact=exact)
        elif semantic == 'fullscreen':
            want = not self._player.fs if arg is None else bool(arg)
            self.set_fullscreen(want, persist=True)

    def _on_client_message(self, event):
        try:
            if hasattr(event, 'as_dict'):
                event = event.as_dict()
                if 'event' in event:
                    event['event'] = event['event'].decode('utf-8')
                if 'args' in event:
                    event['args'] = [d.decode('utf-8') for d in event['args']]
            if 'event_id' in event:
                args = event['event']['args']
            else:
                args = event['args']
            if len(args) == 0:
                return
            if args[0] == 'shim-menu-select':
                if args[1] == 'inf':
                    return
                self.menu.mouse_select(int(args[1]))
            elif args[0] == 'shim-menu-click':
                self.menu.menu_action('ok')
            elif args[0] == 'shim-menu-back':
                self.menu.menu_action('back')
            elif args[0] == self.KEY_MESSAGE and len(args) >= 3:
                self._on_claimed_key(args[1], args[2])
            elif args[0] == self.MENU_MESSAGE and len(args) >= 2:
                self.menu.menu_action(args[1])
            elif args[0] == 'shim-trickplay-need' and len(args) >= 2:
                trickplay = self.trickplay
                if trickplay is not None:
                    trickplay.request_at(float(args[1]))
            elif args[0] == 'jms-lua':
                if self._lua_probe is not None:
                    self._lua_probe.set()
            elif args[0] == 'cinema-hdr-output' and len(args) >= 2:
                if args[1] in ('auto', 'sdr'):
                    self.put_task(self.set_hdr_output, args[1])
        except Exception:
            log.warning('Error when processing client-message.', exc_info=True)

    def _notify_mpv_gone(self):
        handler = self.on_mpv_gone
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log.error('on_mpv_gone handler failed', exc_info=True)

    def _notify_mpv_terminated(self):
        """mpv is really dead — see on_mpv_terminated. Runs on the terminate
        thread, so handlers must not block it for long."""
        handler = self.on_mpv_terminated
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log.error('on_mpv_terminated handler failed', exc_info=True)

    def _queue_finished(self):
        self.pause_ignore = True
        has_lock = self._finished_lock.acquire(False)
        self.put_task(self.finished_callback, has_lock, self._play_epoch)

    def run_action(self, func):
        """Run a UI-originated player action without ever blocking the caller.

        ``_lock`` is held for the whole of a playback start -- the mpv load
        plus the duration wait, bounded only by playback_timeout (30s) and
        routinely the full timeout when a stream fails to open. UI actions run
        inline on the browser's loop thread, so calling a @synchronous method
        straight through froze the window for that whole stretch.

        Fast path is unchanged: a free lock means the action runs inline and
        synchronously. Only a contended lock defers onto the action thread.
        ``func`` takes the PlayerManager.

        NB the deferred path holds **no** lock, which is why targets that
        mutate the player carry their own ``@synchronous``.
        """
        if self._lock.acquire(blocking=False):
            try:
                return func(self)
            finally:
                self._lock.release()
        log.debug('Player is busy; deferring UI action to the action thread.')
        generation = self._hud_generation

        def unless_the_session_ended(pm):
            if pm._hud_generation != generation:
                log.debug('Dropping a UI action queued by a session that has since ended.')
                return None
            return func(pm)
        self.put_task(unless_the_session_ended, self)
        return None

    def put_task(self, func, *args):
        self.evt_queue.put([func, args])
        if self.action_trigger:
            self.action_trigger.set()

    def chapter_seek(self, direction):
        """Jump a chapter back (-1) or forward (+1).

        Not mpv's own ``add chapter``: that bypasses SyncPlay, so one member
        of a group jumping a chapter would simply desync. Going through
        seek() puts it through the same request the seek bar makes.

        Exempt from seek-to-skip-intro for the same reason the HUD's own
        seeks are: someone jumping to the chapter the intro is in asked for
        that chapter, not for the end of the intro.
        """
        try:
            chapters = self._player.chapter_list or []
            pos = self._player.playback_time
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        if pos is None:
            return
        target = chapter_target([{'time': float(ch.get('time') or 0.0)} for ch in chapters], float(pos), direction)
        if target is None:
            return
        self._last_ui_seek_time = time.time()
        self.seek(target, absolute=True)

    def _apply_resume_offset(self, offset):
        """Seek to the saved position on a fresh start.

        **Marked as one of ours**, which is the whole reason this is a method:
        `_on_seeking` treats any forward seek while `is_in_intro` as the
        user asking to skip (`skip_intro_on_seek`), and a resume INTO an
        intro is a forward seek. So quitting mid-intro and resuming skipped
        the intro on open with no input at all. The exemption already exists
        for seeks the UI makes; the resume simply never claimed it, and
        keeping the claim next to the seek is what stops the two drifting.

        **`last_seek` is set BEFORE the seek, and moving it below is a
        regression, not a tidy-up.** A review asked for exactly that, so that
        a resume which never happened is not recorded as a position reached.
        It is `None` for the first item of a session, and the stop report
        reads `int((self.last_seek or 0) * 10000000)`
        (`player_reporting.py:643`) -- so a failed resume would report
        position ZERO and destroy the user's place. Reporting the position
        they asked to return to is the lesser wrong, and surfacing the failure
        to them is the real fix (todo 17).
        Pinned by `TestAFailedResumeStillReportsItsPosition`.
        """
        self.last_seek = offset
        self._last_ui_seek_time = time.time()
        try:
            self._player.command('set', 'playback-time', str(offset))
        except Exception:
            log.debug('Could not apply the resume offset.', exc_info=True)

    def timeline_handle(self):
        if self.timeline_trigger:
            self.timeline_trigger.set()

    def skip_intro(self):
        video = self._video
        if video is None:
            return
        _, intro = video.get_current_intro(self._player.playback_time)
        if intro is None:
            return
        if not self._player.playback_abort:
            self._player.command('seek', intro.end, 'absolute')
        intro.has_triggered = True
        self.timeline_handle()
        self.is_in_intro = False
        self._last_intro_msg_time = time.time()

    @synchronous('_lock')
    def _pump_trickplay(self):
        """Start the trickplay tile fetch once playback is actually running.

        Deferred off the playback-start path on purpose: the fetch is dozens
        of serial HTTP requests to the same host mpv is streaming from, and
        issuing them while the demuxer is still opening the file competed for
        connections with the open itself.

        "core-idle false" is the signal — mpv is decoding and presenting
        frames, so the open is done and the demuxer has what it needs. Falls
        back to a positive playback_time for backends that don't expose
        core-idle. Runs on the action thread, once per playback.

        **A PAUSE satisfies it too, and has to.** mpv reports a paused core as
        `core-idle yes` indefinitely, with `playback_time` frozen where it
        stopped -- 0.0 for someone who paused at the start (measured on 0.41).
        Waiting on core-idle alone left a viewer who paused in the first
        second with no scrub thumbnails for the rest of the item: nothing here
        fires again, and the renderer's lazy re-ask only runs once a first
        window has arrived. A stopped core is also the one moment nothing is
        competing with the fetch.
        """
        if not self._trickplay_pending or self.trickplay is None:
            return
        try:
            idle = self._player.core_idle
            paused = self._player.pause
            position = self._player.playback_time or 0
            live = position > 0 if idle is None else not idle
        except _mpv_errors:
            return
        except Exception:
            log.debug('Could not read playback state for trickplay.', exc_info=True)
            return
        if not live and (not paused):
            return
        self._trickplay_pending = False
        log.debug('Playback is live; starting the trickplay fetch.')
        self.trickplay.fetch_thumbnails(position)

    def update(self):
        while not self.evt_queue.empty():
            func, args = self.evt_queue.get()
            try:
                func(*args)
            except _mpv_errors:
                self._handle_mpv_disconnect()
            except Exception:
                log.exception('Queued task %s failed.', getattr(func, '__name__', func))
        self._pump_trickplay()
        prev_hud_skip = self._hud_skip
        try:
            if conf.any_segment_wanted() and self._video is not None and (self._player.playback_time is not None):
                ready_to_skip, intro = self._video.get_current_intro(self._player.playback_time)
                have_hud = getattr(self, '_osc_style_resolved', None) == 'mpvtk'
                if intro is not None:
                    action = conf.segment_action(intro.type)
                    in_group = self.syncplay.is_enabled()
                    should_skip = not intro.has_triggered and action == 'always' and (not in_group)
                    should_prompt = action == 'ask' or (action == 'always' and in_group)
                    if should_skip and ready_to_skip:
                        intro.has_triggered = True
                        self.skip_intro()
                        self._player.show_text(segment_labels(intro.type)[1], 3000, 1)
                        self._last_intro_msg_time = time.time()
                    if have_hud:
                        self._hud_skip = intro if should_prompt and (not should_skip) else None
                    elif not self.is_in_intro and should_prompt and settings.skip_intro_on_seek and (time.time() - self._last_intro_msg_time > 3):
                        self._player.show_text(segment_labels(intro.type)[2], 3000, 1)
                        self._last_intro_msg_time = time.time()
                    self.is_in_intro = True
                else:
                    self._hud_skip = None
                    self.is_in_intro = False
            else:
                self._hud_skip = None
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        if (self._hud_skip is None) != (prev_hud_skip is None):
            self.push_playstate()
        try:
            if self._video and (not self._player.playback_abort):
                if not self.is_paused():
                    self.last_update.restart()
        except _mpv_errors:
            self._handle_mpv_disconnect()
        try:
            video = self._video
            if video is not None and self.should_send_timeline and (time.time() - (self.start_time or 0) > 5):
                try:
                    eof = self._player.eof_reached
                except _mpv_errors:
                    self._handle_mpv_disconnect()
                    return
                except Exception:
                    eof = None
                if eof is True:
                    self._reached_eof = True
                    self._queue_finished()
                elif self._check_stalled_finish(video):
                    self._queue_finished()
                elif not video.parent.has_next:
                    try:
                        abort = self._player.playback_abort
                    except _mpv_errors:
                        self._handle_mpv_disconnect()
                        return
                    except Exception:
                        abort = False
                    if abort:
                        self._queue_finished()
        except Exception:
            log.exception('End-of-file poll rescue failed.')

    def play(self, video: 'Video_type', offset: int=0, no_initial_timeline: bool=False, is_initial_play: bool=False, apply_memory: bool=True, pause_stills: bool=True):
        if video is None:
            log.error('PlayerManager::play called without a video')
            return
        self.should_send_timeline = False
        self.start_time = time.time()
        self._load_cancelled = False
        self._start_in_progress = True
        try:
            video.auth_via_header = self._apply_auth_headers(video)
            video.resolve_tracks_for_negotiation()
            if is_initial_play:
                self._track_memory = None
            elif apply_memory and self._track_memory is not None:
                self._apply_remembered_tracks(video)
            url = video.get_playback_url()
            if not url:
                log.error('PlayerManager::play no URL found')
                self._revoke_auth_header(video)
                return
            if video.auth_via_header and (not same_origin(url, video.client.config.data.get('auth.server'))):
                log.info('Not sending the auth header to mpv: this item streams from another host.')
                self._revoke_auth_header(video)
            if not video.auth_via_header:
                try:
                    video.reauthorize_sidecars()
                except Exception:
                    log.debug('could not re-authorize subtitle urls', exc_info=True)
            self._warm_shader_scope(video)
            if self._load_cancelled:
                log.info('Playback start cancelled during the profile lookup.')
                self._load_cancelled = False
                return
            self._play_media(video, url, offset, no_initial_timeline, is_initial_play, apply_memory, pause_stills)
        finally:
            self._start_in_progress = False

    def _apply_auth_headers(self, video):
        """Hand mpv this server's Authorization header. True if it took.

        Everything mpv fetches for this file goes through it -- stream and
        subtitle sidecar alike -- so no URL needs a token in its query string.
        ``Authorization: MediaBrowser Token="..."`` is the one scheme not gated
        behind ``EnableLegacyAuthorization``; the apiclient already builds it.

        Returns False rather than raising, and the caller falls back to the
        url; the cost of being wrong is that nothing plays at all.

        **The clear at the top is load-bearing.** ``http-header-fields`` is a
        global, persistent option and mpv is not re-created between queue
        items, so without it a refusal to send the header defeats itself and
        leaks the previous item's token to a third-party host. Clearing here
        rather than on each ``return False`` covers every exit path past this
        line, including the ones nobody has written yet.
        See docs/mpv-backends.md section 6.
        """
        if not self._mpv_alive:
            return False
        try:
            self._player.http_header_fields = []
        except Exception:
            log.debug('could not clear http-header-fields', exc_info=True)
        client = getattr(video, 'client', None)
        if client is None:
            return False
        try:
            header = client.http._get_authenication_header()
        except Exception:
            log.debug('could not build an auth header', exc_info=True)
            return False
        if not header or 'Token=' not in header:
            return False
        try:
            foreign = video.foreign_subtitle_hosts()
        except Exception:
            log.debug('could not check for foreign subtitle hosts', exc_info=True)
            foreign = {'unknown'}
        if foreign:
            log.info('Not sending the auth header to mpv: this item has a subtitle on %s, and the option is not per-URL.', ', '.join(sorted((str(h) for h in foreign))))
            return False
        try:
            self._player.http_header_fields = ['Authorization: ' + header]
        except Exception:
            log.warning('mpv would not take http-header-fields; falling back to a token in the URL', exc_info=True)
            return False
        return True
    SHADER_SCOPE_WAIT_SECS = 2.0

    def _warm_shader_scope(self, video):
        """Resolve this item's library id, bounded, before _play_media.

        `hwdec` is written under the player lock and read when the decoder is
        initialised, so a library-scope profile naming a decoder has to be
        known by then -- but the lookup is a request, and a request on the
        start path must not be able to hold it open indefinitely.

        So: run it on a thread and wait a moment. A server that answers gets
        the profile applied to the first item, which is the whole point; one
        that hangs costs the start `SHADER_SCOPE_WAIT_SECS` and then plays
        without it, which is exactly the behaviour before this was moved off
        the action thread. The lookup keeps running and caches its answer for
        the next item either way.
        """
        try:
            profiles = self.menu.profile_manager if self.menu else None
        except Exception:
            return
        warm = getattr(profiles, 'warm_library_scope', None)
        if warm is None:
            return
        item = getattr(video, 'item', None) or {}
        client = getattr(video, 'client', None)

        def work():
            try:
                warm(item, client)
            except Exception:
                log.debug('could not warm the shader library scope', exc_info=True)
        try:
            thread = threading.Thread(target=work, daemon=True, name='shader-scope-warm')
            thread.start()
            thread.join(self.SHADER_SCOPE_WAIT_SECS)
            if thread.is_alive():
                log.info('The shader library lookup is slow; starting without it. The profile applies from the next item.')
        except Exception:
            log.debug('could not run the shader library lookup', exc_info=True)

    def _revoke_auth_header(self, video=None):
        """Take back a header installed before the stream's host was known.

        ``http-header-fields`` is global and outlives the item it was set for
        (docs/mpv-backends.md section 6), so "do not send it to this host" has
        to mean clearing it, not merely declining to set it again.
        """
        if video is not None:
            video.auth_via_header = False
        if not self._mpv_alive:
            return
        try:
            self._player.http_header_fields = []
        except Exception:
            log.debug('could not clear http-header-fields', exc_info=True)

    def _forced_hwdec(self):
        """Whether a shader profile has named the decoder it requires.

        A profile naming a *specific* decoder (``d3d11va``, ``vaapi``, …)
        is stating a hardware requirement of the thing the user just chose
        -- the shipped ``rtx-vsr`` needs d3d11va because its d3d11vpp
        filter operates on d3d11 surfaces. That is different in kind from
        the blanket ``auto-copy`` every profile used to carry, which was an
        opinion about the machine. The specific one is applied by the
        profile itself; this just stops the per-item write from undoing it
        on the next file.
        """
        try:
            profiles = self.menu.profile_manager if self.menu else None
            return bool(profiles is not None and getattr(profiles, 'forced_hwdec', None))
        except Exception:
            log.debug('could not read the shader profile', exc_info=True)
            return False

    def _needs_copy_hwdec(self):
        """Whether anything downstream needs frames in system RAM.

        Direct hardware decoding hands mpv frames on the GPU, which a video
        filter cannot read -- so where there is a filter, hwdec has to be the
        copy-back kind or it silently does not apply. Three sources:

        * the active shader profile said so (``wants_copy_hwdec``);
        * SVP is enabled, i.e. a VapourSynth filter in the user's mpv.conf;
        * mpv reports a filter chain -- the general case, asked separately
          because it is the only one that sees a filter we know nothing about.

        Never raises: unanswerable means "no filter", and the cost of being
        wrong is a filter that does not apply, not a player that fails.
        """
        try:
            profiles = self.menu.profile_manager if self.menu else None
            if profiles is not None and getattr(profiles, 'wants_copy_hwdec', False):
                return True
        except Exception:
            log.debug('could not read the shader profile', exc_info=True)
        if settings.svp_enable:
            return True
        try:
            return bool(self._player.vf)
        except Exception:
            log.debug('could not read the filter chain', exc_info=True)
        return False

    @synchronous('_lock')
    def _play_media(self, video: 'Video_type', url: str, offset: int=0, no_initial_timeline: bool=False, is_initial_play: bool=False, apply_memory: bool=True, pause_stills: bool=True):
        self._ensure_mpv()
        self.pause_ignore = True
        self.do_not_handle_pause = True
        self.url = url
        self._showing_browse_bg = False
        self._browse_bg_deferred = False
        self.resume_colorspace_hint()
        self.menu.hide_menu()
        if self.trickplay:
            self.trickplay.clear()
        if settings.log_decisions:
            log.info('Playing: {0}'.format(url))
        try:
            self._player.command('set', 'user-data/media-source/Path', video.media_source.get('Path'))
        except Exception:
            log.debug('Could not set user-data/media-source/Path', exc_info=True)
        try:
            v_item = getattr(video, 'item', None) or {}
            v_audio = v_item.get('MediaType') == 'Audio' or v_item.get('Type') == 'Audio'
            self._player.volume = settings.music_volume if v_audio else settings.video_volume
        except _mpv_errors:
            pass
        try:
            profiles = self.menu.profile_manager if self.menu else None
            if profiles is not None:
                if getattr(video, 'is_photo', False):
                    profiles.suspend_for_still()
                else:
                    profiles.apply_for_item(v_item, getattr(video, 'client', None))
        except Exception:
            log.debug('could not adjust the shader profile for this item', exc_info=True)
        try:
            from .mpv_options import hwdec_for
            from .args import get_args
            if getattr(get_args(), 'disable_hwdec', False):
                self._player.hwdec = 'no'
            else:
                forced = self._forced_hwdec()
                want = None if forced else hwdec_for(_source_height(video), self._needs_copy_hwdec())
                if want is not None:
                    self._player.hwdec = want
        except Exception:
            log.debug('could not apply the hwdec setting', exc_info=True)
        try:
            from .mpv_options import deinterlace_value
            self._apply_deinterlace(deinterlace_value(self._deinterlace_override))
        except Exception:
            log.debug('could not apply the deinterlace setting', exc_info=True)
        if self._aspect_override is not None:
            try:
                self._write_aspect(self._aspect_override)
            except Exception:
                log.debug('could not apply the aspect override', exc_info=True)
        self._apply_render_presets()
        if getattr(video, 'is_photo', False):
            try:
                self._player.image_display_duration = max(1, int(settings.photo_display_secs))
            except (_mpv_errors, ValueError, TypeError):
                log.debug('could not set the photo display duration', exc_info=True)
        try:
            self._player.loop_file = 'inf' if self.repeat_mode == 'one' and _item_is_audio(video) else 'no'
        except _mpv_errors:
            pass
        self._load_generation += 1
        self._load_failed.clear()
        self._load_completed.clear()
        self._load_error_detail = None
        self._sync_window_geometry()
        clear_mpv_errors()
        self._loading = True
        self._notify_load_start(video)
        try:
            if self._load_cancelled:
                loaded = False
            else:
                if not _item_is_audio(video):
                    try:
                        self._player.pause = True
                    except _mpv_errors:
                        log.debug('could not hold the load paused', exc_info=True)
                self._player.play(self.url)
                loaded = wait_property(self._player, 'duration', lambda x: x is not None, settings.playback_timeout, skip_initial=True, abort=self._load_failed, satisfied_by=self._load_completed)
        finally:
            self._loading = False
        if not loaded:
            cancelled, self._load_cancelled = (self._load_cancelled, False)
            if cancelled:
                log.info('Playback start cancelled.')
                self._failed_playback = None
                self._abort_load()
                self.stop()
                return
            errored = self._load_failed.is_set()
            detail = self._load_error_detail or last_mpv_error()
            if errored:
                log.error('Could not load media: %s', detail or 'unknown error')
            else:
                log.error('Timeout when waiting for media duration. Stopping playback!')
            self._failed_playback = (video, offset)
            self._notify_load_error(video, detail, timed_out=not errored)
            self._abort_load()
            self.stop()
            return
        log.info('Finished waiting for media duration.')
        if self._load_cancelled:
            log.info('Playback start cancelled just as it completed.')
            self._load_cancelled = False
            self._failed_playback = None
            self._abort_load()
            self.stop()
            return
        self._failed_playback = None
        self._video = video
        hold_still = bool(getattr(video, 'is_photo', False) and pause_stills and is_initial_play)
        if hold_still:
            try:
                self._player.pause = True
            except _mpv_errors:
                log.debug('could not pause on a photo', exc_info=True)
        if settings.fullscreen and (not self.fullscreen_disable) and (not self._current_is_audio()):
            self._player.fs = True
        self._player.force_media_title = video.get_proper_title()
        self._reached_eof = False
        self._last_playback_position = 0
        self._stall_position = None
        self._stall_since = 0.0
        self._play_epoch += 1
        self.is_in_intro = False
        self.external_subtitles = {}
        self._pending_subs = {}
        self.upd_player_hide()
        self.configure_streams()
        self._capture_track_memory(video)
        self.update_subtitle_visuals()
        if win_utils and settings.raise_mpv and is_initial_play:
            win_utils.raise_mpv()
        if offset is not None and offset > 0:
            self._apply_resume_offset(offset)
        if not no_initial_timeline:
            self.send_timeline_initial()
        else:
            self.send_timeline()
        self._start_in_progress = False
        if self.syncplay.is_enabled():
            self.set_speed(1)
            self.syncplay.play_done()
        else:
            self.set_paused(hold_still, False)
        self._trickplay_pending = bool(self.trickplay and (not v_audio) and (not getattr(video, 'is_photo', False)))
        self.should_send_timeline = True
        self._last_offline_record = float('-inf')
        self.do_not_handle_pause = False
        if self._finished_lock.locked():
            self._finished_lock.release()
        self.update_check.check()
        if not self._video.parent.is_local and self._video.is_transcode and (not self.warned_about_transcode) and settings.transcode_warning and (getattr(self, '_osc_style_resolved', None) != 'mpvtk'):
            self.warned_about_transcode = True
            self._player.show_text(_('Your remote video is transcoding!\nPress c to adjust bandwidth settings if this is not needed.'), 5000, 1)

    @staticmethod
    def exec_stop_cmd():
        if settings.stop_cmd:
            os.system(settings.stop_cmd)

    def playback_starting(self):
        """Before a new playback's play(). A start over current playback is
        a hand-over -- the end stays owed and is paid when the new playback
        ends -- unless ``stop_cmd_on_replace`` asks for the old one's end
        first."""
        if settings.stop_cmd_on_replace and self._take_stop_owed():
            self.exec_stop_cmd()
        if settings.pre_media_cmd:
            os.system(settings.pre_media_cmd)

    def playback_started(self):
        """After a new playback's play(). Owes an end only if something is
        playing: a start that failed has already ended."""
        if self._video is not None:
            with _STOP_OWED_LOCK:
                self._stop_owed = True
        if settings.play_cmd:
            os.system(settings.play_cmd)

    def _take_stop_owed(self):
        """True once per announced playback: the caller runs stop_cmd."""
        with _STOP_OWED_LOCK:
            owed, self._stop_owed = (self._stop_owed, False)
        return owed

    def _end_playback(self):
        """Drop the playing video. Every place playback ends by dropping it
        goes through here, so a new one cannot miss the hook. Returns whether
        stop_cmd is owed; the caller runs it where its teardown allows."""
        self._video = None
        return self._take_stop_owed()

    def _release_syncplay(self):
        """Stopping playback while in a SyncPlay group: halt, or leave.

        Halting is what jellyfin-web does -- membership is not a property of
        playback, and dropping the group every time somebody went back to the
        library made SyncPlay unusable for anything but one film start to
        finish. But halting only works if the group is still **reachable**
        afterwards, and on two surfaces it is not: with no GUI at all, and
        when playback was cast to a shim whose browser was never opened. The
        alternative there is a group the user is in, is not watching, and has
        no way to get out of.

        Already halted is nothing to release: a halted member may go on to
        play something of their own, and that ending is not the group's
        business.
        """
        if not self.syncplay.is_enabled():
            return
        ask = self.syncplay_menu_reachable
        reachable = False
        if ask is not None:
            try:
                reachable = bool(ask())
            except Exception:
                log.debug('syncplay_menu_reachable failed', exc_info=True)
        if reachable:
            self.syncplay.halt_group_playback()
        else:
            log.info('Leaving the SyncPlay group: no menu to leave it from later.')
            self.syncplay.disable_sync_play(False)

    @synchronous('_lock')
    def stop(self, leave_group: bool=True):
        """Stop playback.

        ``leave_group`` is False only for a SyncPlay group Stop: the server
        moves the group to Idle and keeps every session in it, so tearing our
        own membership down would leave us out of a group the server still
        thinks we are in.
        """
        if leave_group and self.syncplay.is_enabled():
            self._release_syncplay()
        if self.menu.is_menu_shown:
            self.menu.hide_menu()
        local_video = self._video
        if not local_video or not self._mpv_alive:
            if self._take_stop_owed():
                self.exec_stop_cmd()
            return
        try:
            if self._player.playback_abort:
                if self._take_stop_owed():
                    self.exec_stop_cmd()
                return
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        log.info('PlayerManager::stop stopping playback of %s' % local_video)
        self.should_send_timeline = False
        options = self.get_timeline_options(video=local_video)
        self.set_paused(False)
        owed = self._end_playback()
        self._player.command('stop')
        self.clear_media_title()
        self.push_playstate(stopped=True)
        self.release_stream(local_video)
        if local_video.client is None and hasattr(local_video, 'record_offline_progress'):
            local_video.record_offline_progress(options.get('PositionTicks'))
        self.send_timeline_stopped(options=options, client=local_video.client, stopped=local_video)
        if owed:
            self.exec_stop_cmd()
        if self.trickplay:
            self.trickplay.clear()

    def stop_for_window_close(self):
        """Stop because the window is going away.

        Always LEAVES a SyncPlay group rather than halting it. Halting is for
        a stop you can come back from -- back to the library, where the
        SyncPlay menu is. A closing window has no library behind it, so a
        halted membership is one nobody can see, leave or resume while the
        group goes on waiting for a member who is not there.

        Explicit rather than left to `syncplay_menu_reachable`: that hook does
        answer "no" here, but only because the browser happens to call
        minimize() first, with nothing saying those two lines must stay in
        that order.
        """
        if self.syncplay.in_group():
            log.info('Leaving the SyncPlay group: the window is closing.')
            self.syncplay.disable_sync_play(False)
        self.stop()

    def stop_and_close(self):
        log.info('stop_and_close: stopping playback')
        self.stop_for_window_close()
        if not self._mpv_alive:
            return
        try:
            self._player.keep_open = False
            self._set_force_window(False)
            self._player.command('stop')
        except _mpv_errors:
            self._handle_mpv_disconnect()
        log.info('stop_and_close: done')

    def toggle_stats(self):
        """Toggle mpv's "Playback Data" (stats.lua) overlay, tracking its
        state.

        Reached from the `i` key and from the *lua* OSC's gear sheet. The
        mpvtk HUD's gear no longer has an entry for it: that row is
        "Playback Info" now, which is ours and answers what the server is
        sending rather than what the decoder is doing (#10). Everything
        still funnels through here so ``_stats_shown`` stays truthful and
        clear_stats() can reliably put the overlay away when the library
        returns."""
        if not self._mpv_alive:
            return
        try:
            self._player.command('script-binding', 'stats/display-stats-toggle')
            self._stats_shown = not self._stats_shown
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def _apply_deinterlace(self, value):
        """Write mpv's ``deinterlace``, tolerating a build without ``auto``.

        ``auto`` is mpv 0.38+; older builds reject the value outright. The
        fallback is ``no``, **never ``yes``** -- forcing deinterlacing on
        every file is not a degraded version of "deinterlace what is
        flagged", it is a different setting that softens progressive video.
        Somebody on an old mpv gets the feature they cannot have turned off
        rather than a worse one turned on, and a line in the log saying so.
        """
        if value == 'auto' and self._no_deinterlace_auto:
            value = 'no'
        try:
            self._player.deinterlace = value
        except _mpv_errors:
            raise
        except Exception:
            if value != 'auto':
                raise
            log.info('this mpv has no deinterlace=auto; leaving it off')
            self._no_deinterlace_auto = True
            self._player.deinterlace = 'no'

    def _snapshot_render_pristine(self):
        """Read every property the preset-driven settings can write, from an
        mpv nothing has written to yet.

        This is the definition of "the user's own value" that the restore
        hands back: mpv's defaults as modified by the user's ``mpv.conf``,
        and nothing else.

        **Call it the moment the handle exists, before anything else touches
        it.** The shader pack gets there first two different ways -- a
        remembered profile re-applied by ``VideoProfileManager.__init__`` at
        construction, and ``apply_for_item`` running earlier in
        ``_play_media`` than the settings do -- and snapshotted after either,
        "the user's own value" is the pack's (docs/mpv-backends.md section 6).

        A property this mpv does not have is simply absent, and the restore
        skips it: an older build cannot have a value to put back, and the
        preset write for it will fail on its own terms.
        """
        from .mpv_options import PRESET_SETTINGS, preset_keys
        self._render_pristine = {}
        if self._player is None:
            return
        for key in PRESET_SETTINGS:
            for prop in preset_keys(key):
                if prop in self._render_pristine:
                    continue
                try:
                    self._render_pristine[prop] = getattr(self._player, prop.replace('-', '_'))
                except Exception:
                    log.debug('this mpv has no %s to remember', prop, exc_info=True)
        if not self._render_pristine:
            log.warning('Could not read any picture options from this mpv; turning a picture setting off will not restore your own values this session.')

    def _apply_render_preset(self, key):
        """Write ``key``'s configured preset, or undo ours.

        **"Off" writes nothing until we have written something**, or the
        first item would reach out and undo the user's own ``mpv.conf`` with
        no setting here to put it back. The undo is symmetric with the do:
        off restores every property any preset of this key touches, not just
        this preset's.

        ``_render_written`` carries the second meaning of off -- with a shader
        profile loaded, off is "I have no opinion, leave the pack's value
        alone" rather than "undo the pack". Only a key we wrote is a key we
        may take back.

        The full account, including why preserving ``video-sync`` alone is
        worse than useless: docs/mpv-backends.md section 6.
        """
        from .mpv_options import preset_keys, preset_props
        from .shared_playback import apply_picture_setting
        if apply_picture_setting(self, key):
            return
        props = preset_props(key)
        if props:
            wrote = False
            for prop, value in props.items():
                if self._render_pristine and prop not in self._render_pristine:
                    continue
                setattr(self._player, prop.replace('-', '_'), value)
                wrote = True
            if wrote:
                self._render_written.add(key)
            return
        if key not in self._render_written:
            return
        pack = self._pack_applied()
        for prop in preset_keys(key):
            name = prop.replace('-', '_')
            if name in pack:
                value = pack[name]
            elif prop in self._render_pristine:
                value = self._render_pristine[prop]
            else:
                continue
            setattr(self._player, name, value)
        self._render_written.discard(key)

    def _pack_applied(self):
        """``{mpv property (underscored): value}`` the loaded shader profile
        is currently holding, or ``{}``.

        Read from the profile manager rather than recomputed: it records what
        it applied, and re-deriving it here would be a second implementation
        of the pack's group resolution -- which is the shape this codebase
        keeps getting wrong.
        """
        menu = getattr(self, 'menu', None)
        manager = getattr(menu, 'profile_manager', None) if menu else None
        if manager is None or getattr(manager, 'current_profile', None) is None:
            return {}
        return getattr(manager, 'applied_settings', None) or {}

    def _apply_render_presets(self):
        """Every preset-driven mpv setting, each guarded on its own.

        Separately guarded rather than one try around the loop: they are
        unrelated preferences, and an mpv that rejects one -- an older build
        without ``hdr-contrast-recovery``, say -- has no bearing on the
        others. Sharing a block means the first failure silently drops the
        rest, which is the shape the hwdec/deinterlace split in
        ``_play_media`` already avoids.
        """
        from .mpv_options import PRESET_SETTINGS
        mode = getattr(settings, 'hdr_output', 'auto')
        self._prepare_hdr_output(mode)
        keys = list(PRESET_SETTINGS)
        if mode == 'auto':
            keys.remove('hdr_output')
            keys.insert(0, 'hdr_output')
        for key in keys:
            try:
                self._apply_render_preset(key)
            except Exception:
                log.debug('could not apply the %s setting', key, exc_info=True)

    def _apply_interpolation(self):
        """Motion interpolation, by its own name. See
        :meth:`_apply_render_preset`."""
        self._apply_render_preset('motion_interpolation')

    @synchronous('_lock')
    def set_deinterlace(self, on):
        """Force deinterlacing on or off for this session -- the playback
        HUD's gear-menu toggle. ``clear_deinterlace_override`` is the third
        state, "let the setting decide".

        Deliberately not persisted. It is the answer to one file (or one
        badly-flagged season) rather than a statement about the library, and
        ``deinterlace_auto`` is where a durable answer goes.

        ``@synchronous``, like every other ``run_action`` target that mutates
        the player: ``run_action``'s DEFERRED path holds no lock at all, so
        without this the toggle could land between ``_play_media`` reading the
        override and writing it, and the press would be silently lost.
        """
        self._deinterlace_override = bool(on)
        self.reapply_deinterlace()
    _hud_generation = 0
    _aspect_pristine = _UNSET
    _stop_owed = False
    _ASPECT_AUTO = ('-1', '-1.0', -1, -1.0)

    def _write_aspect(self, value):
        """Write the property, remembering what it held the first time.

        Nothing else in this client writes `video-aspect-override` and no
        setting maps to it, so whatever is there before our first write is
        the user's -- and handing THAT back is what clearing means. Callers
        hold ``_lock``.
        """
        if self._aspect_pristine is _UNSET:
            try:
                self._aspect_pristine = self._player.video_aspect_override
            except Exception:
                log.debug('could not read the aspect mpv came in with', exc_info=True)
                self._aspect_pristine = -1.0
        self._player.video_aspect_override = value

    @synchronous('_lock')
    def set_aspect(self, value):
        """Force an aspect ratio for this session -- the gear menu's Aspect
        Ratio row. ``value`` is mpv's string form ("16:9"); None, or mpv's
        own spelling of auto, hands the decision back to the file.

        ``@synchronous`` and stored, for the same two reasons as
        ``set_deinterlace``: ``run_action``'s deferred path holds no lock, and
        a value written straight at mpv is a global with no owner -- which is
        what this was.
        """
        if value is None or value in self._ASPECT_AUTO:
            self.clear_aspect_override()
            return
        self._aspect_override = value
        try:
            self._write_aspect(value)
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def clear_aspect_override(self):
        """Drop the aspect force. Called on the way back to the library and
        on minimize -- the same two doors as
        :meth:`clear_deinterlace_override`.

        **Unlike that one, this DOES write mpv.** Its sibling can leave the
        property alone because `_play_media` rewrites it for every item and
        there is no picture to correct once playback is over. An aspect
        override outlives playback onto surfaces `_play_media` never runs
        for: a photo, and a comic page, which `show_picture` loads directly.
        Leaving it set is how a 4:3 force reached a comic.

        It puts back what mpv came in with, not -1: those differ for anyone
        whose mpv.conf sets one. Never written means never OURS to put back,
        so that case writes nothing at all.

        No ``@synchronous``, unlike ``set_aspect``: both doors call this
        inline on the browser's loop thread, and the lock is held for the
        whole of a playback start -- taking it here would freeze the window
        for that stretch, which is the freeze ``run_action`` exists to
        avoid. The ordering this used to lose is settled by
        ``end_hud_session`` instead, one door up.
        """
        self._aspect_override = None
        if self._aspect_pristine is _UNSET:
            return
        try:
            self._player.video_aspect_override = self._aspect_pristine
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            log.debug('could not clear the aspect override', exc_info=True)

    def aspect_forced(self):
        """Whether a session force is in effect, for the gear row's way back
        -- the same question `deinterlace_forced` answers."""
        return self._aspect_override is not None

    def end_hud_session(self):
        """Declare the playback session over, so nothing it queued lands
        after it.

        The doors call this BEFORE they clear anything. ``run_action``
        defers a UI action whenever ``_lock`` is contended, which a playback
        start holds for up to ``playback_timeout`` -- so a gear-menu press
        can still be on ``evt_queue`` when the viewer gives up and goes
        back. Draining it then re-installs a force onto the library, where
        the gear menu that would undo it is not reachable.

        A generation rather than a drain of the queue: ``evt_queue`` also
        carries the player's own work (a finished_callback, the shutdown
        teardown), and discarding THOSE at a door is the bug
        ``_teardown_player``'s drain has a comment about. This drops only
        what the ended session asked for, and it covers every gear-menu
        state at once rather than one guard per control.

        It fires more often than "playback ended": `enter_browse` is called
        unconditionally when music stops and on a mirror summon, both of
        which can happen while the library is already up. Dropping a pending
        HUD action there is still the right answer -- the thing it was aimed
        at is over -- but do not read a bump as proof that something was
        playing.
        """
        self._hud_generation += 1

    def deinterlace_forced(self):
        """Whether a session force is in effect, i.e. whether the setting is
        currently being overridden. The gear row needs it to offer a way
        BACK: with a two-state toggle and `deinterlace_auto` on, unticking
        replaced `auto` with a hard `no` for the rest of the session, so a
        later episode that IS flagged played un-deinterlaced [reviewer]."""
        return self._deinterlace_override is not None

    def deinterlace_state(self):
        """``(is_on, is_auto)`` for the gear menu's row, from MPV rather
        than from our own override -- the two differ on a build with no
        ``auto``, and what the menu has to describe is what is happening.

        ``is_on`` is ``deinterlace-active``, NOT the mode. Under ``auto``
        the mode says only "decide per file", so reading it left the row
        unticked while mpv was busily deinterlacing a flagged file -- and
        since the row toggles against what it reports, pressing it then did
        nothing visible. ``deinterlace-active`` is mpv 0.38+, which is the
        same version that introduced ``auto``: where it is missing there is
        no auto either, so the mode IS the answer and the fallback is exact
        rather than approximate.
        """
        try:
            mode = self._player.deinterlace
        except Exception:
            return (False, False)
        try:
            return (bool(self._player.deinterlace_active), mode == 'auto')
        except Exception:
            if isinstance(mode, str):
                return (mode == 'yes', mode == 'auto')
            return (bool(mode), False)

    @synchronous('_lock')
    def set_hdr_output(self, mode):
        """Remember the local output choice and apply it without reloading.

        Idle browser windows keep their colorspace parking intact. Every
        playback route applies the preference before loading the video.
        No media source, server setting or cache file is modified.
        """
        if mode not in ('auto', 'sdr'):
            return False
        if self._player is not None and self._mpv_alive:
            try:
                self._prepare_hdr_output(mode)
            except Exception:
                log.exception('could not prepare SDR output')
                self._player.command('set', 'user-data/hdr-output', settings.hdr_output)
                self._player.show_text('无法切换画面输出，请先关闭 RTX 视频 HDR 后重试', 4000)
                return False
        settings.hdr_output = mode
        settings.save()
        if self._video is not None and self._player is not None and self._mpv_alive:
            self._apply_render_presets()
            try:
                self._player.show_text('HDR 转 SDR 观看' if mode == 'sdr' else '画面输出：自动', 2000)
            except Exception:
                log.debug('could not show the output mode', exc_info=True)
        return True

    def _prepare_hdr_output(self, mode):
        """Coordinate with the shared RTX script before changing color output.

        script-message is asynchronous. Wait for its restore to finish so
        it cannot overwrite the SDR hints after we have applied them.
        This runs on the player's action thread, never the UI render loop.
        """
        self._player.command('set', 'user-data/hdr-output', mode)
        if mode != 'sdr':
            return
        state_key = 'user-data/rtx-video-hdr/state'
        try:
            state = self._player._get_property(state_key)
        except Exception:
            return
        if state not in ('on', 'switching', 'error'):
            return
        self._player.command('script-message', 'rtx-video-hdr', 'off')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self._player._get_property(state_key)
            if state == 'off':
                return
            time.sleep(0.025)
        raise RuntimeError('RTX HDR restore did not complete')

    @synchronous('_lock')
    def reapply_render_presets(self):
        """Write the preset-driven settings again, now.

        Called after every profile load and unload, because **loading any
        profile turns debanding on and unloading one turns it off again** --
        so without this, picking an upscaler mid-film and dropping it again
        takes the user's debanding with it for the rest of the film. The rule
        it enforces: a preset that is not "off" outranks the pack, and "off"
        means whatever the pack (or mpv.conf) wrote stands.

        ``@synchronous`` because it arrives from threads that are not the one
        applying settings for the next item, and every property it writes
        ``_play_media`` writes too -- unlocked, the item wears half of each.
        Why blocking a key handler here is safe: docs/mpv-backends.md section 6.
        """
        if self._player is None or not self._mpv_alive:
            return
        self._apply_render_presets()

    @synchronous('_lock')
    def reapply_deinterlace(self):
        """Write whatever the current override-or-setting comes to, now.

        `clear_deinterlace_override` deliberately does not touch mpv -- it
        runs on the way out of playback, where there is nothing to change.
        The gear menu clears it with something still on screen, so it needs
        the write as a separate step.

        ``@synchronous`` for the reason its sibling above is, and stated here
        too because the pair is exactly the shape this codebase gets wrong:
        the mechanism goes on one side and the second implementation is where
        the bug turns up. It reaches mpv from the HUD gateway's ``_act``,
        whose deferred path holds no lock at all.
        """
        if self._player is None or not self._mpv_alive:
            return
        try:
            from .mpv_options import deinterlace_value
            self._apply_deinterlace(deinterlace_value(self._deinterlace_override))
        except Exception:
            log.debug('could not re-apply deinterlacing', exc_info=True)

    def clear_deinterlace_override(self):
        """Drop the per-session force. Called on the way back to the
        library, like :meth:`clear_stats`, **and on minimize** -- closing
        the window does not go through the first one. The next thing played
        starts from the setting again, so the override lasts exactly as
        long as the watching session it was set during.

        Nothing is re-applied here: with playback over there is no picture
        to change, and ``_play_media`` writes the setting for every item.
        """
        self._deinterlace_override = None

    def clear_stats(self):
        """Hide the Playback Data overlay if it is up. Called when returning
        to the library — the overlay is ASS OSD and otherwise lingers behind
        the in-window browser after playback ends."""
        if self._stats_shown:
            self.toggle_stats()

    def stop_to_browser(self):
        """Stop playback but keep the window, so the in-window browser can
        take it back (the 'q' key while the browser is up).
        push_playstate(stopped) is what tells the browser to re-enter browse.

        That notification is also why the browse re-assert below is
        **conditional**. ``stop()`` ends in ``push_playstate(stopped=True)``,
        so the browser acts *inside* this call, and re-entering browse is only
        one of the things it may decide -- told that playback stopped with
        nothing to show, it minimizes instead. Re-asserting unconditionally
        then summoned a blank window the browser tore down a moment later.

        So the browser gets the last word. ``mpvtk_active`` is the same flag
        ``_set_force_window`` treats as the authority on who owns the window,
        and ``on_minimize`` clears it before releasing anything.
        """
        log.info('stop_to_browser: stopping playback, keeping the window')
        self.stop()
        if not self._mpv_alive or not self.mpvtk_active:
            return
        self.set_browse_window(True)

    def get_volume(self, percent: bool=False):
        if self._player:
            if not percent:
                return self._player.volume / 100
            return self._player.volume

    @synchronous('_lock')
    def toggle_pause(self):
        if not self._player.playback_abort:
            self.set_paused(not self._player.pause)

    @synchronous('_lock')
    def pause_if_playing(self):
        if not self._player.playback_abort:
            if not self._player.pause:
                self.set_paused(True)
        self.timeline_handle()

    @synchronous('_lock')
    def play_if_paused(self):
        if not self._player.playback_abort:
            if self._player.pause:
                self.set_paused(False)
        self.timeline_handle()

    @synchronous('_lock')
    def seek(self, offset: float, absolute: bool=False, force: bool=False, exact: Optional[bool]=None):
        """
        Seek to ``offset`` seconds
        """
        if exact is None:
            exact = absolute
        if absolute and offset < 0:
            log.debug('Clamping negative absolute seek %r to 0.', offset)
            offset = 0.0
        if self.syncplay.is_enabled() and (not force):
            if not absolute:
                offset += self._player.playback_time
            self.syncplay.seek_request(offset)
        elif not self._player.playback_abort:
            if absolute:
                if self.syncplay.is_enabled():
                    self.last_seek = offset
                p2 = 'absolute'
                if exact:
                    p2 += '+exact'
                self._player.command('seek', offset, p2)
            else:
                if self.syncplay.is_enabled():
                    self.last_seek = self._player.playback_time + offset
                if exact:
                    self._player.command('seek', offset, 'exact')
                else:
                    self._player.command('seek', offset)
        self.timeline_handle()
        self.push_playstate()

    @synchronous('_lock')
    def set_volume(self, pct: float, notify: bool=True):
        """Set the player volume.

        ``notify=False`` skips the timeline wake and the bar push. Dragging
        a volume slider produces a value per mouse-move, and each one of
        those was waking the timeline thread — which posts progress to the
        *server*. A single drag across the bar meant a burst of round trips
        for a setting the server does not even track. The UI sets the volume
        live for audible feedback and notifies once, on release.
        """
        if not self._player.playback_abort:
            self._player.volume = pct
        if notify:
            self.timeline_handle()
            self.push_playstate()

    @synchronous('_lock')
    def get_state(self):
        if self._player.playback_abort:
            return 'stopped'
        if self._player.pause:
            return 'paused'
        return 'playing'

    @synchronous('_lock')
    def is_paused(self):
        try:
            if not self._player.playback_abort:
                return self._player.pause
        except _mpv_errors:
            self._handle_mpv_disconnect()
        return False

    @synchronous('_lock')
    def finished_callback(self, has_lock: bool, epoch: Optional[int]=None):
        if epoch is not None and epoch != self._play_epoch:
            log.info('PlayerManager::finished_callback stale, skipping')
            return
        video = self._video
        if not video:
            self.pause_ignore = False
            return
        position = self._stream_interrupted_at(video)
        if position is not None:
            log.warning('PlayerManager::finished_callback stream ended at %.0fs, far from the end: interrupted, not finished', position)
            if self.syncplay.is_enabled():
                self._release_syncplay()
            self._notify_load_error(video, _('The server stopped sending it partway through.'), timed_out=False, owns_window=False)
            options = self.get_timeline_options(video=video)
            if options is not None:
                options['PositionTicks'] = int(position * 10000000)
            self.send_timeline_stopped(False, options=options)
            self._unload_ended(video)
            self.pause_ignore = False
            return
        if settings.force_set_played and self._finished_at_eof(video):
            self.queue_played_mark(video)
        wrap = self.repeat_mode == 'all' and self._current_is_audio() and (not video.parent.has_next) and (not self.syncplay.is_enabled()) and (len(video.parent.queue) > 0)
        if (video.parent.has_next or wrap) and settings.auto_play:
            if has_lock:
                log.info('PlayerManager::finished_callback starting next episode')
                if wrap:
                    first = video.parent.get_from_key(video.parent.queue[0]['Id'])
                    new_video = first.video if first else None
                else:
                    new_video = video.parent.get_next().video
                self.send_timeline_stopped(True)
                if new_video is None:
                    log.warning('Next item is not available offline; stopping.')
                    self.show_text(_('Next episode is not downloaded.'), 5000, 1)
                elif self.syncplay.is_enabled():
                    self.syncplay.request_next(video.get_playlist_id())
                else:
                    self.play(new_video)
            else:
                log.info('PlayerManager::finished_callback No lock, skipping...')
        else:
            if settings.media_ended_cmd:
                os.system(settings.media_ended_cmd)
            if self.syncplay.is_enabled():
                self._release_syncplay()
            log.info('PlayerManager::finished_callback reached end')
            self.send_timeline_stopped(True)
            self._unload_ended(video)
        self.pause_ignore = False

    def _stream_interrupted_at(self, video):
        """Where a network stream stopped, if mpv saw it fail and it ended
        far from its end; None for a finish, a local file, or a length or
        position we cannot name.

        **Only a stream mpv saw fail** (`stream_failed`, Izzie 2026-09-28):
        a file whose runtime metadata overstates its length -- a VBR MP3
        with no length header, a recording cut short -- ends cleanly far
        from the server's end, and is a finish. A failure line in a wording
        we do not recognise also reads as a finish, the pre-#783 behaviour.

        The same margin as `_finished_at_eof` (95%, or within 10 s), read
        from the position alone -- not `_reached_eof`, which a stream that
        failed its way to the end sets too."""
        if getattr(video, 'client', None) is None:
            return None
        try:
            from .sync.offline_media import OfflineVideo
            if isinstance(video, OfflineVideo):
                return None
        except Exception:
            pass
        if not stream_failed():
            return None
        duration = video.get_duration()
        if not duration:
            return None
        position = self._last_playback_position or 0
        live = None
        if self._mpv_alive:
            try:
                live = self._player.playback_time
            except Exception:
                live = None
        if live is not None:
            position = max(position, live)
        if not position:
            return None
        if position >= duration * 0.95 or duration - position <= 10:
            return None
        return position

    def _unload_ended(self, video):
        """Drop a video whose playback is over and hand the window back."""
        self.should_send_timeline = False
        owed = self._end_playback()
        if self._mpv_alive:
            try:
                self._player.command('stop')
            except _mpv_errors:
                self._handle_mpv_disconnect()
            self.clear_media_title()
        self.push_playstate(stopped=True)
        self.release_stream(video)
        if owed:
            self.exec_stop_cmd()

    @synchronous('_lock')
    def watched_skip(self):
        if not self._video:
            return
        video = self._video
        try:
            self.play_next()
        finally:
            self.queue_played_mark(video)

    @synchronous('_lock')
    def unwatched_quit(self):
        if not self._video:
            return
        video = self._video
        self.stop_and_close()
        self.queue_played_mark(video, False)

    @synchronous('_lock')
    def play_next(self):
        video = self._video
        if video and video.parent.has_next:
            new_video = video.parent.get_next().video
            self.send_timeline_stopped(True)
            if self.syncplay.is_enabled():
                self.syncplay.request_next(video.get_playlist_id())
            else:
                self.play(new_video)
            return True
        return False

    @synchronous('_lock')
    def skip_to(self, key: str):
        video = self._video
        media = video.parent.get_from_key(key) if video else None
        if media:
            self.send_timeline_stopped(True)
            if self.syncplay.is_enabled():
                self.syncplay.request_skip(media.video.get_playlist_id())
            else:
                self.play(media.get_video(0))
            return True
        return False

    def _widen_queue_backwards(self, video):
        """Put the episodes *before* this one into the queue. True if it grew.

        Starting an episode from Next Up or Continue Watching builds the queue
        with ``StartItemId``, which is inclusive -- so there is nothing behind
        it to step to (#650). jellyfin-web has the same gap. Done lazily:
        nothing is fetched until someone presses previous, then once for the
        session.

        **Prepends rather than rebuilding.** The entries after the current one
        already carry their PlaylistItemIds and may have been edited
        (``insert_items``, a websocket Play command); reconstructing them from
        the server's episode list would silently discard that.

        Runs under the player lock, like every other blocking server call on
        this path, and returns False on anything unexpected -- this is a
        convenience on a keypress, and the worst honest outcome is the button
        doing nothing.
        """
        from .utils import get_seq
        if self.syncplay.is_enabled():
            return False
        client = getattr(video, 'client', None)
        if client is None:
            return False
        item = getattr(video, 'item', None) or {}
        if item.get('Type') != 'Episode' or not item.get('SeriesId'):
            return False
        try:
            result = client.jellyfin.get_episodes(item['SeriesId']) or {}
        except Exception:
            log.debug('could not read the series for a backwards step', exc_info=True)
            return False
        ids = [e.get('Id') for e in result.get('Items') or [] if e.get('Id')]
        try:
            index = ids.index(video.item_id)
        except ValueError:
            return False
        if index <= 0:
            return False
        media = video.parent
        prefix = [{'PlaylistItemId': 'playlistItem{0}'.format(get_seq()), 'Id': eid} for eid in ids[:index]]
        media.replace_queue(prefix + list(media.queue), len(prefix) + media.seq)
        log.info('Queue widened backwards by %d episode(s).', len(prefix))
        return True

    @synchronous('_lock')
    def play_prev(self):
        video = self._video
        if video is not None and (not video.parent.has_prev):
            self._widen_queue_backwards(video)
        if video and video.parent.has_prev:
            new_video = video.parent.get_prev().video
            self.send_timeline_stopped(True)
            if self.syncplay.is_enabled():
                self.syncplay.request_prev(video.get_playlist_id())
            else:
                self.play(new_video)
            return True
        return False

    @synchronous('_lock')
    def get_queue_ids(self):
        """The currently-playing queue's item ids (for 'add queue to playlist')."""
        video = self._video
        if video is None:
            return []
        return [q.get('Id') for q in video.parent.queue if q.get('Id')]

    @synchronous('_lock')
    def get_queue(self):
        """The full queue for the browser's queue display: each entry's item id
        + PlaylistItemId, which one is playing, and the queue's revision."""
        video = self._video
        if video is None:
            return {'items': [], 'current_id': None, 'queue_rev': None}
        m = video.parent
        return {'items': [{'id': q.get('Id'), 'playlist_item_id': q.get('PlaylistItemId')} for q in m.queue if q.get('Id')], 'current_id': video.item_id, 'queue_rev': m.queue_rev}

    def _publish_queue(self, m, new_queue, current_pid):
        """Publish a rebuilt queue and re-point seq/has_next/has_prev at the
        still-playing track. Never mutate the queue in place — the finished
        callback reads queue/seq/has_next lock-free on other threads."""
        m.publish_queue(new_queue, next((i for i, q in enumerate(new_queue) if q.get('PlaylistItemId') == current_pid), 0))
        m.has_next = m.seq < len(new_queue) - 1
        m.has_prev = m.seq > 0

    @synchronous('_lock')
    def queue_remove_many(self, playlist_item_ids):
        """Drop the given queue entries (never the currently-playing one)."""
        video = self._video
        if video is None:
            return False
        m = video.parent
        current_pid = m.queue[m.seq].get('PlaylistItemId')
        drop = set(playlist_item_ids) - {current_pid}
        if not drop:
            return False
        new_queue = [q for q in m.queue if q.get('PlaylistItemId') not in drop]
        self._publish_queue(m, new_queue, current_pid)
        return True

    @synchronous('_lock')
    def replace_queue_locked(self, media, sp_items, seq):
        """``media.replace_queue`` under the player lock, for SyncPlay's queue
        update. It writes the queue and seq as two stores, and the readers
        that take this lock (get_queue, queue_reorder, queue_remove_many) read
        them as a pair. Accepted, not covered: the progress report reads the
        pair under ``_tl_lock`` only (owner, 2026-10-17)."""
        return media.replace_queue(sp_items, seq)

    @synchronous('_lock')
    def queue_shuffle_upcoming(self):
        """Shuffle the entries after the playing one; the playing entry and
        everything before it stay put. Decided here, from what is playing
        now: the queue page's own copy of the playing entry went stale on
        the first track change. False when fewer than two are left."""
        video = self._video
        if video is None:
            return False
        m = video.parent
        upcoming = list(m.queue[m.seq + 1:])
        if len(upcoming) < 2:
            return False
        random.shuffle(upcoming)
        current_pid = m.queue[m.seq].get('PlaylistItemId')
        self._publish_queue(m, m.queue[:m.seq + 1] + upcoming, current_pid)
        return True

    @synchronous('_lock')
    def queue_reorder(self, ordered_playlist_item_ids):
        """Rebuild the queue to match the given PlaylistItemId order (the
        browser computes it for block Top/Up/Down/Bottom moves), keeping seq on
        the still-playing track. Any entry the browser didn't list is appended
        so the queue can never lose tracks."""
        video = self._video
        if video is None:
            return False
        m = video.parent
        by_pid = {q.get('PlaylistItemId'): q for q in m.queue}
        listed = set(ordered_playlist_item_ids)
        new_queue = [by_pid[p] for p in ordered_playlist_item_ids if p in by_pid]
        new_queue += [q for q in m.queue if q.get('PlaylistItemId') not in listed]
        if not new_queue:
            return False
        current_pid = m.queue[m.seq].get('PlaylistItemId')
        self._publish_queue(m, new_queue, current_pid)
        return True

    @synchronous('_lock')
    def try_skip_within_queue(self, item_ids, start_index):
        """Fast path for clicking another track in the CURRENTLY-PLAYING queue:
        seek within the existing queue instead of rebuilding it (and re-opening
        a whole new play session for the same list). Returns True if handled,
        False to fall back to a normal start_playback."""
        video = self._video
        if video is None:
            return False
        try:
            if self._player.playback_abort:
                return False
        except _mpv_errors:
            return False
        queue = video.parent.queue
        if [q.get('Id') for q in queue] != list(item_ids):
            return False
        if not 0 <= start_index < len(queue):
            return False
        target_id = queue[start_index].get('Id')
        if target_id == video.item_id:
            return True
        return bool(self.skip_to(target_id))

    @synchronous('_lock')
    def set_repeat(self, mode):
        """Repeat mode for the music bar: 'none' | 'all' | 'one'. 'one' loops
        the current file in mpv; 'all' wraps the queue at the end (handled in
        finished_callback); 'none' is the default. Repeat is a MUSIC feature:
        loop-file is applied only while audio plays (and re-applied per track in
        _play_media) so it never makes a video loop."""
        if mode not in ('none', 'all', 'one'):
            return
        self.repeat_mode = mode
        try:
            self._player.loop_file = 'inf' if mode == 'one' and self._current_is_audio() else 'no'
        except _mpv_errors:
            self._handle_mpv_disconnect()
        self.push_playstate()

    @synchronous('_lock')
    def toggle_current_favorite(self):
        """Flip the now-playing item's favorite state (music bar heart)."""
        video = self._video
        if video is None or video.client is None:
            return
        item = getattr(video, 'item', None)
        if item is None:
            return
        ud = item.setdefault('UserData', {})
        new_state = not ud.get('IsFavorite')
        try:
            video.client.jellyfin.favorite(video.item_id, new_state)
            ud['IsFavorite'] = new_state
        except Exception:
            log.error('Failed to toggle favorite for %s', video.item_id, exc_info=True)
        self.push_playstate()

    @staticmethod
    def _safe_title(video):
        try:
            return video.get_proper_title()
        except Exception:
            return ''

    def _notify_load_start(self, video):
        """Tell the UI a load is in flight. Best-effort and never fatal — a
        broken UI hook must not stop playback from starting."""
        cb = self.on_load_start
        if cb is None:
            return
        try:
            cb({'title': self._safe_title(video)})
        except Exception:
            log.error('on_load_start handler failed.', exc_info=True)

    def _notify_load_error(self, video, detail, timed_out: bool, owns_window=None):
        """Report a failed start to the UI, with what a retry could change.

        ``can_transcode`` gates the "retry with transcode" option: it's only
        worth offering when we did NOT already transcode, since re-requesting
        the same transcode would just fail the same way.
        """
        cb = self.on_load_error
        if cb is None:
            return
        try:
            already_transcoding = bool(getattr(video, 'is_transcode', False))
            cb({'title': self._safe_title(video), 'detail': detail, 'timed_out': timed_out, 'can_transcode': not already_transcoding, **({} if owns_window is None else {'owns_window': owns_window})})
        except Exception:
            log.error('on_load_error handler failed.', exc_info=True)

    def _abort_load(self):
        """Tell mpv to drop a start that never completed.

        stop() cannot do this: it is written for stopping a PLAYING item and
        early-returns on `not self._video` — and _video is only assigned once
        the duration wait SUCCEEDS. So on a cancelled or failed start it
        returned without ever issuing `stop` to mpv, leaving the file to
        finish loading and start playing on its own. With the browse window's
        force_window/keep_open already applied, that surfaced as the video
        playing *behind the library*.
        """
        if not self._mpv_alive:
            return
        try:
            self._player.command('stop')
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        except Exception:
            log.debug('Could not stop mpv after an aborted start.', exc_info=True)
            return
        if self._browse_bg_deferred:
            self._browse_bg_deferred = False
            self._showing_browse_bg = True

    def cancel_load(self):
        """Abandon a playback start that is still in flight.

        Reuses the abort the end-file handler sets, so the duration wait gives
        up within a poll interval instead of running out playback_timeout --
        the case worth cancelling is mpv sitting on a stalled stream for 30s.
        The cancelled flag keeps the failure path silent: the user asked for
        this.

        Deliberately takes no lock, so it is safe from the UI thread while
        _play_media holds one. Returns whether a start was in flight.

        Gated on _start_in_progress, not _loading: _loading covers only the
        mpv-side duration wait, but the start begins one PlaybackInfo round
        trip earlier -- and the spinner carrying this Cancel has been up since
        the click.
        """
        if not self._start_in_progress:
            return False
        log.info('Cancelling playback start.')
        self._load_cancelled = True
        self._load_failed.set()
        return True

    def retry_failed_playback(self, force_transcode: bool=False):
        """Re-attempt the start that just failed. Returns whether one was queued.

        Safe to call from the UI thread: the replay is queued onto the action
        thread rather than run here, because play() takes _lock and blocks for
        the whole load — doing that on the browser's loop thread would freeze
        the very dialog the user just clicked.
        """
        failed = self._failed_playback
        if failed is None:
            log.warning('Retry requested with no failed playback to retry.')
            return False
        video, offset = failed
        self._failed_playback = None
        if force_transcode:
            try:
                video.set_trs_override(None, True)
            except Exception:
                log.error('Could not force transcode for retry.', exc_info=True)
        self.put_task(lambda: self.play(video, offset, apply_memory=False))
        return True

    @synchronous('_lock')
    def restart_playback(self):
        video = self._video
        if not video:
            return False
        current_time = self._player.playback_time
        self.play(video, current_time, apply_memory=False)
        return True

    @synchronous('_lock')
    def get_video_attr(self, attr: str, default=None):
        if self._video:
            return self._video.get_video_attr(attr, default)
        return default

    def _capture_track_memory(self, video):
        self._track_memory = (video.media_source or {}, video.aid, video.sid)

    def _apply_remembered_tracks(self, video):
        """Carry the previous episode's audio/subtitle choice into this one,
        matching by language/title/codec/position (jellyfin-web heuristic).

        One step of a four-step chain; `docs/track-selection.md` has the whole
        table and `tests/test_track_truth_table.py` enforces it.
        """
        prev_source, prev_aid, prev_sid = self._track_memory
        streams = (video.source_for_track_rules() or {}).get('MediaStreams') or []
        if settings.remember_audio_track and prev_aid is not None:
            match = _rank_stream(prev_source, prev_aid, streams, 'Audio')
            if match is not None:
                video.aid = match
        if settings.remember_subtitle_track:
            if prev_sid == -1:
                video.sid = -1
            elif prev_sid is not None:
                match = _rank_stream(prev_source, prev_sid, streams, 'Subtitle')
                if match is not None:
                    video.sid = match

    @synchronous('_lock')
    def configure_streams(self):
        video = self._video
        if not video:
            return
        audio_uid = video.aid
        sub_uid = video.sid
        if audio_uid is not None and (not video.is_transcode):
            log.info('PlayerManager::play selecting audio stream index=%s' % audio_uid)
            track = video.audio_seq.get(audio_uid)
            if track is None:
                log.warning("PlayerManager::audio index %s not in the stream map %s; leaving mpv's default track.", audio_uid, video.audio_seq)
            else:
                self._player.audio = track
        if sub_uid is None or sub_uid == -1:
            log.info('PlayerManager::play selecting subtitle stream (none)')
            self._player.sub = 'no'
        else:
            log.info('PlayerManager::play selecting subtitle stream index=%s (embedded map=%s external=%s)' % (sub_uid, video.subtitle_seq, list(video.subtitle_url)))
            if sub_uid in video.subtitle_seq:
                self._player.sub = video.subtitle_seq[sub_uid]
            elif sub_uid in video.subtitle_url:
                log.info('PlayerManager::play selecting external subtitle id=%s' % sub_uid)
                self.load_external_sub(sub_uid)
            else:
                log.warning('PlayerManager::subtitle index %s not in embedded or external maps; leaving current selection.', sub_uid)
        self._apply_secondary_subtitle()

    def _apply_secondary_subtitle(self):
        """Push the video's secondary-subtitle choice onto mpv's secondary-sid.

        Purely client-side: mpv renders it above the primary track, so it only
        applies to subtitles mpv has itself (embedded text, or an external file
        it can fetch) — a burn-in/transcode track can never be a secondary. The
        same track can't be shown twice, so a secondary that matches the primary
        is treated as off. Not @synchronous — always called under _lock."""
        video = self._video
        if video is None:
            return
        sec = getattr(video, 'secondary_sid', None)
        track = None
        if sec is not None and sec != -1 and (sec != video.sid):
            if sec in video.subtitle_seq:
                track = video.subtitle_seq[sec]
            elif sec in video.subtitle_url:
                track = self._ensure_external_sub(sec)
        try:
            self._player.secondary_sid = track if track is not None else 'no'
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            log.warning('PlayerManager::could not set secondary subtitle', exc_info=True)

    def _ensure_external_sub(self, sub_id: int):
        """mpv track id for a loaded external subtitle. Otherwise starts the
        load and returns None; the load applies the secondary when it lands."""
        if sub_id in self.external_subtitles:
            return self.external_subtitles[sub_id]
        self._add_external_sub(sub_id)
        return None

    def _add_external_sub(self, sub_id: int):
        """Hand mpv a sidecar without waiting for it, selecting nothing.

        Never a synchronous ``sub_add``: the server answers an embedded
        sidecar only after extracting every subtitle stream of the file, which
        can take minutes, and the add held a playback start paused on its
        first frame, under ``_lock``, for all of it (docs/track-selection.md
        section 7). What the sidecar is for is decided when it lands, in
        _external_sub_added. Not @synchronous -- always called under _lock."""
        video = self._video
        url = (getattr(video, 'subtitle_url', None) or {}).get(sub_id)
        if url is None or sub_id in self._pending_subs:
            return
        epoch = self._play_epoch
        self._pending_subs[sub_id] = epoch

        def landed(error, result):
            self.put_task(self._external_sub_added, epoch, sub_id, url, error)
        if settings.log_decisions:
            log.info('Load External Subtitle: {0}'.format(url))
        try:
            self._player.command_async('sub-add', url, 'auto', callback=landed)
        except Exception:
            self._pending_subs.pop(sub_id, None)
            log.warning('PlayerManager::could not load external subtitle %s', sub_id, exc_info=True)

    @synchronous('_lock')
    def _external_sub_added(self, epoch, sub_id, url, error):
        """A sidecar load finished. Selected only if it is still wanted: the
        user may have picked something else while it loaded."""
        if epoch != self._play_epoch:
            return
        self._pending_subs.pop(sub_id, None)
        if error is not None:
            log.warning('PlayerManager::external subtitle %s did not load: %s', sub_id, error)
            return
        track = self._external_track_id(url)
        if track is None:
            log.warning("PlayerManager::external subtitle %s loaded but is not in mpv's track list", sub_id)
            return
        self.external_subtitles[sub_id] = track
        video = self._video
        if video is None:
            return
        if video.sid == sub_id:
            self._player.sub = track
        if getattr(video, 'secondary_sid', None) == sub_id:
            self._apply_secondary_subtitle()

    def _external_track_id(self, sub_url: str):
        """The mpv track id of a just-added external subtitle, matched back by
        the filename it was added with (sub_add doesn't return the id on either
        backend)."""
        try:
            tracks = self._player.track_list or []
        except Exception:
            return None
        for tr in tracks:
            if tr.get('type') == 'sub' and tr.get('external-filename') == sub_url:
                return tr.get('id')
        return None

    @synchronous('_lock')
    def set_secondary_subtitle(self, sub_uid: int):
        """Select (or, with -1/None, clear) the secondary subtitle track."""
        video = self._video
        if not video:
            return
        video.secondary_sid = None if sub_uid is None or sub_uid == -1 else sub_uid
        self._apply_secondary_subtitle()
        self.timeline_handle()

    @synchronous('_lock')
    def set_streams(self, audio_uid: int, sub_uid: int):
        video = self._video
        if not video:
            return
        need_restart = video.set_streams(audio_uid, sub_uid)
        if need_restart:
            self.restart_playback()
        else:
            self.configure_streams()
        self._capture_track_memory(self._video)
        self.timeline_handle()

    @synchronous('_lock')
    def load_external_sub(self, sub_id: int):
        if sub_id in self.external_subtitles:
            self._player.sub = self.external_subtitles[sub_id]
            return
        self._player.sub = 'no'
        self._add_external_sub(sub_id)

    @synchronous('_lock')
    def set_mute(self, mute):
        self._player.mute = mute

    @synchronous('_lock')
    def screenshot(self):
        self._player.screenshot()

    @synchronous('_lock')
    def set_paused(self, value: bool, force: bool=False):
        if self.syncplay.is_enabled() and (not force):
            if value:
                self.syncplay.pause_request()
            else:
                self.syncplay.play_request()
        else:
            self.pause_ignore = value
            self._player.pause = value
        self.push_playstate()

    def script_message(self, command, *args):
        if not self._mpv_alive:
            return
        try:
            self._player.command('script-message', command, *args)
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def get_track_ids(self):
        return (self._video.aid, self._video.sid)

    def update_subtitle_visuals(self, shared_setting=None):
        from .shared_playback import apply_subtitle_setting
        if apply_subtitle_setting(self, shared_setting):
            self.timeline_handle()
            return
        self._player.sub_pos = SUBTITLE_POS[settings.subtitle_position]
        self._player.sub_scale = settings.subtitle_size / 100
        self._player.sub_color = settings.subtitle_color
        self.timeline_handle()

    def _current_is_audio(self):
        """Whether the item now playing is audio. See :func:`_item_is_audio`.

        Anything asking this BEFORE `self._video` is assigned must call
        `_item_is_audio(video)` with the incoming item instead -- this answers
        about the outgoing one.
        """
        return _item_is_audio(self._video)

    def _maybe_save_volume(self):
        """Persist the current volume into its per-type bucket if it changed.
        Called from the timeline tick (off mpv's event thread), so a volume
        change made via the music bar OR mpv keys survives a restart without
        hammering the settings file."""
        if self._video is None:
            return
        try:
            vol = int(self._player.volume)
        except (_mpv_errors, TypeError):
            return
        key = 'music_volume' if self._current_is_audio() else 'video_volume'
        if getattr(settings, key) != vol:
            setattr(settings, key, vol)
            settings.save()
    STALL_FINISH_SECS = 20

    def _check_stalled_finish(self, video):
        """Whether playback has silently died at the end of a remote stream.

        A remote origin that stops delivering **without closing the
        connection** makes no statement at all: the demuxer blocks in read, so
        there is no end-file event and eof-reached stays False. With keep_open
        holding the last frame mid-queue that is indistinguishable from a
        normal hold -- the queue just stops forever.

        Deliberately requires the position to be at the END of the media, not
        merely frozen: a bare stall is far more likely to be rebuffering, and
        advancing through that would silently skip the rest of an episode.
        Items with no known duration therefore get no rescue.
        See docs/mpv-backends.md section 9.
        """
        if (video.media_source or {}).get('IsInfiniteStream'):
            return False
        try:
            if self._player.pause:
                return False
            position = self._player.playback_time
        except Exception:
            return False
        if position is None:
            return False
        now = time.time()
        if position != self._stall_position:
            self._stall_position = position
            self._stall_since = now
            return False
        if now - self._stall_since < self.STALL_FINISH_SECS:
            return False
        if not self._finished_at_eof(video, position):
            return False
        log.warning('Playback stalled at %.1fs at the end of the media without an end-of-file from mpv; treating as finished.', position)
        self._reached_eof = True
        return True

    def _finished_at_eof(self, video, playback_time=None):
        """Whether the playback that just ended genuinely reached the end.

        eof-reached only fires while keep_open holds the finished file, and
        keep_open is only set when there is a next item — so the last item in
        a queue ends via playback-abort alone. Accept a last known position
        at/near the duration as a genuine finish too; a mid-file decode or
        network abort stays far from the end and is not counted. The margin
        (95% or within 10s) absorbs the timeline tick interval and metadata
        duration drift."""
        if self._reached_eof:
            return True
        duration = video.get_duration()
        if not duration:
            return False
        position = max(playback_time or 0, self._last_playback_position)
        return position >= duration * 0.95 or duration - position <= 10

    def _ensure_mpv(self):
        """Re-create the mpv process if it is not running — closed by the user,
        crashed, or quit while idle (mpv_idle_quit). The play path's way in,
        so a cast/remote Play transparently re-opens a fresh window; the tray
        and the OSD menu come in through `set_browse_window` and
        `force_window`. Every re-open goes through `_reopen_mpv`."""
        if not self._mpv_alive:
            wlog.info('mpv is not running; re-creating it for playback')
            self._reopen_mpv()

    @synchronous('_lock')
    def idle_quit(self, reason='Idle timeout reached'):
        """Quit mpv while idle to free the window / GPU context / memory
        (opt-in via mpv_idle_quit). Re-created on the next play. Gated hard so
        it never fires while anything still needs the window.

        Also the minimize path on an mpv that cannot drop force-window at
        runtime, which is what ``reason`` distinguishes in the log."""
        if not self._mpv_alive or self._video is not None:
            return
        if self.menu.is_menu_shown or self.syncplay.is_enabled():
            return
        if self.mpvtk_active:
            return
        if is_using_ext_mpv and (not settings.mpv_ext_start):
            return
        wlog.info('QUIT: %s (video=%d mpvtk=%d)', reason, getattr(self, '_video', None) is not None, bool(getattr(self, 'mpvtk_active', False)))
        self.should_send_timeline = False
        player = self._player
        self._teardown_player()
        self._mark_mpv_dead()
        self._terminate_thread = Thread(target=self._terminate_mpv, args=(player,), daemon=True)
        self._terminate_thread.start()
        self._notify_mpv_gone()

    def _on_ipc_closed(self, handle):
        """The external mpv's socket closed. B7.

        mpv's "shutdown" event is not guaranteed to reach an IPC client
        before the socket goes: measured, a window closed after a download or
        a playback delivered none, and nothing else told the app, which then
        stayed up with no window (and, without a tray, unreachable). The
        socket closing is the one certain signal, so it runs the handle's own
        "shutdown" handlers -- the player's and the in-window browser's -- if
        the event did not. Directly, not through the library's event thread:
        that thread is stopped as soon as this returns.

        Not for a handle that has already been replaced, or once the event
        itself arrived. Our own terminate() reaches here too -- the library
        calls quit_callback for every close, and nothing it passes tells the
        two apart -- so every close of ours marks the handle first
        (_mark_own_close), and only an outside close runs the handlers.
        """
        if handle is not self._player:
            return
        if _shutdown_seen(handle):
            return
        log.info("mpv's socket closed without a shutdown event; treating it as one")
        for callback in list(getattr(handle, 'event_bindings', {}).get('shutdown', ())):
            try:
                callback(None)
            except Exception:
                log.debug('a shutdown handler raised', exc_info=True)

    def _handle_mpv_disconnect(self):
        if not self._mark_mpv_dead():
            return
        wlog.info('connection LOST; dead until the next play')
        self.should_send_timeline = False
        video = self._video
        if self._end_playback():
            self.put_task(self.exec_stop_cmd)
        self._terminate_thread = Thread(target=self._terminate_mpv, args=(self._player,), daemon=True)
        self._terminate_thread.start()
        self._notify_mpv_gone()
        if video:
            Thread(target=self._report_stopped_offline, args=(video,), daemon=True).start()

    def _handle_mpv_shutdown(self):
        video = self._video
        owed = self._end_playback()
        if video:
            Thread(target=self._report_stopped_offline, args=(video,), daemon=True).start()
        if owed:
            self.exec_stop_cmd()

    def _terminate_mpv(self, player=None):
        wlog.info('terminating the mpv instance')
        if player is None:
            player = self._player
        self._mark_mpv_dead(player)
        _mark_own_close(player)
        try:
            player.terminate()
        except Exception:
            log.debug('Error terminating mpv', exc_info=True)
        log.info('mpv instance terminated')
        self._notify_mpv_terminated()

    def terminate(self):
        self._save_window_geometry()
        if self.syncplay.in_group():
            self.syncplay.disable_sync_play(False)
        self.stop()
        self._reporter.stop()
        if is_using_ext_mpv:
            _mark_own_close(self._player)
            self._player.terminate()
        if self.trickplay:
            self.trickplay.stop()

    def get_seek_times(self):
        if self._jf_settings is None:
            if self._video.client is None:
                return (-15.0, 30.0)
            self._jf_settings = self._video.client.jellyfin.get_user_settings()
        custom_prefs = self._jf_settings.get('CustomPrefs') or {}
        seek_left = custom_prefs.get('skipBackLength') or 15000
        seek_right = custom_prefs.get('skipForwardLength') or 30000
        return (-int(seek_left) / 1000, int(seek_right) / 1000)

    def is_active(self):
        return bool(self._player and self._video)

    def is_playing(self):
        try:
            return bool(self._video and (not self._player.playback_abort))
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return False

    def is_not_paused(self):
        try:
            return bool(self._video and (not self._player.playback_abort) and (not self._player.pause))
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return False

    def has_video(self):
        return self._video is not None

    def get_video(self):
        return self._video

    def get_mpv(self):
        """The raw mpv handle, so the in-window UI (mpvtk) can attach to
        the same window used for playback instead of opening its own.
        Pair with the module-level ``is_using_ext_mpv`` flag, which tells
        the UI whether it's an external jsonipc process or in-process
        libmpv. See mpvtk.app.MpvtkApp.attach."""
        return self._player

    def show_text(self, text: str, duration: int, level: int=1):
        if not self._mpv_alive:
            return
        try:
            self._player.show_text(text, str(duration), level)
        except _mpv_errors:
            self._handle_mpv_disconnect()
    _default_osd_back_color = '#C8000000'
    _default_osd_font_size = 55

    def get_osd_settings(self):
        if not self._mpv_alive:
            return (self._default_osd_back_color, self._default_osd_font_size, None)
        try:
            try:
                border_style = self._player.osd_border_style
            except Exception:
                border_style = None
            return (self._player.osd_back_color or self._default_osd_back_color, self._player.osd_font_size or self._default_osd_font_size, border_style)
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return (self._default_osd_back_color, self._default_osd_font_size, None)

    def set_osd_settings(self, back_color: str, font_size: int, border_style=None):
        if not self._mpv_alive:
            return
        try:
            self._player.osd_back_color = back_color
            self._player.osd_font_size = font_size
            try:
                self._player.osd_border_style = border_style or 'outline-and-shadow'
            except Exception:
                pass
        except _mpv_errors:
            self._handle_mpv_disconnect()

    @property
    def osc_enabled(self):
        """Whether this player is supposed to have on-screen controls at all.

        The style says so: "none" is the option for no controls, replacing
        the old enable_osc switch, which only ever reached mpv's own OSC and
        so did nothing under the default style (#615). Callers that hide the
        controls temporarily -- the OSD menu -- restore to this rather than
        to a setting of their own.
        """
        return getattr(self, '_osc_style_resolved', None) != 'none'

    def enable_osc(self, enabled: bool):
        if settings.mpv_ext and settings.mpv_ext_no_ovr:
            return
        if not self._mpv_alive:
            return
        style = getattr(self, '_osc_style_resolved', None)
        try:
            self.script_message('osc-idlescreen', 'no', 'False')
            if not enabled:
                self.script_message('osc-visibility', 'never', 'False')
                self._osc_suppressed = True
            elif self._osc_suppressed:
                self.script_message('osc-visibility', 'auto', 'False')
                self._osc_suppressed = False
            if style in REPLACES_OSC and hasattr(self._player, 'osc'):
                self._player.osc = False
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def triggered_menu(self, enabled: bool):
        self.script_message('shim-menu-enable', 'True' if enabled else 'False')

    def playback_is_aborted(self):
        try:
            return self._player.playback_abort
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return True

    def add_ipc(self, ipc_name: str):
        self._player.input_ipc_server = ipc_name

    def get_current_client(self):
        return self._video.client

    def get_time(self):
        return self._player.playback_time

    def get_speed(self):
        return self._player.speed

    def set_speed(self, speed: float):
        self._player.speed = speed
    _DEFAULT_SEEK = {'up': (60, False), 'down': (-60, False), 'right': (5, False), 'left': (-5, False)}

    def _seek_like_the_keyboard(self, action):
        """``(amount, exact)`` that pressing ``action`` on the keyboard
        would seek by, right now, on this mpv.

        **Asked of mpv, not of a setting.** The seek distances left the
        config in version 4, so the only place a distance lives is the
        user's input.conf -- and the keyboard reads it from there whether
        the key is mpv's own or one the shim claimed. A remote that kept
        its own number would seek a different distance from the arrow key
        beside it on the same machine, which is what it did while
        `settings.seek_*` still existed and the keyboard had stopped
        reading them.
        """
        from . import keysweep
        try:
            for key, semantic, arg in self._swept_keys():
                if semantic != keysweep.SEEK or key.lower() != action.lower():
                    continue
                if isinstance(arg, tuple) and len(arg) == 2:
                    return arg
        except Exception:
            log.debug("could not read mpv's seek binding for %r", action, exc_info=True)
        return self._DEFAULT_SEEK[action]

    def kb_seek(self, action):
        if action not in self._DEFAULT_SEEK:
            self.menu.menu_action(action)
            return
        amount, exact = self._seek_like_the_keyboard(action)
        if settings.use_web_seek:
            back, forward = self.get_seek_times()
            amount = forward if amount > 0 else back
        self.seek(amount, exact=exact)
    _NAV_KEYPRESS = {'up': 'UP', 'down': 'DOWN', 'left': 'LEFT', 'right': 'RIGHT', 'back': 'ESC', 'menu': 'MENU'}
    _NAV_COMMANDS = ('home', 'settings', 'search')
    _NAV_COMMANDS_WHILE_PLAYING = ('home',)
    _MENU_ALIAS = {'settings': 'home'}

    def _nav_command(self, action):
        handler = self.on_nav_command
        if handler is None or not self.mpvtk_active:
            return False
        if not self._library_showing() and action not in self._NAV_COMMANDS_WHILE_PLAYING:
            return False
        try:
            return bool(handler(action))
        except Exception:
            log.debug('nav command %r failed', action, exc_info=True)
            return False

    def _nav_back(self):
        handler = self.on_nav_back
        if handler is None or not self.mpvtk_active or (not self._library_showing()):
            return False
        try:
            return bool(handler())
        except Exception:
            log.debug('nav back handler failed', exc_info=True)
            return False

    def _mpvtk_userdata(self, prop):
        if not self.mpvtk_active or self._player is None:
            return False
        try:
            if is_using_ext_mpv:
                return bool(self._player.command('get_property', prop))
            return bool(self._player._get_property(prop))
        except Exception:
            return False

    def _mpvtk_input_active(self):
        """True while the in-window UI's key bindings are live (the
        renderer mirrors it into user-data on every transition)."""
        return self._mpvtk_userdata('user-data/mpvtk/active')

    def _library_showing(self):
        """Whether the library is the thing on screen.

        A video PICTURE is what takes it away; **music does not**. Audio
        keeps `_video` set and keeps the browser up — that is what the
        now-playing bar is for — so `_video is None` answers "is the
        library up?" with a no while the user is looking straight at it.
        Same pairing as `_stats_key` and `_play_media`, for the same
        reason.
        """
        return self._video is None or self._current_is_audio()

    def _video_on_screen(self):
        """The complement of :meth:`_library_showing`, by name.

        Both meanings were spelled `self._video`, so no reader could tell
        which one a site meant without deriving it -- and three sites got it
        wrong that way. Derived from the other, never a second answer to the
        same question.
        """
        return not self._library_showing()

    def _library_has_input(self):
        """The *library* owns input — not merely the renderer.

        A summoned playback HUD owns input too (same bindings, same
        user-data flag: it is what lets a remote's arrows drive the HUD),
        so `_mpvtk_input_active` alone answers this with a yes over a
        playing video. Anything that means one thing in a library and
        another in a player has to ask here.
        """
        return self._library_showing() and self._mpvtk_input_active()

    def _mpvtk_hud_idle(self):
        """True while the playback HUD is attached but hidden: remote
        Move*/Select should reach the renderer's summon bindings (the
        first press shows the HUD) instead of acting as seek keys.
        Back keeps its stop-to-browser meaning while hidden."""
        return self._mpvtk_userdata('user-data/mpvtk/hud')

    def menu_action(self, action):
        if action == 'search':
            self._nav_command('search')
            return
        if action == 'menu' and (not self._library_has_input()):
            self.toggle_settings_menu()
            return
        if self.menu.is_menu_shown:
            self.menu.menu_action(self._MENU_ALIAS.get(action, action))
        elif action in self._NAV_COMMANDS and self._nav_command(action):
            pass
        elif (action in self._NAV_KEYPRESS or action == 'ok') and self._mpvtk_input_active():
            try:
                self._player.command('keypress', self._NAV_KEYPRESS.get(action) or conf.select_key())
            except Exception:
                log.debug('nav keypress failed', exc_info=True)
        elif action == 'settings':
            self.toggle_settings_menu()
        elif action in ('up', 'down', 'left', 'right', 'ok') and self._mpvtk_hud_idle():
            try:
                self.script_message('mpvtk-hud-summon', 'select' if action == 'ok' else 'nav')
            except Exception:
                log.debug('hud summon failed', exc_info=True)
        else:
            self.kb_seek(self._MENU_ALIAS.get(action, action))
playerManager = PlayerManager()

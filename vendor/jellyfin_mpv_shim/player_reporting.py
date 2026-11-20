"""Telling the outside world what is playing.

Two audiences, one job. Jellyfin gets session/progress/stop reports over the
network; the browser's now-playing bar gets a compact snapshot through the
``on_playstate`` callback. Both are *outbound publication*, both run on
threads that are not the one driving playback, and both share the rule that a
report must never disturb what it is reporting on.

**Its own lock.** ``_tl_lock``, not ``_lock``. Server round trips happen under
it, and ``_lock`` is held for the whole of a playback start -- sharing one
would put a progress post behind a load and a load behind a progress post.
The reports themselves are handed to ``SessionReporter`` rather than sent
inline, because they used to sit on the advance path between episodes.

Never drain the reporter while holding ``_tl_lock``: ``_session_playing_safe``
takes it on the worker. The ABBA inversion between ``_lock`` and ``_tl_lock``
that this module is one half of is written up in
``docs/archive/SYNCPLAY_FINDINGS.md``.

**This module deliberately does not touch ``pause_ignore``** -- see the
comment in ``get_timeline_options``, which sits on the line that used to.

Before editing this file, read ``docs/mpv-backends.md``.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import time
from typing import TYPE_CHECKING, Any, Optional
from .books import AUDIOBOOK_TYPE
from .i18n import _
from .media import segment_labels
from .utils import item_is_audio, none_fallback, synchronous
log = logging.getLogger('player')

def _queue_len(video):
    """How many entries the playing queue holds, or 1 when it cannot be
    read. Never raises: this is on the publication path, where nothing may
    disturb what it is reporting on -- and 1 is the answer that changes
    nothing about the bar, so an unreadable queue keeps every control.
    """
    try:
        return len(video.parent.queue)
    except Exception:
        return 1

def _server_uuid_of(video):
    """The uuid of the server a playing item came from, or None."""
    try:
        from .clients import clientManager
        return clientManager.uuid_for_client(getattr(video, 'client', None))
    except Exception:
        log.debug("could not resolve the playing item's server", exc_info=True)
    return None

def _mpris_on():
    """Whether MPRIS is running."""
    try:
        from . import mpris
        return mpris._service is not None
    except Exception:
        return False

def _discord_on():
    """Whether Discord Rich Presence came up.

    The flag is player.py's -- it is set there only if the rich_presence
    import succeeded -- so inside a true branch the hooks are importable.
    Read per call rather than bound at import, for the same reason the
    backend globals are (see player_audio).
    """
    from .player import discord_presence
    return discord_presence

class ReportingMixin:
    """Session reporting and now-playing publication for ``PlayerManager``."""
    on_stopped_at = None
    if TYPE_CHECKING:
        _player: Any
        _video: Any
        _reporter: Any
        _session_ready: Any
        _hud_skip: Any
        syncplay: Any
        start_time: Optional[float]
        repeat_mode: str
        on_playstate: Any
        last_seek: Any
        should_send_timeline: bool
        _last_playback_position: Any
        _last_offline_record: float

        def _finished_at_eof(self, video, playback_time) -> bool:
            ...

        def _handle_mpv_disconnect(self) -> None:
            ...

    def push_playstate(self, stopped=False):
        """Feed the browser's now-playing bar a compact snapshot on each
        playback state change. Never raises and never takes MPV's lock itself —
        a bar refresh must never disturb playback. During a re-open its callback
        can wait on `_lock` for the build (docs/mpv-backends.md section 8). A
        ``stopped`` payload tells the bar to hide."""
        from .player import _mpv_errors
        bar = self.on_playstate
        mpris_on = _mpris_on()
        if bar is None and (not mpris_on):
            return
        video = None

        def cb(payload):
            if bar is not None:
                bar(payload)
            if mpris_on:
                from . import mpris
                mpris.publish(payload, video)
        try:
            video = self._video
            try:
                aborted = self._player.playback_abort
            except _mpv_errors:
                aborted = True
            if not stopped and self._start_in_progress:
                return
            if stopped or video is None or aborted:
                cb({'stopped': True})
                return
            item = getattr(video, 'item', None) or {}
            try:
                pos = self._player.playback_time
                duration = self._player.duration
                paused = self._player.pause
                volume = self._player.volume
                muted = self._player.mute
                fullscreen = getattr(self._player, 'fullscreen', None)
            except _mpv_errors:
                cb({'stopped': True})
                return
            ranges = None
            try:
                cache = self._player.demuxer_cache_state
                if cache:
                    ranges = [[float(r['start']), float(r['end'])] for r in cache.get('seekable-ranges') or []]
            except Exception:
                ranges = None
            chapters = []
            try:
                for chapter in self._player.chapter_list or []:
                    chapters.append({'title': chapter.get('title') or '', 'time': float(chapter.get('time') or 0.0)})
            except Exception:
                chapters = []
            skip = self._hud_skip
            cb({'stopped': False, 'is_audio': item_is_audio(item), 'is_audiobook': item.get('Type') == AUDIOBOOK_TYPE, 'queue_len': _queue_len(video), 'queue_rev': getattr(getattr(video, 'parent', None), 'queue_rev', None), 'is_photo': item.get('Type') == 'Photo', 'skip_label': segment_labels(skip.type)[0] if skip is not None else None, 'id': getattr(video, 'item_id', None), 'server_uuid': _server_uuid_of(video), 'title': item.get('Name') or '', 'series_name': item.get('SeriesName') or '' if item.get('Type') == 'Episode' else '', 'season': item.get('ParentIndexNumber') if item.get('Type') == 'Episode' else None, 'episode': item.get('IndexNumber') if item.get('Type') == 'Episode' else None, 'artist': ', '.join(item.get('Artists') or []), 'album': item.get('Album') or '', 'position': float(pos) if pos is not None else 0.0, 'duration': float(duration) if duration is not None else float(video.get_duration() or 0.0), 'paused': bool(paused), 'volume': int(volume) if volume is not None else 100, 'muted': bool(muted), 'favorite': bool((item.get('UserData') or {}).get('IsFavorite')), 'repeat': self.repeat_mode, 'fullscreen': bool(fullscreen), 'ranges': ranges, 'chapters': chapters})
        except Exception:
            log.debug('push_playstate failed', exc_info=True)

    def get_timeline_options(self, finished=False, video=None):
        from .player import _mpv_errors
        if video is None:
            video = self._video
        if video is None:
            return None
        if getattr(video, 'is_photo', False) or video.playback_info is None:
            return None
        player = self._player
        try:
            volume = player.volume
            mute = player.mute
            pause = player.pause
            duration = player.duration
            cache_buffering = player.cache_buffering_state
            playback_time = player.playback_time
        except _mpv_errors:
            volume = mute = pause = duration = cache_buffering = playback_time = None
        if playback_time is not None:
            self._last_playback_position = playback_time
        if finished and self._finished_at_eof(video, playback_time):
            safe_pos = video.get_duration() or playback_time or self._last_playback_position or 0
        elif finished:
            if playback_time is None:
                safe_pos = self._last_playback_position
            else:
                safe_pos = playback_time
        else:
            safe_pos = playback_time or 0
        self.last_seek = safe_pos
        options = {'VolumeLevel': int(none_fallback(volume, 100)), 'IsMuted': mute, 'IsPaused': pause, 'RepeatMode': {'all': 'RepeatAll', 'one': 'RepeatOne'}.get(self.repeat_mode, 'RepeatNone'), 'PositionTicks': int(safe_pos * 10000000), 'PlaybackStartTimeTicks': int(self.start_time * 10000000), 'SubtitleStreamIndex': none_fallback(video.sid, -1), 'AudioStreamIndex': none_fallback(video.aid, -1), 'BufferedRanges': [], 'PlayMethod': 'Transcode' if video.is_transcode else 'DirectPlay', 'PlaySessionId': video.playback_info['PlaySessionId'], 'PlaylistItemId': video.get_playlist_id(), 'MediaSourceId': video.media_source['Id'], 'CanSeek': True, 'ItemId': video.item_id, 'NowPlayingQueue': video.parent.queue}
        if duration is not None:
            options['BufferedRanges'] = [{'start': int(safe_pos * 10000000), 'end': int((duration - safe_pos * none_fallback(cache_buffering, 0) / 100 + safe_pos) * 10000000)}]
        if _discord_on():
            try:
                from .rich_presence import send_presence
                if video.is_tv and video.item.get('IndexNumber') is not None and (video.item.get('ParentIndexNumber') is not None):
                    title = video.item.get('SeriesName')
                    subtitle = _('Season {0} - Episode {1}').format(video.item.get('ParentIndexNumber'), video.item.get('IndexNumber'))
                else:
                    title = video.item.get('Name')
                    subtitle = str(video.item.get('ProductionYear', ''))
                send_presence(title, subtitle, playback_time, duration, not pause, self.syncplay.current_group, video.item.get('Type'))
            except Exception:
                log.error('Could not send Discord Rich Presence.', exc_info=True)
        return options

    @synchronous('_tl_lock')
    def send_timeline(self):
        from .player import _mpv_errors
        video = self._video
        try:
            if self.should_send_timeline and video and (not self._player.playback_abort):
                if video.client is not None:
                    if not self._session_ready.is_set():
                        return
                    options = self.get_timeline_options(video=video)
                    if options is not None:
                        video.client.jellyfin.session_progress(options)
                    try:
                        if self.syncplay.is_enabled():
                            self.syncplay.sync_playback_time()
                    except:
                        log.error('Error syncing playback time.', exc_info=True)
                now = time.monotonic()
                if now - self._last_offline_record >= 30:
                    options = self.get_timeline_options(video=video)
                    if options is not None:
                        self._last_offline_record = now
                        self._record_progress(video, options.get('PositionTicks'))
        except _mpv_errors:
            log.warning('MPV connection lost during timeline update.')
            self._handle_mpv_disconnect()

    @synchronous('_tl_lock')
    def _session_playing_safe(self, client, options):
        try:
            client.jellyfin.session_playing(options)
        except Exception:
            log.debug('session_playing failed', exc_info=True)
        finally:
            self._session_ready.set()

    def _record_progress(self, video, position_ticks, finished=False):
        """Keep this position where the app can still read it offline.

        One helper for the three reporting paths, because the thing that
        went wrong here twice is a call site: first all three were gated on
        being *offline*, so the method that had just been made to write
        either way was never called when there was a server; then all three
        were gated on the video being an `OfflineVideo`, so streaming an
        episode you also had downloaded wrote nothing and the copy on disk
        never learned you had watched it.

        So the gate is gone rather than corrected. Both branches below are
        no-ops for an item this app holds no download of, and neither
        cares whether a server is reachable:

        * an `OfflineVideo` keeps `record_offline_progress`, which also owns
          the *replay queue* half -- the list of changes the server has not
          been told about, and still the one part that is offline-only;
        * anything else mirrors into the catalog by item id, which
          `db.update_userdata` answers False for when there is no row.

        Never raises: this runs on the timeline thread and on a daemon
        thread during teardown, and losing a resume position must not take
        a stop report down with it.
        """
        try:
            if hasattr(video, 'record_offline_progress'):
                video.record_offline_progress(position_ticks, finished)
                return
            from .sync.manager import syncManager
            syncManager.mirror_playstate(getattr(video, 'item_id', None), position_ticks, played=True if finished else None, server_uuid=_server_uuid_of(video))
        except Exception:
            log.warning('Could not record playback progress locally.', exc_info=True)

    def send_timeline_initial(self):
        video = self._video
        if video is None or video.client is None:
            self._session_ready.set()
            return
        options = self.get_timeline_options(video=video)
        if options is None:
            self._session_ready.set()
            return
        self._session_ready.clear()
        self._reporter.submit(lambda: self._session_playing_safe(video.client, options), 'session_playing')

    @synchronous('_tl_lock')
    def send_timeline_stopped(self, finished=False, options=None, client=None, stopped=None):
        """``stopped`` is the video an explicit stop has already cleared from
        `_video`: only the UI announcement uses it (`_announce_stop`)."""
        self.should_send_timeline = False
        video = self._video
        announced = video if stopped is None else stopped
        if options is None:
            options = self.get_timeline_options(finished, video=video)
        if video is not None and options is not None:
            self._record_progress(video, options.get('PositionTicks'), finished)
        if client is None:
            client = video.client if video else None
        if announced is not None and options is not None:
            self._announce_stop(announced, options.get('PositionTicks'), finished, delivered=client is None)
        if options is not None and client is not None:
            ticks = options.get('PositionTicks')

            def stop_then_announce():
                client.jellyfin.session_stop(options)
                if announced is not None:
                    self._announce_stop(announced, ticks, finished, delivered=True)
            self._reporter.submit(stop_then_announce, 'session_stop')
        if _discord_on():
            try:
                from .rich_presence import clear_presence
                clear_presence()
            except Exception:
                log.error('Could not clear Discord Rich Presence.', exc_info=True)

    def _announce_stop(self, video, ticks, finished, delivered):
        """Tell the UI where this stop left the item. Never raises, and the
        callback must not block or call back in: this can run under the
        player's lock (docs/browser-shell.md on the thread contract)."""
        cb = self.on_stopped_at
        if cb is None:
            return
        try:
            from .clients import clientManager
            cb({'id': video.item_id, 'ticks': ticks, 'finished': finished, 'delivered': delivered, 'server': clientManager.uuid_for_client(video.client)})
        except Exception:
            log.debug('could not announce a stop to the UI', exc_info=True)

    def queue_played_mark(self, video, watched: bool=True):
        """Send an explicit watched/unwatched mark, BEHIND whatever is queued.

        The player's deliberate marks -- "Quit and Mark Unwatched", "Mark
        Watched and Skip", the force_set_played finish -- are each preceded by
        a stop report, and the stop must reach the server first: it carries a
        position, and a stop landing after the mark overwrites a fully-watched
        item with mid-episode progress, or restores a resume point on the item
        the user just marked unwatched.

        That used to be free, because both were synchronous and the call order
        was the delivery order. Once `session_stop` moved onto this FIFO and
        `set_played` stayed inline, the Python order stopped meaning anything:
        with a slow report already queued -- or an unreachable server -- the
        mark went first. Submitting it here puts it back in the same queue, so
        the ordering is the FIFO's rather than a coincidence of timing.
        """
        if video is None:
            return

        def mark():
            try:
                video.set_played(watched)
            except Exception:
                log.warning('Could not mark %s as %s.', getattr(video, 'item_id', '?'), 'played' if watched else 'unplayed', exc_info=True)
        self._reporter.submit(mark, 'item_played')

    def release_stream(self, video):
        """Free ``video``'s server-side stream without blocking the caller.

        ``terminate_transcode`` is one or two blocking HTTP calls, and every
        teardown that reaches it is on a path the window is waiting on:
        ``stop()`` runs inline on the browser's loop thread whenever the
        player's lock happens to be free (run_action's fast path), and
        ``finished_callback`` holds the browser in the finished video until it
        returns. Against a server that is unreachable rather than refusing,
        those calls hang for the full socket timeout — which is exactly when
        the UI most obviously wedges, and it wedged *after* the picture was
        gone, so there was nothing on screen to explain it.

        The session reporter is the right worker rather than a fresh thread:
        one FIFO, so the release still lands in submission order with the stop
        report it accompanies, and ``terminate()`` drains it on the way out —
        so a live TV tuner is still freed by quitting the app.

        Callers that must know the stream is gone before asking for another
        one keep calling ``terminate_transcode`` directly; ``Video`` does that
        against its own source in ``get_playback_url``, which is what keeps
        re-resolving one item ordered. What is no longer ordered is stopping
        live TV and tuning a *different* channel: on a single-tuner box the
        close now has to win a race it used to be ahead of by construction.
        It is submitted before the browser has even repainted the library, so
        it has the whole of the user's navigation to land in.
        """
        if video is None:
            return
        self._reporter.submit(video.terminate_transcode, 'terminate_transcode')

    def upd_player_hide(self):
        video = self._video
        if video:
            self._player.keep_open = video.parent.has_next

    def _report_stopped_offline(self, video):
        if getattr(video, 'is_photo', False) or video.playback_info is None:
            log.debug('no playback info to report a stop against')
            return
        options = {'PositionTicks': int((self.last_seek or 0) * 10000000), 'PlaybackStartTimeTicks': int((self.start_time or 0) * 10000000), 'PlayMethod': 'Transcode' if video.is_transcode else 'DirectPlay', 'PlaySessionId': video.playback_info['PlaySessionId'], 'ItemId': video.item_id}
        self._record_progress(video, options.get('PositionTicks'), False)
        try:
            self.send_timeline_stopped(options=options, client=video.client)
        except Exception:
            log.warning('Could not report playback stop to server.', exc_info=True)
        self.release_stream(video)

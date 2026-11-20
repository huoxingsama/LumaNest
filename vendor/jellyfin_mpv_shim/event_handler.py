from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from .media import Media
from .player import playerManager
from .timeline import timelineManager
log = logging.getLogger('event_handler')
bindings = {}
NAVIGATION_DICT = {'Back': 'back', 'Select': 'ok', 'MoveUp': 'up', 'MoveDown': 'down', 'MoveRight': 'right', 'MoveLeft': 'left', 'GoHome': 'home', 'GoToSettings': 'settings', 'ToggleContextMenu': 'menu', 'GoToSearch': 'search'}
from typing import TYPE_CHECKING, Any, Callable, Optional
if TYPE_CHECKING:
    from jellyfin_apiclient_python import JellyfinClient as JellyfinClient_type

def bind(event_name: str):

    def decorator(func):
        bindings[event_name] = func
        return func
    return decorator

def start_playback(client: 'JellyfinClient_type', item_ids: list, start_index: int=0, offset_ticks=None, user_id=None, aid=None, sid=None, srcid=None, sync_play_group=None, explicit_tracks=False, pause_stills=True):
    """Begin playback of one or more items from the beginning of `item_ids`.

    Shared by the websocket "Play" event and the local library browser so both
    funnel through identical setup (pre/post hooks, timeline, SyncPlay). Returns
    True if a playable video was found and handed to the player.

    `explicit_tracks` marks aid/sid as a deliberate user choice (the library
    browser's track pickers) that should be used as-is, bypassing the
    language_config and server-default selection in Media.map_streams.

    `pause_stills` is whether a photo at the head of the queue opens paused.
    True is "show me this picture" (a click on one); False is "run the
    slideshow" (Play All / Shuffle), where pausing on frame one would be a
    queue that never starts.
    """
    if sync_play_group is None and playerManager.syncplay.in_group():
        log.info('Handing playback to the SyncPlay group instead of starting it locally.')
        client.jellyfin.reset_queue_sync_play(list(item_ids), start_index, offset_ticks or 0)
        return True
    media = Media(client, item_ids, seq=start_index, user_id=user_id, aid=aid, sid=sid, srcid=srcid, explicit_tracks=explicit_tracks)
    offset = offset_ticks
    if offset is not None:
        offset /= 10000000
    video = media.video
    if not video:
        return False
    playerManager.playback_starting()
    playerManager.play(video, offset, is_initial_play=True, pause_stills=pause_stills)
    timelineManager.send_timeline()
    if sync_play_group is not None:
        playerManager.syncplay.join_group(sync_play_group)
    playerManager.playback_started()
    return True

class EventHandler(object):
    mirror = None
    display_content: Optional[Callable[..., Any]] = None
    live_tv_changed: Optional[Callable[..., Any]] = None
    user_data_changed: Optional[Callable[..., Any]] = None

    def handle_event(self, client: 'JellyfinClient_type', event_name: str, arguments: dict):
        if event_name in bindings:
            log.debug('Handled Event {0}: {1}'.format(event_name, arguments))
            bindings[event_name](self, client, event_name, arguments)
        else:
            log.debug('Unhandled Event {0}: {1}'.format(event_name, arguments))

    @bind('Play')
    def play_media(self, client: 'JellyfinClient_type', _event_name, arguments: dict):
        play_command = arguments.get('PlayCommand')
        if not playerManager.has_video():
            play_command = 'PlayNow'
        if play_command == 'PlayNow':
            ids = arguments.get('ItemIds') or []
            seq = arguments.get('StartIndex')
            if seq is None:
                seq = 0
            log.debug('EventHandler::playMedia %s' % ids)
            if not 0 <= seq < len(ids):
                log.warning('Declined a remote Play: %d item(s), StartIndex %s, from user %s (%s)', len(ids), arguments.get('StartIndex'), arguments.get('ControllingUserId'), arguments)
                return
            start_playback(client, ids, start_index=seq, offset_ticks=arguments.get('StartPositionTicks'), user_id=arguments.get('ControllingUserId'), aid=arguments.get('AudioStreamIndex'), sid=arguments.get('SubtitleStreamIndex'), srcid=arguments.get('MediaSourceId'), sync_play_group=arguments.get('SyncPlayGroup'), pause_stills=len(ids) == 1)
        elif play_command in ('PlayLast', 'PlayNext'):
            video = playerManager.get_video()
            if video is not None:
                video.parent.insert_items(arguments.get('ItemIds'), append=play_command == 'PlayLast')
                playerManager.upd_player_hide()

    @bind('GeneralCommand')
    def general_command(self, client: 'JellyfinClient_type', _event_name, arguments: dict):
        command = arguments.get('Name')
        if command == 'SetVolume':
            if playerManager.get_volume(True) != int(arguments['Arguments']['Volume']):
                playerManager.set_volume(int(arguments['Arguments']['Volume']))
        elif command == 'SetAudioStreamIndex':
            playerManager.set_streams(int(arguments['Arguments']['Index']), None)
        elif command == 'SetSubtitleStreamIndex':
            playerManager.set_streams(None, int(arguments['Arguments']['Index']))
        elif command == 'DisplayContent':
            timelineManager.delay_idle()
            if self.display_content:
                try:
                    self.display_content(client, arguments['Arguments']['ItemId'])
                except Exception:
                    log.warning('Could not display remote content.', exc_info=True)
        elif command in NAVIGATION_DICT:
            playerManager.menu_action(NAVIGATION_DICT[command])
        elif command in ('Mute', 'Unmute'):
            playerManager.set_mute(command == 'Mute')
        elif command == 'TakeScreenshot':
            playerManager.screenshot()
        elif command == 'ToggleFullscreen' or command is None:
            playerManager.toggle_fullscreen()

    @bind('Playstate')
    def play_state(self, _client: 'JellyfinClient_type', _event_name, arguments: dict):
        command = arguments.get('Command')
        if command == 'PlayPause':
            playerManager.toggle_pause()
            timelineManager.send_timeline()
        elif command == 'Pause':
            playerManager.pause_if_playing()
            timelineManager.send_timeline()
        elif command == 'Unpause':
            playerManager.play_if_paused()
            timelineManager.send_timeline()
        elif command == 'PreviousTrack':
            playerManager.play_prev()
        elif command == 'NextTrack':
            playerManager.play_next()
        elif command == 'Stop':
            playerManager.stop()
        elif command == 'Seek':
            playerManager.seek(arguments.get('SeekPositionTicks') / 10000000, absolute=True)

    @bind('PlayPause')
    def pause_play(self, _client: 'JellyfinClient_type', _event_name, _arguments: dict):
        playerManager.toggle_pause()
        timelineManager.send_timeline()
    LIVE_TV_EVENTS = ('TimerCreated', 'TimerCancelled', 'SeriesTimerCreated', 'SeriesTimerCancelled')

    @bind('TimerCreated')
    @bind('TimerCancelled')
    @bind('SeriesTimerCreated')
    @bind('SeriesTimerCancelled')
    def live_tv_timers_changed(self, client: 'JellyfinClient_type', _event_name, _arguments: dict):
        """A recording rule changed — possibly from another client entirely.

        Only a nudge: the browser decides whether the screen it is showing
        cares. Recording state is the one thing in the library that a *third
        party* changes while you are looking at it, so a stale Schedule tab
        is not a cosmetic problem — it is the screen telling you something
        will be taped when it will not.
        """
        if self.live_tv_changed:
            try:
                self.live_tv_changed(client)
            except Exception:
                log.warning('live tv refresh hook failed', exc_info=True)

    @bind('UserDataChanged')
    def user_data_change(self, client: 'JellyfinClient_type', _event_name, arguments: dict):
        """Watched / resume state moved for this user, somewhere.

        Usually another client entirely -- finishing an episode on a phone, or
        removing a film from Continue Watching in a browser (#560). The row is
        not cosmetically stale afterwards; it offers to resume something
        already watched.

        Two consumers wanting opposite things: the **browser** takes only the
        nudge and debounces, the **download catalog** takes the payload, since
        ``arguments`` carries the new values and applying them costs no
        request. Hence `arguments`, not `_arguments`.

        **This does not fire for progress reports** -- not even the one that
        finishes an item -- so a device killed mid-playback announces nothing,
        which is the gap `_refresh_userdata` is left in the design to cover.
        See docs/offline-sync.md section 2; `tests/e2e/test_offline_sync.py`
        pins both halves against a real server.
        """
        try:
            from .sync.manager import syncManager
            from .clients import clientManager
            syncManager.apply_userdata_event(arguments, clientManager.uuid_for_client(client))
        except Exception:
            log.debug('could not apply pushed userdata', exc_info=True)
        if self.user_data_changed:
            try:
                self.user_data_changed(client)
            except Exception:
                log.warning('user data refresh hook failed', exc_info=True)

    @bind('SyncPlayGroupUpdate')
    def sync_play_group_update(self, client: 'JellyfinClient_type', _event_name, arguments: dict):
        playerManager.syncplay.client = client
        playerManager.syncplay.process_group_update(arguments)

    @bind('SyncPlayCommand')
    def sync_play_command(self, client: 'JellyfinClient_type', _event_name, arguments: dict):
        playerManager.syncplay.client = client
        playerManager.syncplay.process_command(arguments)
eventHandler = EventHandler()

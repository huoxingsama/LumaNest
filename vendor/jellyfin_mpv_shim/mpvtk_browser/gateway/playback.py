"""Starting playback, and the window handoff around it.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from . import deps
from .base import GatewayCore
log = logging.getLogger('mpvtk_browser.gateway.playback')

class PlaybackMixin(GatewayCore):

    def on_browse_enter(self):
        from ...player import playerManager
        playerManager.end_hud_session()
        playerManager.mpvtk_active = True
        if playerManager.set_browse_window(True) is False:
            return False
        playerManager.enable_osc(False)
        playerManager.clear_stats()
        playerManager.clear_deinterlace_override()
        playerManager.clear_aspect_override()

    def apply_browser_fullscreen(self):
        """Push a just-written ``browser_fullscreen`` at the window (#729).

        Through ``_act`` like every other player call from this thread: the
        settings page runs on the browser's loop, and the player's lock can
        be held by a playback start.
        """
        self._act(lambda pm: pm.apply_browser_fullscreen())

    def on_minimize(self):
        """Drop to the windowless state. set_browse_window(False) releases
        force_window when nothing is playing; if a cast is in flight it
        leaves the picture alone, which is exactly the behaviour we want."""
        from ...player import playerManager
        playerManager.end_hud_session()
        playerManager.mpvtk_active = False
        playerManager.clear_deinterlace_override()
        playerManager.clear_aspect_override()
        playerManager.enable_osc(playerManager.osc_enabled)
        playerManager.set_browse_window(False)
    _DEVICE_TTL = 5.0
    _device_cache = (0.0, None)

    def audio_devices(self):
        """``[(label, value), ...]`` for the audio-device picker.

        Asked of the live mpv (``audio-device-list``) rather than assembled
        here: which devices exist depends on the platform, the audio server
        and what is plugged in *now*, and mpv is the thing that will have to
        open whichever one is chosen. Its own names are the only ones that
        are guaranteed to work.

        The leading entry is None, not ``"auto"`` — see ``conf.audio_device``:
        unset means the setting does not touch mpv at all, so a user's
        mpv.conf keeps whatever it says.
        """
        import time
        from ...i18n import _
        from ...player import playerManager
        stamp, cached = PlaybackMixin._device_cache
        now = time.monotonic()
        if cached is not None and now - stamp < self._DEVICE_TTL:
            return list(cached)
        out = [(_('Default (mpv decides)'), None)]
        try:
            devices = playerManager._mpv_property('audio-device-list') or []
        except Exception:
            log.debug('could not read audio-device-list', exc_info=True)
            return out
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            name = dev.get('name')
            if not name or name == 'auto':
                continue
            out.append((dev.get('description') or name, name))
        PlaybackMixin._device_cache = (now, list(out))
        return out

    def apply_audio_settings(self):
        """Push the saved audio settings to mpv. Live: a mode change takes
        effect without a restart, and mid-playback without a reload."""
        from ...player import playerManager
        try:
            playerManager.apply_audio_settings()
            return True
        except Exception:
            log.error('Could not apply audio settings.', exc_info=True)
            return False

    def any_client(self):
        """One connected client, or None. For the cast screen's random
        backdrop, which needs *a* server rather than a particular one."""
        import random
        try:
            clients = list(deps.clientManager.clients.values())
        except Exception:
            return None
        return random.choice(clients) if clients else None

    def client_for(self, server_uuid):
        """The live client for a server, or None."""
        try:
            return deps.clientManager.clients.get(server_uuid)
        except Exception:
            log.debug('no client for %r', server_uuid, exc_info=True)
            return None

    def cancel_load(self):
        """Abandon a playback start still in flight. Takes no player lock, so
        it is safe from the loop thread while the load holds one."""
        from ...player import playerManager
        try:
            return playerManager.cancel_load()
        except Exception:
            log.error('could not cancel the load', exc_info=True)
            return False

    def retry_playback(self, force_transcode=False):
        """Re-attempt the start that just failed, optionally forcing the
        server to transcode. Returns immediately — the player queues the
        replay onto the action thread rather than loading here."""
        from ...player import playerManager
        return playerManager.retry_failed_playback(force_transcode)

    def get_last_server(self):
        """uuid of the server the active user last browsed, or None.

        Only a hint — the server may have been removed or failed to connect
        since, so the browser falls back when it isn't in the live list.
        """
        from ...users import userManager
        try:
            return userManager.get_last_server()
        except Exception:
            log.debug('could not read last server', exc_info=True)
            return None

    def set_last_server(self, server_uuid):
        """Remember the browsed server for the active user. Best-effort:
        failing to persist a preference must never break navigation."""
        from ...users import userManager
        try:
            userManager.set_last_server(server_uuid)
        except Exception:
            log.debug('could not persist last server', exc_info=True)

    def on_browse_leave(self):
        from ...player import playerManager
        playerManager.browse_yield()
        playerManager.enable_osc(playerManager.osc_enabled)

    def play(self, item, server_uuid, offset_ticks=None, srcid=None, aid=None, sid=None):
        self.play_list([item.get('Id')], server_uuid, 0, offset_ticks=offset_ticks, srcid=srcid, aid=aid, sid=sid)

    def play_list(self, item_ids, server_uuid, start_index, offset_ticks=None, srcid=None, aid=None, sid=None, pause_stills=True):
        from ...event_handler import start_playback
        from ...sync.manager import syncManager
        client = deps.clientManager.clients.get(server_uuid)
        if client is None:
            item_ids = list(item_ids)
            first = item_ids[start_index] if 0 <= start_index < len(item_ids) else None
            from ...sync.db import ANY_SERVER
            if not (first and syncManager.db and syncManager.is_complete(first, ANY_SERVER)):
                log.warning('mpvtk play: no connected client for %s and no local copy of %s', server_uuid, first)
                return
        try:
            start_playback(client, list(item_ids), start_index=start_index, offset_ticks=offset_ticks, aid=aid, sid=sid, srcid=srcid, explicit_tracks=aid is not None or sid is not None, pause_stills=pause_stills)
        except Exception:
            log.error('mpvtk browser failed to start playback', exc_info=True)

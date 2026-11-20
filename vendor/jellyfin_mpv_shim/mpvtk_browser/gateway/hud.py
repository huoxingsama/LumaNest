"""The playback HUD's data and its option writes.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from ...conf import settings
from .base import GatewayCore
log = logging.getLogger('mpvtk_browser.gateway.hud')

class HudMixin(GatewayCore):

    def use_hud(self):
        """Whether video playback uses the in-window playback HUD.
        Reads the player's RESOLVED style (settings may hold the legacy
        "jellyfin" alias, and fallbacks may have applied)."""
        from ...player import playerManager
        return getattr(playerManager, '_osc_style_resolved', None) == 'mpvtk'

    def hud_key_opts(self):
        """Everything the renderer owns about the HUD, sent with the engage.

        Keyboard policy ("grab"/"key"): by default only hud_wake_key is taken
        over during playback so mpv's own seek keys keep working.

        Auto-hide policy ("hide"/"mode") and the no-scrim text halo
        ("shadow") ride the same message, because the renderer owns the
        summon/hide lifecycle and draws the glyphs -- and because engage
        re-sends them, which is what makes a settings change stick without a
        restart.
        """
        return {'grab': bool(settings.hud_grab_keys), 'key': settings.hud_wake_key or 'ENTER', 'hide': max(0.0, float(settings.hud_hide_secs or 0)), 'mode': settings.hud_autohide or 'hover', 'shadow': settings.hud_scrim == 'none', 'click': bool(settings.mouse_click_pauses), 'syncplay': self._syncplay_active()}

    @staticmethod
    def _syncplay_active():
        from ...player import playerManager
        try:
            return bool(playerManager.syncplay.is_enabled())
        except Exception:
            return False

    def hud_menu_state(self):
        """osc_bridge's menu/track state blob for the HUD's pickers
        (audio/subtitles with selection, quality, …), or None."""
        from ...player import playerManager
        try:
            return playerManager.osc_bridge.build_state()
        except Exception:
            log.debug('hud_menu_state failed', exc_info=True)
            return None

    def hud_action(self, verb, arg=None):
        """Route a picker/skip action through the same dispatcher the
        lua OSC uses (osc_bridge.handle_action), so e.g. selecting a
        burn-in subtitle restarts the transcode exactly like the OSD
        menu would."""
        from ...player import playerManager
        args = [verb] if arg is None else [verb, str(arg)]
        playerManager.osc_bridge.handle_action(args)

    def get_speed(self):
        """Current playback speed (1.0 when unknown)."""
        from ...player import playerManager
        try:
            return float(playerManager._player.speed or 1.0)
        except Exception:
            return 1.0

    def set_speed(self, speed):
        self._act(lambda pm: setattr(pm._player, 'speed', float(speed)))

    def hdr_output(self):
        """The local output preference, also used for the menu checkmark."""
        return 'sdr' if getattr(settings, 'hdr_output', 'auto') == 'sdr' else 'auto'

    def set_hdr_output(self, mode, on_applied=None):
        """Apply before an optional playback launch, even if the player is busy."""
        if mode not in ('auto', 'sdr'):
            raise ValueError('Unknown HDR output mode')

        def apply(pm):
            result = pm.set_hdr_output(mode)
            if result is not False and on_applied is not None:
                on_applied()
        self._act(apply)

    def get_aspect(self):
        """Current video-aspect-override (-1.0 = auto/unknown)."""
        from ...player import playerManager
        try:
            return float(playerManager._player.video_aspect_override or -1.0)
        except Exception:
            return -1.0

    def set_aspect(self, value):
        """``value`` is mpv's string form ("-1", "16:9", …) — the
        property parses ratio strings on both backends."""
        self._act(lambda pm: pm.set_aspect(value))

    def toggle_stats(self):
        """Toggle mpv's stats overlay (the gear menu's Playback Data).
        Goes through the player's tracked toggle so the overlay is cleared
        when the library returns (see on_browse_enter -> clear_stats)."""
        self._act(lambda pm: pm.toggle_stats())

    def playback_info(self):
        """What the playback-info panel shows, or None with nothing playing.

        **Not on the playstate snapshot**, unlike everything else the HUD
        draws: that blob is pushed on every position tick and this carries a
        whole MediaSource. It is read once when the panel opens instead,
        which is also when it is true -- none of it can change without
        playback restarting.

        Every field is already decided and sitting on the Video. We do not
        ask the server what it is doing with our session the way
        jellyfin-web has to (``/Sessions`` -> ``TranscodingInfo``, polled):
        the decision was *ours*, taken in
        ``media.Video._get_url_from_source``, so this is an attribute read
        rather than a request per second. The cost is that live transcode
        progress and encoder fps are not available, which is the trade
        recorded in docs/archive/UI_FIXES_4.md §10.
        """
        from ...player import playerManager
        try:
            video = playerManager.get_video()
        except Exception:
            log.debug('could not read the playing video', exc_info=True)
            return None
        if video is None:
            return None
        item = getattr(video, 'item', None) or {}
        return {'title': item.get('Name') or '', 'item_type': item.get('Type') or '', 'media_type': item.get('MediaType') or '', 'play_method': getattr(video, 'play_method', None), 'transcode_reasons': list(getattr(video, 'transcode_reasons', None) or ()), 'direct_path': bool(getattr(video, 'direct_path', False)), 'offline': getattr(video, 'client', None) is None, 'source': getattr(video, 'media_source', None) or {}, 'aid': getattr(video, 'aid', None), 'sid': getattr(video, 'sid', None)}
    _MPV_STATS = (('hwdec-current', 'hwdec'), ('current-vo', 'vo'), ('estimated-vf-fps', 'fps'), ('frame-drop-count', 'drops_vo'), ('decoder-frame-drop-count', 'drops_dec'), ('avsync', 'avsync'), ('demuxer-cache-duration', 'buffered'), ('cache-speed', 'cache_speed'))

    def player_stats(self):
        """Live mpv counters for the playback-info panel, ``{}`` when idle.

        Read per build rather than pushed on the playstate snapshot: the
        panel is only open for seconds at a time, and these would otherwise
        ride every 1s tick for the whole of every playback. While it *is*
        open the HUD's own ticker rebuilds about once a second, which is
        also the rate at which these are worth re-reading.

        Every property is fetched independently and a failure is dropped
        rather than raised: they are not all present on every mpv, on every
        backend, or at every moment (there is no ``estimated-vf-fps``
        before the first frame, and none of the video ones during audio),
        and a panel that shows nothing because one counter was missing is
        worse than one that shows seven rows.
        """
        from ...player import playerManager
        player = getattr(playerManager, '_player', None)
        if player is None or not getattr(playerManager, '_mpv_alive', True):
            return {}
        out = {}
        for prop, key in self._MPV_STATS:
            try:
                value = getattr(player, prop.replace('-', '_'))
            except Exception:
                continue
            if value is not None:
                out[key] = value
        return out

    def deinterlace(self):
        """``(is_on, is_auto)`` for the gear menu's Deinterlace row.

        A direct read like ``get_speed``, not an ``_act``: it is asked on
        the render path, and a deferred answer is no answer at all to a
        menu that is being drawn now.
        """
        from ...player import playerManager
        try:
            return playerManager.deinterlace_state()
        except Exception:
            return (False, False)

    def toggle_deinterlace(self):
        """Force deinterlacing on or off for this session. Applies to what
        is playing right now -- no reload.

        The state is read INSIDE the action, like `toggle_night_mode` and
        `toggle_mute` beside it. `_act` may DEFER to the action thread when
        the player lock is busy, so a read out here is taken before the
        write it decides -- and two presses landing during one playback
        start would both compute the same answer, making the second a
        no-op.
        """

        def toggle(pm):
            if pm.deinterlace_forced():
                pm.clear_deinterlace_override()
                pm.reapply_deinterlace()
            else:
                pm.set_deinterlace(not pm.deinterlace_state()[0])
        self._act(toggle)

    def toggle_night_mode(self):
        """Night mode on/off from the playback HUD's gear menu. Applies to
        what is playing right now — no reload."""
        self._act(lambda pm: pm.set_night_mode(not settings.audio_night_mode))

    def set_paused(self, paused):
        """Explicit pause state (scrub-in-progress pauses; commit or
        cancel restores)."""
        self._act(lambda pm: pm.set_paused(bool(paused)))

    def toggle_mute(self):
        self._act(lambda pm: setattr(pm._player, 'mute', not pm._player.mute))

    def toggle_fullscreen(self):
        """Toggle mpv fullscreen AND record the user's intent, exactly
        like the lua OSC's button (so auto-fullscreen doesn't
        re-fullscreen the next episode against their choice)."""

        def flip(pm):
            was = bool(pm._player.fullscreen)
            pm._player.fullscreen = not was
            pm.put_task(pm.set_fullscreen, not was, True)
        self._act(flip)

    def chapter_seek(self, direction):
        """Previous (-1) / next (+1) chapter, by the player's rule.

        The HUD used to work its own target out from the chapter list it had
        already fetched and seek there. That was a second definition of what
        "previous chapter" means, and it went round SyncPlay -- see
        PlayerManager.chapter_seek, which is now the only one.
        """
        self._act(lambda pm: pm.chapter_seek(direction))

    def chapters(self):
        """mpv's chapter list as [{"title", "time"}], [] when none."""
        from ...player import playerManager
        try:
            chapters = playerManager._player.chapter_list or []
        except Exception:
            return []
        out = []
        for ch in chapters:
            try:
                out.append({'title': ch.get('title') or '', 'time': float(ch.get('time') or 0.0)})
            except Exception:
                continue
        return out

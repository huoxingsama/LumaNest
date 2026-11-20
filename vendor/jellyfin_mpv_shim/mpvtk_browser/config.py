"""Settings-schema helpers for the mpvtk browser's Settings view.

Classifies the
``conf.Settings`` annotations the Tk browser's form uses, and reads/writes
the in-process ``conf.settings`` singleton directly (no IPC — the mpvtk
browser runs in the player's process).
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import sys
import typing
from ..conf import Settings, settings
from ..i18n import _, _p
log = logging.getLogger('mpvtk_browser.config')
_HIDDEN = {'language_config', 'client_uuid', 'config_version', 'window_width', 'window_height', 'window_maximized', 'auto_download_servers'}
AUDIO_PASSTHROUGH_KEYS = [('ac3', 'audio_passthrough_ac3'), ('dts', 'audio_passthrough_dts'), ('eac3', 'audio_passthrough_eac3'), ('dts-hd', 'audio_passthrough_dts_hd'), ('truehd', 'audio_passthrough_truehd')]
AUDIO_MODE_ONLY = {'audio_optical_encode_ac3': {'optical'}}
TRAY_DEPENDENT = ('close_to_tray', 'allow_background')
EXCLUSIVE_PLATFORMS = ('win32', 'darwin')
BACKGROUND_DEPENDENT = ('start_minimized',)
HUD_ONLY = ('hud_grab_keys', 'hud_wake_key', 'hud_scrim', 'hud_autohide', 'hud_hide_secs', 'hud_auto_scale', 'mouse_click_pauses')
TRICKPLAY_DEPENDENT = ('trickplay_fast_mode',)
LANGUAGE_PRESET_DEPENDENT = ('preferred_language',)

def language_preset_builds_rules():
    """Whether the chosen `language_preference` writes rules, and so reads
    `preferred_language`. Asks `preset_rules` itself, so a new preset cannot
    leave the row hidden while it is in use."""
    from ..language_config import preset_rules
    try:
        return bool(preset_rules(settings.language_preference or '', 'eng'))
    except Exception:
        return False

def hud_style_selected():
    """Whether the in-window playback HUD is what `osc_style` resolves to.

    Guarded like `visible_passthrough_keys` below: this is only ever used to
    decide whether to DRAW a row, and a settings form that refuses to open
    is worse than one showing a setting that does nothing.
    """
    try:
        from ..mpv_options import resolve_osc_style
        return resolve_osc_style() == 'mpvtk'
    except Exception:
        log.debug('could not resolve the OSC style', exc_info=True)
        return True
TAB_SECTIONS = {'general': [(_('This Device'), ['lang', 'player_name', 'raise_mpv', 'discord_presence', 'mpris', 'check_updates', 'notify_updates']), (_('Input'), ['input_gamepad', 'gamepad_swap_confirm', 'browse_block_keys']), (_('Window'), ['fullscreen', 'browser_fullscreen', 'remember_window_size', 'window_controls', 'window_controls_fullscreen', 'hide_title_bar', 'close_to_tray', 'allow_background', 'start_minimized', 'display_mirror_summon'])], 'browse': [(_('Theme'), ['theme', 'poster_scale', 'ui_scale', 'ui_text_scale', 'ui_text_min']), (_('Library Browser'), ['library_image_cache_mb', 'scroll_wheel_pixels', 'scroll_mode', 'paginated', 'remember_filters', 'grid_fill', 'backdrop_full_width', 'detail_poster', 'detail_episode_image', 'logo_legibility_live_tv', 'logo_legibility_library', 'clock_12h']), (_('Reading'), ['reader_font_size', 'reader_theme', 'reader_justify', 'comic_fit']), (_('Downloads'), ['sync_path', 'prefer_downloaded', 'auto_download_enable', 'auto_download_next_up', 'auto_download_next_up_limit', 'auto_download_lookahead', 'auto_download_max_gb', 'auto_download_delete_watched', 'auto_download_keep_watched_hours', 'auto_download_keep_days', 'auto_download_interval_mins']), (_('Download Tuning'), ['auto_download_lookahead_min', 'auto_download_lookahead_max', 'auto_download_max_per_pass'])], 'playback': [(_('Player Controls'), ['osc_style', 'thumbnail_enable', 'hud_grab_keys', 'hud_wake_key', 'hud_scrim', 'hud_autohide', 'hud_hide_secs', 'hud_auto_scale', 'mouse_chapter_nav', 'mouse_click_pauses', 'trickplay_fast_mode']), (_('Playback'), ['auto_play', 'hwdec', 'network_buffer', 'always_transcode', 'local_kbps', 'remote_kbps', 'direct_paths', 'remote_direct_paths', 'playback_timeout']), (_('Audio'), ['audio_device', 'audio_exclusive', 'audio_mode', 'audio_night_mode'] + [k for _c, k in AUDIO_PASSTHROUGH_KEYS] + ['audio_optical_encode_ac3']), (_('Subtitles & Languages'), ['subtitle_size', 'subtitle_color', 'subtitle_position', 'language_preference', 'preferred_language', 'remember_audio_track', 'remember_subtitle_track', 'lang_filter', 'lang_filter_sub', 'lang_filter_audio']), (_('Transcoding'), ['allow_transcode_to_h265', 'prefer_transcode_to_h265', 'transcode_hevc', 'transcode_av1', 'transcode_4k', 'transcode_hdr', 'transcode_hi10p', 'transcode_dolby_vision', 'extract_image_subtitles', 'force_video_codec', 'force_audio_codec']), (_('Video Enhancement'), ['hdr_output', 'deband', 'render_quality', 'tone_mapping', 'shader_pack_enable', 'shader_pack_subtype', 'shader_pack_remember', 'shader_pack_gpu_api', 'deinterlace_auto', 'motion_interpolation']), (_('Skip Intro / Credits'), ['segment_intro', 'segment_outro', 'segment_commercial', 'segment_preview', 'segment_recap', 'skip_intro_on_seek'])]}
RESTART_REQUIRED = frozenset({'ui_scale', 'hide_title_bar', 'osc_style', 'thumbnail_enable', 'input_gamepad', 'lang', 'enable_gui', 'headless', 'mpv_ext', 'mpv_ext_path', 'mpv_ext_ipc', 'mpv_ext_start', 'mpv_ext_no_ovr', 'mpv_ext_start_retries', 'mpv_ext_start_retry_delay_ms', 'shader_pack_enable', 'shader_pack_custom', 'discord_presence', 'mpris', 'player_name', 'mpv_log_level', 'menu_mouse', 'media_keys', 'mouse_chapter_nav', 'lang_filter', 'library_image_cache_mb', 'tls_client_cert', 'tls_client_key', 'tls_server_ca'})
ADVANCED_TAB = 'general'
ADVANCED_GROUPS = frozenset({_('Advanced'), _('Download Tuning')})
FORM_TABS = ('general', 'browse', 'playback')
SECTIONS = [group for tab in FORM_TABS for group in TAB_SECTIONS[tab]]
ENUMS = {'subtitle_position': ['top', 'bottom', 'middle'], 'mpv_log_level': ['fatal', 'error', 'warn', 'info', 'debug', 'noise'], 'shader_pack_subtype': ['lq', 'hq']}
_SEGMENT_ACTIONS = [(_('Never'), 'off'), (_('Ask'), 'ask'), (_('Always'), 'always')]
LABELED_ENUMS = {'hdr_output': [('自动', 'auto'), ('SDR（HDR 转 SDR）', 'sdr')], 'hwdec': [(_('Off (software decoding)'), 'no'), (_('Only above 1080p'), 'over-1080p'), (_('On'), 'auto'), (_('Copy (advanced)'), 'auto-copy')], 'motion_interpolation': [(_('Off'), 'off'), (_('Smooth Motion'), 'smooth'), (_('Blend Frames'), 'blend'), (_('Smooth (high quality)'), 'hq')], 'deband': [(_('Off'), 'off'), (_('Light (live action)'), 'light'), (_('Standard (animation)'), 'standard'), (_('Strong'), 'strong')], 'tone_mapping': [(_('Automatic (MPV decides)'), 'auto'), (_('BT.2390 (reference)'), 'bt.2390'), (_('BT.2446a'), 'bt.2446a'), (_('Spline'), 'spline'), (_('Hable'), 'hable'), (_('Reinhard'), 'reinhard'), (_('Clip (no tone mapping)'), 'clip')], 'render_quality': [(_('MPV default'), 'default'), (_('High quality'), 'high')], 'network_buffer': [(_('MPV default (1 second)'), 'default'), (_('Large (20 seconds)'), 'large'), (_('Very large (60 seconds)'), 'huge')], 'osc_style': [(_('Jellyfin UI'), 'mpvtk'), (_('MPV UI'), 'mpv'), (_('Custom OSC'), 'custom'), (_('No player controls'), 'none')], 'ui_text_scale': [(_('75%'), 0.75), (_('85%'), 0.85), (_('90%'), 0.9), (_('100% (no scaling)'), 1.0), (_('110%'), 1.1), (_('125%'), 1.25), (_('150%'), 1.5)], 'ui_text_min': [(_('No minimum'), 0), (_('14 px'), 14), (_('16 px'), 16), (_('18 px'), 18), (_('20 px'), 20)], 'ui_scale': [(_('Follow display'), None), (_('100% (no scaling)'), 1.0), (_('125%'), 1.25), (_('150%'), 1.5), (_('200%'), 2.0)], 'scroll_mode': [(_('Continuous'), 'continuous'), (_('Aligned to rows'), 'aligned'), (_('One row per notch'), 'row')], 'notify_updates': [(_('Automatic'), 'default'), (_('Always'), 'enabled'), (_('Never'), 'disabled')], 'window_controls': [(_('Only when the window has no title bar'), 'auto'), (_('Always'), 'always'), (_('Never'), 'never')], 'segment_intro': _SEGMENT_ACTIONS, 'segment_outro': _SEGMENT_ACTIONS, 'segment_commercial': _SEGMENT_ACTIONS, 'segment_preview': _SEGMENT_ACTIONS, 'segment_recap': _SEGMENT_ACTIONS, 'reader_theme': [(_('Dark'), 'dark'), (_('Sepia'), 'sepia'), (_('Light'), 'light')], 'comic_fit': [(_('Fit Width'), 'width'), (_('Fit Page'), 'page')], 'hud_scrim': [(_p('setting value', 'Default'), 'default'), (_('Panel behind the controls'), 'panel'), (_('None (shadowed text)'), 'none')], 'hud_autohide': [(_('Hide unless hovered'), 'hover'), (_('Always hide'), 'always'), (_('Never hide while paused'), 'paused')], 'grid_fill': [(_('Widen the gaps'), 'justify'), (_('Centre the tiles'), 'center'), (_('Leave it on the right'), 'off')], 'poster_scale': [(_('Theme default'), None), (_('Extra Compact'), 0.75), (_('Compact'), 0.85), (_('Small'), 1.0), (_('Medium'), 1.2), (_('Large'), 1.4), (_('Extra Large'), 1.7)], 'shader_pack_gpu_api': [(_('Automatic (recommended)'), 'auto'), (_('Vulkan'), 'vulkan'), (_('Direct3D 11 (Windows only)'), 'd3d11'), (_('OpenGL (compatibility)'), 'opengl')], 'audio_mode': [(_('Default (auto)'), 'auto'), (_('Force Stereo'), 'stereo'), (_('Optical Surround'), 'optical'), (_('HDMI Passthrough'), 'hdmi')], 'language_preference': [(_('Unset'), 'unset'), (_('Dubbed (shows only)'), 'dubbed_shows'), (_('Subbed (shows only)'), 'subbed_shows'), (_('Dubbed (all)'), 'dubbed_all'), (_('Subbed (all)'), 'subbed_all'), (_('Custom (set in config)'), 'custom')]}
LABEL_OVERRIDES = {'lang': _('Language'), 'notify_updates': _('Tell Me About Updates'), 'input_gamepad': _('Game Controller'), 'gamepad_swap_confirm': _('Swap Confirm and Back Buttons'), 'sync_path': _('Download Folder'), 'prefer_downloaded': _('Prefer Downloaded Copy'), 'mouse_click_pauses': _('Left Click Pauses Playback'), 'detail_poster': _('Show Posters on Detail Pages'), 'detail_episode_image': _('Show Episode Thumbnails on Detail Pages'), 'hwdec': _('Hardware Decoding'), 'deinterlace_auto': _('Deinterlace Automatically'), 'motion_interpolation': _('Motion Interpolation'), 'deband': _('Debanding'), 'hdr_output': '画面输出', 'tone_mapping': _('HDR Tone Mapping'), 'render_quality': _('Rendering Quality'), 'network_buffer': _('Network Buffer'), 'auto_download_enable': _('Automatically Download Upcoming Episodes'), 'auto_download_next_up': _('Include Next Up'), 'auto_download_next_up_limit': _('Next Up Entries to Consider'), 'auto_download_lookahead': _('Episodes to Keep Ahead (0 = off)'), 'auto_download_max_gb': _('Storage Limit for Automatic Downloads (GB)'), 'auto_download_delete_watched': _('Delete Automatic Downloads Once Watched'), 'auto_download_keep_watched_hours': _('Keep Watched Downloads For (hours, 0 = delete right away)'), 'auto_download_keep_days': _('Delete Unwatched After (days, 0 = never)'), 'auto_download_interval_mins': _('Check Every (minutes)'), 'auto_download_lookahead_min': _('Top Up When Fewer Than (episodes)'), 'auto_download_lookahead_max': _('Top Up To (episodes)'), 'auto_download_max_per_pass': _('Maximum Downloads per Check'), 'close_to_tray': _('Close to Tray (keep running)'), 'allow_background': _('Keep Running in Background'), 'remember_window_size': _('Remember Window Size'), 'window_controls': _('Window Buttons in the Top Bar'), 'window_controls_fullscreen': _('Keep Window Buttons in Full Screen'), 'hide_title_bar': _('Hide the Desktop Title Bar'), 'osc_style': _('Player Controls Style'), 'thumbnail_enable': _('Enable Trickplay Thumbnails'), 'trickplay_fast_mode': _('Load All Seek Previews at Once'), 'discord_presence': _("Show What You're Watching in Discord"), 'mpris': _('Desktop Media Controls'), 'ui_scale': _('Interface Scale'), 'ui_text_scale': _('Text size'), 'ui_text_min': _('Minimum Text Size'), 'theme': _('Theme'), 'poster_scale': _('Cover Size'), 'backdrop_full_width': _('Full-Width Backdrops'), 'clock_12h': _('12-Hour Clock'), 'grid_fill': _('Grid Spacing'), 'remember_filters': _('Remember Library Filters'), 'logo_legibility_live_tv': _('Make Live TV logos more legible'), 'logo_legibility_library': _('Make library logos more legible'), 'reader_font_size': _('Type Size'), 'reader_theme': _('Page Colour'), 'reader_justify': _('Justify Text'), 'comic_fit': _('Comic Reading Mode'), 'headless': _('Cast-target mode (no library browsing)'), 'display_mirror_summon': _('Casting Opens the Library Browser'), 'browser_fullscreen': _('Fullscreen Library Browser'), 'browse_block_keys': _('Block Player Keybinds While Browsing'), 'hud_grab_keys': _('Always Bind Arrow Keys to Player Controls'), 'hud_wake_key': _('Player Controls Activation Key'), 'ui_select_key': _('Select Key'), 'segment_intro': _('Skip Intros'), 'segment_outro': _p('setting', 'Skip Credits'), 'segment_commercial': _('Skip Commercials'), 'segment_preview': _('Skip Previews'), 'segment_recap': _('Skip Recaps'), 'hud_scrim': _('Shading Behind the Player Controls'), 'hud_autohide': _('When the Player Controls Hide'), 'hud_hide_secs': _('Hide the Player Controls After (seconds)'), 'hud_auto_scale': _('Shrink the Player Controls on a Narrow Window'), 'mouse_chapter_nav': _('Mouse Back/Forward Buttons Skip Chapters'), 'audio_mode': _('Audio Output Mode'), 'audio_device': _('Audio Output Device'), 'audio_exclusive': _('Take the Device Exclusively'), 'audio_night_mode': _('Night Mode (Auto Volume Adj)'), 'shader_pack_enable': _('Enable Video Playback Profiles'), 'shader_pack_subtype': _('Profile Group'), 'shader_pack_remember': _('Remember Last Used Profile'), 'shader_pack_gpu_api': _('Graphics API for Shaders'), 'audio_passthrough_ac3': _('Pass Through AC3'), 'audio_passthrough_dts': _('Pass Through DTS'), 'audio_passthrough_eac3': _('Pass Through E-AC3'), 'audio_passthrough_dts_hd': _('Pass Through DTS-HD'), 'audio_passthrough_truehd': _('Pass Through TrueHD'), 'audio_optical_encode_ac3': _('Re-encode Others to AC3')}
CAST_TARGET_NOTE = _('With "Start Minimized" and "Fullscreen" this is the classic cast-target setup: the app waits out of the way, plays fullscreen when something casts to it, and goes back to waiting when the video ends or you press Q.')
NOTES = {'lang_filter': _('This list does nothing on its own -- turn on one of the two switches below it. It changes which tracks the audio and subtitle menus offer you, not which one is picked automatically; the preset above is what does that. "und" means untagged, so removing it hides untagged tracks as well.'), 'language_preference': _('"Subbed" keeps the original audio and adds full subtitles in your language. "Dubbed" prefers audio in your language with signs and songs subtitles, and falls back to subbed where there is no dub. The "shows only" pair leaves films at whatever the server picks. The subbed presets assume the original audio is Japanese; if yours is not, choose Custom and write the rules yourself.'), 'lang': _('The percentage is how much of this app that language has been translated so far; the rest stays in English. Translations are contributed at translate.jellyfin.org.'), 'notify_updates': _('"Automatic" turns this off inside a Flatpak, where your desktop already announces updates and "flatpak update" installs them, and leaves it on everywhere else.'), 'input_gamepad': _('Move with the d-pad or left stick, seek with the right stick, shoulder buttons to page, Start for the menu. A controller is not focus-aware, so it will reach this app even when another window is in front. Needs an mpv built with SDL2 gamepad support; if yours lacks it this setting is ignored and the log says so. Any button can be reassigned in your input.conf.'), 'gamepad_swap_confirm': _('Turn this on for a controller whose A button is on the right rather than at the bottom (Switch Pro, most 8BitDo pads).'), 'browse_block_keys': _("Stops MPV's own shortcuts -- picture adjustment, screenshots, subtitle and audio switching -- from acting while you are in the library, where there is no video for them to act on. Turn it off to use your own MPV key bindings there."), 'ui_select_key': _("Activates whatever is highlighted, in the library and on the player controls. An mpv key name; changing it hands Enter back to MPV. The game controller's Confirm button and a phone's Select follow it."), 'auto_download_keep_watched_hours': _('A grace period after you finish something: it stays downloaded for this long, so you can rewatch it or somebody else in the house can catch up, instead of going on the next check. Running out of storage still removes watched downloads before the time is up.'), 'auto_download_lookahead_min': _('Leave these blank unless downloads are waking your disks too often. Set both of the first two together: episodes are then fetched in one batch when a series runs low, instead of one at a time.'), 'detail_episode_image': _("An episode's thumbnail is a frame of an episode you may not have watched yet."), 'mouse_click_pauses': _("Off gives MPV's own mouse behaviour instead: drag the video to move the window, and the right button does whatever your MPV config gives it. Double click is full screen either way."), 'deinterlace_auto': _("Deinterlace video the file says is interlaced. Off by default, as in MPV itself: the flag is not reliable in either direction, and deinterlacing progressive video softens a picture that was fine. To force it on for something that is interlaced without saying so, use Deinterlace in the player's settings menu, which lasts until you return to the library. Needs MPV 0.38 or newer."), 'deband': _('Smooths the blocky steps that appear in gradients — skies, dark scenes, fades. Animation and anime benefit most, because flat gradients are most of the picture; live action is largely unaffected either way, though a strong setting can soften genuinely fine detail. Costs GPU work whatever the content, which is worth knowing on a small or older machine. Leave this Off if you set the deband options in mpv.conf yourself — Off writes nothing at all, so your values are left alone.'), 'tone_mapping': _('How HDR video is fitted to an SDR display. This does nothing when HDR is being passed through to an HDR display, because no tone mapping is happening to change. Leave it on Automatic unless HDR films look wrong to you; BT.2390 is the reference curve, and Clip is what to try if highlights look grey rather than bright.'), 'render_quality': _("High quality applies the same options as MPV's own high-quality preset: better upscaling and HDR handling, at some GPU cost. It needs no shader files and does not use up your shader profile, so it is the thing to try before the shader pack below."), 'extract_image_subtitles': _('When a video is transcoded, show its built-in Blu-ray or DVD (PGS, VobSub) subtitles as a separate track instead of burning them into the picture, so they stay sharp and can be switched without restarting. Showing one makes the server extract every subtitle track in the file first, which takes time and server disk space on a large file. Subtitles in their own file are not affected.'), 'network_buffer': _("How far ahead of playback to read. MPV's default is one second, which is short for a server reached over the internet or a slow connection — raise this if playback keeps stopping for buffering. Larger buffers use more memory and make the first few seconds of a file slower to start."), 'motion_interpolation': _('Frame blending (blends frames together, not the same as SVP/DLSS/framegen). Reduces juddering caused by mismatched frame rate between content and display. May cause dropped frames if your displays have mismatched frame rates.'), 'hwdec': _('Off by default because some graphics drivers handle it badly. "Only above 1080p" is the cautious way to turn it on: most hardware decodes 1080p in software without help. "Copy (advanced)" is slower, but it is the mode that works with video filters, so it is the one to pick if you run SVP or another VapourSynth filter. If video stops working, start with --disable-hwdec and change this back.'), 'close_to_tray': CAST_TARGET_NOTE, 'allow_background': CAST_TARGET_NOTE, 'enable_gui': _("Off means command-line mode: no window, no system tray and no settings screen, so the only way back is to edit conf.json by hand. This is not how you get MPV's own on-screen controls; use Player Controls Style under Interface for that."), 'headless': _('Show only what is cast to this machine. The library, including this settings screen, cannot be reached from here, and without a system tray icon the only way back is to edit conf.json. This is for a shared TV nobody should be able to browse from; for an ordinary cast target, use the Interface settings.'), 'window_controls': _('Some desktops, GNOME on Wayland in particular, draw no title bar on the player window, leaving no way to move or close it. This puts minimize, maximize and close in the top bar instead, and lets you drag the window by it. On "Only when the window has no title bar", MPV is asked whether this window got one, so desktops that draw a title bar are left alone.'), 'window_controls_fullscreen': _('Full screen has no title bar anywhere and nothing to move or maximize, so the buttons are normally hidden there. Turn this on to keep a way out of full screen that is not a keyboard shortcut.'), 'hide_title_bar': _('Asks the desktop for no title bar on the player window, so the top bar above is the only one. Pairs with the setting above: on "Only when the window has no title bar" the buttons appear as soon as this is on. Leave both off unless you want them, since a window with neither can be awkward to move on some desktops.'), 'thumbnail_enable': _('Shows a preview frame while you drag the seek bar. Works with every player control style, including your own OSC script if it supports thumbfast. The images are fetched from the server per video, which costs disk space and memory -- tens of MB for an episode, a few hundred for a long film.'), 'preferred_language': _('The language to use for dubbed audio or subbed subtitles. Note the preference heuristics are built around English naming conventions, your mileage may vary. Use a language code such as eng.'), 'trickplay_fast_mode': _('Seek previews are normally fetched a few minutes at a time around where you are seeking, so scrubbing somewhere new waits briefly. Turn this on to load them all up front instead: nothing ever waits, but a long film can cost several hundred MB of memory. Turning it on applies to what you are watching the next time you seek somewhere new; turning it off applies to the next video.'), 'osc_style': _('MPV keybinds are used by default. Press ENTER to drive the player controls by keyboard. "No player controls" leaves playback bare; the library, the keyboard shortcuts and the menu key still work. Choose "Custom OSC" when you have installed your own OSC script: it turns MPV\'s own off and gives the library a solid background, so the script\'s idle screen cannot show through it.'), 'scroll_wheel_pixels': _('Pixels one wheel notch scrolls. Raise it to scroll faster, lower it for finer control. On a grid the value is rounded so that a whole number of notches spans one row, in every scroll mode, so a trackpad or trackball never leaves you a sliver of a row off.'), 'scroll_mode': _('"Continuous" scrolls by pixels and lands wherever the wheel puts it, lining rows up by itself if the display cannot keep up. Pick "Aligned to rows" if scrolling still stutters, as it can on a very large, slow or remote display, or with an external MPV. Pick "One row per notch" to move a whole row, or one home-screen section, at a time.'), 'discord_presence': _('Discord Rich Presence.'), 'mpris': _('Lets keyboard media keys, headset buttons, the lock screen and desktop media widgets play, pause and skip, and shows what is playing with its cover (MPRIS, Linux only).'), 'audio_device': _("Leave this to Default unless setting up passthrough. Note some audio servers like Pipewire don't like passthrough and will need to be disabled for a card before it'll let *any* audio through in passthrough mode. In my tests I got silence otherwise, but results may vary."), 'audio_exclusive': _('Stop anything else using the device while playing. Needed for passthrough on some systems, and it means other applications will be silent.'), 'paginated': _('Page the library and music tile grids instead of scrolling: each page is one screenful with First / Previous / Next / Last controls and a page number you can type into. Easier than precise scrolling on a trackpad.'), 'remember_filters': _('Reopen each library with the filters it had last time (genre, unplayed, favorites and the rest), saved to your account on the server. Off, filters only last until you leave the library.'), 'logo_legibility_live_tv': _("Channel logos are ink on a transparent background, drawn for the white page other clients put them on, so on a dark one the black ones vanish. On, each logo gets the light plate it was made for, and the few with a white outline get a drop shadow so they keep an edge against it. Off, they get the theme's card colour and no shadow, as Jellyfin Web does."), 'logo_legibility_library': _('The same treatment for a library set to draw Logo artwork. Off by default: a film or series logo is white by convention and already reads on a dark background, and it is the plate that makes it need a shadow. Turn it on if yours are dark.'), 'theme': _('Palette, glow, cover style and default cover size. Colours change immediately; cover and heading sizes take effect after a restart.'), 'grid_fill': _('Where the width a whole number of covers does not use ends up. Widening the gaps keeps the page margins the same on every screen size; centring keeps the covers evenly spaced and moves both margins instead.'), 'backdrop_full_width': _('Run the backdrop on a detail page to the edges of the window, as the web client does. It shows more of the artwork without taking any more vertical space.'), 'poster_scale': _("Overrides the theme's cover size. Also on the View menu of any library."), 'clock_12h': _('Show times of day as "8:30 PM" rather than "20:30" — the Live TV guide and its air times, and the "Ends at" labels on a detail page and the player controls.'), 'hud_scrim': _('The controls have to stay legible over any frame. "None" gives the text a drop shadow instead of shading the picture behind it.'), 'hud_autohide': _('"Hide unless hovered" keeps them up only while the pointer is on them, paused or not.'), 'hud_hide_secs': _('0 hides them as soon as the pointer is not on them, and forces "Hide unless hovered".'), 'hud_auto_scale': _('Narrowing the window shrinks the controls until the smallest button reaches the accessibility minimum, instead of dropping buttons sooner. Off keeps the older, larger floor.'), 'mouse_chapter_nav': _('During playback only; in the library those buttons stay Back and Forward. Off by default because they are easy to hit by accident on some mice.'), 'ui_scale': _('"Follow display" uses the scale your desktop reports, which is 100% on X11.'), 'ui_text_scale': _('Scales the text only. Interface Scale above resizes everything, artwork and spacing and controls included, so use this one when the words are too small rather than the whole interface. It stops at 150%: past that most tile captions are ellipsized, and what needs to be bigger is the whole interface, which is Interface Scale.'), 'ui_text_min': _('Nothing renders smaller than this, whatever Text Size works out to. Raises the smallest labels without enlarging headings, which a percentage cannot do.'), 'audio_mode': _('"Default" changes nothing and lets MPV (and your own mpv.conf) decide. Pick a mode only if you are sending audio to a receiver.'), 'audio_optical_encode_ac3': _('Audio your receiver cannot be sent directly is encoded to AC3, the only way surround fits down an optical cable. Turn this off if the encoder causes audio delay; those tracks become stereo instead.'), 'shader_pack_subtype': _('"hq" offers heavier profiles. Pick it if you have a fast graphics card.'), 'shader_pack_gpu_api': _('Leave on Automatic unless video breaks when a profile is loaded. This only applies while a profile is active, and OpenGL can cost you HDR output.'), 'reader_font_size': _('In pixels, before interface scaling. The A- and A+ buttons in the reader change this too.'), 'comic_fit': _("How a comic page is fitted to the window when you open it. The reader's own buttons change this too."), 'audio_night_mode': _('Evens out loud effects and quiet dialogue. This turns passthrough off while it is enabled, because the volume has to be adjusted before your receiver gets the audio.')}
_ACRONYMS = {'gui': 'GUI', 'ssl': 'SSL', 'tls': 'TLS', 'osc': 'OSC', 'mpv': 'MPV', 'hdr': 'HDR', 'av1': 'AV1', 'h265': 'H265', 'hevc': 'HEVC', 'kbps': 'kbps', 'url': 'URL', 'ipc': 'IPC', 'uuid': 'UUID', 'svp': 'SVP', 'id': 'ID', '4k': '4K', 'hi10p': 'Hi10P', 'ui': 'UI'}

def label_for(key):
    if key in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[key]
    return ' '.join((_ACRONYMS.get(w, w.capitalize()) for w in key.split('_')))

def visible_passthrough_keys():
    """The passthrough toggles the current audio mode can actually use.

    S/PDIF has the bandwidth for AC3 and DTS core only, so offering TrueHD
    beside them would be offering a setting that silently does nothing. In
    "Default" and "Force Stereo" nothing is passed through at all.
    """
    from ..player_audio import AUDIO_PASSTHROUGH_CODECS
    usable = AUDIO_PASSTHROUGH_CODECS.get(settings.audio_mode or 'auto', ())
    return [key for codec, key in AUDIO_PASSTHROUGH_KEYS if codec in usable]

def tray_available():
    """True if a system tray icon is actually up right now.

    Not "is pystray importable": the tray can fail to appear for reasons the
    parent process only learns from the child (missing typelib, no
    StatusNotifier host), and the form has to reflect what the user has, not
    what the install implies.
    """
    try:
        from .ui import user_interface
        tray = getattr(user_interface, '_tray', None)
        return tray is not None and tray.available
    except Exception:
        return False

def sections(tab=None):
    """``[(title, [key, ...]), ...]`` — curated groups, then Advanced with
    everything else that's editable.

    ``tab`` limits the result to one settings tab (see TAB_SECTIONS);
    ``None`` returns every group, which is what anything asking "is this key
    reachable at all" wants.

    **Advanced is computed against every curated key, not the tab's.**
    Otherwise each tab would list the other two tabs' settings as
    uncurated — every key would appear three times, and the split would have
    made the page longer rather than shorter.
    """
    schema = settings_schema()
    mode = settings.audio_mode or 'auto'
    curated = {k for _c, k in AUDIO_PASSTHROUGH_KEYS} | set(AUDIO_MODE_ONLY) | set(TRAY_DEPENDENT) | set(BACKGROUND_DEPENDENT) | set(HUD_ONLY) | set(TRICKPLAY_DEPENDENT) | set(LANGUAGE_PRESET_DEPENDENT) | {'audio_exclusive'}
    out = []
    try:
        shown = set(visible_passthrough_keys())
    except Exception:
        shown = set()
    shown |= {k for k, modes in AUDIO_MODE_ONLY.items() if mode in modes}
    if sys.platform in EXCLUSIVE_PLATFORMS:
        shown.add('audio_exclusive')
    if hud_style_selected():
        shown.update(HUD_ONLY)
    if settings.thumbnail_enable:
        shown.update(TRICKPLAY_DEPENDENT)
    if language_preset_builds_rules():
        shown.update(LANGUAGE_PRESET_DEPENDENT)
    keep_running = 'close_to_tray' if tray_available() else 'allow_background'
    shown.add(keep_running)
    if getattr(settings, keep_running, False):
        shown.update(BACKGROUND_DEPENDENT)
    hidden = curated - shown
    wanted = None if tab is None else {t for t, _k in TAB_SECTIONS.get(tab, [])}
    for title, keys in SECTIONS:
        present = [k for k in keys if k in schema and k not in hidden]
        curated.update(present)
        if present and (wanted is None or title in wanted):
            out.append((title, present))
    if tab is not None and tab != ADVANCED_TAB:
        return out
    advanced = sorted((k for k in schema if k not in curated))
    if advanced:
        out.append((_('Advanced'), advanced))
    return out
SEARCH_ALIASES = {'local_kbps': 'bitrate bandwidth quality', 'remote_kbps': 'bitrate bandwidth quality', 'ignore_ssl_cert': 'certificate https', 'tls_client_cert': 'certificate https', 'tls_client_key': 'certificate https', 'tls_server_ca': 'certificate https', 'render_quality': 'upscale upscaler sharpness', 'allow_background': 'tray systray notification area', 'close_to_tray': 'systray notification area', 'start_minimized': 'minimise', 'ui_scale': 'hidpi dpi', 'audio_mode': 'surround 5.1 7.1', 'motion_interpolation': 'stutter', 'clock_12h': 'am 24 format', 'ui_select_key': 'remap rebind keyboard shortcut', 'hud_wake_key': 'remap rebind keyboard shortcut enter summon', 'browse_block_keys': 'keys keyboard contrast brightness gamma saturation hotkey', 'thumbnail_enable': 'scrubbing seeking bif chapter'}

def search_haystack(key, title='', include_aliases=True, include_note=True):
    """Everything a settings search should match ``key`` on.

    The **note** is in here deliberately, and it is what makes the feature
    worth having: a label is two or three words chosen before anyone knew
    what people would call the thing. "banding", "buffer", "stutter",
    "washed out", "controller" and "tray" are all in notes and none is in a
    label. The cost is that a common word can pull in a setting whose note
    merely mentions it -- which is the right way round for a search box,
    since the alternative is a query that finds nothing and a user who
    concludes the setting does not exist.

    The raw key is included because the docs, the issue tracker and
    `conf.json` all name settings that way, so somebody arriving from any
    of them types `auto_download_lookahead` rather than its label.

    ``include_aliases`` and ``include_note`` exist for the test that checks
    a note-dependent case really does depend on the note: it has to be able
    to ask what the haystack looks like with each part taken away. Without
    that, a case could start matching via an alias or an enum label and go
    on claiming to prove the notes are searched.
    """
    parts = [label_for(key), key, key.replace('_', ' '), title]
    note = NOTES.get(key) if include_note else None
    if note:
        parts.append(note)
    if include_aliases:
        alias = SEARCH_ALIASES.get(key)
        if alias:
            parts.append(alias)
    for label, _value in LABELED_ENUMS.get(key) or ():
        parts.append(label)
    parts.extend(ENUMS.get(key) or ())
    return ' '.join((str(p) for p in parts)).lower()

def search(query, tabs=FORM_TABS):
    """``[(tab, title, [key, ...]), ...]`` for settings matching ``query``.

    Every whitespace-separated word must match somewhere in the setting's
    haystack (AND, not OR): with a corpus this small and notes this long,
    OR returns most of the form for any two common words, which is the same
    as returning nothing.

    Built on :func:`sections`, not on the schema, so a control the form is
    currently **hiding** cannot be found -- the passthrough toggles the
    selected audio mode cannot carry, `close_to_tray` on a machine with no
    tray. Finding a setting that the form then refuses to draw would be a
    search result that leads nowhere.

    Advanced groups are searched and returned like any other. The
    disclosure exists so the tab is not a hundred controls long; somebody
    who has typed a query has already narrowed it, and hiding half the
    answers behind a checkbox they cannot see from here would make the
    search quietly incomplete.
    """
    words = [w for w in (query or '').lower().split() if w]
    if not words:
        return []
    out = []
    for tab in tabs:
        for title, keys in sections(tab):
            hits = [k for k in keys if all((w in search_haystack(k, title) for w in words))]
            if hits:
                out.append((tab, title, hits))
    return out

def _classify(ann):
    if ann is bool:
        return 'bool'
    if ann is int:
        return 'int'
    if ann is float:
        return 'float'
    if ann is str:
        return 'str'
    if typing.get_origin(ann) is typing.Union:
        non_none = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(non_none) == 1:
            return _classify(non_none[0])
    return 'skip'

def is_nullable(key):
    """True when the key's annotation is Optional[...].

    settings_schema() collapses Optional[float] to "float" (the form only
    needs the editor kind), but a nullable key can legitimately be set back
    to None -- ui_scale's "Follow display" is exactly that -- and coerce()
    would otherwise raise on it.
    """
    ann = Settings.__annotations__.get(key)
    return typing.get_origin(ann) is typing.Union and type(None) in typing.get_args(ann)

def settings_schema():
    """``{key: "bool"|"int"|"float"|"str"}`` for the editable settings."""
    out = {}
    for key, ann in Settings.__annotations__.items():
        if key.startswith('_') or key in _HIDDEN:
            continue
        kind = _classify(ann)
        if kind != 'skip':
            out[key] = kind
    return out

def get_settings():
    from ..shared_playback import display_settings
    return display_settings(settings.dict(), LABELED_ENUMS)

def coerce(kind, value):
    if kind == 'bool':
        return bool(value)
    if kind == 'int':
        return int(value)
    if kind == 'float':
        return float(value)
    return str(value)

def materialize_language_preset():
    """The language dropdown writes language_config rules (README-style): a
    preset generates rules, Unset clears them, Custom leaves them alone.

    Without it, choosing "Dubbed (shows only)" persists a
    string that nothing reads and track selection never changes."""
    from ..language_config import preset_rules, parse_language_config
    pref = settings.language_preference
    if pref == 'custom':
        return
    if pref == 'unset':
        settings.language_config = None
        return
    settings.language_config = parse_language_config(preset_rules(pref, settings.preferred_language))

def set_setting(key, value):
    """Coerce ``value`` to the key's declared type, apply, and persist.
    Returns True on success, False if the value was invalid for the type.

    ``sync_path`` is *not* handled here — moving the download store is a long
    filesystem operation, see relocate_downloads()."""
    if key == 'hdr_output' and value not in ('auto', 'sdr'):
        return False
    from ..shared_playback import set_picture_setting
    try:
        shared_result = set_picture_setting(key, value)
    except Exception:
        log.error('Could not apply shared picture setting', exc_info=True)
        return False
    if shared_result is not None:
        return shared_result
    schema = settings_schema()
    kind = schema.get(key)
    if kind is None:
        return False
    try:
        if (value is None or value == '') and is_nullable(key):
            setattr(settings, key, None)
        else:
            setattr(settings, key, coerce(kind, value))
    except (ValueError, TypeError):
        return False
    if key in ('language_preference', 'preferred_language'):
        try:
            materialize_language_preset()
        except Exception:
            pass
    settings.save()
    return True

def relocate_downloads(new_path, progress=None):
    """Move the download store to ``new_path`` and persist the *resolved*
    path. Returns (ok, message). Blocking — call from a worker."""
    from ..sync.manager import syncManager
    ok, message = syncManager.relocate(new_path or None, progress=progress)
    if ok:
        settings.sync_path = syncManager.root if new_path else None
        settings.save()
    return (ok, message)

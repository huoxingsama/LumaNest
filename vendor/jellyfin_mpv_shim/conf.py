from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import os
import uuid
import socket
import json
import os.path
import sys
import getpass
import threading
from typing import List, Optional
from .settings_base import SettingsBase, adv_bool, object_types
from .language_config import LanguageRule, parse_language_config
object_types[Optional[List[LanguageRule]]] = parse_language_config
log = logging.getLogger('conf')
config_path = None
CONFIG_VERSION = 6
_NOTIFY_STATES = ('default', 'enabled', 'disabled')
SEGMENT_SETTINGS = {'Intro': 'segment_intro', 'Outro': 'segment_outro', 'Commercial': 'segment_commercial', 'Preview': 'segment_preview', 'Recap': 'segment_recap'}
SEGMENT_ACTIONS = ('off', 'ask', 'always')

def segment_action(segment_type):
    """What to do about a segment of this type: "off", "ask" or "always".

    Unknown types -- a newer server, a hand-edited value -- are "off". A
    segment the shim does not understand must not be skipped out from under
    the viewer, and a Skip button for it would have nothing to say.
    """
    key = SEGMENT_SETTINGS.get(segment_type)
    value = getattr(settings, key, None) if key else None
    return value if value in SEGMENT_ACTIONS else 'off'

def any_segment_wanted():
    """Whether any segment type is set to anything but "off" -- i.e.
    whether it is worth asking the server for segments at all."""
    return any((segment_action(t) != 'off' for t in SEGMENT_SETTINGS))

def wanted_segment_types():
    """The segment types to request, newest-first order irrelevant."""
    return [t for t in SEGMENT_SETTINGS if segment_action(t) != 'off']
SCROLL_MODES = ('continuous', 'aligned', 'row')
_save_lock = threading.Lock()

def select_key():
    """The key that activates whatever the in-window UI has focused.

    **One reader for three producers.** The gamepad's Confirm button, a
    Jellyfin remote's "ok" and the renderer's own binding all have to name
    this same key: each of the first two synthesizes a KEYPRESS, so a
    remap that moves one and not the others leaves that one pressing a key
    nothing listens for. `gamepad.bindings` takes it as a parameter (that
    module is pure), the player reads it per press, and the fallback lives
    here rather than at each call site -- two spellings of a default is
    how they come to differ.

    Falls back when cleared: a config with no select key is an interface
    that cannot be operated by anything but the mouse, and the settings
    screen that would fix it is inside that interface.
    """
    return getattr(settings, 'ui_select_key', None) or 'ENTER'

def get_default_sdir():
    if sys.platform.startswith('win32'):
        if os.environ.get('USERPROFILE'):
            return os.path.join(os.environ['USERPROFILE'], 'Desktop')
        else:
            username = getpass.getuser()
            return os.path.join('C:\\Users', username, 'Desktop')
    else:
        return None

class Settings(SettingsBase):
    config_version: int = 0
    player_name: str = socket.gethostname()
    client_uuid: str = str(uuid.uuid4())
    media_ended_cmd: Optional[str] = None
    pre_media_cmd: Optional[str] = None
    stop_cmd: Optional[str] = None
    auto_play: bool = True
    music_volume: int = 100
    video_volume: int = 100
    idle_cmd: Optional[str] = None
    idle_ended_cmd: Optional[str] = None
    play_cmd: Optional[str] = None
    stop_cmd_on_replace: bool = False
    idle_cmd_delay: int = 60
    mpv_idle_quit: bool = True
    mpv_idle_quit_secs: int = 300
    direct_paths: bool = False
    remote_direct_paths: bool = False
    path_substitutions: list = []
    detail_poster: bool = True
    detail_episode_image: bool = True
    hwdec: str = 'no'
    deinterlace_auto: bool = False
    motion_interpolation: str = 'off'
    deband: str = 'off'
    tone_mapping: str = 'auto'
    hdr_output: str = 'auto'
    render_quality: str = 'default'
    network_buffer: str = 'default'
    always_transcode: bool = False
    transcode_hi10p: bool = False
    transcode_hdr: bool = False
    transcode_hevc: bool = False
    transcode_av1: bool = False
    transcode_4k: bool = False
    transcode_dolby_vision: bool = False
    extract_image_subtitles: bool = True
    allow_transcode_to_h265: bool = False
    prefer_transcode_to_h265: bool = False
    remote_kbps: int = 10000
    local_kbps: int = 2147483
    subtitle_size: int = 100
    subtitle_color: str = '#FFFFFFFF'
    subtitle_position: str = 'bottom'
    fullscreen: bool = False
    enable_gui: bool = True
    browser_fullscreen: bool = False
    start_minimized: bool = False
    window_width: int = 1280
    window_height: int = 720
    window_maximized: bool = False
    window_controls: str = 'auto'
    hide_title_bar: bool = False
    window_controls_fullscreen: bool = False
    remember_window_size: bool = True
    close_to_tray: bool = True
    allow_background: bool = False
    library_image_cache_mb: int = 96
    scroll_wheel_pixels: int = 80
    scroll_mode: str = 'continuous'
    paginated: bool = False
    remember_filters: bool = True
    logo_legibility_live_tv: bool = True
    logo_legibility_library: bool = False
    sync_path: Optional[str] = None
    work_offline: bool = False
    prefer_downloaded: bool = True
    auto_download_enable: bool = False
    auto_download_next_up: bool = True
    auto_download_next_up_limit: int = 10
    auto_download_lookahead: int = 2
    auto_download_lookahead_min: Optional[int] = None
    auto_download_lookahead_max: Optional[int] = None
    auto_download_max_per_pass: Optional[int] = None
    auto_download_max_gb: int = 20
    auto_download_delete_watched: bool = True
    auto_download_keep_watched_hours: int = 24
    auto_download_keep_days: int = 30
    auto_download_interval_mins: int = 60
    auto_download_servers: Optional[str] = None
    media_key_seek: bool = False
    mouse_chapter_nav: bool = False
    mpv_ext: bool = sys.platform.startswith('darwin')
    mpv_ext_path: Optional[str] = None
    mpv_ext_ipc: Optional[str] = None
    mpv_ext_start: bool = True
    mpv_ext_start_retries: int = 10
    mpv_ext_start_retry_delay_ms: int = 3000
    mpv_ext_no_ovr: bool = False
    use_web_seek: bool = False
    headless: bool = False
    display_mirror_summon: bool = False
    log_decisions: bool = False
    mpv_log_level: str = 'info'
    idle_when_paused: bool = False
    stop_idle: bool = False
    kb_stop: Optional[str] = 'q'
    kb_prev: Optional[str] = '<'
    kb_next: Optional[str] = '>'
    kb_watched: Optional[str] = 'w'
    kb_unwatched: Optional[str] = 'u'
    kb_menu: Optional[str] = 'c'
    kb_menu_esc: Optional[str] = 'esc'
    kb_nav_back: Optional[str] = None
    kb_menu_ok: Optional[str] = 'enter'
    kb_menu_left: Optional[str] = 'left'
    kb_menu_right: Optional[str] = 'right'
    kb_menu_up: Optional[str] = 'up'
    kb_menu_down: Optional[str] = 'down'
    kb_pause: Optional[str] = 'space'
    kb_fullscreen: Optional[str] = 'f'
    kb_kill_shader: Optional[str] = 'k'
    shader_pack_enable: bool = True
    shader_pack_custom: bool = False
    shader_pack_remember: bool = True
    shader_pack_profile: Optional[str] = None
    shader_pack_subtype: str = 'lq'
    shader_pack_gpu_api: str = 'auto'
    svp_enable: bool = False
    svp_url: str = 'http://127.0.0.1:9901/'
    svp_socket: Optional[str] = None
    sanitize_output: bool = True
    write_logs: bool = False
    playback_timeout: int = 30
    photo_display_secs: int = 5
    sync_max_delay_speed: int = 50
    sync_max_delay_skip: int = 300
    sync_method_thresh: int = 2000
    sync_speed_time: int = 1000
    sync_speed_attempts: int = 3
    sync_attempts: int = 5
    sync_osd_message: bool = True
    screenshot_menu: bool = True
    check_updates: bool = True
    update_skip_version: Optional[str] = None
    notify_updates: str = 'default'
    lang: Optional[str] = None
    discord_presence: bool = False
    mpris: bool = True
    ignore_ssl_cert: bool = False
    menu_mouse: bool = True
    media_keys: bool = True
    input_gamepad: bool = False
    gamepad_swap_confirm: bool = False
    connect_retry_mins: int = 0
    transcode_warning: bool = True
    lang_filter: str = 'und,eng,jpn,mis,mul,zxx'
    lang_filter_sub: bool = False
    lang_filter_audio: bool = False
    remember_audio_track: bool = True
    remember_subtitle_track: bool = True
    audio_mode: str = 'auto'
    audio_device: Optional[str] = None
    audio_exclusive: bool = False
    audio_passthrough_ac3: bool = True
    audio_passthrough_dts: bool = True
    audio_passthrough_eac3: bool = True
    audio_passthrough_dts_hd: bool = True
    audio_passthrough_truehd: bool = True
    audio_optical_encode_ac3: bool = True
    audio_night_mode: bool = False
    language_preference: str = 'custom'
    preferred_language: str = 'eng'
    force_set_played: bool = False
    screenshot_dir: Optional[str] = get_default_sdir()
    raise_mpv: bool = True
    force_video_codec: Optional[str] = None
    force_audio_codec: Optional[str] = None
    health_check_interval: Optional[int] = 300
    segment_intro: str = 'ask'
    segment_outro: str = 'ask'
    segment_commercial: str = 'off'
    segment_preview: str = 'off'
    segment_recap: str = 'off'
    skip_intro_on_seek: bool = False
    thumbnail_enable: bool = True
    osc_style: str = 'mpvtk'
    hud_scrim: str = 'default'
    hud_autohide: str = 'hover'
    hud_hide_secs: float = 4.0
    hud_auto_scale: bool = True
    ui_scale: Optional[float] = None
    ui_text_scale: float = 1.0
    ui_text_min: int = 0
    theme: str = 'default'
    poster_scale: Optional[float] = None
    reader_font_size: int = 21
    reader_theme: str = 'dark'
    reader_justify: bool = True
    comic_fit: str = 'page'
    grid_fill: str = 'justify'
    backdrop_full_width: bool = True
    clock_12h: bool = False
    mouse_click_pauses: bool = True
    hud_grab_keys: bool = False
    hud_wake_key: str = 'ENTER'
    ui_select_key: str = 'ENTER'
    browse_block_keys: bool = True
    thumbnail_preferred_size: int = 320
    thumbnail_scale: Optional[float] = None
    trickplay_fast_mode: bool = False
    tls_client_cert: Optional[str] = None
    tls_client_key: Optional[str] = None
    tls_server_ca: Optional[str] = None
    language_config: Optional[List[LanguageRule]] = None

    def notify_updates_wanted(self):
        """Whether an unasked-for update notice should be raised here.

        Anything that is not one of the two explicit answers resolves by
        platform, so a hand-edited or older conf.json degrades to the
        default rather than to silence.
        """
        from . import hostinfo
        if self.notify_updates == 'enabled':
            return True
        if self.notify_updates == 'disabled':
            return False
        return not hostinfo.flatpak_managed()

    def _migrate(self, data=None):
        """Apply one-time upgrades for configs written by older versions.

        Every key is written to disk on save (see SettingsBase.dict), so a
        changed default alone never reaches an existing install -- the old
        value is already recorded. Migrations here bring those installs
        forward. Returns whether anything changed (the caller saves).

        Only migrate a default whose old value is actively harmful; a setting
        the user may have deliberately chosen cannot be distinguished from an
        inherited default, so each step here silently overrides a real choice.
        A step that RENAMES a setting is exempt from that caution -- carrying
        a real choice across is the whole point -- and needs ``data``, the
        raw config as read from disk, because the old key is no longer in the
        schema and so never reaches an attribute.
        """
        changed = False
        data = data or {}
        if self.config_version < 3:
            for key in [k for k in self.__fields__ if k.startswith('kb_')]:
                if getattr(self, key, None) == 'None':
                    log.info('Config migration: %s = null (was the string "None", which is what a cleared binding used to be written as).', key)
                    setattr(self, key, None)
                    changed = True
        if self.config_version < 2:
            for prefix, key in (('skip_intro', 'segment_intro'), ('skip_credits', 'segment_outro')):
                if adv_bool(data.get(prefix + '_always')):
                    action = 'always'
                elif prefix + '_enable' in data:
                    action = 'ask' if adv_bool(data[prefix + '_enable']) else 'off'
                else:
                    continue
                if getattr(self, key) != action:
                    log.info('Config migration: %s = %s (from %s_*)', key, action, prefix)
                    setattr(self, key, action)
        if self.config_version < 1:
            if self.transcode_dolby_vision:
                log.info('Config migration: disabling transcode_dolby_vision (mpv now supports Dolby Vision natively).')
                self.transcode_dolby_vision = False
        if self.config_version < 6:
            raw = data.get('notify_updates')
            if not isinstance(raw, str) or raw not in _NOTIFY_STATES:
                want = 'disabled' if raw is not None and (not adv_bool(raw)) else 'default'
                if self.notify_updates != want:
                    log.info('Config migration: notify_updates = %s (was %r; the platform now answers for "default").', want, raw)
                    self.notify_updates = want
                    changed = True
            from . import hostinfo
            if not self.input_gamepad and hostinfo.is_steamos():
                log.info('Config migration: input_gamepad = True (SteamOS).')
                self.input_gamepad = True
                changed = True
        if self.config_version < 5:
            if self.osc_style == 'default':
                log.info('Config migration: osc_style "default" -> "mpv" (one MPV UI; see CONFIG_VERSION 5).')
                self.osc_style = 'mpv'
                changed = True
        if self.config_version < 4:
            from . import conffile, input_conf
            from .constants import APP_NAME
            try:
                if input_conf.migrate(self, conffile.get(APP_NAME, 'input.conf'), raw=data):
                    changed = True
            except Exception:
                log.warning('Could not migrate key bindings to input.conf', exc_info=True)
        if self.config_version != CONFIG_VERSION:
            self.config_version = CONFIG_VERSION
            changed = True
        return changed
    READ_ENCODING = 'utf-8-sig'
    WRITE_ENCODING = 'utf-8'

    def __get_file(self, path: str, mode: str='r', create: bool=True):
        created = False
        encoding = self.WRITE_ENCODING if 'w' in mode or 'a' in mode else self.READ_ENCODING
        if not os.path.exists(path):
            try:
                _fh = open(path, mode, encoding=encoding)
            except IOError as e:
                if e.errno == 2 and create:
                    fh = open(path, 'w', encoding=self.WRITE_ENCODING)
                    json.dump(self.dict(), fh, indent=4, sort_keys=True)
                    fh.close()
                    created = True
                else:
                    raise e
            except Exception:
                log.error('Error opening settings from path: %s' % path)
                return None
        return (open(path, mode, encoding=encoding), created)

    def load(self, path: str, create: bool=True):
        global config_path
        fh, created = self.__get_file(path, 'r', create)
        config_path = path
        try:
            raw = None if created else fh.read()
        except (UnicodeDecodeError, OSError) as e:
            log.error('Could not read settings from json: %s' % e)
            self.__dict__['_unreadable'] = True
            return False
        finally:
            fh.close()
        if created:
            self.config_version = CONFIG_VERSION
            self.save()
        if not created:
            try:
                data = json.loads(raw)
                safe_data = self.parse_obj(data)
                input_params = 0
                for key in safe_data.__fields_set__:
                    setattr(self, key, getattr(safe_data, key))
                    self.__fields_set__.add(key)
                    input_params += 1
                for key, value in data.items():
                    if key not in safe_data.__fields_set__:
                        log.warning('Config item {0} was ignored.'.format(key))
                    elif value != getattr(safe_data, key):
                        log.warning('Config item {0} was was coerced from {1} to {2}.'.format(key, repr(value), repr(getattr(safe_data, key))))
                log.info('Loaded settings from json: %s' % path)
                migrated = self._migrate(data)
                if migrated or input_params < len(self.__fields__):
                    log.info('Saving back due to schema change.')
                    self.save()
            except Exception as e:
                log.error('Error loading settings from json: %s' % e)
                self.__dict__['_unreadable'] = True
                return False
        return True

    def save(self):
        if config_path is None:
            raise FileNotFoundError('Config path not set.')
        if self.__dict__.get('_unreadable'):
            log.error('Refusing to save: the config file could not be read, and writing defaults over it would discard settings this session never saw.')
            return
        with _save_lock:
            tmp = config_path + '.tmp'
            try:
                with open(tmp, 'w', encoding=self.WRITE_ENCODING) as fh:
                    json.dump(self.dict(), fh, indent=4, sort_keys=True)
                    fh.flush()
                os.replace(tmp, config_path)
            except Exception as e:
                log.error('Error saving settings to json: %s' % e)
                return False
        return True
settings = Settings()

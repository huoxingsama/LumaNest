"""Opt-in bridge to the existing shared mpv rendering configuration.

No UI layout changes and no media/track/volume synchronization. Lua owns the
shared state file and remains the only writer of quality-state.json.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import json
import logging
import re
from pathlib import Path
from .conf import settings
log = logging.getLogger(__name__)
ROOT = Path(_cinema_path('data/mpv-common'))
NEUTRAL = {'motion_interpolation': 'off', 'deband': 'off', 'tone_mapping': 'auto', 'render_quality': 'default'}
SUBTITLE_KEYS = {'subtitle_size', 'subtitle_position', 'subtitle_color'}
PRESETS = {'personal': '我的预设 · 自动适配片源', 'fsrcnnx': 'FSRCNNX · 通用超分', 'anime': 'Anime4K · 动画超分', 'fsr': 'FSR · 轻量超分', 'ssim': 'SSimSuperRes · 超分细化', 'custom': '自定义着色器组合'}

def active(player=None):
    """Detect the explicit shared-config include, even before Lua is ready."""
    try:
        from . import conffile
        from .constants import APP_NAME
        text = Path(conffile.get(APP_NAME, 'mpv.conf', True)).read_text(encoding='utf-8-sig')
        for line in text.splitlines():
            line = line.split('#', 1)[0].strip().replace('\\', '/')
            key, sep, value = line.partition('=')
            if sep and key.strip() == 'include' and (value.strip().strip('"\'').lower() == (ROOT / 'mpv.conf').as_posix().lower()):
                return (ROOT / 'scripts/shared-quality.lua').is_file()
    except Exception:
        log.debug('Could not inspect shared playback configuration', exc_info=True)
    return False

def send(player, action, *args):
    player.command('script-message', 'shared-quality', action, *('yes' if v is True else 'no' if v is False else str(v) for v in args))

def _state(player):
    try:
        value = player._get_property('user-data/shared-quality/preset')
        if value in PRESETS or value == 'off':
            return value
    except Exception:
        pass
    try:
        value = json.loads((ROOT / 'quality-state.json').read_text(encoding='utf-8'))['preset']
        if value in PRESETS or value == 'off':
            return value
    except Exception:
        pass
    return 'personal'

def kill_shaders(pm):
    if not active(pm._player):
        return False
    send(pm._player, 'preset', 'off')
    return True

def apply_picture_setting(pm, key):
    """Shared Lua owns these values; startup/next-file paths never rewrite them."""
    return key in NEUTRAL and active(pm._player)

def _manager():
    from .player import playerManager
    return playerManager

def _picture_presets(key):
    from .mpv_options import INTERPOLATION_PRESETS, DEBAND_PRESETS, TONE_MAPPING_PRESETS, RENDER_QUALITY_PRESETS
    tables = {'motion_interpolation': INTERPOLATION_PRESETS, 'deband': DEBAND_PRESETS, 'tone_mapping': TONE_MAPPING_PRESETS, 'render_quality': RENDER_QUALITY_PRESETS}
    choices = {name: dict(props) for name, props in tables[key].items()}
    if key == 'motion_interpolation':
        choices['off'] = {'interpolation': False, 'video-sync': 'audio', 'tscale': 'oversample'}
    elif key == 'deband':
        choices['off'] = {'deband': False}
    elif key == 'tone_mapping':
        choices['auto'] = {'tone-mapping': 'auto'}
    else:
        choices['default'] = {'scale': 'lanczos', 'scale-antiring': 0, 'scale-blur': 0, 'hdr-peak-percentile': 0, 'hdr-contrast-recovery': 0}
        if choices['high'].get('scale') == 'ewa_lanczossharp':
            choices['high']['scale'] = 'ewa_lanczos'
            choices['high']['scale-blur'] = 0.981251
    return choices

def set_picture_setting(key, value, pm=None):
    """Explicit settings-form path. None delegates to the native setter.

    Neutral rows are explicit commands here, unlike the per-file apply path.
    Save the chosen native value, but derive display labels from live state.
    """
    if key not in NEUTRAL and key not in SUBTITLE_KEYS or not active():
        return None
    pm = pm or _manager()
    if pm._player is None:
        raise RuntimeError('播放器尚未就绪，无法应用共享画质设置')
    if value == 'custom':
        pm._player.command('script-binding', 'select/context-menu')
        return True
    if key in SUBTITLE_KEYS:
        if key == 'subtitle_size':
            try:
                value = int(value)
            except (TypeError, ValueError):
                return False
            if value < 10 or value > 1000:
                return False
        elif key == 'subtitle_position' and value not in ('top', 'middle', 'bottom'):
            return False
        elif key == 'subtitle_color' and (not re.fullmatch('#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?', str(value))):
            return False
        setattr(settings, key, value)
        apply_subtitle_setting(pm, key)
        settings.save()
        return True
    choices = _picture_presets(key)
    if value not in choices:
        return False
    send(pm._player, 'set-many', json.dumps(choices[value], ensure_ascii=False, separators=(',', ':')))
    setattr(settings, key, value)
    settings.save()
    return True

def subtitle_view(player):
    """Read actual subtitle values without changing settings or player state."""
    if player is None or not active(player):
        return {}
    view = {}
    for prop, key in (('sub-scale', 'subtitle_size'), ('sub-pos', 'subtitle_position'), ('sub-color', 'subtitle_color')):
        try:
            value = player._get_property(prop)
            if value is None:
                continue
            if key == 'subtitle_size':
                percent = round(float(value) * 100, 6)
                view[key] = int(percent) if percent.is_integer() else percent
                view['size_label'] = f'{percent:g}%'
            elif key == 'subtitle_position':
                percent = float(value)
                match = next((name for name, pos in (('top', 0), ('middle', 80), ('bottom', 100)) if abs(percent - pos) < 1e-06), None)
                view[key] = match or 'custom'
                view['position_label'] = f'当前位置：{percent:g}%'
            else:
                color = str(value)
                if re.fullmatch('#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?', color):
                    color = color.upper()
                    if len(color) == 7:
                        color = '#FF' + color[1:]
                view[key] = color
                view['color_label'] = color
        except Exception:
            log.debug('Could not read live subtitle property %s', prop, exc_info=True)
    return view

def display_settings(values, labels=None, pm=None):
    """Reflect standalone changes in the Cinema form without writing settings."""
    if not active():
        return values
    try:
        pm = pm or _manager()
        player = pm._player
        if player is None:
            return values
        result = dict(values)
        for key in NEUTRAL:
            if labels is not None and (not any((v == 'custom' for _label, v in labels[key]))):
                labels[key].append(('共享自定义 · 打开完整菜单', 'custom'))
            choices = _picture_presets(key)
            found = None
            for choice, props in choices.items():
                matches = True
                for prop, expected in props.items():
                    actual = player._get_property(prop)
                    if isinstance(expected, (int, float)) and (not isinstance(expected, bool)):
                        if actual is None or abs(float(actual) - expected) > 0.0001:
                            matches = False
                            break
                    elif actual != expected:
                        matches = False
                        break
                if matches:
                    found = choice
                    break
            result[key] = found or 'custom'
        subtitle = subtitle_view(player)
        for key in SUBTITLE_KEYS:
            if key in subtitle:
                result[key] = subtitle[key]
        if labels is not None and 'subtitle_position' in subtitle:
            positions = [('顶部', 'top'), ('中间', 'middle'), ('底部', 'bottom')]
            if subtitle['subtitle_position'] == 'custom':
                positions.append((subtitle['position_label'] + ' · 共享自定义', 'custom'))
            labels['subtitle_position'] = positions
        return result
    except Exception:
        log.debug('Could not read shared rendering state for the settings form', exc_info=True)
        return values

def apply_subtitle_setting(pm, explicit_key=None):
    if not active(pm._player):
        return False
    if explicit_key == 'subtitle_size':
        props = {'sub-scale': settings.subtitle_size / 100}
    elif explicit_key == 'subtitle_position':
        positions = {'bottom': 100, 'middle': 80, 'top': 0}
        props = {'sub-pos': positions[settings.subtitle_position]}
    elif explicit_key == 'subtitle_color':
        props = {'sub-color': settings.subtitle_color}
    else:
        return True
    send(pm._player, 'set-many', json.dumps(props, ensure_ascii=False, separators=(',', ':')))
    return True

class _GlobalScope:

    def resolve(self, keys, default):
        return ('default', default)

    def has_any(self, scope):
        return False

class SharedVideoProfiles:
    """Small compatibility facade consumed by existing OSD/HUD profile UI."""
    profile_subtypes = ()
    forced_hwdec = None
    wants_copy_hwdec = False
    applied_settings = {}
    suppressed = False
    active_scope = 'default'

    def __init__(self, menu, player_manager, player):
        self.menu, self.playerManager, self.player = (menu, player_manager, player)
        try:
            player.load_context_menu = False
            player.command('set', 'user-data/shared-quality/cinema-bridge', 'connected')
        except Exception:
            log.debug('Could not select native Cinema context menu', exc_info=True)
        self.profiles = {key: {'displayname': value} for key, value in PRESETS.items()}
        self.profiles['shared-menu'] = {'displayname': '全部共享功能 / 自定义着色器…'}
        self.overrides = _GlobalScope()

    @property
    def current_profile(self):
        current = _state(self.player)
        return None if current == 'off' else current

    @staticmethod
    def profile_is_available(profile):
        return True

    def _default_profile(self):
        return self.current_profile

    def profile_label(self, value):
        return PRESETS.get(value, '关闭额外着色器' if value is None else str(value))

    def scope_keys(self, item, client=None, force=False):
        return {}

    def scope_rows(self, item, force=True):
        return [('default', None, self.current_profile, True)]

    def menu_action(self):
        if self.menu.is_menu_shown:
            self.menu.hide_menu()
        self.player.command('script-binding', 'select/context-menu')

    def set_scope_profile(self, item, scope, profile):
        if profile == 'shared-menu':
            self.menu_action()
        else:
            if profile is not None and profile not in PRESETS:
                raise ValueError('Unknown shared preset: ' + str(profile))
            send(self.player, 'preset', profile or 'off')
        return True

    def clear_scope(self, item, scope):
        return self.set_scope_profile(item, scope, 'personal')

    def load_profile(self, profile, reset=True):
        return self.set_scope_profile({}, 'default', profile)

    def unload_profile(self):
        send(self.player, 'preset', 'off')

    def apply_for_item(self, *args, **kwargs):
        return True

    def warm_library(self, *args, **kwargs):
        return None

    def suspend_for_still(self):
        return None

    def resume_after_still(self):
        return None

def profile_factory(menu, player_manager, player, fallback):
    if active(player):
        return SharedVideoProfiles(menu, player_manager, player)
    return fallback(menu, player_manager, player)

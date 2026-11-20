"""Assembling the option set mpv is constructed with.

A pure function of ``settings`` plus three facts the player knows (which
backend is in use, whether the browser wants a window, whether trickplay
started), so the answers can be checked without constructing an mpv -- which,
on the libmpv backend, means opening a real window.

Nothing here touches mpv, and nothing here imports ``player``. The
directory-creating calls (``conffile.get_dir`` / ``conffile.get``)
deliberately live in ``_init_mpv`` instead: ``confdir`` is a path lookup and
safe to call, but creating the config tree is a side effect and belongs with
the code that is actually starting a player.

Before editing this file, read ``docs/mpv-backends.md``.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import ctypes
import glob
import logging
import os
import platform
import sys
from collections import OrderedDict
from . import conffile
from .conf import settings
from .constants import APP_NAME, DESKTOP_ID, USER_APP_NAME
from .utils import get_resource
log = logging.getLogger('mpv_options')
REPLACES_OSC = ('mpv', 'mpvtk', 'none', 'custom')
_HWDEC_STATIC = {'no': 'no', 'auto': 'auto', 'auto-safe': 'auto-safe', 'auto-copy': 'auto-copy', 'auto-copy-safe': 'auto-copy-safe'}
HWDEC_THRESHOLD_H = 1080
NAIVE_HWDEC = frozenset({'yes', 'auto', 'auto-safe', 'auto-unsafe', 'auto-copy', 'auto-copy-safe', 'auto-copy-unsafe'})

def hwdec_pinned_by_config():
    """The ``hwdec`` the user's own ``mpv.conf`` sets, or None.

    **A pin, not a default: where this answers, nothing else writes hwdec at
    all** -- not the setting, not the copy upgrade, not a shader profile
    (docs/configuration.md under ``hwdec``).

    Deliberately a plain top-level scan rather than an mpv-accurate parse:
    profile sections (``[name]``) are conditional, so reading them as
    unconditional would pin on a value that may never apply. A `hwdec`
    reachable only inside a profile is therefore *not* a pin -- the safe
    direction, since mpv's own precedence still applies it where it fires.
    """
    from . import conffile
    from .constants import APP_NAME
    try:
        path = conffile.get(APP_NAME, 'mpv.conf', True)
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.split('#', 1)[0].strip()
                if line.startswith('['):
                    break
                if not line:
                    continue
                key, sep, value = line.partition('=')
                if sep and key.strip().lstrip('-') == 'hwdec':
                    return value.strip().strip('"\'') or None
    except Exception:
        log.debug('could not read mpv.conf for an hwdec pin', exc_info=True)
    return None
_COPY_OF = {'auto': 'auto-copy', 'auto-safe': 'auto-copy-safe'}

def hwdec_for(height=None, needs_copy=False):
    """mpv's ``hwdec`` for the configured mode and this file's height.

    ``needs_copy`` says something downstream needs frames in system RAM, which
    the direct modes cannot feed. It **upgrades, it never enables**: off stays
    off, since turning hardware decoding off is not a preference about *which*
    hardware decoding. That asymmetry is the whole design -- a pack knows what
    it will do with the frames, not what the machine can do
    (docs/mpv-backends.md section 11).

    ``height`` is None when nothing is loaded, which resolves the threshold
    mode to "no": the failure modes this setting is cautious about happen at
    *decoder init*, so starting a file with hwdec on and turning it off is the
    wrong way round.

    ``--disable-hwdec`` overrides everything and is checked here, so there is
    one place that can be wrong. Per-run rather than a config write, for the
    case where hardware decoding stops the window opening at all.
    """
    from .args import get_args
    try:
        if getattr(get_args(), 'disable_hwdec', False):
            return 'no'
    except Exception:
        log.debug('could not read the hwdec override', exc_info=True)
    pinned = hwdec_pinned_by_config()
    if pinned is not None:
        return None

    def resolved(value):
        return _COPY_OF.get(value, value) if needs_copy else value
    mode = (settings.hwdec or 'no').strip().lower()
    if mode in _HWDEC_STATIC:
        return resolved(_HWDEC_STATIC[mode])
    if mode == 'over-1080p':
        try:
            on = int(height or 0) > HWDEC_THRESHOLD_H
        except (TypeError, ValueError):
            on = False
        return resolved('auto') if on else 'no'
    log.warning('Unknown hwdec setting %r; using software decoding.', mode)
    return 'no'

def resolve_osc_style():
    """Which in-player UI to load, after the aliases and fallbacks.

    ``settings.osc_style`` is not the answer on its own: it may hold a legacy
    alias, and `enable_gui` can force a fallback. The result is stored on the
    player as ``_osc_style_resolved`` because the c-menu routing, enable_osc
    and the skip-button path all key off the resolved value rather than the
    configured one.

    **Nothing resolves TO "default" any more.** It is an inbound legacy value
    only -- a pre-CONFIG_VERSION-5 conf.json, or one hand-edited -- and the
    alias below turns it into "mpv". It survived as an internal resolution
    for exactly one caller, the `thumbnail_osc_builtin` opt-out, and went
    with it; `build_mpv_options` still maps the value because it is a pure
    style -> options function and that mapping is still what "default"
    means.
    """
    osc_style = settings.osc_style
    if osc_style == 'jellyfin':
        osc_style = 'mpvtk'
    if osc_style == 'default':
        osc_style = 'mpv'
    if osc_style == 'mpvtk' and (not settings.enable_gui):
        osc_style = 'mpv'
    return osc_style

def osc_script_opts(osc_style):
    """The ``osc-*`` entries appended to ``script-opts`` at construction.

    Every osc.lua and every fork of it reads ``script-opts`` under the
    ``osc`` prefix at startup, so these are read by construction rather than
    in time. They are appended, never written over the whole property, which
    is shared with the user's own scripts.

    ``osc-idlescreen`` is unconditional: it has to cover the styles where
    mpv loads the OSC itself as well as the one we load by hand, and it is
    inert for a style with no OSC and for a third-party one reading another
    prefix.

    ``osc-windowcontrols`` is **not**, and the difference is who is drawing
    window buttons over PLAYBACK. Only ``mpvtk`` does -- its HUD asks
    ``window_chrome.window_controls`` for a set -- so only there does a
    second set from the OSC mean two. Under every other style the OSC on
    screen is the only thing that could offer any: the stock OSC, our
    ``trickplay-osc.lua`` fork under ``mpv``, and whatever ``custom``
    loaded. Their default is "auto", i.e. "whenever the window has no
    border" -- which is precisely the ``hide_title_bar`` case, where saying
    no left a windowed video with no title bar and no buttons anywhere and
    nothing but an undocumented key to close it (#727 follow-up).

    The library's own top bar is not the answer to that: it is not on screen
    while a video is playing.
    """
    opts = ['osc-idlescreen=no']
    if osc_style == 'mpvtk':
        opts.append('osc-windowcontrols=no')
    return opts

def mpv_scripts(osc_style, trickplay):
    """Lua scripts to load at construction, in load order.

    ``trickplay`` is whether the TrickPlay worker actually started -- not
    whether it was asked to. thumbfast is the script side of that feature, so
    a worker that failed to come up must not advertise it to mpv.

    **The classic OSC is deliberately not here.** Which one to load depends
    on the mpv that is about to be built -- the stock OSC where it can drive
    our previews (0.41+, the OSC Preview API), our fork where it cannot --
    and the version is not knowable until the player exists. The libmpv
    client API cannot stand in for it either: 0.40 and 0.41 both report 2.5.
    So `PlayerManager._load_classic_osc` picks one and `load-script`s it
    right after construction, while nothing is on screen yet.
    """
    scripts = []
    if settings.menu_mouse:
        scripts.append(get_resource('mouse.lua'))
    if trickplay:
        scripts.append(get_resource('thumbfast.lua'))
    return scripts
FLATPAK_MARKER = '/.flatpak-info'
FLATPAK_CPLUGIN_DIR = '/app/etc/mpv/scripts'

def bundled_cplugins(ext_mpv):
    """mpv cplugins this package ships that mpv will not find by itself.

    Only the Flatpak has any: ``mpv_inhibit_gnome.so``, which asks
    ``org.gnome.SessionManager`` not to blank the screen during playback -- a
    bus name the manifest has granted, and a plugin it has installed, since
    3.0.0. **It has never once loaded.** Measured against the shipped Flathub
    build with a stand-in session manager on the bus: zero ``Inhibit`` calls
    during playback, and one with the two changes below.

    Two independent reasons it does not load, and **neither fix works without
    the other**:

    * ``--config-dir`` (set below unless the user asked for
      ``mpv_ext_no_ovr``) switches off every other config directory, the
      global one included, so ``/app/etc/mpv/scripts`` is never scanned. mpv's
      own man page says so and a probe confirms it: a script in ``MPV_HOME``
      loads with ``MPV_HOME`` alone and stops the moment ``config_dir`` is
      set. Naming the file explicitly is what gets past that.
    * python-mpv ``dlopen``s libmpv ``RTLD_LOCAL``, so a cplugin linked
      against it fails with ``undefined symbol: mpv_observe_property``.
      Promoting those symbols to global scope is what gets past *that*, and
      promoting an object that is already loaded works -- so there is **no
      import-order rule here to get wrong** and this is strictly additive.

    **The promotion does not by itself load anything.** With ``config_dir``
    set nothing is scanned, so making libmpv's symbols global only makes a
    cplugin *able* to load; the explicit paths below decide which ones do.

    Those paths are every ``.so`` this package installs into
    ``FLATPAK_CPLUGIN_DIR`` -- one file today (the GNOME inhibitor, see the
    Flatpak manifest), and whatever a later release puts beside it. That is
    the intended reach and not an accident: a cplugin we ship into our own
    ``/app`` is one we mean to load. Said plainly because this paragraph used
    to say "one plugin", which was a true statement about what ships and a
    loose one about what the code does.

    libmpv only: the external backend runs a real mpv binary, which scans for
    and loads its own cplugins, and on a box with no libmpv the ``CDLL``
    raises ``OSError``.
    """
    if ext_mpv or not os.path.exists(FLATPAK_MARKER):
        return []
    plugins = sorted(glob.glob(os.path.join(FLATPAK_CPLUGIN_DIR, '*.so')))
    if not plugins:
        return []
    try:
        ctypes.CDLL('libmpv.so.2', mode=ctypes.RTLD_GLOBAL)
    except OSError:
        log.debug("could not promote libmpv's symbols for a bundled cplugin", exc_info=True)
        return []
    return plugins

def mpv_binary_location():
    """Path to the mpv binary for the external-mpv backend, or None to let
    the library find one.

    Only the frozen macOS build ships its own; everywhere else an unset
    setting means "whatever is on PATH".
    """
    mpv_location = settings.mpv_ext_path
    if mpv_location is None and platform.system() == 'Darwin' and getattr(sys, 'frozen', False):
        mpv_location = get_resource('mpv')
    return mpv_location
OSC_OPTION = 'osc'
GAMEPAD_OPTION = 'input_gamepad'
INTERPOLATION_PRESETS = {'off': {}, 'smooth': {'video-sync': 'display-resample', 'interpolation': True, 'tscale': 'oversample'}, 'blend': {'video-sync': 'display-resample', 'interpolation': True, 'tscale': 'linear'}, 'hq': {'video-sync': 'display-resample', 'interpolation': True, 'tscale': 'mitchell'}}
DEBAND_PRESETS = {'off': {}, 'light': {'deband': True, 'deband-iterations': 1, 'deband-threshold': 32, 'deband-range': 16, 'deband-grain': 16}, 'standard': {'deband': True, 'deband-iterations': 2, 'deband-threshold': 48, 'deband-range': 14, 'deband-grain': 24}, 'strong': {'deband': True, 'deband-iterations': 4, 'deband-threshold': 64, 'deband-range': 12, 'deband-grain': 32}}
TONE_MAPPING_PRESETS = {'auto': {}, 'bt.2390': {'tone-mapping': 'bt.2390'}, 'bt.2446a': {'tone-mapping': 'bt.2446a'}, 'spline': {'tone-mapping': 'spline'}, 'hable': {'tone-mapping': 'hable'}, 'reinhard': {'tone-mapping': 'reinhard'}, 'clip': {'tone-mapping': 'clip'}}
HDR_OUTPUT_PRESETS = {'auto': {}, 'sdr': {'target-prim': 'bt.709', 'target-trc': 'srgb', 'target-peak': 100, 'hdr-reference-white': 100, 'target-colorspace-hint-mode': 'target', 'target-colorspace-hint': 'yes', 'inverse-tone-mapping': False, 'icc-profile-auto': False, 'icc-profile': '', 'tone-mapping': 'bt.2390', 'gamut-mapping-mode': 'perceptual'}}
RENDER_QUALITY_PRESETS = {'default': {}, 'high': {'scale': 'ewa_lanczossharp', 'scale-antiring': 0.6, 'hdr-peak-percentile': 99.995, 'hdr-contrast-recovery': 0.3}}
BUFFER_PRESETS = {'default': {}, 'large': {'demuxer-max-bytes': 400 * 1024 * 1024, 'demuxer-max-back-bytes': 100 * 1024 * 1024, 'demuxer-readahead-secs': 20}, 'huge': {'demuxer-max-bytes': 1024 * 1024 * 1024, 'demuxer-max-back-bytes': 200 * 1024 * 1024, 'demuxer-readahead-secs': 60}}
PRESET_SETTINGS = OrderedDict((('motion_interpolation', (INTERPOLATION_PRESETS, 'off')), ('deband', (DEBAND_PRESETS, 'off')), ('tone_mapping', (TONE_MAPPING_PRESETS, 'auto')), ('render_quality', (RENDER_QUALITY_PRESETS, 'default')), ('network_buffer', (BUFFER_PRESETS, 'default')), ('hdr_output', (HDR_OUTPUT_PRESETS, 'auto'))))

def preset_keys(key):
    """Every mpv property any preset of ``key`` writes.

    What "off" has to put back is this whole set, not the ones the CURRENT
    preset happens to name -- somebody who used interpolation's `hq` and
    then switched to off must get their `tscale` back too.
    """
    presets, _fallback = PRESET_SETTINGS[key]
    return tuple(sorted({prop for props in presets.values() for prop in props}))

def preset_props(key):
    """``{mpv property: value}`` for ``key``'s configured preset, or ``{}``.

    **``{}`` for the off value is NOT the same as writing mpv's defaults
    back**: these are properties a user may have set in their own ``mpv.conf``,
    and writing an idea of "not doing this" would undo their config with
    nothing here to put it back. Turning a feature off is the player's job
    (``PlayerManager._apply_render_preset``, docs/mpv-backends.md section 6).

    An unrecognised value reads as off -- a plain string in a JSON file
    somebody can type into, where the alternative to a default is a KeyError
    out of the middle of starting playback.
    """
    presets, fallback = PRESET_SETTINGS[key]
    return dict(presets.get(getattr(settings, key, fallback), presets[fallback]))
INTERPOLATION_KEYS = preset_keys('motion_interpolation')

def interpolation_props():
    """``{mpv property: value}`` for the configured preset, or ``{}``.
    See :func:`preset_props`."""
    return preset_props('motion_interpolation')

def deinterlace_value(override=None):
    """mpv's ``deinterlace`` for the configured mode, or for ``override``.

    ``override`` is the playback HUD's per-session toggle: ``True`` forces
    it on for a file whose interlacing is not flagged, ``None`` means
    nobody has said anything and the setting decides.
    """
    if override is not None:
        return 'yes' if override else 'no'
    return 'auto' if settings.deinterlace_auto else 'no'

def build_mpv_options(osc_style, scripts, ext_mpv, browser_wants_window):
    """The full option set to construct mpv with.

    ``osc_style`` comes from :func:`resolve_osc_style` and ``scripts`` from
    :func:`mpv_scripts` -- both are passed in rather than recomputed, because
    the player has to interleave the TrickPlay worker's startup between them.

    ``ext_mpv`` is the backend in use (``player.is_using_ext_mpv``).
    ``browser_wants_window`` is whether the in-window UI should be on screen
    as soon as mpv comes up; see the force_window comment below for why the
    player, not this function, decides it.
    """
    mpv_options = OrderedDict()
    if ext_mpv:
        mpv_options.update({'start_mpv': settings.mpv_ext_start, 'ipc_socket': settings.mpv_ext_ipc, 'mpv_location': mpv_binary_location(), 'player-operation-mode': 'cplayer', 'start_retries': settings.mpv_ext_start_retries, 'start_retry_delay_ms': settings.mpv_ext_start_retry_delay_ms, 'discard_output': True})
    hwdec = hwdec_for()
    if hwdec is not None:
        mpv_options['hwdec'] = hwdec
    if osc_style in REPLACES_OSC:
        mpv_options['osc'] = False
    elif not ext_mpv:
        mpv_options['osc'] = True
    scripts = list(scripts) + bundled_cplugins(ext_mpv)
    if scripts:
        if settings.mpv_ext:
            mpv_options['script'] = scripts
        else:
            mpv_options['scripts'] = (';' if sys.platform.startswith('win32') else ':').join(scripts)
    if not (settings.mpv_ext and settings.mpv_ext_no_ovr):
        mpv_options['config'] = True
        mpv_options['config_dir'] = conffile.confdir(APP_NAME)
    if settings.tls_client_cert and settings.tls_client_key:
        mpv_options['tls_cert_file'] = settings.tls_client_cert
        mpv_options['tls_key_file'] = settings.tls_client_key
        if settings.tls_server_ca:
            mpv_options['tls_ca_file'] = settings.tls_server_ca
    mpv_options['audio_display'] = 'no'
    mpv_options['title'] = '${?media-title:${media-title} - }%s' % USER_APP_NAME
    if settings.hide_title_bar:
        mpv_options['border'] = False
    width = max(320, int(settings.window_width or 1280))
    height = max(240, int(settings.window_height or 720))
    mpv_options['geometry'] = '%dx%d' % (width, height)
    if settings.window_maximized:
        mpv_options['window_maximized'] = True
    mpv_options['auto_window_resize'] = False
    if osc_style == 'mpvtk' and browser_wants_window:
        mpv_options['force_window'] = True
    if sys.platform not in ('win32', 'darwin'):
        mpv_options['x11_name'] = DESKTOP_ID
        mpv_options['wayland_app_id'] = DESKTOP_ID
    if settings.input_gamepad:
        mpv_options[GAMEPAD_OPTION] = True
    return mpv_options

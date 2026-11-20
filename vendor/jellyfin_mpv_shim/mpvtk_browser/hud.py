"""Playback HUD — YouTube-on-TV style controls inside the mpv window.

This IS the jellyfin-styled player UI (``osc_style: mpvtk``, the default).
It is built here and driven by ``hud_control.HudController``; playstate comes
from the same ``push_playstate`` snapshots that feed the audio now-playing
bar, kept fresh by the shared 1s ticker.

The summon and auto-hide lifecycle is the renderer's, not ours. See
docs/browser-shell.md section 14.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import time
from typing import Optional
from ..conf import settings
from ..i18n import _
from ..mpvtk.layout import natural_size
from ..mpvtk.widgets import trigger_box
from ..mpvtk.widgets import Box, Button, Column, Dialog, Dropdown, Element, Gradient, Menu, Row, Slider, Spacer, Stack, Text, VScroll
from . import theme, timefmt, window_chrome
from .components import media_info
from .window_chrome import WINDOW_CONTROL_W
log = logging.getLogger('mpvtk_browser.hud')
SCRIM_FRAC = 0.42
SCRIM_MAX = 200
TOP_SCRIM_FRAC = 0.2
TOP_SCRIM_MAX = 130
PANEL_BG = '000000'
PANEL_ALPHA = 170
_SKIP_BOTTOM = 106
_SKIP_SIZE = 18
_SKIP_PAD = 10
_SKIP_RIGHT = 24
_SKIP_BG = '202020'
_SKIP_BG_HOVER = '3a3a3a'
_SKIP_FG = 'ffffff'
_SKIP_ALPHA = 180

def _episode_context(st):
    """``"Series   ·   S1E2"`` for an episode, ``""`` for anything else.

    The old lua OSC got this free from mpv's media-title, which the shim
    sets to ``Media.get_proper_title()`` ("Show - s1e02 - Name"). The mpvtk
    HUD reads the playstate instead, which carried only the item's own name
    — so an episode showed "Pilot" with no clue which show it belonged to.

    Either part alone is still worth showing: a season/episode number with
    no series, or a series whose numbering the server doesn't have.
    """
    if not st:
        return ''
    season, episode = (st.get('season'), st.get('episode'))
    se = 'S%sE%s' % (season, episode) if season is not None and episode is not None else ''
    return '   ·   '.join((p for p in (st.get('series_name'), se) if p))

def _clock(secs):
    secs = int(secs or 0)
    if secs >= 3600:
        return '%d:%02d:%02d' % (secs // 3600, secs % 3600 // 60, secs % 60)
    return '%d:%02d' % (secs // 60, secs % 60)

def _hud_action(b, verb, arg=None):
    b._ctl(lambda c: c.hud_action(verb, arg))
HUD_ICON = 30
MIN_TARGET_PX = 24
LEGACY_SCALE_FLOOR = 0.72

def hud_scale_floor():
    """How far the bar may shrink before controls are given up instead.

    The trigger box is the smallest interactive thing on the bar -- a
    transport button is its glyph plus `Button`'s 10px padding either
    side, which does not shrink -- so it is what the target applies to,
    and `widgets.trigger_box` is asked rather than the ratio copied.
    """
    if not settings.hud_auto_scale:
        return LEGACY_SCALE_FLOOR
    return MIN_TARGET_PX / trigger_box(HUD_ICON)

def _option_picker(b, node_id, icon, tip, options, verb, icon_size=None):
    """Icon-trigger dropdown over osc_bridge option dicts
    ([{id, label, selected}]); selecting routes through hud_action so
    the change lands exactly like the lua OSC's menus."""
    sel = next((i for i, o in enumerate(options) if o.get('selected')), 0)
    return Dropdown(node_id, [o.get('label') or '' for o in options], selected=sel, force=True, trigger_icon=icon, tip=tip, icon_size=icon_size, on_select=lambda i, v, opts=options: _hud_action(b, verb, opts[i]['id']))

def _secondary_available(st, subs):
    """Whether the subtitle picker should offer 'Secondary…': a primary track
    is active AND there is a second, mpv-renderable track to pick. A secondary
    with no primary is just a differently-placed primary, so it's suppressed."""
    sub2 = st.get('secondary_subtitles') or []
    primary_on = any((s.get('id', -1) != -1 and s.get('selected') for s in subs))
    return len(sub2) > 1 and primary_on

def _subtitle_picker(b, st, subs, icon_size=None):
    """The primary subtitle dropdown, with a trailing 'Secondary…' entry that
    opens the secondary-track submenu (see _menu_rows' 'secondary_sub'). Custom
    rather than _option_picker because that last row opens a menu instead of
    setting a track."""
    labels = [o.get('label') or '' for o in subs]
    ids = [o.get('id') for o in subs]
    sel = next((i for i, o in enumerate(subs) if o.get('selected')), 0)
    secondary = _secondary_available(st, subs)
    if secondary:
        cur = next((o.get('label') for o in st.get('secondary_subtitles') or [] if o.get('selected') and o.get('id', -1) != -1), None)
        labels.append('%s  ·  %s' % (_('Secondary…'), cur) if cur else _('Secondary…'))

    def on_select(i, v):
        if secondary and i == len(labels) - 1:
            _open_hud_menu(b, 'secondary_sub', anchor='hud-sub')
        else:
            _hud_action(b, 'set-sub', ids[i])
    return Dropdown('hud-sub', labels, selected=sel, force=True, trigger_icon='closed_caption', tip=_('Subtitle Track'), icon_size=icon_size, on_select=on_select)

def _chapters(b, icon_size=None):
    if b.controller is None or not hasattr(b.controller, 'chapters'):
        return []
    try:
        return b.controller.chapters() or []
    except Exception:
        return []

def _chapter_jump(b, direction):
    """Seek to the previous/next chapter start (the lua OSC's
    ch_prev/ch_next).

    The rule -- prev re-seeks the current chapter's start unless pressed
    within its first 2 seconds, like mpv's 'add chapter -1' -- lives in
    player.chapter_target, because the mouse's back/forward buttons ask the
    same question (mouse_chapter_nav) and two copies of it would drift.
    Going through the player also puts the jump through SyncPlay, which
    working the target out here and seeking to it did not.
    """
    b._ctl(lambda c: c.chapter_seek(direction))

def _pickers(b, menu_state, pos, chapters, icon_size=None):
    """Right-aligned controls: chapters, audio/subtitle tracks, quality —
    each only when there is a real choice to make.

    Returns (shed key or None, widget) pairs, in display order: whether
    there is *room* is no longer decided here but by `_shed`, once the
    whole row can be measured. The track pickers are None because a file
    with two audio languages and no way to pick between them is not a
    narrower bar, it is a broken one."""
    out = []
    if chapters:
        cur = 0
        for i, ch in enumerate(chapters):
            if ch['time'] <= pos:
                cur = i
        labels = ['%s  %s' % (_clock(ch['time']), ch['title'] or _('Chapter %d') % (i + 1)) for i, ch in enumerate(chapters)]
        out.append(('chapters', Dropdown('hud-chapters', labels, selected=cur, force=True, trigger_icon='bookmark', tip=_('Chapters'), icon_size=icon_size, on_select=lambda i, v, chs=chapters: b._ctl(lambda c: c.seek(chs[i]['time'])))))
    st = menu_state if menu_state and menu_state.get('has_media') else None
    if st is None:
        return out
    audio = st.get('audio') or []
    if len(audio) > 1:
        out.append((None, _option_picker(b, 'hud-audio', 'audiotrack', _('Audio Track'), audio, 'set-audio', icon_size=icon_size)))
    subs = st.get('subtitles') or []
    if len(subs) > 1:
        out.append((None, _subtitle_picker(b, st, subs, icon_size)))
    quality = st.get('quality') or {}
    if quality.get('options'):
        out.append(('quality', _option_picker(b, 'hud-quality', 'hd', _('Video Quality'), quality['options'], 'set-quality', icon_size=icon_size)))
    return out
_SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
_ASPECTS = [(None, -1.0, '-1'), ('16:9', 16.0 / 9.0, '16:9'), ('4:3', 4.0 / 3.0, '4:3'), ('2.35:1', 2.35, '2.35:1')]

def _open_hud_menu(b, kind, anchor=None):
    """``anchor`` names the button node the menu hangs off (gear or the
    top bar's SyncPlay button); omitted on submenu/Back transitions so
    the menu stays where it opened."""
    if kind == 'syncplay':
        _hud_action(b, 'syncplay-refresh')
    if kind == 'profiles':
        _hud_action(b, 'profiles-scopes')
    if anchor is not None:
        b.hud.menu_anchor = anchor
    b.hud.menu = kind
    b.invalidate()

def _close_hud_menu(b):
    b.hud.menu = None
    b.invalidate()

def _ctl_get(b, name, default):
    fn = getattr(b.controller, name, None)
    if fn is None:
        return default
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default
SHED_ORDER = ('ends_at', 'volbar', 'ch_btns', 'chapters', 'favorite', 'quality', 'clock', 'seek_btns')

def _shed(items, avail, gap):
    """Which of ``items``' optional controls to give up so the row fits.

    ``items`` is the whole transport row as (shed key or None, widget) in
    display order; the answer is the shortest prefix of `SHED_ORDER` that
    makes it fit, so nothing is sacrificed for room that was already
    there.

    **This replaced five hand-tuned pixel breakpoints, which had drifted
    out of true.** Measured across 400-1400px, the bar overflowed its own
    window at every width between ~700 and ~1000 -- by up to 113px --
    because controls were added to it over time and the numbers never
    moved. Nothing showed it, because the overflow was being absorbed by
    the four track pickers (the only children of the row with no shrink
    floor), which collapsed to zero width while the renderer went on
    drawing their full-size glyphs across each other: #721.

    `natural_size` exists for exactly this and says so -- "no hardcoded
    breakpoints" -- and this bar was the one that never took it up.

    **The now-playing bar already worked this way** (`music._np_plan`,
    whose docstring reports the same discovery: "thresholds are guesses
    about a sum, and they were wrong"). Two bars, one lesson, learnt once
    -- which is why the second one still had the bug. That bar estimates
    the sum from per-control width constants rather than measuring; it
    fits at every width today, so it is left alone, but the constants are
    a second authority on how wide a control is and would drift the same
    way the breakpoints did.
    """
    width = {}
    for _key, el in items:
        width[id(el)] = natural_size(el)[0]

    def fits(dropped):
        keep = [el for key, el in items if key not in dropped]
        if not keep:
            return True
        return sum((width[id(el)] for el in keep)) + gap * (len(keep) - 1) <= avail
    present = {key for key, _el in items if key}
    dropped = set()
    for key in SHED_ORDER:
        if fits(dropped):
            break
        if key in present:
            dropped.add(key)
    return dropped

def _menu_rows(b, st, shed=None):
    """(label, icon, action) rows for the open settings-menu level.
    ``st`` is the osc_bridge state blob ({} when unavailable).

    ``shed`` is what the bar gave up on its last build (`_shed`), for the
    one row whose presence depends on whether the bar already has a button
    for it. ``None`` means a caller that does not know, and then the row is
    kept: an unreachable setting is worse than a duplicated one.

    The window width used to answer this, against the breakpoint that
    decided the button. There is no such breakpoint now, and width was
    always the weaker question -- at 600px it said the Video Quality button
    was on screen while the bar it sat in overflowed by 36."""
    kind = b.hud.menu
    rows = []
    label: Optional[str]

    def leaf(fn):

        def run():
            fn()
            _close_hud_menu(b)
        return run

    def option_rows(group, verb):
        for o in (group or {}).get('options') or []:
            rows.append((o.get('label') or '', 'check' if o.get('selected') else None, leaf(lambda oid=o.get('id'): _hud_action(b, verb, oid))))

    def with_current(label, current):
        return '%s  ·  %s' % (label, current) if current else label
    sub_style = st.get('sub_style') or {}
    if kind == 'root':
        quality = st.get('quality') or {}
        if quality.get('options') and (shed is None or 'quality' in shed):
            rows.append((with_current(_('Change Video Quality'), quality.get('current')), None, lambda: _open_hud_menu(b, 'quality')))
        speed = float(_ctl_get(b, 'get_speed', 1.0))
        rows.append((with_current(_('Playback Speed'), '%gx' % speed), None, lambda: _open_hud_menu(b, 'speed')))
        rows.append((_('Aspect Ratio'), None, lambda: _open_hud_menu(b, 'aspect')))
        output = _ctl_get(b, 'hdr_output', 'auto')
        rows.append(('HDR 转 SDR 观看', 'check' if output == 'sdr' else None, leaf(lambda: b._ctl(lambda c: c.set_hdr_output('sdr')))))
        rows.append(('自动画面输出', 'check' if output == 'auto' else None, leaf(lambda: b._ctl(lambda c: c.set_hdr_output('auto')))))
        on, auto = _ctl_get(b, 'deinterlace', (False, False))
        rows.append((with_current(_('Deinterlace'), _('Auto') if auto and (not on) else None), 'check' if on else None, leaf(lambda: b._ctl(lambda c: c.toggle_deinterlace()))))
        profiles = st.get('profiles') or {}
        if profiles.get('options'):
            rows.append((with_current(_('Change Video Playback Profile'), profiles.get('current')), None, lambda: _open_hud_menu(b, 'profiles')))
        for key, label in (('size', _('Subtitle Size')), ('position', _('Subtitle Position')), ('color', _('Subtitle Color'))):
            group = sub_style.get(key)
            if group:
                rows.append((with_current(label, group.get('current')), None, lambda k=key: _open_hud_menu(b, 'sub_' + k)))
        rows.append((_('Night Mode (Auto Volume Adj)'), 'check' if settings.audio_night_mode else None, leaf(lambda: b._ctl(lambda c: c.toggle_night_mode()))))
        rows.append((_('Playback Info'), None, leaf(lambda: _open_info(b))))
        if st.get('allow_screenshot'):
            rows.append((_('Screenshot'), None, leaf(lambda: _hud_action(b, 'screenshot'))))
        if st.get('has_media'):
            rows.append((_('Quit and Mark Unplayed'), None, leaf(lambda: _hud_action(b, 'unwatched-quit'))))
        return rows
    if kind.startswith('profiles:'):
        rows.append((_('Back'), 'arrow_back', lambda: _open_hud_menu(b, 'profiles')))
    elif b.hud.menu_anchor not in ('hud-syncplay', 'hud-sub'):
        rows.append((_('Back'), 'arrow_back', lambda: _open_hud_menu(b, 'root')))
    if kind == 'quality':
        option_rows(st.get('quality'), 'set-quality')
    elif kind == 'speed':
        cur = float(_ctl_get(b, 'get_speed', 1.0))
        for s in _SPEEDS:
            rows.append(('%gx' % s, 'check' if abs(cur - s) < 0.005 else None, leaf(lambda s=s: b._ctl(lambda c: c.set_speed(s)))))
    elif kind == 'aspect':
        cur = float(_ctl_get(b, 'get_aspect', -1.0))
        for label, num, value in _ASPECTS:
            rows.append((label or _('Auto'), 'check' if abs(cur - num) < 0.01 else None, leaf(lambda v=value: b._ctl(lambda c: c.set_aspect(v)))))
    elif kind == 'profiles':
        scopes = (st.get('profiles') or {}).get('scopes') or []
        if not scopes:
            option_rows(st.get('profiles'), 'set-profile')
        for scope in scopes:
            rows.append((with_current(scope.get('label') or '', scope.get('value')), 'check' if scope.get('in_effect') else None, lambda sid=scope.get('id'): _open_hud_menu(b, 'profiles:' + str(sid))))
    elif kind.startswith('profiles:'):
        wanted = kind.split(':', 1)[1]
        for scope in (st.get('profiles') or {}).get('scopes') or []:
            if scope.get('id') == wanted:
                option_rows(scope, 'set-profile')
                break
    elif kind in ('sub_size', 'sub_position', 'sub_color'):
        option_rows(sub_style.get(kind[4:]), 'set-' + kind.replace('_', '-'))
    elif kind == 'secondary_sub':
        option_rows({'options': st.get('secondary_subtitles')}, 'set-secondary-sub')
    elif kind == 'syncplay':
        sp = st.get('syncplay') or {}
        rows.append((_('None (Disabled)'), 'check' if not sp.get('enabled') else None, leaf(lambda: _hud_action(b, 'syncplay-disable'))))
        if not sp.get('enabled'):
            rows.append((_('New Group'), None, leaf(lambda: _hud_action(b, 'syncplay-new'))))
        for g in sp.get('groups') or []:
            rows.append((g.get('label') or '', 'check' if g.get('selected') else None, leaf(lambda gid=g.get('id'): _hud_action(b, 'syncplay-join', gid))))
    return rows

def _settings_menu(b, menu_state, size):
    """The open gear menu as a Menu node anchored at the gear button
    (renderer clamps to the screen and flips above near the bottom)."""
    if not b.hud.menu:
        return None
    st = menu_state if menu_state and menu_state.get('has_media') else {}
    w, h = size
    rows = _menu_rows(b, st, b.hud.shed)
    if not rows:
        return None
    x, y = (w - 300, h - 160)
    anchor = b.hud.menu_anchor or 'hud-settings'
    if b.app is not None and hasattr(b.app, 'node_rect'):
        rect = b.app.node_rect(anchor)
        if rect is not None:
            x = rect['x']
            y = rect['y'] + rect['h'] + 4 if rect['y'] < h / 2 else rect['y'] - 4
    return Menu('hud-menu', [r[0] for r in rows], x=x, y=y, icons=[r[1] for r in rows], on_select=lambda i, v, rr=rows: rr[i][2](), on_dismiss=lambda: _close_hud_menu(b))
INFO_W = 520
INFO_MAX_H = 420

def _mpv_rows(stats):
    """The live-counter block, from mpv rather than from the DTO.

    Only what a *viewer* asks: is my GPU being used, why is it stuttering,
    why did it stall. Each row is omitted when mpv had nothing to say --
    which is a real state rather than an error (no rendered-fps estimate
    before the first frame, none of the video counters during audio), and
    is why they are individually guarded rather than shown as zeroes.
    """
    rows = []
    hwdec = stats.get('hwdec')
    if hwdec:
        rows.append((_('Hardware acceleration'), _('No') if hwdec == 'no' else str(hwdec)))
    if stats.get('vo'):
        rows.append((_('Video output'), str(stats['vo'])))
    fps = stats.get('fps')
    if fps:
        rows.append((_('Framerate'), '%.2f' % float(fps)))
    drops_vo, drops_dec = (stats.get('drops_vo'), stats.get('drops_dec'))
    if drops_vo is not None or drops_dec is not None:
        rows.append((_('Dropped frames'), _('%(vo)d output, %(dec)d decoder') % {'vo': int(drops_vo or 0), 'dec': int(drops_dec or 0)}))
    if stats.get('avsync') is not None:
        rows.append((_('A/V sync'), '%+.3f s' % float(stats['avsync'])))
    if stats.get('buffered') is not None:
        rows.append((_('Buffered'), _('%.1f s') % float(stats['buffered'])))
    speed = stats.get('cache_speed')
    if speed:
        rows.append((_('Download speed'), _('%.1f Mbps') % (float(speed) * 8 / 1000000.0)))
    return rows

def _info_rows(info, stats=None):
    """``[(heading, [(label, value), ...]), ...]`` for the panel.

    jellyfin-web's playerstats categories, minus the two that need the
    server polled (transcode completion and encoder fps) — see the gateway's
    ``playback_info`` — plus one they do not have: what mpv is doing with
    the stream once it arrives.
    """
    source = info.get('source') or {}
    method = info.get('play_method')
    playback = []
    if info.get('item_type'):
        playback.append((_('Media type'), info['item_type']))
    label = media_info.play_method_label(method)
    if label:
        if method == media_info.DIRECT_PLAY:
            label += '  (%s)' % (_('downloaded copy') if info.get('offline') else _('local file') if info.get('direct_path') else _('stream from server'))
        playback.append((_('Play method'), label))
    reasons = media_info.transcode_reasons(info.get('transcode_reasons'))
    out = [(_('Playback'), playback)] if playback else []
    if reasons:
        out.append((_('Reasons'), [('%d.' % (i + 1), r) for i, r in enumerate(reasons)]))
    player_rows = _mpv_rows(stats or {})
    if player_rows:
        out.append((_('Player'), player_rows))
    file_rows = media_info.source_attributes(source)
    if file_rows:
        out.append((_('File'), file_rows))
    for stream in media_info.visible_streams(source):
        rows = media_info.stream_attributes(stream, source)
        if rows:
            out.append((media_info.stream_heading(stream), rows))
    return out

def _info_dialog(b, size):
    """The playback-info panel, or None when it is closed.

    A ``Dialog`` rather than a floating Box: ESC and click-outside
    dismissal for free, and an open modal counts as a busy HUD, so it
    cannot be yanked away with the bar it is attached to. See
    docs/browser-shell.md section 14.
    """
    if not b.hud.info:
        return None
    info = _ctl_get(b, 'playback_info', None)
    if not info:
        return None
    win_w, win_h = size
    w = max(280, min(INFO_W, win_w - 40))
    body_h = max(60, min(INFO_MAX_H, win_h - 220))
    blocks = [Text(_('Playback Info'), size='title', bold=True)]
    if info.get('title'):
        blocks.append(Text(info['title'], size='normal', color=theme.SUBTLE_FG, wrap=True, w=w - 48))
    body = []
    stats = _ctl_get(b, 'player_stats', {}) or {}
    for heading, rows in _info_rows(info, stats):
        body.append(Text(heading, size='normal', bold=True, color=theme.ACCENT))
        for label, value in rows:
            label_w = min(160, w // 3)
            body.append(Row([Text(label, size='small', color=theme.SUBTLE_FG, w=label_w), Text(value, size='small', wrap=True, w=max(80, w - 48 - label_w - 8))], gap=8, align='start'))
    if not body:
        body.append(Text(_('Nothing is playing.'), size='small', color=theme.SUBTLE_FG))
    blocks.append(VScroll(Column(body, gap=6, align='stretch'), id='hud-info-scroll', h=body_h))
    blocks.append(Row([Spacer(flex=1), Button(_('Close'), id='hud-info-close', on_click=lambda: _close_info(b))], gap=10))
    return Dialog('hud-info', Column(blocks, pad=24, gap=14, bg=theme.PANEL_BG, radius=12, border=theme.BORDER, w=w, align='stretch'), on_dismiss=lambda: _close_info(b))

def _open_info(b):
    b.hud.menu = None
    b.hud.info = True
    b.invalidate()

def _close_info(b):
    b.hud.info = False
    b.invalidate()

def _toggle_tc(b):
    b.hud.tc_remaining = not b.hud.tc_remaining
    b.invalidate()

def _toggle_hud_mute(b):
    """Flip the icon on the click, not on the round trip.

    The player now observes ``mute`` and pushes a snapshot (see
    _on_volume_change), so this only covers the trip out to the action
    thread and back — but that trip runs behind the player's lock, which a
    playback start holds for its whole duration. Same optimism as the
    favourite button below, and self-correcting: the next snapshot is the
    truth whatever we guessed."""
    st = b.hud.state or {}
    st['muted'] = not st.get('muted')
    b._ctl(lambda c: c.toggle_mute())
    b.invalidate()

def _toggle_hud_favorite(b):
    st = b.hud.state or {}
    st['favorite'] = not st.get('favorite')
    _hud_action(b, 'toggle-favorite')
    b.invalidate()

def _skip_float(b, size):
    """Floating Skip Intro / Skip Credits button above the bar's right
    edge (jellyfin-web's placement), when the player says a skippable
    segment is live (playstate skip_label).

    Positioned by a constant inset from the bottom rather than off the
    laid-out slider rect, which is a frame stale -- keying off it left the
    button out of the HUD's very first scene. The constants also have to
    agree with renderer.lua's copy of this button; see
    docs/browser-shell.md section 14."""
    label = (b.hud.state or {}).get('skip_label')
    if not label:
        return None
    return Button(label, id='hud-skip', size=_SKIP_SIZE, pad=_SKIP_PAD, bg=_SKIP_BG, alpha=_SKIP_ALPHA, fg=_SKIP_FG, hover={'fill': _SKIP_BG_HOVER}, on_click=lambda: _hud_action(b, 'skip-segment'), anchor='se', dx=-_SKIP_RIGHT, dy=-_SKIP_BOTTOM)

def _panel():
    """Box styling for the two bars: a flat translucent band under the
    "panel" scrim, and an invisible one otherwise.

    Invisible rather than absent because the renderer needs the bars to
    EXIST as scene nodes: it holds the auto-hide off while the pointer is
    over them (phud_busy), and layout only emits a node for a container that
    has a fill, a border or a click. Alpha 0 costs one ASS event that draws
    nothing, and the node is not a hit target -- node_at ignores a rect with
    no click, tip or hover of its own.
    """
    return {'bg': PANEL_BG, 'alpha': PANEL_ALPHA if settings.hud_scrim == 'panel' else 0}

def _scrim(h, w):
    """The wash behind the controls, per ``hud_scrim``.

    It is not decoration: white-on-white is what the controls hit without
    it, over a frame nobody chose. So "none" is not simply the absence of
    the others -- it moves the job onto the glyphs, which is the ``shadow``
    flag the renderer draws them with (see gateway.hud_key_opts).
    """
    style = settings.hud_scrim
    if style in ('none', 'panel'):
        return []
    return [Gradient(color='000000', top=0, bottom=215, w=w, h=int(min(h * SCRIM_FRAC, SCRIM_MAX)), anchor='sw'), Gradient(color='000000', top=170, bottom=0, w=w, h=int(min(h * TOP_SCRIM_FRAC, TOP_SCRIM_MAX)), anchor='nw')]

def build_hud(b, size):
    """The summoned HUD scene. ``b`` is the Browser (playstate snapshot,
    scrub state, controller plumbing); returns the full-window tree."""
    w, h = size
    st = b.hud.state or {}
    pos = st.get('position', 0) or 0
    dur = st.get('duration', 0) or 0
    pp = 'play_arrow' if st.get('paused') else 'pause'
    scrub = b.hud.scrub
    floor = hud_scale_floor()
    scale = min(1.0, max(floor, w / 900.0))

    def sz(v):
        return int(v * scale + 0.5)
    picker_icon = sz(HUD_ICON)
    chapters = _chapters(b, picker_icon)
    photo = bool(st.get('is_photo'))

    def tbtn(icon, node_id, cb, autofocus=False, icon_size=HUD_ICON, tip=None, repeat=False, fg='eeeeee', nav_gravity=False):
        return Button('', id=node_id, icon=icon, flat=True, fg=fg, icon_size=sz(icon_size), autofocus=autofocus, tip=tip, repeat=repeat, on_click=cb, nav_gravity=nav_gravity)
    seek = Slider('hud-seek', value=pos, min=0, max=max(1.0, dur), on_video=True, force=True, flex=1, h=26, autofocus=True, always_adjust=True, marks=[ch['time'] / dur for ch in chapters if 0 < ch['time'] < dur] if dur > 0 else None, ranges=[(max(0.0, a / dur), min(1.0, e / dur)) for a, e in st.get('ranges') or [] if e > a] if dur > 0 else None, on_change=b.hud.scrub_change, on_commit=b.hud.scrub_commit, on_cancel=b.hud.scrub_cancel, preview=dur > 0)
    menu_state = None
    if b.controller is not None and hasattr(b.controller, 'hud_menu_state'):
        try:
            menu_state = b.controller.hud_menu_state()
        except Exception:
            menu_state = None
    items = [(None, tbtn('skip_previous', 'hud-prev', lambda: b._ctl(lambda c: c.prev()), tip=_('Previous')))]
    if chapters:
        items.append(('ch_btns', tbtn('undo', 'hud-ch-prev', lambda: _chapter_jump(b, -1), tip=_('Previous chapter'))))
    if not photo:
        items.append(('seek_btns', tbtn('replay_10', 'hud-seek-back', lambda: b._ctl(lambda c: c.seek_relative(-10)), tip=_('Back 10 Seconds'), repeat=True)))
    items.append((None, tbtn(pp, 'hud-pp', lambda: b._ctl(lambda c: c.toggle_pause()), icon_size=36, nav_gravity=not photo)))
    if not photo:
        items.append(('seek_btns', tbtn('forward_30', 'hud-seek-fwd', lambda: b._ctl(lambda c: c.seek_relative(30)), tip=_('Forward 30 Seconds'), repeat=True)))
    if chapters:
        items.append(('ch_btns', tbtn('redo', 'hud-ch-next', lambda: _chapter_jump(b, 1), tip=_('Next chapter'))))
    items.append((None, tbtn('skip_next', 'hud-next', lambda: b._ctl(lambda c: c.next()), tip=_('Next'))))
    shown_pos = pos if scrub is None else scrub
    if not photo:
        if b.hud.tc_remaining and dur > 0:
            end_part = '-' + _clock(max(0.0, dur - shown_pos))
        else:
            end_part = _clock(dur)
        clock_pad = 4
        ceiling = max(dur, shown_pos)
        widest = '%s / -%s' % (_clock(ceiling), _clock(ceiling))
        items.append(('clock', Box([Text('%s / %s' % (_clock(shown_pos), end_part), size=sz(17), color='ffffff' if scrub is not None else 'dddddd')], id='hud-clock', pad=clock_pad, align='center', direction='row', w=natural_size(Text(widest, size=sz(17)))[0] + 2 * clock_pad, on_click=lambda: _toggle_tc(b))))
    if dur > 0 and (not photo):
        speed = max(0.01, float(_ctl_get(b, 'get_speed', 1.0)))
        ends = timefmt.clock_epoch(time.time() + max(0.0, dur - pos) / speed)
        items.append(('ends_at', Text(_('Ends at {0}').format(ends), size=sz(16), color='aaaaaa')))
    items.append((None, Spacer()))
    fav = bool(st.get('favorite'))
    items.append(('favorite', tbtn('favorite' if fav else 'favorite_border', 'hud-fav', lambda: _toggle_hud_favorite(b), tip=_('Favorite'), fg=theme.FAV_RED if fav else 'eeeeee')))
    items.extend(_pickers(b, menu_state, pos, chapters, picker_icon))
    muted = bool(st.get('muted'))
    vol = st.get('volume', 100) or 0
    if not photo:
        items.append((None, tbtn('volume_off' if muted else 'volume_up' if vol >= 50 else 'volume_down', 'hud-mute', lambda: _toggle_hud_mute(b), tip=_('Mute'))))
        items.append(('volbar', Slider('hud-vol', value=0 if muted else vol, min=0, max=100, on_video=True, w=sz(110), force=True, on_change=lambda v: b._ctl(lambda c: c.set_volume(v)))))
    items.append((None, tbtn('settings', 'hud-settings', lambda: _open_hud_menu(b, 'root', anchor='hud-settings'), tip=_('Settings'))))
    items.append((None, tbtn('fullscreen_exit' if st.get('fullscreen') else 'fullscreen', 'hud-fs', lambda: b._ctl(lambda c: c.toggle_fullscreen()), tip=_('Fullscreen'))))
    gap = sz(6)
    step_w = w
    if floor < w / 900.0 < 1.0:
        step_w = max(900.0 * floor, 900.0 * (sz(HUD_ICON) - 0.5) / HUD_ICON)
    b.hud.shed = _shed(items, step_w - 2 * sz(24), gap)
    transport = Row([el for key, el in items if key not in b.hud.shed], gap=gap, align='center')
    bar_rows = ([] if st.get('is_photo') else [Row([seek], align='center')]) + [transport]
    bar = Column(bar_rows, gap=sz(6), pad=(sz(24), sz(14)), w=w, anchor='s', align='stretch', id='hud-bar', **_panel())
    heading: Element = Text(st.get('title') or '', size=sz(20), bold=True, flex=1)
    context = _episode_context(st)
    if context:
        heading = Column([Text(context, size=sz(15), color='bbbbbb'), Text(st.get('title') or '', size=sz(20), bold=True)], gap=sz(1), flex=1, align='stretch')
    top_items = [tbtn('arrow_back', 'hud-back', lambda: b._ctl(lambda c: c.stop()), tip=_('Back')), heading]
    st_menu = menu_state if menu_state and menu_state.get('has_media') else {}
    syncplay = st_menu.get('syncplay')
    if syncplay is not None:
        top_items.append(tbtn('groups', 'hud-syncplay', lambda: _open_hud_menu(b, 'syncplay', anchor='hud-syncplay'), tip=_('SyncPlay'), fg=theme.ACCENT if syncplay.get('enabled') else 'eeeeee'))
    top_items += window_chrome.window_controls(b, prefix='hud-win', icon_size=sz(20), w=sz(WINDOW_CONTROL_W), fg='eeeeee', gap=sz(4))
    top = Row(top_items, gap=sz(10), pad=(sz(24), sz(10)), w=w, anchor='n', align='center', id='hud-topbar', window_drag=b.window_controls, **_panel())
    children = _scrim(h, w) + [bar, top]
    skip = _skip_float(b, size)
    if skip is not None:
        children.append(skip)
    menu = _settings_menu(b, menu_state, size)
    if menu is not None:
        children.append(menu)
    info = _info_dialog(b, size)
    if info is not None:
        children.append(info)
    grip = window_chrome.resize_grip(b, w, h)
    if grip is not None:
        children.append(grip)
    return Stack(children, w=w, h=h)

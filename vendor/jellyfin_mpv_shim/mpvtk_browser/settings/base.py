"""What every settings tab shares.

The config store (``_config`` / ``_set_setting``) and the one layout helper
more than one tab uses. Everything else lives with the tab that draws it.

``_set_setting`` is the single write path, and it is where a setting's side
effects are dispatched -- applying audio config, switching to offline mode,
seeding the auto-download server. Those live with the general tab because
that is where the controls are; they are declared below rather than imported,
since it is the composed ``SettingsMixin`` that makes the call valid.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from typing import TYPE_CHECKING
from ...conf import settings
from ...i18n import _
from ...mpvtk.widgets import Column, Text
from .. import theme
log = logging.getLogger('mpvtk_browser.settings')

class SettingsBase:
    if TYPE_CHECKING:

        def _apply_audio_settings(self) -> None:
            ...

        def _apply_work_offline(self, enabled) -> None:
            ...

        def _seed_auto_download_server(self) -> None:
            ...

    def _note_w(self, size):
        """Wrap width for an explanatory note on a settings page.

        Settings notes used to wrap to the whole column, which on a wide
        window is ~200 characters per line under a control 340px wide -- the
        note stopped looking like it belonged to the setting above it. See
        chrome.PROSE_CH for why the cap is a measure and not a pixel width.
        """
        from ..components import chrome
        avail = chrome.body_width(size[0] if size else 0, self.CONTENT_PAD)
        return chrome.prose_w('caption', avail)

    def _config(self):
        if self._config_obj is not None:
            return self._config_obj
        from .. import config as cfg
        return cfg

    def _set_setting(self, key, value):
        cfg = self._config()
        restarts = getattr(cfg, 'RESTART_REQUIRED', ())
        before = cfg.get_settings().get(key) if key in restarts else None
        ok = cfg.set_setting(key, value)
        if ok and key in restarts:
            if cfg.get_settings().get(key) != before:
                note = getattr(self, 'note_restart_needed', None)
                if note is not None:
                    note(key)
        self.set_status((_('Saved: %s') if ok else _('Invalid value: %s')) % key)
        if ok and (not value) and (key in getattr(cfg, 'TRAY_DEPENDENT', ())):
            if settings.start_minimized and cfg.set_setting('start_minimized', False):
                self.set_status(_('Saved: %(key)s (also turned off "%(other)s")') % {'key': key, 'other': cfg.label_for('start_minimized')})
        if ok and key in ('window_controls', 'window_controls_fullscreen'):
            refresh = getattr(self, 'refresh_window_controls', None)
            if refresh is not None:
                refresh()
        if ok and key == 'browse_block_keys':
            push = getattr(self.app, 'push_browse_keys', None) if self.app else None
            if push is not None:
                push()
            self.invalidate()
        if ok and key == 'browser_fullscreen':
            ctl = getattr(self, 'controller', None)
            apply_fs = getattr(ctl, 'apply_browser_fullscreen', None)
            if apply_fs is not None:
                apply_fs()
        if ok and key == 'hdr_output':
            ctl = getattr(self, 'controller', None)
            apply_output = getattr(ctl, 'set_hdr_output', None)
            if apply_output is not None:
                apply_output(value)
            self.set_status('画面输出：' + ('SDR（HDR 转 SDR）' if value == 'sdr' else '自动'))
        if ok and key == 'work_offline':
            self._apply_work_offline(bool(value))
        if ok and key == 'auto_download_enable' and value:
            self._seed_auto_download_server()
        if ok and key.startswith('audio_'):
            self._apply_audio_settings()
        if ok and key in ('scroll_wheel_pixels', 'scroll_mode'):
            if self.app is not None and hasattr(self.app, 'push_scroll_config'):
                self.app.push_scroll_config()
        if ok and key == 'gamepad_swap_confirm':
            if self.app is not None and hasattr(self.app, 'push_gamepad'):
                self.app.push_gamepad()
        if ok and key == 'ui_select_key':
            for name in ('push_select_key', 'push_gamepad'):
                push = getattr(self.app, name, None) if self.app else None
                if push is not None:
                    push()
        if ok and key == 'poster_scale':
            apply_cover = getattr(self, 'apply_cover_size', None)
            if apply_cover is not None:
                apply_cover()
        if ok and key in ('ui_text_scale', 'ui_text_min'):
            from .. import theme as browser_theme
            browser_theme.apply_to_toolkit(glow=bool((browser_theme.active() or {}).get('glow')))
            apply_cover = getattr(self, 'apply_cover_size', None)
            if apply_cover is not None:
                apply_cover()
        if ok and key.startswith('logo_legibility'):
            apply_logos = getattr(self, 'apply_logo_legibility', None)
            if apply_logos is not None:
                apply_logos()
        if ok and key == 'theme':
            setter = getattr(self, 'set_theme', None)
            if setter is not None:
                try:
                    setter(value)
                except Exception:
                    log.exception('could not switch theme live')
        self.invalidate()

    def _section(self, title, children, subtitle=None):
        """A full-width titled card. Settings panels are forms, not tile
        grids — they should span the pane rather than sit in a ragged
        left-aligned column."""
        head = [Text(title, size='large', bold=True)]
        if subtitle:
            head.append(Text(subtitle, size='caption', color=theme.SUBTLE_FG, wrap=True))
        return Column(head + children, pad=14, gap=8, bg=theme.CARD_BG, radius=10, align='stretch')
    INDENT = 26

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

VENDOR = Path(__file__).resolve().parents[1] / 'vendor'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))
from jellyfin_mpv_shim import args, mpv_options


class NativeHardwareDecodeTests(unittest.TestCase):
    def test_safe_hardware_preference_reaches_mpv_and_copy_mode(self):
        with patch.object(args, 'get_args', return_value=SimpleNamespace(disable_hwdec=False)), \
             patch.object(mpv_options, 'hwdec_pinned_by_config', return_value=None), \
             patch.object(mpv_options.settings, 'hwdec', 'auto-safe'):
            self.assertEqual(mpv_options.hwdec_for(), 'auto-safe')
            self.assertEqual(mpv_options.hwdec_for(needs_copy=True), 'auto-copy-safe')

    def test_recovery_flag_and_explicit_mpv_config_still_take_precedence(self):
        with patch.object(args, 'get_args', return_value=SimpleNamespace(disable_hwdec=True)), \
             patch.object(mpv_options.settings, 'hwdec', 'auto-safe'):
            self.assertEqual(mpv_options.hwdec_for(needs_copy=True), 'no')
        with patch.object(args, 'get_args', return_value=SimpleNamespace(disable_hwdec=False)), \
             patch.object(mpv_options, 'hwdec_pinned_by_config', return_value='d3d11va'), \
             patch.object(mpv_options.settings, 'hwdec', 'auto-safe'):
            self.assertIsNone(mpv_options.hwdec_for())


if __name__ == '__main__':
    unittest.main()

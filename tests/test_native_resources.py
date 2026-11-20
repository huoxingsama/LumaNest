"""The wheel finds native support libraries without a frozen bundle or cwd."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

VENDOR = Path(__file__).resolve().parents[1] / 'vendor'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from personal_cinema.embedded_mpv import extend_environment
from jellyfin_mpv_shim import win_fribidi


class NativeResourceTests(unittest.TestCase):
    def test_explicit_player_directory_reaches_fribidi_before_system_candidates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = extend_environment({}, root / 'mpv-2.dll')
            self.assertEqual(env['LUMANEST_PLAYER_DIRECTORY'], str(root))
            with patch.dict(os.environ, env), patch.object(sys, '_MEIPASS', str(root / 'unrelated-bundle'), create=True):
                self.assertEqual(win_fribidi._candidate_dirs()[0], str(root))

    def test_default_prefix_player_is_a_candidate_and_cwd_is_not(self):
        with TemporaryDirectory() as directory:
            prefix = Path(directory).resolve()
            with patch.dict(os.environ, {'LUMANEST_PLAYER_DIRECTORY': ''}), patch.object(sys, 'prefix', str(prefix)):
                candidates = win_fribidi._candidate_dirs()
            self.assertIn(str(prefix / 'player'), candidates)
            self.assertNotIn(os.getcwd(), candidates)

    def test_relative_environment_directory_is_not_loaded(self):
        with patch.dict(os.environ, {'LUMANEST_PLAYER_DIRECTORY': 'untrusted-relative'}):
            self.assertNotIn('untrusted-relative', win_fribidi._candidate_dirs())


if __name__ == '__main__':
    unittest.main()

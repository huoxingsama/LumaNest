"""The installed Python entry point must retain the native Windows bootstrap."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WindowsBootstrapTests(unittest.TestCase):
    def test_icon_cache_contains_every_packaged_resolution_and_is_reused(self):
        from personal_cinema.windows_bootstrap import cached_icon
        from PIL import Image
        with TemporaryDirectory() as directory:
            path = cached_icon(Path(directory))
            self.assertTrue(path.is_relative_to(Path(directory).resolve()))
            with Image.open(path) as icon:
                self.assertEqual(icon.ico.sizes(), {(size, size) for size in (16, 32, 48, 64, 128, 256)})
            stamp = path.stat().st_mtime_ns
            self.assertEqual(cached_icon(Path(directory)), path)
            self.assertEqual(path.stat().st_mtime_ns, stamp)

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows HWND and DPI APIs')
    def test_real_process_dpi_identity_and_hidden_window_icons(self):
        # A separate process is essential: changing DPI mode in the test runner
        # could hide a bootstrap ordering regression in later tests.
        script = r'''
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import sys
import time
sys.path[:0] = sys.argv[1:3]
from personal_cinema.windows_bootstrap import prepare_process, prepare_window
prepare_process()
prepare_window(Path(sys.argv[3]))
assert 'mpv' not in sys.modules
from jellyfin_mpv_shim import windows_identity
user = ctypes.WinDLL('user32', use_last_error=True)
user.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user.AreDpiAwarenessContextsEqual.restype = w.BOOL
assert user.AreDpiAwarenessContextsEqual(user.GetThreadDpiAwarenessContext(), ctypes.c_void_p(-4))
shell = ctypes.WinDLL('shell32')
shell.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
shell.GetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
identity = ctypes.c_void_p()
assert shell.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(identity)) == 0
app_id = ctypes.wstring_at(identity)
ole = ctypes.WinDLL('ole32')
ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole.CoTaskMemFree(identity)
assert app_id == 'LumaNest.Cinema'
callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user.DefWindowProcW.restype = ctypes.c_ssize_t
callback = callback_type(user.DefWindowProcW)
class WindowClass(ctypes.Structure):
    _fields_ = [('style', w.UINT), ('procedure', callback_type), ('class_extra', ctypes.c_int),
        ('window_extra', ctypes.c_int), ('instance', w.HINSTANCE), ('icon', w.HICON),
        ('cursor', w.HANDLE), ('background', w.HBRUSH), ('menu', w.LPCWSTR), ('name', w.LPCWSTR)]
window_class = WindowClass()
window_class.procedure = callback
window_class.name = 'mpv'
user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
user.RegisterClassW.restype = w.WORD
assert user.RegisterClassW(ctypes.byref(window_class))
user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
user.CreateWindowExW.restype = w.HWND
hwnd = user.CreateWindowExW(0, 'mpv', 'LumaNest isolated icon test', 0x00CF0000,
    0, 0, 320, 180, None, None, None, None)
assert hwnd
user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user.SendMessageW.restype = ctypes.c_ssize_t
user.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
user.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user.DispatchMessageW.restype = ctypes.c_ssize_t
user.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user.IsWindowVisible.argtypes = [w.HWND]
user.GetDpiForWindow.argtypes = [w.HWND]
user.GetDpiForWindow.restype = w.UINT
user.DestroyWindow.argtypes = [w.HWND]
try:
    assert not user.IsWindowVisible(hwnd)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        message = w.MSG()
        while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            user.TranslateMessage(ctypes.byref(message))
            user.DispatchMessageW(ctypes.byref(message))
        icons = [user.SendMessageW(hwnd, 127, kind, 0) for kind in (0, 1)]
        if all(icons):
            break
        time.sleep(0.02)
    assert all(icons), 'icon keeper did not apply both sizes to the real HWND'
    assert set(icons).issubset(set(windows_identity._keeper.icons.values()))
    print(json.dumps({'app_id': app_id, 'dpi_mode': 'per-monitor-v2',
        'window_dpi': user.GetDpiForWindow(hwnd), 'small_icon': True, 'large_icon': True,
        'visible': False}))
finally:
    user.DestroyWindow(hwnd)
'''
        with TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-c', script, str(ROOT / 'src'),
                str(ROOT / 'vendor'), directory], cwd=directory, capture_output=True,
                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['dpi_mode'], 'per-monitor-v2')
        self.assertTrue(report['small_icon'] and report['large_icon'])
        self.assertFalse(report['visible'])


if __name__ == '__main__':
    unittest.main()

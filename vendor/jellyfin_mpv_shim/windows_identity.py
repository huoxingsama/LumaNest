"""Give this pythonw-hosted cinema its own Windows taskbar identity."""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import ctypes
import logging
import os
from pathlib import Path
import sys
import threading
import time
APP_ID = 'Jellyfin.MPVShim.Cinema'
_keeper = None
log = logging.getLogger(__name__)

class _WindowsAPI:

    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.shell = ctypes.WinDLL('shell32', use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback_type, w.LPARAM]
        self.user.EnumWindows.restype = w.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.user.GetWindowThreadProcessId.restype = w.DWORD
        self.user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self.user.GetClassNameW.restype = ctypes.c_int
        self.user.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
        self.user.LoadImageW.restype = w.HANDLE
        self.user.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.user.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self.shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [w.LPCWSTR]
        self.shell.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        self.get_dpi = getattr(self.user, 'GetDpiForWindow', None)
        if self.get_dpi is not None:
            self.get_dpi.argtypes = [w.HWND]
            self.get_dpi.restype = w.UINT

    def set_identity(self):
        result = self.shell.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        if result < 0:
            raise OSError('Cannot set cinema AppUserModelID: %s' % result)

    def windows(self, pid):
        matches = []

        @self.callback_type
        def collect(hwnd, _):
            owner = self.w.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                name = ctypes.create_unicode_buffer(256)
                self.user.GetClassNameW(hwnd, name, len(name))
                if name.value.lower() == 'mpv':
                    matches.append(hwnd)
            return True
        self.user.EnumWindows(collect, 0)
        return matches

    def dpi(self, hwnd):
        return (self.get_dpi(hwnd) if self.get_dpi else 96) or 96

    def load_icon(self, path, size):
        handle = self.user.LoadImageW(None, str(path), 1, size, size, 16)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def message(self, hwnd, message, kind, icon=0):
        result = ctypes.c_size_t()
        ok = self.user.SendMessageTimeoutW(hwnd, message, kind, icon, 2, 250, ctypes.byref(result))
        if not ok:
            raise OSError('Cinema window did not accept its icon message')
        return result.value

class _IconKeeper:

    def __init__(self, api, path):
        self.api = api
        self.path = path
        self.pid = os.getpid()
        self.icons = {}

    def refresh(self):
        for hwnd in self.api.windows(self.pid):
            dpi = self.api.dpi(hwnd)
            for kind, base_size in ((0, 16), (1, 64)):
                size = max(16, min(256, round(base_size * dpi / 96)))
                if size not in self.icons:
                    self.icons[size] = self.api.load_icon(self.path, size)
                icon = self.icons[size]
                if self.api.message(hwnd, 127, kind) != icon:
                    self.api.message(hwnd, 128, kind, icon)

    def run(self):
        started = time.monotonic()
        while True:
            try:
                self.refresh()
            except (OSError, ValueError):
                pass
            time.sleep(0.25 if time.monotonic() - started < 10 else 2)

def install(icon_path):
    """Call before importing code that may create libmpv's first window."""
    global _keeper
    if _keeper is not None or not sys.platform.startswith('win32'):
        return
    try:
        api = _WindowsAPI()
        api.set_identity()
        path = Path(icon_path).resolve()
        if not path.is_file():
            log.warning('Cinema icon is missing: %s', path)
            return
        _keeper = _IconKeeper(api, path)
        threading.Thread(target=_keeper.run, name='cinema-window-icon', daemon=True).start()
    except (AttributeError, OSError, ValueError):
        log.warning('Could not configure the cinema taskbar icon', exc_info=True)

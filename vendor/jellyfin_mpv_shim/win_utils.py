from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import win32gui
import logging
from .constants import USER_APP_NAME
log = logging.getLogger('win_utils')
MPV_WINDOW_CLASS = 'mpv'

def window_enumeration_handler(hwnd, top_windows):
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        cls = ''
    top_windows.append((hwnd, win32gui.GetWindowText(hwnd), cls))

def is_mpv_window(entry):
    """Does this enumerated window belong to our mpv?

    Class first, because it does not change when the title does. The title
    fallback covers builds whose class differs, and deliberately accepts
    both the current title and mpv's stock " - mpv" so an external-mpv
    setup that never got our --title still matches.
    """
    _hwnd, title, cls = entry
    if cls == MPV_WINDOW_CLASS:
        return True
    low = (title or '').lower()
    return low.endswith(USER_APP_NAME.lower()) or ' - mpv' in low

def raise_mpv():
    try:
        top_windows = []
        fg_win = win32gui.GetForegroundWindow()
        win32gui.EnumWindows(window_enumeration_handler, top_windows)
        for entry in top_windows:
            if is_mpv_window(entry):
                if entry[0] != fg_win:
                    win32gui.ShowWindow(entry[0], 6)
                    win32gui.ShowWindow(entry[0], 9)
                break
    except Exception:
        log.error('Could not raise MPV.', exc_info=True)

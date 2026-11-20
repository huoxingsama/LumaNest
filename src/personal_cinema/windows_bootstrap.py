"""Windows process settings required before libmpv creates its first HWND."""
from __future__ import annotations

import ctypes
from hashlib import sha256
from importlib.resources import files
import logging
import multiprocessing
import os
from pathlib import Path
import struct
import sys
import uuid


log = logging.getLogger(__name__)


def prepare_process():
    """Run first in the player entry point, before importing native UI code."""
    multiprocessing.freeze_support()
    if sys.platform != 'win32':
        return
    from ctypes import wintypes as w
    user = ctypes.WinDLL('user32', use_last_error=True)
    # Python-hosted libmpv does not inherit the DPI manifest of mpv.exe.
    # An already-aware host can reject the call; do not downgrade that host.
    try:
        set_context = user.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = w.BOOL
        if set_context(ctypes.c_void_p(-4)) or ctypes.get_last_error() == 5:
            return
    except (AttributeError, OSError):
        pass
    try:
        set_awareness = ctypes.WinDLL('shcore').SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        result = set_awareness(2)
        if result in (0, -2147024891):  # S_OK or E_ACCESSDENIED (already set)
            return
    except (AttributeError, OSError):
        pass
    try:
        user.SetProcessDPIAware.argtypes = []
        user.SetProcessDPIAware.restype = w.BOOL
        user.SetProcessDPIAware()
    except (AttributeError, OSError):
        log.warning('Windows could not enable high-DPI drawing for the cinema')


def cached_icon(config_dir: Path) -> Path:
    """Wrap the shipped PNG resolutions in an ICO without loading font DLLs.

    Windows supports PNG images inside ICO. Keeping the existing PNG bytes
    preserves their artwork and avoids resizing or any private bundle files.
    """
    assets = files('jellyfin_mpv_shim').joinpath('integration')
    images = [(size, assets.joinpath(f'jellyfin-{size}.png').read_bytes())
              for size in (16, 32, 48, 64, 128, 256)]
    header = struct.pack('<HHH', 0, 1, len(images))
    entries = []
    offset = len(header) + 16 * len(images)
    for size, image in images:
        if image[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', image[16:24]) != (size, size):
            raise ValueError('Invalid packaged window icon')
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0,
                                   1, 32, len(image), offset))
        offset += len(image)
    payload = header + b''.join(entries) + b''.join(image for _, image in images)
    directory = Path(config_dir).resolve() / 'cache' / 'window-icon'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f'lumanest-{sha256(payload).hexdigest()[:16]}.ico'
    if destination.is_file() and destination.read_bytes() == payload:
        return destination
    temporary = directory / f'.icon-{uuid.uuid4().hex}.tmp'
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def prepare_window(config_dir: Path):
    """Set the app identity and watch only this process's native MPV windows."""
    if sys.platform != 'win32':
        return
    from jellyfin_mpv_shim.windows_identity import install
    install(cached_icon(config_dir))

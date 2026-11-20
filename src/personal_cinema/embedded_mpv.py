"""Locate the bundled or explicitly selected libMPV without launching a player."""
import os
from pathlib import Path
import shutil
import sys

NAMES = ('mpv-2.dll', 'libmpv-2.dll', 'mpv-1.dll')


def library_path(values):
    selected = values.get('foundation.mpv_library_path', '')
    if selected:
        path = Path(selected)
        return path.resolve() if path.is_file() and path.name.lower() in NAMES else None
    directories = (Path(sys.prefix) / 'player', Path(sys.executable).parent / 'player',
                   Path(__file__).parent / 'resources/player')
    for directory in directories:
        for name in NAMES:
            if (directory / name).is_file():
                return (directory / name).resolve()
    for name in NAMES:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None


def extend_environment(env, library):
    if library:
        directory = str(Path(library).resolve().parent)
        env['LUMANEST_PLAYER_DIRECTORY'] = directory
        env['PATH'] = directory + os.pathsep + env.get('PATH', '')
    return env

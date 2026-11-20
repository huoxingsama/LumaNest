"""Keep verified local videos accompanied by their current catalog subtitles."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import os
from personal_cinema.runtime_paths import RuntimePath as Path
import secrets
import shutil
from cache_policy import cache_path
SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup', '.smi'}

def subtitle_suffix(name, video_stem):
    """A full video stem followed by a dot, preserving language/player flags."""
    if Path(name).suffix.lower() not in SUBTITLE_EXTENSIONS:
        return None
    if not name.casefold().startswith(video_stem.casefold() + '.'):
        return None
    return name[len(video_stem):]

def _same_content(source, destination):
    if not destination.is_file() or source.stat().st_size != destination.stat().st_size:
        return False
    with source.open('rb') as left, destination.open('rb') as right:
        while True:
            data = left.read(64 * 1024)
            if data != right.read(64 * 1024):
                return False
            if not data:
                return True

def sync_entry(root, entry):
    """Copy matching sidecars only when the bound video is a verified cache."""
    root = Path(root)
    if not entry.get('ready'):
        return 0
    ready_root = _cinema_cache_path('ready')
    video = cache_path(ready_root, entry.get('cache_relative') or entry['relative'])
    if not video.is_file() or video.stat().st_size != entry['size']:
        return 0
    catalog_root = root / 'catalog'
    catalog = cache_path(catalog_root, entry.get('catalog_relative') or entry['relative'])
    if not catalog.parent.is_dir():
        return 0
    files = list(catalog.parent.iterdir())
    other_stems = [p.stem for p in files if p.suffix.lower() == '.strm' and p.stem.casefold() != catalog.stem.casefold()]
    changed = 0
    for source in files:
        ending = subtitle_suffix(source.name, catalog.stem)
        if ending is None or not source.is_file():
            continue
        if any((len(stem) > len(catalog.stem) and subtitle_suffix(source.name, stem) for stem in other_stems)):
            continue
        source = cache_path(catalog_root, source.relative_to(catalog_root).as_posix())
        destination = cache_path(ready_root, video.with_name(video.stem + ending).relative_to(ready_root).as_posix())
        if _same_content(source, destination):
            continue
        temporary = cache_path(ready_root, destination.with_name(destination.name + '.' + secrets.token_hex(8) + '.tmp').relative_to(ready_root).as_posix())
        try:
            before = source.stat()
            shutil.copyfile(source, temporary)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise OSError('Catalog subtitle changed during copy; retry later')
            os.replace(temporary, destination)
            changed += 1
        finally:
            temporary.unlink(missing_ok=True)
    return changed

def sync_ready(root, entries):
    """Best effort per video; a subtitle error must not interrupt playback."""
    changed = 0
    for entry in list(entries):
        try:
            changed += sync_entry(root, entry)
            entry.pop('subtitle_cache_error', None)
        except (OSError, ValueError, KeyError) as error:
            entry['subtitle_cache_error'] = type(error).__name__
            logging.warning('Cached subtitle sync deferred: %s (%s)', entry.get('id'), type(error).__name__)
    return changed

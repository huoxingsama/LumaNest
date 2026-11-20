"""Policy primitives for the local, disposable media cache."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import os
import shutil
import stat
from pathlib import PurePosixPath
from personal_cinema.runtime_paths import RuntimePath as Path

def remote_path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith('/') or '\\' in value or ('\x00' in value):
        raise ValueError('Use an absolute OpenList path, not a URL')
    if any((part in ('.', '..') for part in value.split('/'))):
        raise ValueError('Path traversal is not allowed')
    return '/' + '/'.join((part for part in value.split('/') if part))

def cache_path(root: Path, relative: str) -> Path:
    root = Path(root).absolute()
    path = PurePosixPath(relative.replace('\\', '/'))
    if path.is_absolute() or not path.parts or any((part in ('.', '..') or ':' in part for part in path.parts)):
        raise ValueError('Cache path must stay under the managed root')
    candidate = root.joinpath(*path.parts)
    for parent in (*reversed(root.parents), root, *[root.joinpath(*path.parts[:i]) for i in range(1, len(path.parts) + 1)]):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
            raise ValueError('Linked/reparse cache paths are not allowed')
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('Cache path escapes root')
    return candidate

def eviction_ids(items, now, max_bytes, disk_free, reserve_bytes, retention_seconds):
    total = sum((item.get('size', 0) for item in items))
    free = disk_free
    result = []
    candidates = sorted((item for item in items if item.get('watched_at') is not None and (not item.get('active')) and (not item.get('pinned')) and (not item.get('favorite'))), key=lambda item: item.get('last_access', item['watched_at']))
    for item in candidates:
        expired = retention_seconds is not None and now - max(item['watched_at'], item.get('last_access', 0), item.get('last_played_at', 0)) >= retention_seconds
        if not expired and total <= max_bytes and (free >= reserve_bytes):
            continue
        result.append(item['id'])
        total -= item.get('size', 0)
        free += item.get('size', 0)
    return result

def complete_download(source, staging: Path, target: Path, expected_size: int):
    if target.exists():
        raise FileExistsError(target.name)
    staging.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    with staging.open('wb') as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    if staging.stat().st_size != expected_size:
        raise IOError('Incomplete download; kept outside the playable library')
    if target.exists():
        raise FileExistsError(target.name)
    os.rename(staging, target)

def delete_cached(root: Path, relative: str):
    target = cache_path(root, relative)
    if target.exists() and (not target.is_file()):
        raise ValueError('Only individual managed cache files may be deleted')
    target.unlink(missing_ok=True)

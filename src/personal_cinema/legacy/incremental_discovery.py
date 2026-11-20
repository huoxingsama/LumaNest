"""New uploads live outside managed work folders; skip those folders entirely."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from library_tasks import read

def managed_prefixes(root, source):
    prefixes = set()
    for row in read(root / 'data/source-bindings.json').values():
        path = row.get('remote', '')
        if not row.get('normalized') or not path.startswith(source + '/'):
            continue
        parts = path[len(source) + 1:].split('/')
        if len(parts) > 1:
            prefixes.add(source + '/' + parts[0])
    return prefixes

def managed(path, prefixes):
    return any((path == p or path.startswith(p + '/') for p in prefixes))

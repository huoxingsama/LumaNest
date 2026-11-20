"""Confirm missing sources only within directories actually read this scan."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging

def _missing(row, directory, fid, size):
    if row is None:
        return True
    if directory:
        return not bool(row.get('dir'))
    if row.get('dir'):
        return True
    if fid:
        return str(row.get('fid') or '') != str(fid)
    return int(row.get('size', 0)) != int(size or 0)

def confirm_scanned_absences(cloud_ids, entries, found_ids, completed):
    """Return confirmed missing IDs and the scanned parent proving each absence.

    ``completed`` contains complete OpenList/native agreement, including child
    directories. Existing managed children are not descended into. A missing
    child, however, proves its old descendants unavailable without visiting
    those descendants. Refresh each affected native parent once before using
    that proof; an error or a single empty response never retires a source.
    """
    source = cloud_ids.source_root.rstrip('/')
    tree = getattr(cloud_ids, 'tree', None)
    if not tree or getattr(cloud_ids, 'failed', False) or (not completed):
        return {}
    snapshots = {path: {r['file_name']: r for r in rows} for path, rows in completed.items()}
    candidates = {}
    bindings = getattr(cloud_ids, 'rows', {})
    for ident, entry in entries.items():
        remote = entry.get('remote', '')
        if ident in found_ids or not remote.startswith(source + '/') or entry.get('source_retired_at'):
            continue
        fid = entry.get('source_fid') or bindings.get(ident, {}).get('fid')
        parts = remote[len(source) + 1:].split('/')
        parent = source
        for index, name in enumerate(parts):
            rows = snapshots.get(parent)
            if rows is None:
                break
            directory = index < len(parts) - 1
            if _missing(rows.get(name), directory, None if directory else fid, entry.get('size')):
                candidates[ident] = (parent, name, directory, fid, entry.get('size'))
                break
            if not directory:
                break
            parent += '/' + name
    refreshed = {}
    for parent in dict.fromkeys((row[0] for row in candidates.values())):
        try:
            rows = tree.rows(parent, fresh=True)
            mapped = {r['file_name']: r for r in rows}
            if len(mapped) != len(rows):
                continue
            refreshed[parent] = mapped
        except (OSError, ValueError, RuntimeError, KeyError, StopIteration):
            logging.warning('Source absence confirmation unavailable; preserving entries under %s', parent)
    observed_fids = {str(row['fid']) for rows in [*snapshots.values(), *refreshed.values()] for row in rows.values() if row.get('fid') and (not row.get('dir'))}
    confirmed = {}
    for ident, (parent, name, directory, fid, size) in candidates.items():
        if fid and str(fid) in observed_fids:
            continue
        if parent not in refreshed:
            continue
        if _missing(refreshed[parent].get(name), directory, None if directory else fid, size):
            confirmed[ident] = parent
    return confirmed

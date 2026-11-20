"""Apply known episode names/numbers even when a remote media probe fails."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re, time, urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write

def sync_episode_names(worker, entry_ids=None):
    root = worker.root
    rows = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Episode&Fields=Path')['Items']
    by_short = {e['id'][:12]: e for e in worker.entries.values() if (entry_ids is None or e['id'] in entry_ids) and (not e.get('source_missing')) and (not e.get('catalog_hidden')) and e.get('episode_title')}
    changed = []
    for brief in rows:
        match = re.search('\\[([a-f0-9]{12})\\]', Path(brief.get('Path', '')).stem)
        entry = by_short.get(match[1]) if match else None
        if not entry:
            continue
        generated = bool(re.search('S\\d+E\\d+', brief.get('Name', '')))
        if not generated and brief.get('IndexNumber') is not None:
            continue
        item = worker.api('jellyfin', '/Items/' + brief['Id'])
        wanted = entry['episode_title']
        if entry.get('episode_title_prefix', True) and (not entry.get('is_extra')):
            wanted = f"第{entry['episode']}集 · " + wanted
        item.update(Name=wanted, IndexNumber=entry['episode'], ParentIndexNumber=entry['season'])
        if entry.get('episode_overview'):
            item['Overview'] = entry['episode_overview']
        worker.api('jellyfin', '/Items/' + item['Id'], item, method='POST')
        changed.append({'id': item['Id'], 'name': wanted, 'season': entry['season'], 'episode': entry['episode']})
    if changed:
        write(root / 'data/episode-metadata-sync.json', {'updated_at': time.time(), 'changed': changed})
    return changed

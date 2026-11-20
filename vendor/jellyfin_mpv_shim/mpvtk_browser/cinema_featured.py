"""Bounded, account-scoped home recommendations. No downloads or server writes."""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from personal_cinema.runtime_paths import option
import hashlib
import logging
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
log = logging.getLogger(__name__)
FEATURED_LIMIT = 8
CACHE_SECONDS = 300

def refresh_on_home():
    """Back never calls this; explicit Home can opt into a new batch."""
    return option('appearance.home_refresh', 'explicit') == 'explicit'

def franchise_key(item):
    name = re.sub('[\\s·・]', '', item.get('Name') or '')
    for aliases in (('海贼王', '航海王'), ('宝可梦', '精灵宝可梦', '宠物小精灵'), ('名侦探柯南',), ('火影忍者',), ('哈利波特', '神奇动物')):
        if name.startswith(aliases):
            return aliases[0]
    collection = item.get('CollectionName') or item.get('CollectionId')
    if collection:
        return str(collection)
    prefix = re.split('[:：]', name, maxsplit=1)[0]
    return re.sub('(?:剧场版|电影版|第[一二三四五六七八九十\\d]+部|\\d+)$', '', prefix)

def select_featured(items, limit=FEATURED_LIMIT, day=None):
    """Prefer actual artwork and vary franchises, with a fresh visit order."""
    by_id = {}
    for item in items:
        if item.get('Type') in ('Movie', 'Series') and item.get('Id') and (not item.get('IsVirtualItem')) and (item.get('LocationType') != 'Virtual') and ((item.get('ImageTags') or {}).get('Primary') or item.get('BackdropImageTags')):
            by_id.setdefault(item['Id'], item)
    salt = day or secrets.token_hex(12)

    def rank(item):
        seed = hashlib.sha256((salt + item['Id']).encode()).hexdigest()
        return (not bool(item.get('BackdropImageTags')), seed)
    candidates = sorted(by_id.values(), key=rank)
    selected, remainder, seen = ([], [], set())
    for item in candidates:
        key = franchise_key(item)
        if key in seen:
            remainder.append(item)
        else:
            seen.add(key)
            selected.append(item)
    return (selected + remainder)[:limit]

def load_featured(source, server, libraries, rows, excludes=()):
    """One small listing per visible video library, at most eight details.

    Uses the same authenticated source as the regular grid. An offline or
    failing source falls back to already-loaded rows and never breaks home.
    """
    pool = [i for row in rows for i in row.get('items', [])]
    for library in libraries:
        collection_type = library.get('CollectionType')
        if library.get('Id') in excludes or collection_type not in ('movies', 'tvshows'):
            continue
        try:
            items, _total = source.get_library_items(server, library['Id'], sort_by='Random', sort_order='Descending', limit=80, collection_type=collection_type)
            pool.extend(items)
        except Exception:
            log.info('Cinema recommendations use the already loaded video rows')

    def expand(item):
        try:
            detail = source.get_item(server, item['Id'])
            if detail and (detail.get('IsVirtualItem') or detail.get('LocationType') == 'Virtual'):
                return None
            return {**item, **(detail or {})}
        except Exception:
            return item
    selected = select_featured(pool)
    if not selected:
        return []
    with ThreadPoolExecutor(max_workers=min(3, len(selected)), thread_name_prefix='cinema-featured') as pool:
        return [item for item in pool.map(expand, selected) if item is not None]

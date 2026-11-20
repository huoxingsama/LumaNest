"""Movie-world directories come from provider identities, never category folders."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import re
import time
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write
GROUPS = {'1241': ('哈利·波特', '哈利·波特系列'), '435259': ('哈利·波特', '神奇动物系列'), '17255': ('生化危机', '电影'), '23456': ('海贼王', '电影'), '23616': ('火影忍者', '电影'), '39199': ('名侦探柯南', '电影'), '34055': ('宝可梦', '电影'), '661025': ('宝可梦', '电影'), '661026': ('宝可梦', '电影'), '661028': ('宝可梦', '电影'), '661029': ('宝可梦', '电影'), '661031': ('宝可梦', '电影')}
MOVIES = {**dict.fromkeys(('671', '672', '673', '674', '675', '767', '12444', '12445'), '1241'), **dict.fromkeys(('259316', '338952', '338953'), '435259'), **dict.fromkeys(('1576', '1577', '7737', '35791', '71679', '173897', '1423191'), '17255'), **dict.fromkeys(('10228', '12599', '10991', '12600', '33875'), '34055'), **dict.fromkeys(('36218', '34065', '34067', '16808'), '661025'), **dict.fromkeys(('25961', '47292', '39057', '50087'), '661026'), **dict.fromkeys(('115223', '88557', '150213', '227679'), '661028'), **dict.fromkeys(('303903', '350499', '382190'), '661029'), **dict.fromkeys(('436931', '494407', '662708'), '661031')}
WORLD_MOVIES = {'571891': ('宝可梦', '电影'), '407482': ('宝可梦', '电视特别篇'), '1677413': ('宝可梦', '电视特别篇'), '36897': ('宝可梦', '电视特别篇'), '249424': ('宝可梦', '电视特别篇')}
GENERIC = {'电影', '電影', '影片', '影视', '影視', '合集', '合辑', '合輯', 'movie', 'movies', 'film', 'films', 'collection', 'collections'}
COLLECTION_LINK_LABELS = {'查看合集', '查看系列', '浏览合集', '查看电影合集'}

def _collection_name(panel, link=None):
    candidate = clean_movie_title(link.text()) if link is not None else ''
    if not re.search('[\\u3400-\\u9fff]', candidate) or candidate in COLLECTION_LINK_LABELS:
        headings = panel.find('h2') + panel.find('h3')
        candidate = clean_movie_title(headings[0].text()) if headings else ''
    candidate = re.sub('^(?:属于|屬於|隶属于|隸屬於)\\s*', '', candidate)
    candidate = re.sub('\\s*(?:的)?(?:一部分|其中之一|之一)[。.]?$', '', candidate).strip(' 《》「」')
    candidate = re.sub('\\s*[（(]?(?:电影|電影)?(?:合集|合辑|合輯|系列)[）)]?\\s*$', '', candidate).strip()
    return candidate if candidate and re.search('[\\u3400-\\u9fff]', candidate) and (candidate.casefold() not in GENERIC) else ''

def clean_movie_title(title):
    """Provider IDs and release years belong to metadata, not the title."""
    value = str(title or '').strip()
    for _ in range(3):
        value = re.sub('\\s*\\[(?:tmdbid|imdbid|tvdbid)[-=: ]+[^\\]]+\\]\\s*$', '', value, flags=re.I)
        value = re.sub('\\s*[（(](?:19|20)\\d{2}[）)]\\s*$', '', value)
    return re.sub('[。．]+$', '', value.strip(' ._-')).rstrip()

def collection_names(items):
    """Only provider-linked Chinese collection names may name a new family."""
    result = {}
    for item in items:
        if item.get('Type') != 'BoxSet':
            continue
        ids = item.get('ProviderIds') or {}
        ident = str(ids.get('TmdbCollection') or ids.get('Tmdb') or '')
        title = clean_movie_title(item.get('Name'))
        if not ident.isdecimal() or not re.search('[\\u3400-\\u9fff]', title):
            continue
        title = re.sub('\\s*[（(]?(?:电影|電影)?(?:合集|合辑|合輯|系列)[）)]?\\s*$', '', title).strip()
        if title and title.casefold() not in GENERIC:
            result[ident] = title
    return result

def source_collection(content, ident):
    """Read an explicit collection relation from the exact movie's main panel.

    Recommendation cards and navigation links cannot establish membership.
    There is deliberately no filename/title-prefix inference here.
    """
    from chinese_credits import Document
    doc = Document(content)
    canonical = next((node.attrs.get('href', '') for node in doc.root.find('link') if 'canonical' in node.attrs.get('rel', '').split()), '')
    if not re.fullmatch('https://www\\.themoviedb\\.org/movie/' + re.escape(str(ident)) + '(?:-[^/?#]*)?/?', canonical):
        raise ValueError('Collection page identity mismatch')
    panels = [node for node in doc.root.find() if 'collection' in node.attrs.get('class', '').split() or node.attrs.get('id') in ('collection', 'belongs_to_collection')]
    relations = {}
    for panel in panels:
        for script in panel.find('script'):
            code = ''.join((child for child in script.children if isinstance(child, str)))
            for collection in re.findall('kendo\\.format\\(\\s*decodeURI\\(\\s*[\'"]/collection/\\{0\\}/static_cache/movie_card\\?[^\'"]*[\'"]\\s*\\)\\s*,\\s*[\'"](\\d+)[\'"]\\s*\\)', code):
                relations[collection] = {'movie_id': str(ident), 'collection_id': collection, 'name': '', 'source_url': canonical, 'collection_url': 'https://www.themoviedb.org/collection/' + collection}
        for link in panel.find('a'):
            href = urllib.parse.urljoin('https://www.themoviedb.org', link.attrs.get('href', ''))
            match = re.fullmatch('https://www\\.themoviedb\\.org/collection/(\\d+)(?:-[^/?#]*)?/?(?:\\?language=[A-Za-z-]+)?', href)
            if not match:
                continue
            candidate = _collection_name(panel, link)
            if not candidate:
                continue
            relations[match[1]] = {'movie_id': str(ident), 'collection_id': match[1], 'name': candidate, 'source_url': canonical, 'collection_url': 'https://www.themoviedb.org/collection/' + match[1]}
    if len(relations) > 1:
        raise ValueError('Multiple main collection relations')
    return next(iter(relations.values()), None)

def collection_card_name(content, ident):
    """An exact collection link binds a fetched lazy card's localized heading."""
    from chinese_credits import Document
    doc = Document(content)
    panels = [node for node in doc.root.find() if 'collection' in node.attrs.get('class', '').split()]
    for panel in panels:
        for link in panel.find('a'):
            href = urllib.parse.urljoin('https://www.themoviedb.org', link.attrs.get('href', ''))
            if re.fullmatch('https://www\\.themoviedb\\.org/collection/' + re.escape(str(ident)) + '(?:-[^/?#]*)?/?(?:\\?language=[A-Za-z-]+)?', href):
                return _collection_name(panel, link)
    raise ValueError('Collection card identity mismatch')

def verified_collection(root, ident):
    """Reuse a verified page already fetched for artwork; no extra network scan."""
    if not str(ident).isdecimal():
        return None
    page = root / 'data/artwork-source-pages' / ('movie-' + str(ident) + '.html')
    registry_path = root / 'data/verified-movie-collections.json'
    registry = read(registry_path)
    if not isinstance(registry, dict):
        registry = {}
    prior = registry.get(str(ident))
    if not isinstance(prior, dict) or prior.get('movie_id') != str(ident) or (not str(prior.get('collection_id', '')).isdecimal()) or (not re.search('[\\u3400-\\u9fff]', str(prior.get('name', '')))) or (str(prior.get('name', '')).casefold() in GENERIC) or (not re.fullmatch('https://www\\.themoviedb\\.org/movie/' + re.escape(str(ident)) + '(?:-[^/?#]*)?/?', str(prior.get('source_url') or ''))):
        prior = None
    if not page.exists():
        return prior
    try:
        stat = page.stat()
        if prior and prior.get('page_mtime_ns') == stat.st_mtime_ns and (prior.get('page_size') == stat.st_size):
            return prior
        content = page.read_text('utf-8')
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if prior and prior.get('page_sha256') == digest:
            return prior
        relation = source_collection(content, ident)
        if not relation:
            return None
        if not relation['name']:
            collection = relation['collection_id']
            if collection in GROUPS:
                relation['name'] = GROUPS[collection][0]
            else:
                card = root / 'data/collection-source-pages' / ('collection-' + collection + '.html')
                if card.exists():
                    fragment = card.read_text('utf-8')
                else:
                    url = f'https://www.themoviedb.org/collection/{collection}/static_cache/movie_card?language=zh-CN&version=1&translate=false'
                    with urllib.request.urlopen(url, timeout=15) as response:
                        raw = response.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise ValueError('Collection card exceeds limit')
                    fragment = raw.decode('utf-8')
                    collection_card_name(fragment, collection)
                    card.parent.mkdir(parents=True, exist_ok=True)
                    card.write_text(fragment, 'utf-8')
                relation['name'] = collection_card_name(fragment, collection)
                if not relation['name']:
                    return None
        relation.update(page_sha256=digest, page_mtime_ns=stat.st_mtime_ns, page_size=stat.st_size, updated_at=time.time())
        registry[str(ident)] = relation
        write(registry_path, registry)
        return relation
    except (OSError, ValueError, KeyError):
        return prior

def movie_group(root, item, franchise=None, names=None):
    """Return (world, branch), or no world for a genuinely standalone film."""
    ids = item.get('ProviderIds') or {}
    ident = str(ids.get('Tmdb') or '')
    collection = MOVIES.get(ident) or str(ids.get('TmdbCollection') or '')
    if ident in {str(row.get('provider_ids', {}).get('Tmdb')) for row in (franchise or {}).values()}:
        return ('海贼王', '电影')
    custom = read(root / 'data/movie-franchise-rules.json')
    rule = custom.get('movies', {}).get(ident) or custom.get('collections', {}).get(collection)
    if isinstance(rule, dict):
        family = clean_movie_title(rule.get('world'))
        branch = clean_movie_title(rule.get('branch') or '电影')
        if family and family.casefold() not in GENERIC:
            return (family, branch)
    if ident in WORLD_MOVIES:
        return WORLD_MOVIES[ident]
    if collection in GROUPS:
        return GROUPS[collection]
    relation = verified_collection(root, ident)
    if relation:
        if relation['collection_id'] in GROUPS:
            return GROUPS[relation['collection_id']]
        return (relation['name'], '电影')
    family = (names or {}).get(collection)
    if family and family.casefold() not in GENERIC:
        return (family, '电影')
    if clean_movie_title(item.get('Name')).startswith('名侦探柯南'):
        return ('名侦探柯南', '电影')
    return (None, None)

def sync_collections(worker, entry_ids=None):
    """Keep one app collection and requeue cloud routes when membership arrives late."""
    root = worker.root
    rows = worker.api('jellyfin', '/Items?Recursive=true&IncludeItemTypes=Movie,BoxSet&Fields=Path,ProviderIds')['Items']
    boxes = [row for row in rows if row.get('Type') == 'BoxSet']
    names = collection_names(boxes)
    franchise = read(root / 'data/franchise-movies.json').get('海贼王', {})
    active_parents = set()
    entries_by_parent = {}
    bindings_path = root / 'data/source-bindings.json'
    bindings = read(bindings_path)
    changed_bindings = False
    route_pending = set()
    selected_parents = set()
    for entry in worker.entries.values():
        if entry.get('kind') != 'Movies' or entry.get('source_missing') or entry.get('catalog_hidden'):
            continue
        relative = entry.get('catalog_relative') or entry.get('relative')
        if relative:
            parent = str((root / 'catalog' / relative).parent.absolute()).casefold()
            active_parents.add(parent)
            entries_by_parent.setdefault(parent, []).append(entry)
            if entry_ids is not None and entry['id'] in entry_ids:
                selected_parents.add(parent)
    groups = {}
    selected_groups = set()
    for item in rows:
        if item.get('Type') != 'Movie' or not item.get('Path'):
            continue
        if str(Path(item['Path']).parent.absolute()).casefold() not in active_parents:
            continue
        if entry_ids is not None and str(Path(item['Path']).parent.absolute()).casefold() not in selected_parents:
            continue
        family, branch = movie_group(root, item, franchise, names)
        if not family:
            continue
        from cloud_organizer import clean
        parent = str(Path(item['Path']).parent.absolute()).casefold()
        for entry in entries_by_parent.get(parent, []):
            if entry_ids is not None and entry['id'] not in entry_ids:
                continue
            binding = bindings.get(entry['id'])
            remote = (binding or {}).get('remote') or entry.get('remote', '')
            source = next((base for base in ('/夸克/movie', '/百度/movie') if remote.startswith(base + '/')), None)
            if not source or not binding or binding.get('phase') not in ('done', 'bound'):
                continue
            expected = source + '/' + clean(family) + '/' + clean(branch) + '/'
            if remote.startswith(expected):
                continue
            binding.update(normalized=False, layout_group=family, layout_branch=branch, layout_reason='verified_collection_changed')
            route_pending.add(entry['id'])
            changed_bindings = True
        title = family + '剧场版' if family in ('海贼王', '火影忍者', '名侦探柯南') else family
        groups.setdefault(title, set()).add(item['Id'])
        if parent in selected_parents:
            selected_groups.add(title)
    if changed_bindings:
        write(bindings_path, bindings)
    worker.collection_route_pending = route_pending
    managed = read(root / 'data/managed-movie-collections.json')
    results = []
    for title, ids in sorted(groups.items()):
        if entry_ids is not None and title not in selected_groups:
            continue
        existing = next((box for box in boxes if box.get('Name') == title), None)
        if entry_ids is not None and existing:
            ids.update((i['Id'] for i in worker.api('jellyfin', '/Items?ParentId=' + existing['Id'])['Items']))
        if len(ids) < 2:
            continue
        if existing:
            ident = existing['Id']
            current = {i['Id'] for i in worker.api('jellyfin', '/Items?ParentId=' + ident)['Items']}
            missing = ids - current
            if missing:
                worker.api('jellyfin', '/Collections/' + ident + '/Items?ids=' + ','.join(sorted(missing)), {}, method='POST')
            obsolete = current - ids if managed.get(title, {}).get('id') == ident else set()
            if obsolete:
                worker.api('jellyfin', '/Collections/' + ident + '/Items?ids=' + ','.join(sorted(obsolete)), method='DELETE')
        else:
            query = urllib.parse.urlencode({'name': title, 'ids': ','.join(sorted(ids)), 'isLocked': 'true'})
            created = worker.api('jellyfin', '/Collections?' + query, {}, method='POST')
            ident = created['Id']
            missing, obsolete = (ids, set())
        managed[title] = {'id': ident, 'movie_ids': sorted(ids), 'updated_at': time.time()}
        results.append({'name': title, 'id': ident, 'movies': len(ids), 'added': len(missing), 'removed': len(obsolete)})
    write(root / 'data/managed-movie-collections.json', managed)
    return results

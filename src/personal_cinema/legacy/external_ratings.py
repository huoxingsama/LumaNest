"""Exact-work ratings, refreshed by explicit batches and served from local cache.

IMDb uses its official daily public TSV. MDBList is optional and queries only
TMDB movie/show IDs; no title search, season inheritance, or page-time network.
The key never enters this module's persistent state or client-facing objects.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import contextlib
import csv
import datetime as dt
import gzip
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from personal_cinema.runtime_paths import RuntimePath as Path
from personal_cinema.runtime_paths import option, proxy_handler
IMDB_URL = 'https://datasets.imdbws.com/title.ratings.tsv.gz'
CACHE_FILE = 'data/ratings/external-ratings.json'
DATASET_FILE = 'data/ratings/title.ratings.tsv.gz'
KEY_FILE = 'secrets/mdblist-api-key.txt'
DAY = 86400
SOURCES = ('imdb', 'trakt', 'tomatoes', 'audience', 'metacritic', 'letterboxd', 'myanimelist')
SCALES = {'imdb': 10, 'trakt': 10, 'tomatoes': 100, 'audience': 100, 'metacritic': 100, 'letterboxd': 5, 'myanimelist': 10}
MDBLIST_SOURCES = tuple((source for source in SOURCES if source != 'trakt'))
MDBLIST_PARSER_VERSION = 2
_LOCK = threading.Lock()

def rating_policy():
    """Read once per batch/response; retaining cache does not enable display."""
    sources = option('ratings.providers', ['imdb', 'tomatoes', 'audience', 'metacritic', 'letterboxd', 'myanimelist', 'bangumi'])
    return bool(option('ratings.enabled', True)), tuple(dict.fromkeys(sources)), int(option('ratings.refresh_days', 7)) * DAY

def _read(path, fallback=None):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else fallback or {}
    except (OSError, ValueError):
        return fallback or {}

def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def _iso(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None

def _votes(value):
    number = _number(value.replace(',', '') if isinstance(value, str) else value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None

def provider_ids(item):
    raw = item.get('ProviderIds') or {}
    return {str(key).lower(): str(value).strip() for key, value in raw.items()} if isinstance(raw, dict) else {}

def identity(item):
    """Movie/Series only; TMDB namespaces are never interchangeable."""
    scope = {'Movie': 'movie', 'Series': 'series'}.get(item.get('Type'))
    ids = provider_ids(item)
    tmdb = ids.get('tmdb', '')
    if not scope or not re.fullmatch('[1-9][0-9]*', tmdb):
        return None
    imdb = ids.get('imdb', '')
    return {'key': scope + ':tmdb:' + tmdb, 'scope': scope, 'tmdb': tmdb, 'imdb': imdb if re.fullmatch('tt[0-9]{7,12}', imdb) else None}

def confirmed_items(root, items):
    """Require the matcher to have confirmed the same canonical TMDB ID."""
    state = _read(Path(root) / 'data/metadata-state.json')
    result = []
    for item in items:
        ident = identity(item)
        record = state.get(item.get('Id'), {})
        known = str((record.get('provider_ids') or {}).get('Tmdb') or '')
        if ident and record.get('status') in ('matched', 'already_matched', 'locked') and (known == ident['tmdb']):
            result.append(item)
    return result

def _batch_keys(keys):
    if keys is None:
        return None
    result = set()
    for key in keys:
        if isinstance(key, (tuple, list)) and len(key) == 2:
            kind, ident = key
            scope = {'Movie': 'movie', 'Series': 'series', 'movie': 'movie', 'series': 'series'}.get(kind)
            if scope and re.fullmatch('[1-9][0-9]*', str(ident)):
                result.add(scope + ':tmdb:' + str(ident))
        elif isinstance(key, str) and re.fullmatch('(movie|series):tmdb:[1-9][0-9]*', key):
            result.add(key)
    return result

def _safe_url(source, value, imdb=None):
    domains = {'imdb': {'imdb.com', 'www.imdb.com'}, 'trakt': {'trakt.tv', 'www.trakt.tv'}, 'tomatoes': {'rottentomatoes.com', 'www.rottentomatoes.com'}, 'audience': {'rottentomatoes.com', 'www.rottentomatoes.com'}, 'metacritic': {'metacritic.com', 'www.metacritic.com'}, 'letterboxd': {'letterboxd.com', 'www.letterboxd.com'}, 'myanimelist': {'myanimelist.net', 'www.myanimelist.net'}}
    relative = {'tomatoes': ('https://www.rottentomatoes.com', ('/m/', '/tv/')), 'audience': ('https://www.rottentomatoes.com', ('/m/', '/tv/')), 'metacritic': ('https://www.metacritic.com', ('/movie/', '/tv/')), 'letterboxd': ('https://letterboxd.com', ('/film/',)), 'myanimelist': ('https://myanimelist.net', ('/anime/',))}
    if isinstance(value, str):
        if source in relative and value.startswith(relative[source][1]):
            value = relative[source][0] + value
        try:
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme == 'https' and parsed.hostname in domains[source] and (not parsed.username) and (not parsed.password):
                return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', parsed.fragment))
        except ValueError:
            pass
    if source == 'imdb' and imdb and re.fullmatch('tt[0-9]{7,12}', imdb):
        return 'https://www.imdb.com/title/' + imdb + '/'
    return None

def sanitize_ratings(values, scope):
    """The proxy accepts only the documented finite, scoped public fields."""
    selected = {}
    for rating in values if isinstance(values, list) else []:
        if not isinstance(rating, dict):
            continue
        source = rating.get('source')
        value = _number(rating.get('value'))
        if source not in SOURCES or rating.get('scope') != scope or rating.get('scale') != SCALES[source]:
            continue
        if value is None or value < 0 or value > SCALES[source]:
            continue
        provider = rating.get('provider')
        if source == 'trakt' and provider != 'trakt':
            continue
        fetched = rating.get('fetched_at')
        try:
            if not isinstance(fetched, str) or dt.datetime.fromisoformat(fetched.replace('Z', '+00:00')).tzinfo is None:
                continue
        except ValueError:
            continue
        clean = {'source': source, 'value': value, 'scale': SCALES[source], 'url': _safe_url(source, rating.get('url')), 'votes': _votes(rating.get('votes')), 'fetched_at': fetched, 'scope': scope}
        if provider in ('mdblist', 'imdb', 'trakt'):
            clean['provider'] = provider
        if source not in selected or (clean['votes'] or 0) > (selected[source]['votes'] or 0):
            selected[source] = clean
    return [selected[source] for source in SOURCES if source in selected]

class RatingFetchError(Exception):
    """A safe error descriptor: never contains an authenticated URL or body."""

    def __init__(self, status, retry_after=300):
        self.status = status
        self.retry_after = max(1, min(DAY, int(retry_after)))
        super().__init__(status)

class _NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None

def direct_http():
    return urllib.request.build_opener(proxy_handler(), _NoRedirect())

def _retry_after(headers, now):
    value = headers.get('Retry-After', '3600')
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return max(1, int(parsedate_to_datetime(value).timestamp() - now))
        except (TypeError, ValueError, OverflowError):
            return 3600

class MDBListClient:

    def __init__(self, key, opener=None):
        self._key = key
        self.http = opener or direct_http()
        self._last_request = 0

    def fetch(self, ident, now=None):
        delay = 1.0 - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()
        kind = 'movie' if ident['scope'] == 'movie' else 'show'
        url = 'https://api.mdblist.com/tmdb/' + kind + '/' + ident['tmdb'] + '/'
        request = urllib.request.Request(url + '?' + urllib.parse.urlencode({'apikey': self._key}), headers={'Accept': 'application/json', 'User-Agent': 'PersonalMediaLibrary/1.0'})
        try:
            with self.http.open(request, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise RatingFetchError('response-too-large')
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('error') or value.get('response') is False:
                raise RatingFetchError('invalid-response')
            return value
        except urllib.error.HTTPError as error:
            status = 'rate-limited' if error.code == 429 else 'auth-required' if error.code in (401, 403) else 'not-found' if error.code == 404 else 'http-' + str(error.code)
            raise RatingFetchError(status, _retry_after(error.headers, now or time.time()) if error.code == 429 else DAY if error.code == 404 else 3600) from None
        except (OSError, urllib.error.URLError, ValueError):
            raise RatingFetchError('network-or-invalid-response') from None

def parse_mdblist(payload, ident, now):
    """Validate the resolved TMDB identity before interpreting source scales."""
    ids = payload.get('ids') or {}
    if not isinstance(ids, dict):
        raise RatingFetchError('identity-mismatch', DAY)
    tmdb = payload.get('tmdbid') or payload.get('tmdb_id') or ids.get('tmdb') or payload.get('id')
    kind = payload.get('mediatype') or payload.get('type')
    expected = 'movie' if ident['scope'] == 'movie' else 'show'
    imdb = payload.get('imdbid') or payload.get('imdb_id') or ids.get('imdb')
    if str(tmdb) != ident['tmdb'] or kind != expected or (imdb and ident['imdb'] and (imdb != ident['imdb'])):
        raise RatingFetchError('identity-mismatch', DAY)
    values = []
    aliases = {'popcorn': 'audience', 'tomatoesaudience': 'audience', 'audience': 'audience'}
    for row in payload.get('ratings', []) if isinstance(payload.get('ratings'), list) else []:
        if not isinstance(row, dict):
            continue
        source = aliases.get(row.get('source'), row.get('source'))
        if source not in MDBLIST_SOURCES:
            continue
        value = _number(row.get('value'))
        raw_scale = SCALES[source]
        if value is None or not 0 <= value <= raw_scale:
            continue
        values.append({'source': source, 'value': value, 'scale': SCALES[source], 'url': _safe_url(source, row.get('url'), ident['imdb'] or imdb), 'votes': _votes(row.get('votes')), 'fetched_at': _iso(now), 'scope': ident['scope'], 'provider': 'mdblist'})
    return sanitize_ratings(values, ident['scope'])

def ensure_imdb_dataset(root, now, opener=None):
    path = Path(root) / DATASET_FILE
    if path.exists() and now - path.stat().st_mtime < rating_policy()[2]:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='imdb-', suffix='.gz.tmp', dir=path.parent)
    try:
        request = urllib.request.Request(IMDB_URL, headers={'User-Agent': 'PersonalMediaLibrary/1.0'})
        with (opener or direct_http()).open(request, timeout=30) as response, os.fdopen(handle, 'wb') as output:
            handle = None
            total = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 64 * 1024 * 1024:
                    raise RatingFetchError('imdb-dataset-too-large')
                output.write(chunk)
        with gzip.open(temporary, 'rt', encoding='utf-8') as data:
            if data.readline().rstrip('\r\n') != 'tconst\taverageRating\tnumVotes':
                raise RatingFetchError('imdb-invalid-header')
            decoded = 0
            while True:
                block = data.read(1024 * 1024)
                if not block:
                    break
                decoded += len(block)
                if decoded > 512 * 1024 * 1024:
                    raise RatingFetchError('imdb-dataset-too-large')
        os.replace(temporary, path)
        return path
    except (OSError, EOFError, ValueError, urllib.error.URLError, RatingFetchError):
        if path.exists():
            return path
        raise RatingFetchError('imdb-download-unavailable') from None
    finally:
        if handle is not None:
            os.close(handle)
        Path(temporary).unlink(missing_ok=True)

def read_imdb_ratings(path, wanted):
    """Streaming read: only library IDs are retained, never the whole table."""
    wanted = set(wanted)
    found = {}
    if not wanted:
        return found
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as stream:
        rows = csv.DictReader(stream, delimiter='\t')
        if rows.fieldnames != ['tconst', 'averageRating', 'numVotes']:
            raise RatingFetchError('imdb-invalid-header')
        for row in rows:
            ident = row.get('tconst')
            if ident not in wanted:
                continue
            value, votes = (_number(row.get('averageRating')), _votes(row.get('numVotes')))
            if value is not None and 0 <= value <= 10 and (votes is not None) and (votes > 0):
                found[ident] = (value, votes)
            if len(found) == len(wanted):
                break
    return found

@contextlib.contextmanager
def _sync_lock(root):
    if not _LOCK.acquire(blocking=False):
        yield False
        return
    stream = None
    locked = False
    try:
        path = Path(root) / 'data/ratings/sync.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open('a+b')
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass
        yield locked
    finally:
        if stream:
            if locked:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        _LOCK.release()

def sync_ratings(root, items, *, batch_keys=None, force=False, imdb_only=False, max_requests=100, now=None, imdb_loader=None, mdblist_client=None):
    """Refresh confirmed Movie/Series DTOs; explicit empty batch does no work.

    Canonical batch keys are {('Movie', 'tmdbid'), ('Series', 'tmdbid')}.
    Callers must establish identities first; ``confirmed_items`` filters a
    whole-library query against the persisted matcher before calling here.
    Ordinary provider failures become cached retry states, not ingestion errors.
    """
    root = Path(root)
    enabled, selected, refresh_seconds = rating_policy()
    if not enabled or not selected:
        return {'state': 'disabled', 'works': 0, 'imdb_updated': 0, 'mdblist_updated': 0, 'mdblist_requests': 0, 'errors': []}
    now = time.time() if now is None else float(now)
    allowed = _batch_keys(batch_keys)
    targets = {}
    native_ids = {}
    for item in items:
        ident = identity(item)
        if not ident or (allowed is not None and ident['key'] not in allowed):
            continue
        previous = targets.get(ident['key'])
        if previous and previous['imdb'] and ident['imdb'] and (previous['imdb'] != ident['imdb']):
            previous['conflict'] = True
        elif previous and (not previous['imdb']) and ident['imdb']:
            previous['imdb'] = ident['imdb']
        else:
            targets.setdefault(ident['key'], ident)
        if item.get('Id'):
            native_ids[str(item['Id'])] = ident['key']
    targets = {key: value for key, value in targets.items() if not value.get('conflict')}
    summary = {'state': 'complete', 'works': len(targets), 'imdb_updated': 0, 'mdblist_updated': 0, 'mdblist_requests': 0, 'errors': []}
    if not targets:
        return summary
    with _sync_lock(root) as locked:
        if not locked:
            return {**summary, 'state': 'busy'}
        path = root / CACHE_FILE
        cache = _read(path, {'schema': 1, 'items': {}, 'item_ids': {}, 'providers': {}})
        if any((not isinstance(cache.get(field, {}), dict) for field in ('items', 'item_ids', 'providers'))):
            cache = {'schema': 1, 'items': {}, 'item_ids': {}, 'providers': {}}
        records = cache.setdefault('items', {})
        bindings = cache.setdefault('item_ids', {})
        global_state = cache.setdefault('providers', {})
        for native, key in native_ids.items():
            if key in targets:
                bindings[native] = key
        for key, ident in targets.items():
            old = records.get(key, {})
            if not isinstance(old, dict):
                old = {}
            if old.get('imdb') and ident['imdb'] and (old['imdb'] != ident['imdb']):
                old = {}
            records[key] = {**old, **ident, 'ratings': sanitize_ratings(old.get('ratings', []), ident['scope']), 'providers': old.get('providers') if isinstance(old.get('providers'), dict) else {}}
        wanted = {ident['imdb'] for ident in targets.values() if ident['imdb']} if 'imdb' in selected else set()
        if wanted:
            try:
                imdb_time = now
                if imdb_loader:
                    found = imdb_loader(wanted)
                else:
                    dataset = ensure_imdb_dataset(root, now)
                    imdb_time = min(now, dataset.stat().st_mtime)
                    found = read_imdb_ratings(dataset, wanted)
                for key, ident in targets.items():
                    if ident['imdb'] not in found:
                        continue
                    value, votes = found[ident['imdb']]
                    rating = {'source': 'imdb', 'value': value, 'scale': 10, 'url': _safe_url('imdb', None, ident['imdb']), 'votes': votes, 'fetched_at': _iso(imdb_time), 'scope': ident['scope'], 'provider': 'imdb'}
                    records[key]['ratings'] = [r for r in records[key]['ratings'] if r.get('source') != 'imdb'] + [rating]
                    records[key]['providers']['imdb'] = {'checked_at': now, 'status': 'updated', 'origin': IMDB_URL}
                    summary['imdb_updated'] += 1
                global_state['imdb'] = {'checked_at': now, 'status': 'updated'}
            except (RatingFetchError, OSError, EOFError, ValueError) as error:
                summary['errors'].append('imdb-unavailable')
                global_state['imdb'] = {'checked_at': now, 'status': 'retry', 'next_attempt_at': now + 3600}
        key_text = ''
        use_mdblist = not imdb_only and bool(set(selected) & (set(MDBLIST_SOURCES) - {'imdb'}))
        if use_mdblist and mdblist_client is None:
            try:
                key_text = (root / KEY_FILE).read_text(encoding='utf-8').strip()
            except OSError:
                pass
        client = (mdblist_client or (MDBListClient(key_text) if key_text else None)) if use_mdblist else None
        quota = global_state.setdefault('mdblist', {})
        key_changed = False
        if key_text:
            try:
                revision = (root / KEY_FILE).stat().st_mtime_ns
            except OSError:
                revision = quota.get('key_revision')
            key_changed = quota.get('key_revision') != revision
            quota['key_revision'] = revision
            if key_changed and quota.get('status') == 'auth-required':
                quota.pop('next_attempt_at', None)
                quota.pop('status', None)
        day = _iso(now)[:10]
        if quota.get('day') != day:
            quota.update(day=day, requests=0)
        summary['mdblist_status'] = 'not-configured' if client is None else 'cached'
        if client and quota.get('next_attempt_at', 0) <= now:
            for key, ident in targets.items():
                record = records[key]
                prior = record['providers'].get('mdblist', {})
                if prior.get('next_attempt_at', 0) > now and (not (key_changed and prior.get('status') == 'auth-required')):
                    continue
                if not force and prior.get('parser_version') == MDBLIST_PARSER_VERSION and (prior.get('status') == 'updated') and (now - prior.get('checked_at', 0) < refresh_seconds):
                    continue
                if summary['mdblist_requests'] >= max(0, max_requests) or quota.get('requests', 0) >= 900:
                    summary['mdblist_status'] = 'batch-limit'
                    break
                summary['mdblist_requests'] += 1
                quota['requests'] = quota.get('requests', 0) + 1
                try:
                    fresh = parse_mdblist(client.fetch(ident, now=now), ident, now)
                    independent = [r for r in record['ratings'] if r.get('source') == 'imdb' or (r.get('source') == 'trakt' and r.get('provider') == 'trakt')]
                    retained_sources = {r['source'] for r in independent}
                    record['ratings'] = independent + [r for r in fresh if r['source'] not in retained_sources]
                    record['providers']['mdblist'] = {'status': 'updated', 'checked_at': now, 'parser_version': MDBLIST_PARSER_VERSION}
                    summary['mdblist_updated'] += 1
                    summary['mdblist_status'] = 'updated'
                except RatingFetchError as error:
                    record['providers']['mdblist'] = {'status': error.status, 'checked_at': now, 'next_attempt_at': now + error.retry_after}
                    summary['errors'].append(error.status)
                    if error.status in ('rate-limited', 'auth-required'):
                        quota.update(status=error.status, next_attempt_at=now + error.retry_after)
                        summary['mdblist_status'] = error.status
                        _write(path, cache)
                        break
                _write(path, cache)
        elif client:
            summary['mdblist_status'] = quota.get('status', 'retry-later')
        for key in targets:
            records[key]['ratings'] = sanitize_ratings(records[key]['ratings'], records[key]['scope'])
        cache.update(schema=1, updated_at=now)
        summary['errors'] = sorted(set(summary['errors']))
        cache['last_sync'] = summary
        _write(path, cache)
        return summary

class CachedRatings:
    """mtime-based local-only DTO augmentation, safe on the HTTP hot path."""

    def __init__(self, root):
        self.path = Path(root) / CACHE_FILE
        self.season_path = Path(root) / 'data/season-ratings-state.json'
        self.lock = threading.Lock()
        self.stamp = None
        self.cache = {}
        self.season_stamp = None
        self.seasons = {}

    def snapshot(self):
        with self.lock:
            try:
                stat = self.path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != self.stamp and stat.st_size < 16 * 1024 * 1024:
                    value = _read(self.path)
                    if value.get('schema') == 1 and isinstance(value.get('items'), dict):
                        self.cache, self.stamp = (value, stamp)
            except OSError:
                pass
            return self.cache

    def season_snapshot(self):
        """Bind only native Season IDs, never the parent show's TMDB score."""
        with self.lock:
            try:
                stat = self.season_path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != self.season_stamp and stat.st_size < 8 * 1024 * 1024:
                    state = _read(self.season_path)
                    self.seasons = {str(row['item_id']): row for row in state.values() if isinstance(row, dict) and row.get('item_id')}
                    self.season_stamp = stamp
            except OSError:
                pass
            return self.seasons

    def apply(self, value):
        enabled, selected, _refresh_seconds = rating_policy()
        cache = self.snapshot()
        seasons = self.season_snapshot()
        records, bindings = (cache.get('items', {}), cache.get('item_ids', {}))

        def walk(item):
            if isinstance(item, list):
                return [walk(child) for child in item]
            if not isinstance(item, dict):
                return item
            result = {key: walk(child) for key, child in item.items()}
            if not enabled:
                if 'ExternalRatings' in result or item.get('Type') in ('Movie', 'Series', 'Season'):
                    result['ExternalRatings'] = []
                return result
            if isinstance(result.get('ExternalRatings'), list):
                known = {r.get('source'): r for r in result['ExternalRatings'] if isinstance(r, dict)}
                result['ExternalRatings'] = [known[source] for source in selected if source in known]
            if item.get('Type') == 'Season':
                record = seasons.get(str(item.get('Id', '')), {})
                if not record or (item.get('IndexNumber') is not None and item['IndexNumber'] != record.get('season')):
                    return result
                if record.get('series_id') and item.get('SeriesId') and (record['series_id'] != item['SeriesId']):
                    return result
                status = record.get('status')
                result['SeasonRatingStatus'] = status
                result['SeasonRatingSource'] = 'Bangumi'
                if status == 'no-independent-rating':
                    result.update(CommunityRating=None, CriticRating=None, ExternalRatings=[])
                    return result
                score = _number(record.get('score'))
                votes = _votes(record.get('votes'))
                url = record.get('source_url') or ''
                if status in ('applied', 'retry') and score is not None and (0 < score <= 10) and votes and re.fullmatch('https://bgm\\.tv/subject/[1-9][0-9]*', url):
                    result['CommunityRating'] = score
                    result['CriticRating'] = None
                    result['ExternalRatings'] = [{'source': 'bangumi', 'scope': 'season', 'season': record['season'], 'value': score, 'scale': 10, 'votes': votes, 'url': url, 'fetched_at': _iso(record.get('checked_at') or 0)}] if 'bangumi' in selected else []
                return result
            scope = {'Movie': 'movie', 'Series': 'series'}.get(item.get('Type'))
            if not scope:
                return result
            ident = identity(item)
            key = ident['key'] if ident else bindings.get(str(item.get('Id', '')))
            record = records.get(key, {})
            if not isinstance(record, dict) or record.get('scope') != scope:
                return result
            ids = provider_ids(item)
            if ids.get('tmdb') and ids['tmdb'] != record.get('tmdb'):
                return result
            if ids.get('imdb') and record.get('imdb') and (ids['imdb'] != record['imdb']):
                return result
            ratings = {r['source']: r for r in sanitize_ratings(record.get('ratings', []), scope)}
            result['ExternalRatings'] = [ratings[source] for source in selected if source in ratings]
            return result
        return walk(value)

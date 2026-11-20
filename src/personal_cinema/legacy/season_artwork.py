"""Verified per-season artwork, with local copies and source provenance."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import base64
import hashlib
import json
import re
import shutil
import time
import tempfile
import urllib.request
import urllib.error
from personal_cinema.runtime_paths import RuntimePath as Path
from PIL import Image
from cache_policy import cache_path
from chinese_credits import Document
from metadata_matcher import read_json, write_json
from series_view import season_source
from artwork_quality import fetch_best, remote_images, needs_upgrade, quality_record, original_url

def api(client, path, data=None, method=None):
    return client.json(path, data, method) if hasattr(client, 'json') else client.api('jellyfin', path, data, method)

def prefer_existing_backdrop(client, ident, digest):
    """Promote verified original bytes; Jellyfin's upload index is unused."""
    root = Path(getattr(client, 'root', _cinema_path(''))).resolve()
    allowed = [(root / part).resolve() for part in ('catalog', 'data/jellyfin/metadata')]

    def matches(info):
        if info.get('ImageType') != 'Backdrop' or not info.get('Path'):
            return False
        path = Path(info['Path']).resolve()
        if not any((path.is_relative_to(base) for base in allowed)):
            return False
        try:
            return path.stat().st_size <= 24 * 1024 * 1024 and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        except OSError:
            return False
    infos = api(client, '/Items/' + ident + '/Images')
    found = next((info for info in infos if matches(info)), None)
    if found is None:
        return False
    index = int(found.get('ImageIndex', 0))
    if index:
        api(client, '/Items/' + ident + '/Images/Backdrop/' + str(index) + '/Index?newIndex=0', method='POST')
        current = next((info for info in api(client, '/Items/' + ident + '/Images') if info.get('ImageType') == 'Backdrop' and int(info.get('ImageIndex', 0)) == 0), {})
        if not matches(current):
            raise ValueError('Native default backdrop does not match selected original')
    return True

def upload_image(client, ident, path, kind='Primary'):
    data = Path(path).read_bytes()
    with Image.open(path) as im:
        assert im.width >= 200 and im.height >= 100
        mime = 'image/png' if im.format == 'PNG' else 'image/jpeg'
    digest = hashlib.sha256(data).hexdigest()
    if kind == 'Backdrop' and prefer_existing_backdrop(client, ident, digest):
        return
    if not client.token:
        api(client, '/System/Info')
    authorization = 'MediaBrowser Client="LibraryArtwork", Device="Windows", DeviceId="library-artwork", Version="1.0", Token="' + client.token + '"'
    for attempt in range(2):
        request = urllib.request.Request(_cinema_endpoint('jellyfin') + '/Items/' + ident + '/Images/' + kind, data=base64.b64encode(data), headers={'Authorization': authorization, 'Content-Type': mime}, method='POST')
        try:
            with client.http.open(request, timeout=45) as response:
                assert response.status == 204
            if kind == 'Backdrop' and (not prefer_existing_backdrop(client, ident, digest)):
                raise ValueError('Uploaded backdrop original was not registered')
            return
        except urllib.error.HTTPError as error:
            if error.code != 401 or attempt:
                raise
            client.token = None
            api(client, '/System/Info')
            authorization = 'MediaBrowser Token="' + client.token + '"'

def install_artwork(root, client, item, path, kind='Primary', replace_backdrops=False):
    base = Path(item['Path']) if item['Type'] in ('Series', 'Season', 'BoxSet') else Path(item['Path']).parent
    relative = base.absolute().relative_to((root / 'catalog').absolute())
    target = cache_path(root / 'catalog', (relative / ('poster.jpg' if kind == 'Primary' else 'fanart.jpg')).as_posix())
    backup = root / 'data/artwork-backups' / item['Id']
    backup.mkdir(parents=True, exist_ok=True)
    if target.exists() and (not (backup / target.name).exists()):
        shutil.copy2(target, backup / target.name)
    infos = api(client, '/Items/' + item['Id'] + '/Images')
    for index, info in enumerate(infos):
        source = Path(info.get('Path', ''))
        if info.get('ImageType') == kind and source.is_file() and (not (backup / (f'original-{kind}-{index}' + source.suffix)).exists()):
            shutil.copy2(source, backup / (f'original-{kind}-{index}' + source.suffix))
    if kind == 'Backdrop' and replace_backdrops:
        for info in sorted([i for i in infos if i.get('ImageType') == 'Backdrop'], key=lambda i: i.get('ImageIndex', 0), reverse=True):
            api(client, '/Items/' + item['Id'] + '/Images/Backdrop/' + str(info.get('ImageIndex', 0)), method='DELETE')
    shutil.copy2(path, target)
    upload_image(client, item['Id'], path, kind)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def season_source_poster(content, expected_tmdb=None, expected_season=None):
    doc = Document(content)
    primary = None
    if expected_tmdb is not None:
        canonical = next((node.attrs.get('href', '') for node in doc.root.find('link') if node.attrs.get('rel') == 'canonical'), '')
        wanted = 'https://www\\.themoviedb\\.org/tv/' + re.escape(str(expected_tmdb)) + '(?:-[^/?#]*)?/season/' + re.escape(str(expected_season)) + '/?'
        if not re.fullmatch(wanted, canonical):
            raise ValueError('Season artwork page identity mismatch')
        primary = next((node.attrs.get('content', '') for node in doc.root.find('meta') if node.attrs.get('property') == 'og:image'), None)
    for image in doc.root.find('img'):
        url = image.attrs.get('src', '')
        if 'poster' in image.attrs.get('class', '').split() and re.match('https://(?:media\\.themoviedb\\.org|image\\.tmdb\\.org)/t/p/', url) and (expected_tmdb is None or (primary and url.rsplit('/', 1)[-1] == primary.rsplit('/', 1)[-1])):
            return re.sub('/t/p/[^/]+/', '/t/p/original/', url)

def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def source_proof(root, parent, season, sources):
    """A copied original plus its exact season page proves an existing image."""
    tmdb = str(parent.get('ProviderIds', {}).get('Tmdb', ''))
    number = season.get('IndexNumber')
    record = sources.get(tmdb + ':' + str(number))
    if not record or str(record.get('parent_tmdb')) != tmdb or record.get('season') != number:
        return None
    reference = cache_path(root, record['reference_path'])
    page = cache_path(root, record['page_path'])
    if not reference.is_file() or not page.is_file() or _sha(reference) != record.get('sha256'):
        return None
    content = page.read_text('utf-8')
    doc = Document(content)
    canonical = next((node.attrs.get('href', '') for node in doc.root.find('link') if node.attrs.get('rel') == 'canonical'), '')
    if canonical != record.get('source_url'):
        return None
    source = season_source_poster(content, record['provider_id'], record['provider_season'])
    if not source or source.rsplit('/', 1)[-1] != record.get('image_url', '').rsplit('/', 1)[-1]:
        return None
    return {**record, 'reference_path': str(reference)}

class ArtworkWorker:

    def __init__(self, root, client):
        self.root = root
        self.client = client
        self.state = read_json(root / 'data/season-artwork-state.json', {})

    def upgrade_quality(self, parent, season, record):
        """Only upgrade a proven season; custom mappings never use parent images."""
        target = Path(season['Path']) / 'poster.jpg'
        source = record.get('image_url')
        if not original_url(source) or record.get('quality_retry_at', 0) > time.time() or (not needs_upgrade(target, 'Primary', record.get('quality'))):
            return record

        def verify():
            current = api(self.client, '/Items/' + parent['Id'])
            child = api(self.client, '/Items/' + season['Id'])
            if str((current.get('ProviderIds') or {}).get('Tmdb')) != str(record['parent_tmdb']) or child.get('IndexNumber') != season['IndexNumber'] or child.get('SeriesId', child.get('ParentId')) != parent['Id']:
                raise ValueError('Season artwork identity changed')
            if child.get('IsLocked') or 'Images' in (child.get('LockedFields') or []):
                raise ValueError('Season artwork manually locked')
        try:
            verify()
            ordinary = str(record.get('provider_id')) == str(record['parent_tmdb']) and record.get('provider_season') == season['IndexNumber']
            remote = remote_images(self.client, season['Id']) if ordinary else []
            folder = self.root / 'data/artwork-staging'
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='season-quality-', dir=folder) as directory:
                result = fetch_best(directory, 'Primary', source, remote, current_path=target, language_proofs=read_json(self.root / 'data/artwork-language-proofs.json', {}))
                verify()
                if not result['kept_current']:
                    digest = install_artwork(self.root, self.client, season, result['path'])
                    record = {**record, 'sha256': digest, 'image_url': result['url'], 'proof': 'exact-season-provider-original'}
                record = {**record, 'quality': quality_record(target, 'Primary', language=result.get('language'), source_url=result['url'], kept_current=result['kept_current'])}
        except (OSError, ValueError, KeyError, Image.DecompressionBombError) as error:
            record = {**record, 'quality_error': type(error).__name__, 'quality_retry_at': time.time() + 3600}
        return record

    def cycle(self, limit=1, target_ids=None):
        target_ids = getattr(self, 'target_ids', None) if target_ids is None else target_ids
        if limit <= 0:
            return self.state
        done = 0
        sources = read_json(self.root / 'data/season-artwork-sources.json', {})
        baseline = sources.get('_existing_season_ids')
        profiles = read_json(self.root / 'data/chinese-season-profiles.json', {})
        series = api(self.client, '/Items?Recursive=true&IncludeItemTypes=Series&Fields=ProviderIds')['Items']
        for parent in series:
            if target_ids is not None and parent['Id'] not in target_ids:
                continue
            tmdb = (parent.get('ProviderIds') or {}).get('Tmdb')
            if not tmdb or not tmdb.isdecimal():
                continue
            seasons = api(self.client, '/Shows/' + parent['Id'] + '/Seasons?Fields=Path,LockedFields')['Items']
            for season in seasons:
                number = season.get('IndexNumber')
                prior = self.state.get(season['Id'], {})
                if number is None:
                    continue
                image = (season.get('ImageTags') or {}).get('Primary')
                if season.get('IsLocked') or 'Images' in season.get('LockedFields', []):
                    continue
                target = Path(season['Path']) / 'poster.jpg' if season.get('Path') else None
                try:
                    proof = source_proof(self.root, parent, season, sources)
                except (OSError, ValueError, KeyError):
                    proof = None
                if image and proof and target and target.is_file() and (_sha(target) == proof['sha256']):
                    self.state[season['Id']] = {'status': 'applied', 'parent_tmdb': tmdb, 'season': number, 'provider_id': str(proof['provider_id']), 'provider_season': proof['provider_season'], 'source_url': proof['source_url'], 'image_url': proof['image_url'], 'sha256': proof['sha256'], 'image_tag': image, 'proof': 'exact-season-page-and-original-sha', 'updated_at': time.time()}
                    self.state[season['Id']] = self.upgrade_quality(parent, season, {**self.state[season['Id']], 'quality': prior.get('quality', {}), 'quality_retry_at': prior.get('quality_retry_at', 0)})
                    write_json(self.root / 'data/season-artwork-state.json', self.state)
                    continue
                if image and prior.get('status') == 'applied' and prior.get('proof') and (prior.get('parent_tmdb') == tmdb) and (prior.get('season') == number) and target and target.is_file() and (_sha(target) == prior.get('sha256')):
                    self.state[season['Id']] = self.upgrade_quality(parent, season, prior)
                    write_json(self.root / 'data/season-artwork-state.json', self.state)
                    continue
                if prior.get('next_attempt_at', 0) > time.time():
                    continue
                mapped = season_source(self.root, parent, number)
                if proof:
                    digest = install_artwork(self.root, self.client, season, Path(proof['reference_path']))
                    self.state[season['Id']] = {'status': 'applied', 'parent_tmdb': tmdb, 'season': number, 'provider_id': str(proof['provider_id']), 'provider_season': proof['provider_season'], 'source_url': proof['source_url'], 'image_url': proof['image_url'], 'sha256': digest, 'proof': 'exact-season-page-and-original-sha', 'updated_at': time.time()}
                    self.state[season['Id']] = self.upgrade_quality(parent, season, self.state[season['Id']])
                    write_json(self.root / 'data/season-artwork-state.json', self.state)
                    done += 1
                    if done >= limit:
                        return self.state
                    continue
                relative = Path(season['Path']).absolute().relative_to((self.root / 'catalog').absolute()).as_posix() if season.get('Path') else ''
                custom = (profiles.get(relative, {}).get('provider_ids') or {}).get('Custom')
                existing_unproved = bool(image and (baseline is None or season['Id'] in baseline))
                if existing_unproved or custom or mapped.get('local_group') or mapped.get('poster_path'):
                    self.state[season['Id']] = {'status': 'needs-review', 'reason': 'existing-season-source-unverified' if image else 'independent-season-source-required', 'parent_tmdb': tmdb, 'season': number, 'updated_at': time.time()}
                    write_json(self.root / 'data/season-artwork-state.json', self.state)
                    continue
                url = f"https://www.themoviedb.org/tv/{mapped['tmdb']}/season/{mapped['season']}?language=zh-CN"
                try:
                    with urllib.request.urlopen(url, timeout=20) as response:
                        content = response.read(4 * 1024 * 1024).decode('utf-8')
                    source = season_source_poster(content, mapped['tmdb'], mapped['season'])
                    if not source:
                        raise ValueError('No independent season poster')
                    folder = self.root / 'data/artwork-downloads'
                    folder.mkdir(exist_ok=True)
                    ordinary = str(mapped['tmdb']) == str(tmdb) and mapped['season'] == number
                    with tempfile.TemporaryDirectory(prefix='season-', dir=folder) as directory:
                        selected = fetch_best(directory, 'Primary', source, remote_images(self.client, season['Id']) if ordinary else [])
                        digest = install_artwork(self.root, self.client, season, selected['path'])
                    source = selected['url']
                    self.state[season['Id']] = {'status': 'applied', 'parent_tmdb': tmdb, 'season': number, 'provider_id': str(mapped['tmdb']), 'provider_season': mapped['season'], 'source_url': url, 'image_url': source, 'sha256': digest, 'proof': 'exact-season-main-poster', 'updated_at': time.time()}
                    self.state[season['Id']] = self.upgrade_quality(parent, season, self.state[season['Id']])
                except (OSError, ValueError, KeyError) as error:
                    self.state[season['Id']] = {'status': 'needs-review', 'error_type': type(error).__name__, 'next_attempt_at': time.time() + 86400}
                write_json(self.root / 'data/season-artwork-state.json', self.state)
                done += 1
                if done >= limit:
                    return self.state
        return self.state

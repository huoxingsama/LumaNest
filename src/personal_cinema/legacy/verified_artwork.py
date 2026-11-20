"""Bind artwork to an exact metadata work, rather than a search result image."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import http.client
import re
import shutil
import tempfile
import time
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from personal_cinema.runtime_paths import RuntimePath as Path
from PIL import Image
from chinese_credits import Document
from library_tasks import read, write, observe
from cache_policy import cache_path
from artwork_quality import fetch_best, remote_images, needs_upgrade, quality_record, inspect, landscape, NoAdequateBackdrop

def source_artwork(content, kind, ident):
    """Only the main work poster on a page whose canonical ID agrees is valid."""
    doc = Document(content)
    category = 'movie' if kind == 'Movie' else 'tv'
    canonical = next((n.attrs.get('href', '') for n in doc.root.find('link') if n.attrs.get('rel') == 'canonical'), '')
    if not re.fullmatch('https://www\\.themoviedb\\.org/' + category + '/' + re.escape(str(ident)) + '(?:-[^/?#]*)?/?', canonical):
        raise ValueError('Artwork page identity mismatch')

    def valid(url):
        return bool(re.fullmatch('https://(?:media\\.themoviedb\\.org|image\\.tmdb\\.org)/t/p/[^/]+/[A-Za-z0-9]+\\.(?:jpg|png|webp)', url))
    primary = next((n.attrs.get('content', '') for n in doc.root.find('meta') if n.attrs.get('property') == 'og:image' and valid(n.attrs.get('content', ''))), None)
    poster = next((n.attrs.get('src', '') for n in doc.root.find('img') if 'poster' in n.attrs.get('class', '').split() and valid(n.attrs.get('src', '')) and primary and (n.attrs['src'].rsplit('/', 1)[-1] == primary.rsplit('/', 1)[-1])), None)
    if not poster:
        raise ValueError('No main work poster')
    images = {'Primary': re.sub('/t/p/[^/]+/', '/t/p/original/', poster)}
    backdrop = next((url for url in re.findall('background-image:\\s*url\\([\\\'"]?(https://[^\\\'"\\s)]+)', content) if valid(url) and '/w1920_and_h800_' in url), None)
    if backdrop:
        images['Backdrop'] = re.sub('/t/p/[^/]+/', '/t/p/original/', backdrop)
    return (canonical, images)

def artwork_target(root, item, kind):
    base = Path(item['Path']).absolute()
    base.relative_to((root / 'catalog').absolute())
    if item['Type'] == 'Series':
        target = base / ('poster.jpg' if kind == 'Primary' else 'fanart.jpg')
    else:
        target = base.with_name(base.stem + ('-poster.jpg' if kind == 'Primary' else '-fanart.jpg'))
    return cache_path(root / 'catalog', target.relative_to((root / 'catalog').absolute()).as_posix())
ART_ERRORS = (OSError, ValueError, KeyError, AssertionError, RuntimeError, http.client.HTTPException, Image.DecompressionBombError)
_PROOF_LOCK = threading.RLock()
_FILE_HASHES = {}
_NATIVE_PROOFS = {}

def file_stamp(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]

def file_sha256(path):
    """Read unchanged image bytes once, rather than once per UI poll."""
    path = Path(path)
    stamp = file_stamp(path)
    key = (str(path.absolute()), *stamp)
    with _PROOF_LOCK:
        if key in _FILE_HASHES:
            return _FILE_HASHES[key]
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            digest.update(chunk)
    if file_stamp(path) != stamp:
        raise ValueError('Artwork changed during verification')
    result = digest.hexdigest()
    with _PROOF_LOCK:
        if len(_FILE_HASHES) > 1024:
            _FILE_HASHES.clear()
        _FILE_HASHES[key] = result
    return result

def _native_file(root, path):
    path = Path(path).absolute()
    resolved = path.resolve()
    if not any((resolved.is_relative_to((Path(root) / part).resolve()) for part in ('catalog', 'data/jellyfin/metadata'))):
        raise ValueError('Native artwork path outside media storage')
    return path

def proof_matches(root, item, record, proof):
    if not proof or not proof.get('verified'):
        return False
    if str((item.get('ProviderIds') or {}).get('Tmdb') or '') != str(record.get('provider_id') or ''):
        return False
    if proof.get('image_tag') != (item.get('ImageTags') or {}).get('Primary') or proof.get('sha256') != record.get('sha256'):
        return False
    try:
        return file_stamp(_native_file(root, proof['path'])) == proof.get('file_stamp')
    except (OSError, KeyError, ValueError):
        return False

def verify_native_poster(root, item, record, lookup):
    """Confirm the current Native primary's real bytes against source SHA.

    Image tags are lookup/cache keys, not image identity. A new tag requires
    one Native path lookup; unchanged files reuse their verified digest.
    """
    ident = str((item.get('ProviderIds') or {}).get('Tmdb') or '')
    tag = (item.get('ImageTags') or {}).get('Primary')
    expected = record.get('sha256')
    if record.get('status') != 'applied' or ident != str(record.get('provider_id') or '') or (not tag) or (not re.fullmatch('[a-f0-9]{64}', str(expected or ''))):
        return None
    stored = record.get('native_verification')
    if proof_matches(root, item, record, stored):
        return stored
    key = (str(Path(root).absolute()), item['Id'], ident, tag, expected)
    with _PROOF_LOCK:
        cached = _NATIVE_PROOFS.get(key)
    if cached:
        try:
            if file_stamp(_native_file(root, cached['path'])) == cached.get('file_stamp'):
                return cached
        except (OSError, KeyError, ValueError):
            pass
    infos = lookup('/Items/' + item['Id'] + '/Images')
    image = next((i for i in infos if i.get('ImageType') == 'Primary'), None)
    if not image or image.get('ImageTag') != tag or (not image.get('Path')):
        return None
    path = _native_file(root, image['Path'])
    stamp = file_stamp(path)
    if stored and stored.get('path') == str(path) and (stored.get('file_stamp') == stamp):
        digest = stored.get('sha256')
    else:
        digest = file_sha256(path)
    proof = {'verified': digest == expected, 'image_tag': tag, 'sha256': digest, 'path': str(path), 'file_stamp': stamp, 'verified_at': time.time()}
    with _PROOF_LOCK:
        if len(_NATIVE_PROOFS) > 1024:
            _NATIVE_PROOFS.clear()
        _NATIVE_PROOFS[key] = proof
    return proof

def _prefetch(root, item, ident, force, remote=(), verified_current=(), proven_sources=None, current_quality=None):
    """Public network only. At most three jobs keep their images on disk."""
    staging = root / 'data/artwork-staging'
    staging.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='work-', dir=staging)
    folder = Path(temporary.name)
    try:
        category = 'movie' if item['Type'] == 'Movie' else 'tv'
        url = f'https://www.themoviedb.org/{category}/{ident}?language=zh-CN'
        page = root / 'data/artwork-source-pages' / (category + '-' + ident + '.html')
        fetched = force or not page.exists()
        if not fetched:
            content = page.read_text('utf-8')
        else:
            with urllib.request.urlopen(url, timeout=20) as response:
                raw = response.read(7 * 1024 * 1024 + 1)
            if len(raw) > 7 * 1024 * 1024:
                raise ValueError('Artwork page exceeds limit')
            content = raw.decode('utf-8')
        canonical, images = source_artwork(content, item['Type'], ident)
        if fetched:
            (folder / 'page.html').write_text(content, 'utf-8')
        files = {}
        hashes = {}
        quality = {}
        kept = set()
        current_quality = current_quality or {}
        language_proofs = read(root / 'data/artwork-language-proofs.json')
        images.setdefault('Backdrop', None)
        for kind, source in list(images.items()):
            current = artwork_target(root, item, kind) if kind in verified_current else None
            if current and (proven_sources or {}).get(kind):
                source = proven_sources[kind]
            if current and (not force) and (not needs_upgrade(current, kind, current_quality.get(kind))):
                selected = {**current_quality.get(kind, {}), **inspect(current, kind), 'path': current, 'sha256': file_sha256(current), 'url': source, 'kept_current': True}
            else:
                try:
                    selected = fetch_best(folder, kind, source, remote, current_path=current, language_proofs=language_proofs, current_quality=current_quality.get(kind))
                except NoAdequateBackdrop:
                    quality[kind] = {'selection': 'no_adequate_source', 'textless': False, 'textless_evidence': ''}
                    images.pop(kind, None)
                    previous = inspect(current, kind) if current else None
                    if not previous or not landscape(previous['width'], previous['height']):
                        continue
                    selected = {**previous, 'path': current, 'sha256': file_sha256(current), 'url': source, 'kept_current': True}
            files[kind] = selected['path']
            hashes[kind] = selected['sha256']
            images[kind] = selected['url']
            quality[kind] = {key: selected.get(key) for key in ('width', 'height', 'adequate', 'language', 'kept_current', 'textless', 'textless_evidence')}
            if selected['kept_current']:
                kept.add(kind)
        return {'temporary': temporary, 'folder': folder, 'page': page, 'page_fetched': fetched, 'canonical': canonical, 'images': images, 'files': files, 'hashes': hashes, 'quality': quality, 'kept': kept}
    except BaseException:
        temporary.cleanup()
        raise

def sync_artwork(root, client, items=None, target_ids=None, force=False, max_items=None, concurrency=3):
    """Prefetch public sources in parallel; native writes remain serialized."""
    from season_artwork import upload_image
    root = Path(root)
    state = read(root / 'data/verified-artwork-state.json')
    changed = []
    attempted = 0
    seen = set()
    quality_attempts = set()
    if items is None:
        items = client.json('/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=Path,ProviderIds')['Items']
    candidates = iter(items)
    pending = {}
    exhausted = False
    limit = max(1, min(3, int(concurrency)))

    def failed(brief, ident, error):
        prior = state.get(brief['Id'], {})
        if brief['Id'] in quality_attempts and prior.get('status') == 'applied' and (str(prior.get('provider_id')) == ident):
            state[brief['Id']] = {**prior, 'quality_error': type(error).__name__, 'quality_retry_at': time.time() + 3600}
        else:
            state[brief['Id']] = {'status': 'needs-review', 'provider_id': ident, 'name': brief['Name'], 'error_type': type(error).__name__, 'updated_at': time.time()}
        write(root / 'data/verified-artwork-state.json', state)

    def locked(item, ident):
        if not (item.get('IsLocked') or 'Images' in (item.get('LockedFields') or [])):
            return False
        state[item['Id']] = {'status': 'manual-lock', 'provider_id': ident, 'updated_at': time.time()}
        write(root / 'data/verified-artwork-state.json', state)
        return True
    with ThreadPoolExecutor(max_workers=limit) as pool:
        try:
            while pending or not exhausted:
                while len(pending) < limit and (not exhausted):
                    if max_items is not None and attempted >= max_items:
                        exhausted = True
                        break
                    try:
                        brief = next(candidates)
                    except StopIteration:
                        exhausted = True
                        break
                    if brief['Id'] in seen or (target_ids is not None and brief['Id'] not in target_ids):
                        continue
                    seen.add(brief['Id'])
                    ident = str((brief.get('ProviderIds') or {}).get('Tmdb', ''))
                    if not ident.isdecimal() or brief.get('Type') not in ('Movie', 'Series') or (not brief.get('Path')):
                        continue
                    prior = state.get(brief['Id'], {})
                    try:
                        target = artwork_target(root, brief, 'Primary')
                    except ValueError:
                        continue
                    verified_current = False
                    if prior.get('status') == 'applied' and str(prior.get('provider_id')) == ident:
                        try:
                            proof = verify_native_poster(root, brief, prior, client.json)
                            local_ok = target.is_file() and file_sha256(target) == prior.get('sha256')
                            if proof_matches(root, brief, prior, proof):
                                if not local_ok:
                                    target.parent.mkdir(parents=True, exist_ok=True)
                                    restored = target.with_name(target.name + '.verified-restore.tmp')
                                    shutil.copy2(proof['path'], restored)
                                    restored.replace(target)
                                backdrop = artwork_target(root, brief, 'Backdrop')
                                backdrop_sha = (prior.get('image_hashes') or {}).get('Backdrop')
                                backdrop_tag = next(iter(brief.get('BackdropImageTags') or []), None)
                                if backdrop_sha and backdrop.is_file() and (file_sha256(backdrop) == backdrop_sha) and (not prior.get('backdrop_image_tag') or prior['backdrop_image_tag'] != backdrop_tag):
                                    current = client.json('/Items/' + brief['Id'])
                                    if locked(current, ident):
                                        continue
                                    if str((current.get('ProviderIds') or {}).get('Tmdb', '')) != ident:
                                        raise ValueError('Artwork identity changed')
                                    upload_image(client, brief['Id'], backdrop, 'Backdrop')
                                    current = client.json('/Items/' + brief['Id'])
                                    prior = {**prior, 'backdrop_image_tag': next(iter(current.get('BackdropImageTags') or []), None)}
                                prior = {**prior, 'image_tag': brief.get('ImageTags', {}).get('Primary'), 'native_verification': proof, 'verified_at': time.time()}
                                state[brief['Id']] = prior
                                write(root / 'data/verified-artwork-state.json', state)
                                verified_current = True
                                kinds = set(prior.get('images', {})) | {'Primary', 'Backdrop'}
                                if not force and (prior.get('quality_retry_at', 0) > time.time() or not any((needs_upgrade(artwork_target(root, brief, kind), kind, (prior.get('quality') or {}).get(kind)) for kind in kinds))):
                                    continue
                            if local_ok and (not verified_current):
                                item = client.json('/Items/' + brief['Id'])
                                if locked(item, ident):
                                    continue
                                if str((item.get('ProviderIds') or {}).get('Tmdb') or '') != ident:
                                    raise ValueError('Artwork identity changed')
                                upload_image(client, item['Id'], target, 'Primary')
                                refreshed = client.json('/Items/' + item['Id'])
                                proof = verify_native_poster(root, refreshed, prior, client.json)
                                if proof_matches(root, refreshed, prior, proof):
                                    state[item['Id']] = {**prior, 'image_tag': refreshed.get('ImageTags', {}).get('Primary'), 'native_verification': proof, 'verified_at': time.time()}
                                    write(root / 'data/verified-artwork-state.json', state)
                                    verified_current = True
                                    if not force and (not any((needs_upgrade(artwork_target(root, brief, kind), kind, (prior.get('quality') or {}).get(kind)) for kind in ('Primary', 'Backdrop')))):
                                        continue
                        except ART_ERRORS:
                            pass
                    attempted += 1
                    observe(root, 'metadata', 'poster', brief['Name'], item_id=brief['Id'], count=attempted)
                    try:
                        item = client.json('/Items/' + brief['Id'])
                        if str((item.get('ProviderIds') or {}).get('Tmdb', '')) != ident or item.get('Type') != brief['Type']:
                            raise ValueError('Artwork identity changed')
                        if locked(item, ident):
                            continue
                        artwork_target(root, item, 'Primary')
                        remote = remote_images(client, item['Id'])
                        proven_kinds = set()
                        if verified_current:
                            quality_attempts.add(item['Id'])
                            proven_kinds.add('Primary')
                            backdrop = artwork_target(root, item, 'Backdrop')
                            if backdrop.is_file() and (prior.get('image_hashes') or {}).get('Backdrop') == file_sha256(backdrop):
                                proven_kinds.add('Backdrop')
                        pending[pool.submit(_prefetch, root, item, ident, force, remote, proven_kinds, prior.get('images'), prior.get('quality'))] = (brief, ident)
                    except ART_ERRORS as error:
                        failed(brief, ident, error)
                if not pending:
                    continue
                completed, _ = wait(pending, timeout=5, return_when=FIRST_COMPLETED)
                if not completed:
                    brief, _ = next(iter(pending.values()))
                    observe(root, 'metadata', 'poster', brief['Name'], item_id=brief['Id'], count=attempted)
                for future in completed:
                    brief, ident = pending.pop(future)
                    prepared = None
                    try:
                        prepared = future.result()
                        item = client.json('/Items/' + brief['Id'])
                        if str((item.get('ProviderIds') or {}).get('Tmdb', '')) != ident or item.get('Type') != brief['Type']:
                            raise ValueError('Artwork identity changed')
                        if locked(item, ident):
                            continue
                        backup = root / 'data/artwork-identity-backups' / item['Id']
                        backup.mkdir(parents=True, exist_ok=True)
                        for kind, temporary in prepared['files'].items():
                            destination = artwork_target(root, item, kind)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            if kind in prepared['kept']:
                                continue
                            if destination.exists() and (not (backup / destination.name).exists()):
                                shutil.copy2(destination, backup / destination.name)
                            temporary.replace(destination)
                            upload_image(client, item['Id'], destination, kind)
                            if kind == 'Primary' and item['Type'] == 'Movie':
                                for alias in (destination.parent / 'poster.jpg', destination.parent / 'folder.jpg'):
                                    if alias.exists():
                                        if not (backup / alias.name).exists():
                                            shutil.copy2(alias, backup / alias.name)
                                        shutil.copy2(destination, alias)
                            if kind == 'Backdrop':
                                upload_image(client, item['Id'], destination, 'Thumb')
                        if prepared['page_fetched']:
                            prepared['page'].parent.mkdir(parents=True, exist_ok=True)
                            (prepared['folder'] / 'page.html').replace(prepared['page'])
                        refreshed = client.json('/Items/' + item['Id'])
                        quality = {kind: quality_record(artwork_target(root, item, kind), kind, language=value.get('language'), source_url=prepared['images'].get(kind), kept_current=value.get('kept_current', False), textless=value.get('textless', False), textless_evidence=value.get('textless_evidence', '')) for kind, value in prepared['quality'].items()}
                        record = {'status': 'applied', 'provider_id': ident, 'source_url': prepared['canonical'], 'images': prepared['images'], 'image_hashes': prepared['hashes'], 'sha256': prepared['hashes']['Primary'], 'image_tag': refreshed.get('ImageTags', {}).get('Primary'), 'backdrop_image_tag': next(iter(refreshed.get('BackdropImageTags') or []), None), 'quality': quality, 'updated_at': time.time()}
                        proof = verify_native_poster(root, refreshed, record, client.json)
                        if proof:
                            record['native_verification'] = proof
                        state[item['Id']] = record
                        changed.append(item['Id'])
                        write(root / 'data/verified-artwork-state.json', state)
                    except ART_ERRORS as error:
                        failed(brief, ident, error)
                    finally:
                        if prepared:
                            prepared['temporary'].cleanup()
        finally:
            for future in pending:
                try:
                    prepared = future.result()
                except BaseException:
                    continue
                prepared['temporary'].cleanup()
    observe(root, 'metadata', 'poster', '本批海报校验完成', 'idle', count=attempted)
    return {'attempted': attempted, 'updated': changed, 'pending': [key for key, value in state.items() if value.get('status') not in ('applied', 'manual-lock')]}

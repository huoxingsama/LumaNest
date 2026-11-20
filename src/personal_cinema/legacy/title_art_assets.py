"""Fetch exact-work TMDb transparent logos through Jellyfin's image provider.

No API key is copied or exposed. Call after a work identity is established;
existing logos are preserved unless an explicit replacement is requested.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import io
import re
import time
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from PIL import Image, ImageChops
from library_tasks import read, write
from season_artwork import upload_image
from cache_policy import cache_path
from library_routing import catalog_relative
MAX_BYTES = 8 * 1024 * 1024
URL_PATTERN = re.compile('https://(?:image\\.tmdb\\.org|media\\.themoviedb\\.org)/t/p/original/[A-Za-z0-9]+\\.png')

class NoLogoAvailable(ValueError):
    """The exact work has no suitable provider logo; its text title remains."""

def check_identity(item, expected_tmdb):
    ident = str((item.get('ProviderIds') or {}).get('Tmdb') or '')
    if item.get('Type') not in ('Movie', 'Series') or not ident.isdecimal() or ident != str(expected_tmdb):
        raise ValueError('Logo work identity mismatch')
    return (item['Type'], ident, item.get('ProductionYear'))

def logo_candidates(images):
    return [value for value in images if value.get('Type') == 'Logo' and value.get('ProviderName') == 'TheMovieDb' and URL_PATTERN.fullmatch(value.get('Url', ''))]

def choose_logo(images):

    def score(value):
        language = value.get('Language') or ''
        priority = 0 if language.startswith('zh') else 1 if language == '' else 2 if language == 'en' else 3
        width = int(value.get('Width') or 0)
        return (priority, -int(width >= 1200), -float(value.get('CommunityRating') or 0), -int(value.get('VoteCount') or 0), -width)
    values = sorted(logo_candidates(images), key=score)
    return values[0] if values else None

def inspect_logo(data):
    if len(data) > MAX_BYTES:
        raise ValueError('Logo exceeds size limit')
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'PNG' or image.width < 200 or image.height < 100 or (image.width * image.height > 12000000):
            raise ValueError('Invalid logo image dimensions or format')
        image.load()
        alpha = image.convert('RGBA').getchannel('A')
        histogram = alpha.histogram()
        transparent = sum(histogram[:17]) / (image.width * image.height)
        if transparent < 0.03 or transparent > 0.98 or alpha.getextrema()[1] < 224:
            raise ValueError('Title logo must have real transparency and visible artwork')
        return {'width': image.width, 'height': image.height, 'transparent_fraction': round(transparent, 6), 'sha256': hashlib.sha256(data).hexdigest()}

def sync_logo(root, client, item_id, expected_tmdb, replace=False, preferred_url=None, cached_asset=None):
    root = Path(root)
    item = client.json('/Items/' + item_id)
    identity = check_identity(item, expected_tmdb)
    if (item.get('ImageTags') or {}).get('Logo') and (not replace):
        return {'status': 'preserved', 'item_id': item_id, 'provider_id': str(expected_tmdb)}
    values = client.json('/Items/' + item_id + '/RemoteImages?type=Logo&includeAllLanguages=true&limit=200').get('Images', [])
    candidates = logo_candidates(values)
    choice = next((value for value in candidates if value['Url'] == preferred_url), None) if preferred_url else choose_logo(candidates)
    if choice is None:
        raise NoLogoAvailable('No verified exact-work logo candidate')
    folder = root / 'data/title-art-assets' / (identity[0].lower() + '-' + identity[1])
    folder.mkdir(parents=True, exist_ok=True)
    asset = folder / choice['Url'].rsplit('/', 1)[-1]
    if cached_asset is not None:
        source = Path(cached_asset)
        if source.name != asset.name:
            raise ValueError('Cached logo does not match its verified source URL')
        data = source.read_bytes()
    elif asset.exists():
        data = asset.read_bytes()
    else:
        with urllib.request.urlopen(choice['Url'], timeout=20) as response:
            data = response.read(MAX_BYTES + 1)
    proof = inspect_logo(data)
    if not asset.exists():
        asset.write_bytes(data)
    if check_identity(client.json('/Items/' + item_id), expected_tmdb) != identity:
        raise ValueError('Work changed before logo installation')
    if (item.get('ImageTags') or {}).get('Logo'):
        backup = folder / ('previous-' + item_id + '.png')
        if not backup.exists():
            backup.write_bytes(client.image('/Items/' + item_id + '/Images/Logo?format=png'))
    upload_image(client, item_id, asset, 'Logo')
    refreshed = client.json('/Items/' + item_id)
    if check_identity(refreshed, expected_tmdb) != identity or not (refreshed.get('ImageTags') or {}).get('Logo'):
        raise ValueError('Native logo installation did not preserve identity')
    native = client.image('/Items/' + item_id + '/Images/Logo?format=png')
    native_proof = inspect_logo(native)
    with Image.open(io.BytesIO(data)) as original, Image.open(io.BytesIO(native)) as returned:
        if original.size != returned.size or any((high for low, high in ImageChops.difference(original.convert('RGBA'), returned.convert('RGBA')).getextrema())):
            raise ValueError('Native logo pixels differ from the verified source')
    state = read(root / 'data/title-art-state.json')
    record = {'status': 'applied', 'item_id': item_id, 'provider_id': identity[1], 'item_type': identity[0], 'source': 'TMDb', 'source_url': choice['Url'], 'language': choice.get('Language'), 'image_tag': refreshed['ImageTags']['Logo'], 'asset_path': str(asset), **proof, 'native_sha256': native_proof['sha256'], 'updated_at': time.time()}
    state[item_id] = record
    write(root / 'data/title-art-state.json', state)
    return record

def _path_key(path):
    return str(Path(path).absolute()).replace('\\', '/').rstrip('/').casefold()

def confirmed_batch_work_keys(root, entries, items, metadata_state):
    """Bind only this batch's files to identities confirmed by the matcher.

    Return work keys, not Native IDs: canonical moves may recreate a Native
    item after this point. Historical metadata targets are deliberately absent.
    """
    root = Path(root)
    catalog = _path_key(root / 'catalog')
    confirmed = []
    for item in items:
        record = metadata_state.get(item.get('Id'), {})
        ident = str((item.get('ProviderIds') or {}).get('Tmdb') or '')
        if item.get('Type') not in ('Movie', 'Series') or not ident.isdecimal() or (not item.get('Path')):
            continue
        if record.get('status') not in ('matched', 'already_matched', 'locked'):
            continue
        if str((record.get('provider_ids') or {}).get('Tmdb') or '') != ident:
            continue
        path = _path_key(item['Path'])
        if not path.startswith(catalog + '/'):
            continue
        confirmed.append((item, path, (item['Type'], ident)))
    keys = set()
    for entry in entries:
        if entry.get('source_missing') or entry.get('catalog_hidden') or entry.get('identity_needs_review'):
            continue
        try:
            relative = Path(catalog_relative(entry)).with_suffix('.strm').as_posix()
            path = _path_key(cache_path(root / 'catalog', relative))
        except (OSError, ValueError, KeyError):
            continue
        if entry.get('kind') == 'Movies':
            matches = [key for item, known, key in confirmed if item['Type'] == 'Movie' and known == path]
            if not matches:
                parent = path.rsplit('/', 1)[0]
                matches = [key for item, known, key in confirmed if item['Type'] == 'Movie' and known.rsplit('/', 1)[0] == parent]
        elif entry.get('kind') == 'Shows':
            matches = [(len(known), key) for item, known, key in confirmed if item['Type'] == 'Series' and path.startswith(known + '/')]
            longest = max((length for length, key in matches), default=0)
            matches = [key for length, key in matches if length == longest]
        else:
            continue
        if len(set(matches)) == 1:
            keys.add(matches[0])
    return keys

def sync_missing_batch_logos(root, client, items, work_keys):
    """One optional-art pass over an explicit batch; never lists/scans a library."""
    root = Path(root)
    results = []
    for item in items:
        ident = str((item.get('ProviderIds') or {}).get('Tmdb') or '')
        if (item.get('Type'), ident) not in work_keys or (item.get('ImageTags') or {}).get('Logo'):
            continue
        record = {'item_id': item['Id'], 'title': item.get('Name', '')}
        try:
            result = sync_logo(root, client, item['Id'], ident)
            record['status'] = result['status']
        except (OSError, ValueError, KeyError, Image.DecompressionBombError) as error:
            record.update(status='unavailable' if isinstance(error, NoLogoAvailable) else 'retry' if isinstance(error, OSError) else 'needs-review', error_type=type(error).__name__)
            state = read(root / 'data/title-art-state.json')
            state[item['Id']] = {**record, 'provider_id': ident, 'item_type': item['Type'], 'source': 'TMDb', 'updated_at': time.time()}
            write(root / 'data/title-art-state.json', state)
        results.append(record)
    return results

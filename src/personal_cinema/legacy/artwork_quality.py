"""Bounded, exact-provider artwork upgrades; never upscale or switch to English.

Callers establish the work/season identity before supplying RemoteImages. This
module only selects trusted TMDb original URLs and verifies downloaded pixels.
"""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import re
import time
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from personal_cinema.runtime_paths import option
from PIL import Image, ImageStat
VERSION = 1
BACKDROP_POLICY_VERSION = 2
MAX_BYTES = 24 * 1024 * 1024
MAX_PIXELS = 40000000
RECHECK_SECONDS = 7 * 86400
COMPOSITION_POLICY_VERSION = 1
SOURCE = re.compile('https://(?:image\\.tmdb\\.org|media\\.themoviedb\\.org)/t/p/[^/]+/([A-Za-z0-9]+\\.(?:jpg|png|webp))')

class NoAdequateBackdrop(ValueError):
    """The exact work has no proven textless HD landscape candidate."""

def landscape(width, height):
    return bool(height and 1.45 <= width / height <= 2.4)

def original_url(url):
    match = SOURCE.fullmatch(str(url or ''))
    return 'https://image.tmdb.org/t/p/original/' + match[1] if match else None

def minimum_width(kind):
    return int(option('artwork.minimum_width', 600) if kind == 'Primary' else option('artwork.backdrop_width', 1920))

def inspect(path, kind):
    with Image.open(path) as image:
        width, height = image.size
        if width < 200 or height < 100 or width * height > MAX_PIXELS or (kind == 'Primary' and height < width):
            raise ValueError('Invalid artwork dimensions')
        image.verify()
    minimum = (minimum_width(kind), 800 if kind == 'Primary' else 900)
    return {'width': width, 'height': height, 'adequate': width >= minimum[0] and height >= minimum[1] and (kind != 'Backdrop' or landscape(width, height))}

def backdrop_composition(path):
    """Detect asymmetric copy space, not faces or a semantic subject.

    A tiny preview is enough to penalize promotional backgrounds whose one
    side is almost a flat colour. Balanced studio portraits are not penalized
    merely for having space on both sides. This runs during source selection,
    never during rendering or page navigation.
    """
    with Image.open(path) as source:
        image = source.convert('RGB').resize((128, 72), Image.Resampling.BILINEAR)
    left = ImageStat.Stat(image.crop((0, 6, 46, 62))).stddev
    right = ImageStat.Stat(image.crop((82, 6, 128, 62))).stddev
    spread = [sum(left) / 3, sum(right) / 3]
    low, high = sorted(spread)
    one_sided = low < 18 and high > 32 and (high > max(1, low) * 2.8)
    return {'composition_version': COMPOSITION_POLICY_VERSION, 'composition': 'one_sided_negative_space' if one_sided else 'balanced_or_unclassified', 'negative_space_side': ('left' if spread[0] < spread[1] else 'right') if one_sided else None, 'side_spread': [round(value, 2) for value in spread]}

def needs_upgrade(path, kind, record=None, now=None):
    path = Path(path)
    record = record or {}
    now = time.time() if now is None else now
    policy_changed = record.get('minimum_width') != minimum_width(kind)
    if kind == 'Backdrop':
        stamp = [path.stat().st_size, path.stat().st_mtime_ns] if path.is_file() else None
        reviewed = not policy_changed and record.get('policy_version') == BACKDROP_POLICY_VERSION and record.get('file_stamp') == stamp
        if reviewed and record.get('selection') == 'no_adequate_source' and (now < record.get('retry_at', 0)):
            return False
        if not reviewed:
            return True
    if not path.is_file():
        return True
    try:
        quality = inspect(path, kind)
    except (OSError, ValueError, Image.DecompressionBombError):
        return True
    if quality['adequate'] and (kind != 'Backdrop' or (record.get('textless') and record.get('textless_evidence'))):
        return False
    record = record or {}
    stamp = [path.stat().st_size, path.stat().st_mtime_ns]
    return policy_changed or not (record.get('version') == VERSION and record.get('file_stamp') == stamp and (now - record.get('checked_at', 0) < RECHECK_SECONDS))

def quality_record(path, kind, **extra):
    path = Path(path)
    now = time.time()
    quality = inspect(path, kind) if path.is_file() else {'width': 0, 'height': 0, 'adequate': False}
    value = {**quality, 'version': VERSION, 'checked_at': now, 'file_stamp': [path.stat().st_size, path.stat().st_mtime_ns] if path.is_file() else None, **extra, 'minimum_width': minimum_width(kind)}
    if kind == 'Backdrop':
        if path.is_file():
            value.update(backdrop_composition(path))
        ready = quality['adequate'] and extra.get('textless') and extra.get('textless_evidence')
        value.update(policy_version=BACKDROP_POLICY_VERSION, selection='textless_landscape' if ready else 'no_adequate_source', retry_at=0 if ready else now + RECHECK_SECONDS)
    return value

def _language(value):
    language = str(value or '').lower().replace('_', '-')
    if language in ('zh', 'zh-cn', 'zh-hans'):
        return 0
    if language.startswith('zh'):
        return 1
    return 2 if not language else 3

def candidates(images, kind, fallback, preserve_language=False, language_proofs=None):
    """Alternative posters must be Chinese; neutral backdrops are also allowed.

    A proven main image remains eligible in its original language. Provider
    popularity never outweighs resolution within the same language group.
    """
    fallback = original_url(fallback)
    values = {}
    language_proofs = language_proofs or {}
    width_floor = minimum_width(kind)
    current_language = str(language_proofs.get(fallback, {}).get('language') or '').lower()
    for image in images:
        source = original_url(image.get('Url'))
        if image.get('Type') != kind or image.get('ProviderName') != 'TheMovieDb' or (not source):
            continue
        proof = language_proofs.get(source, {})
        language = proof['language'] if 'language' in proof else image.get('Language')
        neutral = not str(language or '').strip()
        evidence = ('reviewed_language' if 'language' in proof else 'provider_language' if 'Language' in image else '') if neutral else ''
        width = int(image.get('Width') or 0)
        height = int(image.get('Height') or 0)
        if kind == 'Backdrop':
            if not evidence or (width and height and (not landscape(width, height) or width < width_floor or height < 900)):
                continue
        if source != fallback and (not (_language(language) < 2 or (kind == 'Backdrop' and (not language)))):
            continue
        if kind == 'Primary' and preserve_language and (source != fallback):
            explicit = str(language).lower().replace('_', '-')
            if explicit not in ('zh-cn', 'zh-hans') and (not (current_language in ('zh-hant', 'zh-tw', 'zh-hk') and explicit in ('zh-hant', 'zh-tw', 'zh-hk'))):
                continue
        values[source] = {'url': source, 'language': language, 'width': width, 'height': height, 'textless': bool(evidence), 'textless_evidence': evidence}
    if fallback and fallback not in values and (kind != 'Backdrop' or ('language' in language_proofs.get(fallback, {}) and (not language_proofs[fallback]['language']))):
        values[fallback] = {'url': fallback, 'language': language_proofs.get(fallback, {}).get('language'), 'width': 0, 'height': 0}
        if kind == 'Backdrop':
            values[fallback].update(textless=True, textless_evidence='reviewed_language')

    def rank(value):
        if kind == 'Backdrop':
            width, height = (value['width'], value['height'])
            return (not (width >= width_floor and height >= 900 and landscape(width, height)), abs(width / height - 16 / 9) if height else 10, -width * height)
        return (_language(value['language']), -value['width'] * value['height'], -value['width'])
    return sorted(values.values(), key=rank)

def remote_images(client, item_id):
    """Called on the native API thread; unavailable providers use proven main art."""
    try:
        call = client.json if hasattr(client, 'json') else lambda path: client.api('jellyfin', path)
        result = call('/Items/' + item_id + '/RemoteImages?includeAllLanguages=true&limit=200')
        return result.get('Images', []) if isinstance(result, dict) else []
    except (OSError, ValueError, KeyError, RuntimeError):
        return []

def fetch_best(folder, kind, fallback, images=(), current_path=None, language_proofs=None, current_quality=None):
    """Fetch at most four originals; misleading advertised sizes do not win.

    Keep a verified current image when every candidate is smaller. A source
    with no HD original is retained honestly, not enlarged to fake HD.
    """
    folder = Path(folder)
    choices = candidates(images, kind, fallback, preserve_language=bool(current_path), language_proofs=language_proofs)
    if not choices and kind != 'Backdrop':
        raise ValueError('No trusted original artwork source')
    current = None
    if current_path and Path(current_path).is_file():
        try:
            current = inspect(current_path, kind)
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
    current_choice = next((v for v in choices if v['url'] == original_url(fallback)), None)
    current_evidence = current_choice or current_quality or {}
    current_good = bool(current and current['adequate'] and current_evidence.get('textless') and current_evidence.get('textless_evidence'))

    def backdrop_rank(value):
        return (value.get('composition') != 'one_sided_negative_space', -abs(value['width'] / value['height'] - 16 / 9), value['width'] * value['height'])
    best = None
    errors = []
    if kind == 'Backdrop' and current_good:
        best = {**current, **{key: current_evidence.get(key) for key in ('language', 'textless', 'textless_evidence')}, **backdrop_composition(current_path), 'url': original_url(fallback), 'path': Path(current_path), 'sha256': hashlib.sha256(Path(current_path).read_bytes()).hexdigest(), 'kept_current': True}
    for index, choice in enumerate(choices[:4]):
        if kind == 'Backdrop' and current_good and (choice['url'] == original_url(fallback)):
            continue
        path = folder / (kind + '-' + str(index) + '.image')
        digest = hashlib.sha256()
        size = 0
        try:
            with urllib.request.urlopen(choice['url'], timeout=25) as response, path.open('wb') as output:
                while True:
                    data = response.read(65536)
                    if not data:
                        break
                    size += len(data)
                    if size > MAX_BYTES:
                        raise ValueError('Artwork exceeds image limit')
                    output.write(data)
                    digest.update(data)
            quality = inspect(path, kind)
            known = (language_proofs or {}).get(choice['url'], {})
            if known.get('sha256') and known['sha256'] != digest.hexdigest():
                raise ValueError('Reviewed artwork pixels changed')
            if kind == 'Backdrop' and (not quality['adequate']):
                continue
            if kind != 'Backdrop' and current and (quality['width'] < current['width'] or quality['height'] < current['height']):
                continue
            value = {**choice, **quality, 'path': path, 'sha256': digest.hexdigest()}
            if kind == 'Backdrop':
                value.update(backdrop_composition(path))
                if best is None or backdrop_rank(value) > backdrop_rank(best):
                    best = value
            elif best is None or (value['adequate'], -_language(value['language']), value['width'] * value['height']) > (best['adequate'], -_language(best['language']), best['width'] * best['height']):
                best = value
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            errors.append(type(error).__name__)
    if best:
        return {**best, 'kept_current': best.get('kept_current', False)}
    if kind == 'Backdrop':
        if errors:
            raise OSError('Backdrop sources unavailable: ' + ','.join(errors))
        raise NoAdequateBackdrop('No proven textless HD landscape for this work')
    if current:
        return {**current, 'path': Path(current_path), 'url': original_url(fallback), 'language': None, 'sha256': hashlib.sha256(Path(current_path).read_bytes()).hexdigest(), 'kept_current': True, 'errors': errors}
    raise ValueError('No valid original artwork: ' + ','.join(errors))

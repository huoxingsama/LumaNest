"""Resolve Chinese encyclopedia pages through a verified provider identity."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import json
import re
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from chinese_credits import Document
from library_tasks import read, write

def provider_links(content, kind, ident):
    category = 'movie' if kind == 'Movie' else 'tv'
    doc = Document(content)
    canonical = next((n.attrs.get('href', '') for n in doc.root.find('link') if n.attrs.get('rel') == 'canonical'), '')
    match = 'https://www\\.themoviedb\\.org/' + category + '/' + re.escape(str(ident)) + '(?:-[^/?#]*)?/?'
    if not re.fullmatch(match, canonical):
        raise ValueError('Provider page identity mismatch')
    for raw in re.findall('<script\\b[^>]*type=[\\\'"]application/ld\\+json[\\\'"][^>]*>(.*?)</script>', content, re.S | re.I):
        try:
            metadata = json.loads(raw)
        except ValueError:
            continue
        records = metadata if isinstance(metadata, list) else [metadata]
        for record in records:
            if not isinstance(record, dict) or not re.fullmatch(match, record.get('url', '')):
                continue
            links = record.get('sameAs', [])
            if isinstance(links, str):
                links = [links]
            result = {'source_url': canonical}
            for url in links:
                imdb = re.fullmatch('https://(?:www\\.)?imdb\\.com/title/(tt\\d+)/?', url)
                wikidata = re.fullmatch('https://www\\.wikidata\\.org/wiki/(Q\\d+)', url)
                if imdb:
                    result['imdb'] = imdb[1]
                if wikidata:
                    result['wikidata'] = wikidata[1]
            return result
    return {'source_url': canonical}

def wikidata_chinese(payload, qid, imdb=None, tmdb=None):
    entity = payload.get('entities', {}).get(qid, {})
    if entity.get('id') not in (None, qid):
        raise ValueError('Wikidata entity mismatch')
    claims = entity.get('claims', {})
    for property_id, wanted in [('P345', imdb), ('P4947', tmdb)]:
        values = [v.get('mainsnak', {}).get('datavalue', {}).get('value') for v in claims.get(property_id, [])]
        if wanted and values and (str(wanted) not in [str(v) for v in values]):
            raise ValueError('Linked external identity mismatch')
    title = entity.get('sitelinks', {}).get('zhwiki', {}).get('title')
    names = []
    for language, value in entity.get('labels', {}).items():
        if language.startswith('zh') and value.get('value'):
            names.append(value['value'])
    for language, values in entity.get('aliases', {}).items():
        if language.startswith('zh'):
            names.extend((v['value'] for v in values if v.get('value')))
    return {'wiki_title': title, 'names': list(dict.fromkeys(names + [title] if title else names))}

def enrich_source_identity(item, config):
    """Use exact TMDb sameAs and Wikidata's Chinese sitelink, never a guess."""
    ident = str(item.get('ProviderIds', {}).get('Tmdb', ''))
    if not ident.isdecimal():
        return (item, None)
    root = Path(config.get('library_root', _cinema_path('')))
    cache_dir = Path(config.get('source_identity_cache_directory', root / 'data/chinese-identity-cache'))
    cache = cache_dir / (item['Type'] + '-' + ident + '.json')
    identity = read(cache)
    try:
        if not identity.get('wiki_title'):
            category = 'movie' if item['Type'] == 'Movie' else 'tv'
            page = root / 'data/artwork-source-pages' / (category + '-' + ident + '.html')
            if page.exists():
                content = page.read_text('utf-8')
            else:
                with urllib.request.urlopen('https://www.themoviedb.org/' + category + '/' + ident + '?language=zh-CN', timeout=15) as response:
                    content = response.read(7 * 1024 * 1024).decode('utf-8')
            identity = provider_links(content, item['Type'], ident)
            qid = identity.get('wikidata')
            if qid:
                url = 'https://www.wikidata.org/wiki/Special:EntityData/' + qid + '.json'
                request = urllib.request.Request(url, headers={'User-Agent': 'PersonalMediaLibrary/1.0'})
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read(8 * 1024 * 1024))
                identity.update(wikidata_chinese(payload, qid, identity.get('imdb'), ident))
            if identity.get('wiki_title'):
                write(cache, identity)
        providers = {**item.get('ProviderIds', {})}
        if identity.get('imdb'):
            providers['Imdb'] = identity['imdb']
        item = {**item, 'ProviderIds': providers, 'RecognitionAliases': list(dict.fromkeys(item.get('RecognitionAliases', []) + identity.get('names', [])))}
        title = identity.get('wiki_title')
        return (item, 'https://zh.wikipedia.org/wiki/' + urllib.parse.quote(title) if title else None)
    except (OSError, ValueError, KeyError, TypeError):
        return (item, None)

"""Display grouping is independent of a metadata provider's series numbering."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from metadata_matcher import read_json

def literal_sidecars(path):
    return [p for p in path.parent.iterdir() if p.is_file() and p.name.startswith(path.stem) and p.name[len(path.stem):].startswith(('.', '-'))]

def season_source(root, parent, number):
    tmdb = (parent.get('ProviderIds') or {}).get('Tmdb')
    settings = read_json(root / 'data/series-view-map.json', {})
    source = settings.get(str(tmdb), {}).get('seasons', {}).get(str(number), {})
    return source or {'tmdb': tmdb, 'season': number}

def episode_source(root, parent, item):
    number = item.get('IndexNumber')
    source = season_source(root, parent, item.get('ParentIndexNumber'))
    if number is None:
        return (None, None, None)
    mapped = source.get('episodes', {}).get(str(number), number - source.get('episode_offset', 0))
    return (str(source['tmdb']) if source.get('tmdb') is not None else None, source.get('season'), mapped)

def own_episode_image(root, parent, item):
    source = season_source(root, parent, item.get('ParentIndexNumber'))
    path = source.get('episode_images', {}).get(str(item.get('IndexNumber')))
    return root / path if path else None

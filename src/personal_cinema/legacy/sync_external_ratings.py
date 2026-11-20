"""One-shot explicit refresh; never starts a polling service."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import argparse
import json
import os
from personal_cinema.runtime_paths import RuntimePath as Path
from external_ratings import confirmed_items, sync_ratings

def load_items(root):
    from metadata_matcher import Jellyfin
    client = Jellyfin(root)
    rows = []
    start = 0
    while True:
        result = client.json('/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=ProviderIds&StartIndex=' + str(start) + '&Limit=200')
        page = result.get('Items', [])
        rows.extend(page)
        start += len(page)
        if not page or start >= result.get('TotalRecordCount', start):
            return rows

def main(argv=None):
    parser = argparse.ArgumentParser(description='手动同步已确认电影/剧集的真实外部评分')
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('MEDIA_LIBRARY_ROOT', _cinema_path(''))))
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--all-confirmed', action='store_true', help='这一次检查现有全部已确认作品')
    scope.add_argument('--key', action='append', help='明确作品键，例如 movie:tmdb:129；可重复')
    scope.add_argument('--batch-file', type=Path, help='含canonical键数组或{batch_keys:[...]}的本地JSON')
    parser.add_argument('--imdb-only', action='store_true')
    parser.add_argument('--force', action='store_true', help='刷新已缓存MDBList评分；仍遵守429退避')
    parser.add_argument('--max-requests', type=int, default=100)
    args = parser.parse_args(argv)
    try:
        items = confirmed_items(args.root, load_items(args.root))
        keys = args.key
        if args.batch_file:
            data = json.loads(args.batch_file.read_text(encoding='utf-8'))
            keys = data.get('batch_keys', []) if isinstance(data, dict) else data
            if not isinstance(keys, list):
                raise ValueError('invalid-batch')
        result = sync_ratings(args.root, items, batch_keys=keys, force=args.force, imdb_only=args.imdb_only, max_requests=args.max_requests)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['state'] == 'complete' else 1
    except Exception as error:
        print(json.dumps({'state': 'error', 'error_type': type(error).__name__}, ensure_ascii=False))
        return 1
if __name__ == '__main__':
    raise SystemExit(main())

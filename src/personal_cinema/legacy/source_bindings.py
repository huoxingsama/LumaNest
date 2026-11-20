"""Stable stream/cache identities across confirmed cloud moves and renames."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write

class Bindings:

    def __init__(self, root):
        self.root = root
        self.stamp = None
        self.rows = {}
        self.by_path = {}

    def refresh(self):
        path = self.root / 'data/source-bindings.json'
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return
        if stamp == self.stamp:
            return
        rows = read(path)
        self.rows = rows
        self.stamp = stamp
        self.by_path = {}
        for ident, r in rows.items():
            if r.get('phase') not in ('bound', 'done'):
                continue
            for p in [r.get('original_remote'), r.get('remote'), *r.get('previous_paths', [])]:
                if p:
                    self.by_path[p] = (ident, r)

    def resolve(self, path, entry_id=None):
        self.refresh()
        row = self.rows.get(entry_id) if entry_id else None
        return row['remote'] if row and row.get('phase') in ('bound', 'done') else path

    def identity(self, path, size):
        self.refresh()
        value = self.by_path.get(path)
        if value and value[1].get('remote') == path and (value[1].get('size') == size):
            return (value[0], value[1].get('fid'))
        return (None, None)

    def hidden(self, ident, path, fid, rules):
        self.refresh()
        row = self.rows.get(ident, {})
        for remote in [path, row.get('original_remote'), *row.get('previous_paths', [])]:
            record = rules.get(remote)
            if not record:
                continue
            if isinstance(record, dict):
                if record.get('replacement') == ident or record.get('canonical_id') == ident:
                    continue
                if record.get('fid') and str(record['fid']) != str(fid):
                    continue
            return True
        return False

    def apply(self, entries):
        self.refresh()
        for ident, r in self.rows.items():
            e = entries.get(ident)
            if not e or r.get('phase') not in ('bound', 'done') or r.get('size') != e.get('size'):
                continue
            e['remote'] = r['remote']
            e['source_fid'] = r.get('fid')
            manifest = self.root / 'data/range-resume' / (ident + '.json')
            if manifest.exists():
                data = read(manifest)
                if data.get('size') == r['size'] and data.get('remote') in [r.get('original_remote'), r['remote'], *r.get('previous_paths', [])]:
                    if data.get('remote') != r['remote'] or data.get('source_fid') != r.get('fid'):
                        data['remote'] = r['remote']
                        data['source_fid'] = r.get('fid')
                        write(manifest, data)

def classify_normalized(path, root):
    from catalog_intelligence import episode_label, season_folder
    from absolute_series import classify_absolute
    absolute = classify_absolute(path, read(root / 'data/absolute-series.json', []))
    if absolute:
        return absolute
    anchors = read(root / 'data/cloud-normalization-rules.json')
    for prefix, r in sorted(anchors.items(), key=lambda pair: -len(pair[0])):
        if not path.startswith(prefix.rstrip('/') + '/'):
            continue
        if r['kind'] == 'Movies':
            relative = f"Movies/{r['series']}/{r['title']}" + Path(path).suffix.lower()
            return {'kind': 'Movies', 'series': r['series'], 'display_title': r['title'], 'relative': relative, 'catalog_relative': relative, 'catalog_rule_applied': True}
        label = episode_label(Path(path).name, True)
        if not label:
            return None
        season = label['season'] if label['season'] is not None else season_folder(path.split('/'))
        if season is None:
            season = 99
        rel = f"Shows/{r['series']}/Season {season:02}/{r['title']} S{season:02}E{label['episode']:04}" + Path(path).suffix.lower()
        return {'kind': 'Shows', 'series': r['series'], 'display_title': r['title'], 'season': season, 'episode': label['episode'], 'relative': rel, 'catalog_relative': r.get('catalog_kind', 'Shows') + '/' + rel[len('Shows/'):], 'identity_needs_review': season == 99, 'is_extra': bool(r.get('is_extra') or season in (0, 90, 91)), 'extra_title': r.get('extra_title')}

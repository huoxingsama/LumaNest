"""Use cloud object IDs to distinguish a move from replacement or deletion."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import logging
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write

class CloudIdentity:

    def __init__(self, worker, source_root):
        self.worker = worker
        self.root = worker.root
        self.source_root = source_root
        from source_policy import selected, SUPPORTED, _config
        source = selected(self.root, source_root)
        self.source_id = source['id']
        self.supports_native_identity = source.get('driver') in SUPPORTED
        self.identity_required = bool(self.supports_native_identity and source.get('allow_write') and
            _config(self.root).get_effective('source:' + self.source_id).get('organizing.enabled'))
        self.rows = read(self.root / 'data/source-bindings.json')
        self.dirty = False
        self.index = {str(r['fid']): ident for ident, r in self.rows.items() if r.get('fid') and
            (r.get('source_id') == self.source_id or (not r.get('source_id') and r.get('remote', '').startswith(source_root + '/')))}
        self.legacy = {e['remote']: e for e in worker.entries.values() if e.get('remote', '').startswith(source_root + '/') and (not e.get('source_retired_at'))}
        self.tree = None
        self.failed = False
        self.directory_rows = {}
        self.unavailable_paths = set()

    def directory(self, path):
        if not self.supports_native_identity:
            return {}
        if self.failed:
            return {}
        try:
            if self.tree is None:
                from cloud_organizer import Tree
                from source_policy import manager_for
                q = manager_for(self.worker, self.source_root)
                self.tree = Tree(q)
            rows = self.tree.rows(path)
            self.unavailable_paths.discard(path)
            self.directory_rows[path] = rows
            return {r['file_name']: r for r in rows if not r.get('dir')}
        except (OSError, ValueError, RuntimeError, KeyError, StopIteration):
            self.unavailable_paths.add(path)
            self.failed = self.identity_required
            if self.failed:
                logging.warning('Stable cloud identity unavailable for %s; preserving existing bindings', self.source_root)
            return {}

    def validate_listing(self, path, listed, objects=None, complete=False, total=None):
        """Never accept absence from a view contradicted by direct cloud IDs.

        Per-page checking precedes cache/catalog updates. The completed directory
        check also covers missing children and child directories in either view.
        """
        if not self.identity_required and (self.tree is None or path in self.unavailable_paths):
            return
        direct = self.directory_rows.get(path)
        if direct is None:
            if objects is None:
                return
            direct = list(objects.values())
        actual = {row['file_name']: row for row in direct}
        if len(actual) != len(direct):
            raise RuntimeError('云盘真实目录存在同名对象，不能确认本轮扫描完整')
        seen = set()
        for row in listed:
            name = row['name']
            obj = actual.get(name)
            if name in seen or not obj or bool(row.get('is_dir')) != bool(obj.get('dir')):
                raise RuntimeError('网盘挂载清单与真实对象不一致，本轮不判断来源删除')
            seen.add(name)
            if not row.get('is_dir') and int(row.get('size', 0)) != int(obj.get('size', 0)):
                raise RuntimeError('网盘挂载文件长度与真实对象不一致，本轮不更新缓存身份')
        if complete and (seen != set(actual) or (total is not None and int(total) != len(listed))):
            raise RuntimeError('网盘挂载目录清单不完整，本轮不判断来源删除')

    def identify(self, remote, size, objects):
        obj = objects.get(Path(remote).name)
        if not obj or obj.get('size') != size:
            return (None, None)
        fid = str(obj['fid'])
        ident = self.index.get(fid)
        if ident and self.rows[ident].get('size') != size:
            ident = None
        old = self.legacy.get(remote)
        if not ident and old and (not old.get('source_fid')) and (old.get('size') == size):
            ident = old['id']
        if not ident:
            ident = hashlib.sha256((self.source_root + '|' + fid + '|' + str(size)).encode()).hexdigest()[:24]
        prior = self.rows.get(ident, {})
        if prior.get('remote') != remote or prior.get('size') != size or prior.get('fid') != fid:
            self.rows[ident] = {**prior, 'source_id': self.source_id, 'fid': fid, 'size': size, 'original_remote': prior.get('original_remote') or remote, 'previous_paths': list(dict.fromkeys([*prior.get('previous_paths', []), *([prior['remote']] if prior.get('remote') else [])])), 'remote': remote, 'phase': 'bound', 'normalized': False}
            self.index[fid] = ident
            self.dirty = True
        return (ident, fid)

    def flush(self):
        if self.dirty:
            write(self.root / 'data/source-bindings.json', self.rows)

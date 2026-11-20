from personal_cinema.runtime_paths import proxy_handler
from personal_cinema.network_security import credential_opener
"""Baidu OAuth management adapter. Never expose credential-bearing request URLs."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import json
import time
import http.client
import urllib.parse
import urllib.error
import urllib.request
from pathlib import PurePosixPath
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read
from source_policy import SourceGuard
from quark_manage import QuarkError, retryable_read_error

class Baidu(SourceGuard):
    def __init__(self, root=Path(_cinema_path('')), entry=None, auth=None):
        if not entry or entry.get('driver') != 'BaiduNetdisk':
            raise QuarkError('请明确选择百度媒体来源，不使用默认云盘目录。')
        self._init_source(root, entry)
        auth = dict(auth or {})
        self.token = auth.get('AccessToken') or auth.get('access_token')
        if not self.token:
            raise QuarkError('百度管理授权尚未配置。')
        self.http = credential_opener(proxy_handler())
        self.paths = {}
        self.last = 0
        native = str(entry.get('native_root') or '')
        if not native:
            mount = str(entry.get('mount_path') or '').rstrip('/')
            if not mount or not self.source_root.startswith(mount + '/'):
                raise QuarkError('请选择挂载点内具体媒体目录。')
            base = str(auth.get('root_folder_path') or auth.get('root_folder') or '/').rstrip('/')
            native = base + self.source_root[len(mount):]
        if (not native.startswith('/') or native == '/' or '..' in native.split('/')
                or any(c in native for c in '\\\x00\r\n')):
            raise QuarkError('百度原生目录必须是具体绝对媒体目录。')
        self.native_root = native.rstrip('/')
        self._bootstrapping = True
        try:
            matches = [r for r in self.list_path(str(PurePosixPath(self.native_root).parent))
                       if r.get('dir') and r.get('path') == self.native_root]
        finally:
            self._bootstrapping = False
        if len(matches) != 1:
            raise QuarkError('未唯一确认百度媒体目录。')
        self.movie_root_fid = str(matches[0]['fid'])
        self._remember(self.movie_root_fid, self.source_root, True)

    def api(self, method, data=None, **params):
        if data is not None:
            self.require_write(cleanup=params.get('opera') == 'delete')
            if method not in ('create', 'filemanager'):
                raise QuarkError('不允许执行未知百度写操作。')
            if method == 'filemanager' and params.get('opera') not in ('move', 'delete'):
                raise QuarkError('不允许执行未知百度文件操作。')
            paths = [data['path']] if method == 'create' else json.loads(data.get('filelist', '[]'))
            for item in paths:
                candidates = [item] if isinstance(item, str) else [item.get('path'), item.get('dest')]
                for index, path in enumerate(candidates):
                    if path != self.native_root and not str(path).startswith(self.native_root + '/'):
                        raise QuarkError('百度写操作超出所选媒体目录。')
                    if index == 0 and path == self.native_root:
                        raise QuarkError('不能移动或回收来源根目录。')
                    if '..' in str(path).split('/') or '\\' in str(path):
                        raise QuarkError('百度写操作包含无效路径。')
                if method == 'filemanager':
                    matches = [fid for fid, path in self.paths.items() if path == candidates[0]]
                    if len(matches) != 1:
                        raise QuarkError('百度源对象身份尚未确认。')
                    self._path(matches[0], allow_root=False)
                    if params.get('opera') == 'delete' and (not self._objects[matches[0]]['dir'] or self.listing(matches[0])):
                        raise QuarkError('只允许回收重新确认空的旧目录。')
        query = {'method': method, 'access_token': self.token, **params}
        request = urllib.request.Request('https://pan.baidu.com/rest/2.0/xpan/file?' + urllib.parse.urlencode(query), data=None if data is None else urllib.parse.urlencode(data).encode(), headers={'User-Agent': 'pan.baidu.com'})
        attempts = 3 if data is None else 1
        for attempt in range(attempts):
            delay = 0.15 - (time.monotonic() - self.last)
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()
            try:
                with self.http.open(request, timeout=15 if data is None else 40) as response:
                    value = json.load(response)
                break
            except (OSError, ValueError, http.client.IncompleteRead) as error:
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                if attempt + 1 < attempts and retryable_read_error(error):
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise QuarkError('百度管理接口暂时不可用（' + type(error).__name__ + '）') from None
        if value.get('errno', 0) != 0:
            code = value.get('errno') if isinstance(value.get('errno'), int) else '未知'
            raise QuarkError('百度操作未成功（错误码 ' + str(code) + '）')
        return value

    def normalize(self, row):
        fid = str(row['fs_id'])
        self.paths[fid] = row['path']
        if row['path'] == self.native_root or row['path'].startswith(self.native_root + '/'):
            self._remember(fid, self.source_root + row['path'][len(self.native_root):], row.get('isdir'), row.get('size', 0))
        return {**row, 'fid': fid, 'file_name': row['server_filename'], 'dir': bool(row['isdir']), 'size': row.get('size', 0)}

    def list_path(self, path):
        result = []
        offset = 0
        while True:
            rows = self.api('list', dir=path, start=offset, limit=1000, order='name').get('list') or []
            result.extend((self.normalize(r) for r in rows))
            if len(rows) < 1000:
                break
            offset += len(rows)
        return result

    def listing(self, fid):
        self._path(fid)
        return self._track_listing(fid, self.list_path(self.paths[str(fid)]))

    def mkdir(self, parent, name):
        self.require_write([parent], allow_root=True)
        name = self._name(name)
        rows = self.listing(parent)
        existing = [r for r in rows if r['file_name'] == name]
        if existing:
            if len(existing) != 1 or not existing[0]['dir']:
                raise QuarkError('百度目标目录冲突')
            return existing[0]['fid']
        path = self.paths[str(parent)].rstrip('/') + '/' + name
        value = self.api('create', {'path': path, 'isdir': 1, 'rtype': 0, 'block_list': '[]'})
        if not value.get('fs_id'):
            raise QuarkError('百度目录建立结果不完整')
        fid = str(value['fs_id'])
        self.paths[fid] = path
        self._remember(fid, self._path(parent) + '/' + name, True)
        return fid

    def relocate(self, batch, dest):
        self.require_write([op['fid'] for op in batch])
        self._path(dest)
        for op in batch:
            self._name(PurePosixPath(op['target']).name)
        files = [{'path': self.paths[str(op['fid'])], 'dest': self.paths[str(dest)], 'newname': PurePosixPath(op['target']).name, 'ondup': 'fail'} for op in batch]
        try:
            value = self.api('filemanager', {'async': 0, 'filelist': json.dumps(files, ensure_ascii=False), 'ondup': 'fail'}, opera='move')
        except QuarkError:
            verified = False
            for delay in (0.5, 1, 2, 3):
                time.sleep(delay)
                actual = {r['fid']: r for r in self.listing(dest)}
                if all((str(op['fid']) in actual and actual[str(op['fid'])]['file_name'] == PurePosixPath(op['target']).name and (actual[str(op['fid'])]['size'] == op['size']) for op in batch)):
                    verified = True
                    break
            if not verified:
                raise
            value = {'info': [{'errno': 0} for _ in batch]}
        info = value.get('info') or []
        if len(info) != len(batch) or any((r.get('errno', 0) != 0 for r in info)):
            raise QuarkError('百度批量移动未全部成功，请按日志恢复')
        for op in batch:
            self.paths[str(op['fid'])] = self.paths[str(dest)].rstrip('/') + '/' + PurePosixPath(op['target']).name
            self._moved(op['fid'], self._path(dest) + '/' + PurePosixPath(op['target']).name)

    def recycle(self, fids):
        self.require_write(fids, cleanup=True)
        for fid in fids:
            if not self._objects[str(fid)]['dir'] or self.listing(fid):
                raise QuarkError('只允许回收重新确认空的旧目录。')
        paths = [self.paths[str(fid)] for fid in fids]
        value = self.api('filemanager', {'async': 0, 'filelist': json.dumps(paths, ensure_ascii=False)}, opera='delete')
        if any((r.get('errno', 0) != 0 for r in value.get('info') or [])):
            raise QuarkError('百度回收任务未全部确认')

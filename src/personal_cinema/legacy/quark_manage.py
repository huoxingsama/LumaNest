from personal_cinema.runtime_paths import proxy_handler
from personal_cinema.network_security import credential_opener
"""Task-scoped Quark web management. Secrets never enter reports or exceptions."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import json
import time
import threading
import http.client
import urllib.error
import urllib.parse
import urllib.request
from personal_cinema.runtime_paths import RuntimePath as Path
from library_tasks import read, write
from source_policy import SourceGuard

class QuarkError(RuntimeError):
    pass

def retryable_read_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 425, 429, 500, 502, 503, 504)
    return isinstance(error, (OSError, http.client.IncompleteRead))

class Quark(SourceGuard):
    def __init__(self, root=Path(_cinema_path('')), entry=None, auth=None):
        if not entry or entry.get('driver') != 'Quark':
            raise QuarkError('请明确选择夸克媒体来源，不使用默认云盘目录。')
        self._init_source(root, entry)
        self.auth = dict(auth or {})
        if not self.auth.get('cookie'):
            raise QuarkError('夸克可写登录尚未完成')
        self.http = credential_opener(proxy_handler())
        self.rate_lock = threading.Lock()
        self.next_request = 0
        selected_fid = str(entry.get('native_root') or '')
        if selected_fid:
            if selected_fid == '0' or not __import__('re').fullmatch(r'[A-Za-z0-9_-]{1,128}', selected_fid):
                raise QuarkError('请选择具体媒体文件夹 ID，不能管理整个云盘。')
        else:
            mount = str(entry.get('mount_path') or '').rstrip('/')
            if not mount or not self.source_root.startswith(mount + '/'):
                raise QuarkError('媒体目录必须位于明确选择的挂载点内。')
            selected_fid = str(self.auth.get('root_folder_id') or '0')
            self._bootstrapping = True
            try:
                for name in self.source_root[len(mount) + 1:].split('/'):
                    matches = [r for r in self.listing(selected_fid) if r.get('dir') and r.get('file_name') == name]
                    if len(matches) != 1:
                        raise QuarkError('未唯一确认所选媒体目录。')
                    selected_fid = str(matches[0]['fid'])
            finally:
                self._bootstrapping = False
        self.movie_root_fid = selected_fid
        self._remember(selected_fid, self.source_root, True)

    def api(self, path, data=None, params=None):
        if data is not None:
            self.require_write(cleanup=path == '/file/delete')
            if path not in ('/file', '/file/rename', '/file/move', '/file/delete'):
                raise QuarkError('不允许执行未知云盘写操作。')
            if data.get('fid'):
                self._path(data['fid'], allow_root=False)
            for fid in data.get('filelist', []):
                self._path(fid, allow_root=False)
                if path == '/file/delete' and (not self._objects[str(fid)]['dir'] or self.listing(fid)):
                    raise QuarkError('只允许回收已重新确认空的旧目录。')
            for key in ('pdir_fid', 'to_pdir_fid'):
                if key in data:
                    self._path(data[key])
        query = {'pr': 'ucpro', 'fr': 'pc', **(params or {})}
        url = 'https://drive-pc.quark.cn/1/clouddrive' + path + '?' + urllib.parse.urlencode(query)
        headers = {'Cookie': self.auth['cookie'], 'User-Agent': self.auth.get('user_agent', 'Mozilla/5.0'), 'Referer': 'https://pan.quark.cn/', 'Origin': 'https://pan.quark.cn', 'Content-Type': 'application/json'}
        attempts = 3 if data is None else 1
        for attempt in range(attempts):
            with self.rate_lock:
                delay = self.next_request - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self.next_request = time.monotonic() + 0.12
            try:
                req = urllib.request.Request(url, headers=headers, data=None if data is None else json.dumps(data).encode())
                with self.http.open(req, timeout=15 if data is None else 30) as response:
                    value = json.load(response)
                break
            except urllib.error.HTTPError as error:
                code = error.code
                retryable = attempt + 1 < attempts and retryable_read_error(error)
                error.close()
                if retryable:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise QuarkError('夸克操作未成功（HTTP ' + str(code) + '）') from None
            except (OSError, ValueError, http.client.IncompleteRead) as error:
                if attempt + 1 < attempts and retryable_read_error(error):
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise QuarkError('夸克管理接口暂时不可用（' + type(error).__name__ + '）') from None
        if value.get('status') != 200 or value.get('code', 0) != 0:
            status = value.get('status') if isinstance(value.get('status'), int) else '未知'
            raise QuarkError('夸克管理操作未确认成功（状态 ' + str(status) + '）')
        return value

    def listing(self, parent):
        if not self._bootstrapping:
            self._path(parent)
        result = []
        page = 1
        while True:
            value = self.api('/file/sort', params={'pdir_fid': parent, '_page': page, '_size': 100, '_fetch_total': 1, '_fetch_sub_dirs': 0, '_sort': 'file_type:asc,file_name:asc'})
            rows = value.get('data', {}).get('list') or []
            result.extend(rows)
            total = value.get('metadata', {}).get('_total', len(result))
            if not rows or len(result) >= total:
                break
            page += 1
        return self._track_listing(parent, result)

    def resolve(self, remote):
        if not remote.startswith(self.source_root + '/'):
            raise QuarkError('操作超出所选媒体目录。')
        parent = self.movie_root_fid
        row = None
        for part in remote[len(self.source_root) + 1:].split('/'):
            matches = [r for r in self.listing(parent) if r.get('file_name') == part]
            if len(matches) != 1:
                raise QuarkError('源文件不存在或有重名，已停止该条操作')
            row = matches[0]
            parent = row['fid']
        return row

    def wait_task(self, value):
        task = (value.get('data') or {}).get('task_id')
        if not task:
            return
        for retry in range(30):
            r = self.api('/task', params={'task_id': task, 'retry_index': retry})
            data = r.get('data') or {}
            if data.get('status') == 2:
                return
            if data.get('status') in (3, 4):
                raise QuarkError('夸克异步任务失败，已保留操作日志')
            time.sleep(0.25)
        raise QuarkError('夸克任务未及时确认，需按日志核对后继续')

    def mkdir(self, parent, name):
        self.require_write([parent], allow_root=True)
        name = self._name(name)
        current = [r for r in self.listing(parent) if r.get('file_name') == name]
        if current:
            if len(current) != 1 or not current[0].get('dir'):
                raise QuarkError('目标目录与现有文件冲突')
            return current[0]['fid']
        value = self.api('/file', {'pdir_fid': parent, 'file_name': name, 'dir_path': '', 'dir_init_lock': False})
        fid = (value.get('data') or {}).get('fid')
        if not fid:
            raise QuarkError('新目录未确认建立')
        self._remember(fid, self._path(parent) + '/' + name, True)
        return fid

    def rename(self, fid, name):
        self.require_write([fid])
        name = self._name(name)
        result = self.api('/file/rename', {'fid': fid, 'file_name': name})
        self._moved(fid, str(__import__('pathlib').PurePosixPath(self._path(fid)).parent) + '/' + name)
        return result

    def move(self, fids, parent):
        self.require_write(fids)
        self._path(parent)
        if not 0 < len(fids) <= 100:
            raise QuarkError('批次大小无效')
        value = self.api('/file/move', {'action_type': 1, 'filelist': fids, 'to_pdir_fid': parent, 'exclude_fids': []})
        self.wait_task(value)
        for fid in fids:
            self._moved(fid, self._path(parent) + '/' + __import__('pathlib').PurePosixPath(self._path(fid)).name)

    def recycle(self, fids):
        self.require_write(fids, cleanup=True)
        for fid in fids:
            if not self._objects[str(fid)]['dir'] or self.listing(fid):
                raise QuarkError('只允许回收已重新确认空的旧目录。')
        self.wait_task(self.api('/file/delete', {'action_type': 1, 'filelist': fids, 'exclude_fids': []}))

    def trash(self, remote, expected_size):
        raise QuarkError('公开版本不自动删除或替换媒体文件。')

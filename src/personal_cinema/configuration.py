"""Atomic typed configuration, explicit inheritance and revision-safe updates."""
from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .schema import BY_KEY, defaults

VERSION = 1
MAX_CONFIG_BYTES = 2 * 1024 * 1024
_LOCKS = {}
_LOCKS_LOCK = threading.Lock()
_HELD_LOCKS = threading.local()
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')
SOURCE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
LANGUAGE = re.compile(r'^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$')


class RevisionConflict(ValueError):
    def __init__(self, expected=None, actual=None):
        super().__init__('设置已被其他窗口修改，请刷新后重新应用。')
        self.expected, self.actual = expected, actual


def _json_read(path, fallback):
    if path.is_symlink(): raise ValueError('配置文件不能是符号链接。')
    if not path.exists(): return deepcopy(fallback)
    if path.stat().st_size > MAX_CONFIG_BYTES: raise ValueError('配置文件过大。')
    try:
        return json.loads(path.read_text('utf-8-sig'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError('配置文件损坏，原文件已保留，请从备份恢复。') from exc


def _atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('配置文件不能是符号链接。')
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try: Path(temporary).unlink()
        except FileNotFoundError: pass


@contextmanager
def _file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve Windows short/long path aliases to the same lock identity.
    name = str(path.resolve()).casefold() if os.name == 'nt' else str(path.resolve())
    with _LOCKS_LOCK: thread_lock = _LOCKS.setdefault(name, threading.RLock())
    with thread_lock:
        if path.is_symlink(): raise ValueError('锁文件不能是符号链接。')
        held = getattr(_HELD_LOCKS, 'names', None)
        if held is None:
            held = _HELD_LOCKS.names = set()
        if name in held:
            # RLock permits this thread to re-enter; the outer context already
            # owns the OS lock. Locking a second file handle deadlocks on Windows.
            yield
            return
        with path.open('a+b') as stream:
            stream.seek(0, 2)
            if stream.tell() == 0: stream.write(b'0'); stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                deadline = time.monotonic() + 10
                while True:
                    try: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1); break
                    except OSError:
                        if time.monotonic() >= deadline: raise TimeoutError('另一任务正在更新设置，请重试。') from None
                        time.sleep(0.02)
                held.add(name)
                try: yield
                finally:
                    held.remove(name)
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                held.add(name)
                try: yield
                finally:
                    held.remove(name)
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _scope(value):
    if value in ('global', 'device'): return value
    if not isinstance(value, str) or ':' not in value: raise ValueError('设置范围无效。')
    kind, ident = value.split(':', 1)
    if kind not in ('source', 'item', 'season') or not SOURCE_ID.fullmatch(ident):
        raise ValueError('设置范围无效。')
    return kind


def _url(value, *, optional=False):
    if optional and value == '': return
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('服务地址必须为 HTTP/HTTPS 字符串。')
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
        if parsed.port is not None and not 1 <= parsed.port <= 65535: valid = False
    except ValueError: valid = False
    if not valid or any(c.isspace() for c in value):
        raise ValueError('请输入不含账号、令牌或查询参数的 HTTP/HTTPS 地址。')


def _path(value, *, allow_empty=True):
    if value == '' and allow_empty: return
    if '\x00' in value or any(ord(c) < 32 for c in value): raise ValueError('目录或程序路径包含无效字符。')
    if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise ValueError('请选择绝对目录或程序路径。')
    if '..' in re.split(r'[\\/]', value): raise ValueError('目录不能包含上级路径。')


def _sources(entries):
    if len(entries) > 100: raise ValueError('最多配置100个媒体来源。')
    ids = set()
    for source in entries:
        allowed = {'id','name','kind','path','enabled','allow_write','driver','mount_path','url','credential','library_type','native_root'}
        if not isinstance(source, dict) or set(source) - allowed: raise ValueError('来源字段无效；凭据必须单独保存。')
        required = {'id','name','kind','path','enabled','allow_write'}
        if not required.issubset(source): raise ValueError('来源缺少名称、目录或权限字段。')
        ident = source['id']
        if not isinstance(ident, str) or not SOURCE_ID.fullmatch(ident) or ident in ids: raise ValueError('来源编号无效或重复。')
        ids.add(ident)
        if not isinstance(source['name'], str) or not 1 <= len(source['name'].strip()) <= 120: raise ValueError('来源名称无效。')
        if source['kind'] not in ('openlist','local'): raise ValueError('不支持的媒体来源类型；WebDAV 请先挂载到 OpenList。')
        if type(source['enabled']) is not bool or type(source['allow_write']) is not bool: raise ValueError('来源权限必须是开关值。')
        path = source['path']
        if not isinstance(path, str) or not path or len(path) > 4096: raise ValueError('来源目录无效。')
        if source['kind'] == 'local':
            if source['allow_write']: raise ValueError('当前版本本地媒体来源只读；尚不支持自动整理本地文件。')
            _path(path, allow_empty=False)
            local = PureWindowsPath(path) if PureWindowsPath(path).is_absolute() else Path(path)
            if source['allow_write'] and local.parent == local:
                raise ValueError('不能授权整理整个磁盘或共享根目录。')
        elif not path.startswith('/') or '\\' in path or '..' in path.split('/') or '\x00' in path or '//' in path:
            raise ValueError('远程来源应为不含上级跳转的绝对目录。')
        if source['allow_write'] and path in ('/', '\\'):
            raise ValueError('不能授权整理整个来源根目录。')
        if 'url' in source: _url(source['url'])
        if 'native_root' in source:
            native = source['native_root']
            if not isinstance(native, str) or len(native) > 1024 or '..' in native or any(ord(c) < 32 for c in native):
                raise ValueError('云端整理根目录或编号无效。')
        if 'credential' in source and (not isinstance(source['credential'], str) or not NAME.fullmatch(source['credential'])):
            raise ValueError('来源凭据引用名称无效。')
        if source.get('library_type', 'mixed') not in ('movies','tvshows','mixed'): raise ValueError('来源媒体库类型无效。')
        for key in ('driver','mount_path'):
            if key in source and (not isinstance(source[key], str) or len(source[key]) > 512): raise ValueError('来源扩展字段无效。')


def _value(key, value):
    if key not in BY_KEY: raise ValueError('未知设置项：' + str(key)[:100])
    descriptor = BY_KEY[key]
    if descriptor.get('invariant') and value != descriptor['default']:
        raise ValueError(descriptor['label'] + '：这是固定正确性规则，不能关闭。')
    kind = descriptor['kind']
    if kind == 'bool': valid = type(value) is bool
    elif kind == 'int': valid = type(value) is int
    elif kind == 'float': valid = type(value) in (int, float) and math.isfinite(value)
    elif kind in ('text','path','select'): valid = isinstance(value, str) and len(value) <= 4096 and not any(ord(c) < 32 for c in value)
    elif kind == 'list': valid = isinstance(value, list)
    else: valid = False
    if not valid: raise ValueError(descriptor['label'] + '：输入类型无效。')
    if kind in ('int','float'):
        if 'min' in descriptor and value < descriptor['min'] or 'max' in descriptor and value > descriptor['max']:
            raise ValueError(descriptor['label'] + '：数值超出允许范围。')
    if kind == 'select' and value not in [o['value'] for o in descriptor['options']]: raise ValueError(descriptor['label'] + '：选项无效。')
    if kind == 'path': _path(value)
    if key.endswith('_url'): _url(value, optional=key == 'network.proxy_url')
    if key.endswith('_credential') or key.endswith('credential_ref'):
        if not NAME.fullmatch(value) or value == '__proto__': raise ValueError('只能填写凭据引用名称。')
    if key == 'sources.entries': _sources(value)
    elif kind == 'list':
        if len(value) > 32 or any(not isinstance(x,str) or len(x)>80 for x in value) or len(set(value)) != len(value): raise ValueError(descriptor['label'] + '：列表无效或有重复项。')
        if key.endswith('languages') and (not value or any(not LANGUAGE.fullmatch(x) for x in value)): raise ValueError('语言请使用 zh-CN、en 等代码。')
        if key == 'ratings.providers' and any(x not in ('imdb','tomatoes','audience','metacritic','letterboxd','myanimelist','bangumi') for x in value): raise ValueError('评分来源尚未支持。')
    return deepcopy(value)


class ConfigStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().absolute()
        self.path = self.root / 'config.json'
        self.lock_path = self.root / '.config.lock'

    def _normalize(self, record):
        if not isinstance(record, dict) or set(record) - {'version','revision','values','overrides'}: raise ValueError('配置文档结构无效。')
        version = record.get('version', VERSION)
        if type(version) is not int or version not in (0, VERSION): raise ValueError('此配置来自不支持的版本，请使用兼容版本导入。')
        record = deepcopy(record)
        values = record.get('values', {})
        if not isinstance(values, dict): raise ValueError('配置值必须为对象。')
        if version == 0:
            rename = {'jellyfin_url':'foundation.jellyfin_url', 'mpv_path':'foundation.mpv_path','cache_gib':'cache.playback_gib'}
            migrated = {}
            for key, value in values.items():
                new_key = rename.get(key, key)
                if new_key in migrated: raise ValueError('旧版配置字段冲突。')
                migrated[new_key] = value
            values = migrated
        revision = record.get('revision', 0)
        if type(revision) is not int or revision < 0: raise ValueError('配置修订号无效。')
        normalized = defaults()
        normalized.update({key: _value(key, value) for key, value in values.items()})
        overrides = record.get('overrides', {})
        if not isinstance(overrides, dict) or len(overrides) > 10000: raise ValueError('配置覆盖项无效。')
        clean_overrides = {}
        for scope, data in overrides.items():
            scope_kind = _scope(scope)
            if scope_kind == 'global' or not isinstance(data, dict): raise ValueError('配置覆盖范围无效。')
            clean = {}
            for key, value in data.items():
                clean[key] = _value(key, value)
                if scope_kind not in BY_KEY[key]['scope']: raise ValueError('该设置不能覆盖到所选范围。')
            if clean: clean_overrides[scope] = clean
        if normalized['network.mode'] == 'proxy' and not normalized['network.proxy_url']:
            raise ValueError('指定代理模式需要填写代理地址。')
        if clean_overrides.get('device',{}).get('network.mode', normalized['network.mode']) == 'proxy' and not clean_overrides.get('device',{}).get('network.proxy_url', normalized['network.proxy_url']):
            raise ValueError('指定代理模式需要填写代理地址。')
        return {'version': VERSION, 'revision': revision, 'values': normalized, 'overrides': clean_overrides}

    def _read(self):
        return self._normalize(_json_read(self.path, {'version': VERSION, 'revision': 0, 'values': {}, 'overrides': {}}))

    def read(self):
        with _file_lock(self.lock_path): return self._read()

    def read_scope(self, scope='global'):
        _scope(scope)
        record = self.read()
        return deepcopy(record['values'] if scope == 'global' else record['overrides'].get(scope, {}))

    def get_effective(self, scope='global', parents=()):
        """Resolve explicitly provided source/item ancestry; never infer it from names."""
        return self.effective_snapshot(scope, parents)['values']

    def effective_snapshot(self, scope='global', parents=()):
        """Return effective values and their revision from one locked document."""
        _scope(scope)
        if isinstance(parents, str) or not isinstance(parents, (list, tuple)): raise ValueError('继承范围列表无效。')
        hierarchy = list(parents) + ([] if scope == 'global' else [scope])
        order = {'source': 1, 'item': 2, 'season': 3}
        if parents:
            ranks = [order.get(_scope(item), -1) for item in hierarchy]
            if any(rank < 0 for rank in ranks) or ranks != sorted(set(ranks)): raise ValueError('继承顺序必须为来源、作品、季。')
        with _file_lock(self.lock_path):
            record = self._read()
            result = deepcopy(record['values'])
            for item in hierarchy: result.update(deepcopy(record['overrides'].get(item, {})))
            return {'revision': record['revision'], 'values': result}

    def update(self, changes, expected_revision, scope='global'):
        kind = _scope(scope)
        if not isinstance(changes, dict) or len(changes) > len(BY_KEY): raise ValueError('设置变更内容无效。')
        with _file_lock(self.lock_path):
            record = self._read()
            if type(expected_revision) is not int or expected_revision != record['revision']:
                raise RevisionConflict(expected_revision, record['revision'])
            target = record['values'] if scope == 'global' else record['overrides'].setdefault(scope, {})
            for key, value in changes.items():
                if key not in BY_KEY: raise ValueError('未知设置项：' + str(key)[:100])
                if kind not in BY_KEY[key]['scope']: raise ValueError('该设置不能覆盖到所选范围。')
                if value is None:
                    if scope == 'global': target[key] = deepcopy(BY_KEY[key]['default'])
                    else: target.pop(key, None)
                else:
                    checked = _value(key, value)
                    prior = target.get(key, record['values'][key])
                    if not BY_KEY[key]['available'] and checked != prior:
                        raise ValueError(BY_KEY[key]['label'] + '：尚未接入运行模块，当前不可修改。')
                    target[key] = checked
            record['revision'] += 1
            record = self._normalize(record)
            _atomic_write(self.path, record)
            return deepcopy(record)

    def export(self):
        """Portable preferences only: omit identity, addresses, paths and source lists."""
        record = self.read()
        record['values'] = {key: value for key, value in record['values'].items() if not BY_KEY[key].get('private')}
        record['overrides'] = {scope: {key: value for key, value in values.items() if not BY_KEY[key].get('private')}
                               for scope, values in record['overrides'].items() if scope == 'device'}
        # A redacted proxy URL must not leave an unusable selected proxy mode.
        if record['values']['network.mode'] == 'proxy': record['values']['network.mode'] = 'direct'
        if record['overrides'].get('device', {}).get('network.mode') == 'proxy': record['overrides']['device']['network.mode'] = 'direct'
        return record

    def import_data(self, payload, expected_revision):
        normalized = self._normalize(payload)
        names = {'jellyfin_url':'foundation.jellyfin_url', 'mpv_path':'foundation.mpv_path', 'cache_gib':'cache.playback_gib'} if payload.get('version') == 0 else {}
        supplied = {names.get(key, key) for key in payload.get('values', {})}
        with _file_lock(self.lock_path):
            current = self._read()
            if type(expected_revision) is not int or expected_revision != current['revision']:
                raise RevisionConflict(expected_revision, current['revision'])
            for key in BY_KEY:
                if key not in supplied:
                    normalized['values'][key] = current['values'][key]
                elif not BY_KEY[key]['available'] and normalized['values'][key] != current['values'][key]:
                    raise ValueError(BY_KEY[key]['label'] + '：尚未接入运行模块，当前不可导入新值。')
            merged = deepcopy(current['overrides'])
            for scope, values in normalized['overrides'].items():
                previous = merged.setdefault(scope, {})
                for key, value in values.items():
                    if not BY_KEY[key]['available'] and value != previous.get(key, current['values'][key]):
                        raise ValueError(BY_KEY[key]['label'] + '：尚未接入运行模块，当前不可导入新值。')
                    previous[key] = value
            normalized['overrides'] = merged
            normalized['revision'] = current['revision'] + 1
            normalized = self._normalize(normalized)
            _atomic_write(self.path, normalized)
            return deepcopy(normalized)

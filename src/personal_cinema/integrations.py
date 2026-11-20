"""Capability-aware setup for the services used by Personal Cinema.

Connections and provisioning are separate. Existing installations are never
started, stopped or initialized by merely selecting their address.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from .service_manager import ServiceManager, atomic_json, read_json
from .component_installer import ComponentInstaller, COMPONENTS


class IntegrationError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        fp.close()
        raise IntegrationError('服务返回重定向，请在设置中填写最终服务地址')


def _base_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('服务地址格式无效')
    p = urllib.parse.urlsplit(value)
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password
            or p.query or p.fragment or any(c in value for c in '\r\n\x00')):
        raise ValueError('请填写不带账号、参数或片段的 HTTP(S) 服务地址')
    try:
        p.port
    except ValueError:
        raise ValueError('服务端口无效') from None
    return value.rstrip('/')


def _loopback(value):
    host = urllib.parse.urlsplit(value).hostname
    if host == 'localhost': return True
    try: return ipaddress.ip_address(host).is_loopback
    except ValueError: return False


def _text(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(c in value for c in '\r\n\x00'):
        raise ValueError(name + '无效')
    return value.strip()


def _credential_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,100}', value):
        raise ValueError('凭据引用名称无效')
    return value


def _remote_path(value):
    if not isinstance(value, str) or not value.startswith('/') or '\\' in value or any(c in value for c in '\r\n\x00'):
        raise ValueError('请选择以 / 开头的来源目录')
    if any(p in ('.', '..') for p in value.split('/')):
        raise ValueError('来源目录不能包含相对跳转')
    return '/' + '/'.join(p for p in value.split('/') if p)


def _library_path(value):
    value = _text(value, '媒体目录', 2048)
    if not (value.startswith('/') or PureWindowsPath(value).is_absolute()):
        raise ValueError('媒体库需要服务器上的绝对目录')
    if any(part in ('.', '..') for part in value.replace('\\', '/').split('/')):
        raise ValueError('媒体目录不能包含相对跳转')
    return value


def _has_secret_fields(value):
    if isinstance(value, dict):
        return any(re.search(r'pass|token|cookie|secret|credential|authorization', str(key), re.I)
                   or _has_secret_fields(item) for key, item in value.items())
    if isinstance(value, list): return any(_has_secret_fields(item) for item in value)
    return False


class IntegrationManager:
    def __init__(self, root, config_store, secret_store):
        self.root = Path(root).resolve()
        self.config = config_store
        self.secrets = secret_store
        self.services = ServiceManager(self.root)
        self.installer = ComponentInstaller(self.root, config_store)
        self.lock = threading.RLock()
        self.state_path = self.root / 'services' / 'integrations.json'

    def _values(self):
        return self.config.get_effective('device')

    def _mode(self, provider, values=None):
        values = values or self._values()
        if provider == 'jellyfin': return values.get('foundation.jellyfin_mode', 'existing')
        if provider == 'openlist': return values.get('sources.openlist_mode', 'disabled')
        if provider == 'hypomux': return 'existing' if values.get('network.hypomux_path') else 'disabled'
        return 'existing'

    def _url(self, provider):
        key = 'foundation.jellyfin_url' if provider == 'jellyfin' else 'sources.openlist_url'
        return _base_url(self._values().get(key, ''))

    def _credential_ref(self, provider):
        key = 'foundation.jellyfin_credential' if provider == 'jellyfin' else 'sources.openlist_credential'
        return _credential_name(self._values().get(key) or provider + '.token')

    def _fingerprint(self, provider):
        values = self._values()
        prefixes = {'jellyfin': ('foundation.jellyfin_',), 'openlist': ('sources.openlist_',),
                    'mpv': ('foundation.mpv_',), 'ffprobe': ('foundation.ffprobe_',), 'hypomux': ('network.',)}[provider]
        data = {key: value for key, value in values.items() if any(key.startswith(prefix) for prefix in prefixes)}
        if provider == 'jellyfin': data['sources.entries'] = values.get('sources.entries', [])
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _save_state(self, provider, result):
        with self.lock:
            records = read_json(self.state_path, {})
            records[provider] = {**result, 'checked_at': time.time(), 'fingerprint': self._fingerprint(provider)}
            atomic_json(self.state_path, records)

    def status(self):
        values = self._values()
        records = read_json(self.state_path, {})
        output = []
        for provider, name, required in [('jellyfin', 'Jellyfin 媒体库', True), ('mpv', 'MPV 播放器', True),
                                         ('ffprobe', '媒体参数探测', False),
                                         ('openlist', 'OpenList 媒体来源', False), ('hypomux', 'HypoMux 网络增强', False)]:
            mode = self._mode(provider, values)
            if provider == 'openlist':
                required = any(s.get('enabled', True) and s.get('kind') == 'openlist' for s in values.get('sources.entries', []))
            previous = records.get(provider, {})
            current = previous.get('fingerprint') == self._fingerprint(provider)
            state = previous.get('state', 'unconfigured') if current else 'unconfigured'
            detail = previous.get('detail', '请完成配置并检测连接') if current else '配置已变化，请重新检测'
            actions = ['probe']
            if provider in ('jellyfin', 'openlist'):
                actions += ['login', 'plan']
                actions += ['libraries'] if provider == 'jellyfin' else ['browse', 'drivers']
                if mode == 'managed': actions += ['start', 'stop']
            if provider == 'hypomux': actions += ['plan']
            if provider in COMPONENTS: actions += ['install_plan']
            if mode == 'disabled':
                state, detail, actions = 'disabled', '未启用，不影响基础影院', []
            row = {'id': provider, 'name': name, 'required': required, 'mode': mode,
                   'state': state, 'detail': detail, 'actions': actions,
                   'checked_at': previous.get('checked_at') if current else None}
            if provider in ('jellyfin', 'openlist') and mode == 'managed':
                row['process'] = self.services.status(provider)
                if not row['process']['running'] and state == 'ready':
                    row.update(state='stopped', detail='托管服务已停止，请启动后重新检测')
            if provider in COMPONENTS: row['download_page'] = COMPONENTS[provider]['homepage']
            output.append(row)
        return output

    def validate_setup(self):
        """Verify selected sources actually reach the library, not just saved fields."""
        sources = [s for s in self._values().get('sources.entries', []) if s.get('enabled', True)]
        missing, checked = [], []

        def fail(ident, detail, action='plan'):
            missing.append({'id': ident, 'detail': detail, 'action': action})

        if sources and not _loopback(self._url('jellyfin')):
            fail('jellyfin', '当前来源生成或使用本机目录，远端 Jellyfin 不能直接读取。请使用本机 Jellyfin，或停用这些来源后连接远端已有媒体库', 'configure')
            return {'state': 'needs_setup', 'missing': missing, 'checked': checked, 'playback_verified': False}
        try:
            jellyfin = self.perform('jellyfin', 'probe')
            checked.append({'id': 'jellyfin', 'state': jellyfin['state']})
            if jellyfin['state'] != 'ready': fail('jellyfin', jellyfin['detail'], 'login')
        except (IntegrationError, ValueError):
            jellyfin = {}
            fail('jellyfin', 'Jellyfin 连接或授权未通过，请先登录并检测', 'login')

        cloud_sources = [s for s in sources if s.get('kind') == 'openlist']
        if cloud_sources:
            if not self.secrets.get('openlist.password') or not self._values().get('sources.openlist_username'):
                fail('openlist', '当前视频读取使用 WebDAV，需要通过用户名密码登录 OpenList；仅 API 令牌不足以读取影片', 'login')
            if self._mode('openlist') == 'disabled':
                fail('openlist', '已选择网盘来源，请启用并连接 OpenList，或停用这些来源', 'configure')
            else:
                try:
                    openlist = self.perform('openlist', 'probe')
                    checked.append({'id': 'openlist', 'state': openlist['state']})
                    if openlist['state'] != 'ready': fail('openlist', openlist['detail'], 'login')
                    else:
                        for source in cloud_sources:
                            try:
                                self._browse({'path': source['path']})
                                checked.append({'id': 'source:' + str(source.get('id', source['name'])), 'state': 'readable'})
                            except (IntegrationError, ValueError):
                                fail('openlist', '来源目录不能读取：' + str(source.get('name', '未命名来源')), 'browse')
                except (IntegrationError, ValueError):
                    fail('openlist', 'OpenList 连接或授权未通过，请先登录并检测', 'login')

        expected = []
        if cloud_sources:
            expected.extend(str(self.root / 'runtime' / 'catalog' / name) for name in ('Movies', 'Shows'))
        for source in sources:
            if source.get('kind') == 'local':
                local = Path(source['path'])
                if not local.is_absolute() or not local.is_dir():
                    fail('jellyfin', '本地来源目录不存在：' + str(source.get('name', '未命名来源')), 'configure')
                else: expected.append(str(local.resolve()))
            elif source.get('kind') != 'openlist':
                fail('sources', '来源类型尚未支持，请改为本地目录或已挂载的 OpenList 来源', 'configure')
        if expected and jellyfin.get('state') == 'ready':
            if not jellyfin.get('is_admin'):
                fail('jellyfin', '核对新增来源目录需要管理员授权；请用管理员应用建库计划，或仅连接已有媒体库', 'login')
            else:
                try:
                    libraries = self._http('jellyfin', '/Library/VirtualFolders')
                    locations = {os.path.normcase(str(Path(p).resolve())) for row in libraries for p in row.get('Locations', [])}
                    absent = [p for p in expected if os.path.normcase(str(Path(p).resolve())) not in locations]
                    if absent: fail('jellyfin', '所选来源尚未全部加入 Jellyfin，请先预览并应用建库计划', 'plan')
                    else: checked.append({'id': 'library-mapping', 'state': 'ready', 'paths': len(expected)})
                except (IntegrationError, ValueError):
                    fail('jellyfin', '无法核对 Jellyfin 库目录，请检查管理员权限后重试', 'login')
        return {'state': 'needs_setup' if missing else 'ready', 'missing': missing, 'checked': checked,
                'playback_verified': False, 'detail': '来源与媒体库映射已验证，真实播放仍需验证' if not missing else '请完成缺少的来源或建库步骤'}

    def _http(self, provider, path, data=None, method=None, authenticated=True):
        if not path.startswith('/') or '://' in path:
            raise ValueError('内部接口路径无效')
        url = self._url(provider) + path
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if authenticated:
            token = self.secrets.get(self._credential_ref(provider))
            if not token:
                raise IntegrationError('请先登录该服务')
            if '\r' in token or '\n' in token:
                raise IntegrationError('服务凭据格式无效，请重新登录')
            if provider == 'jellyfin': headers['X-Emby-Token'] = token
            else: headers['Authorization'] = token
        if provider == 'jellyfin':
            headers['Authorization'] = 'MediaBrowser Client="PersonalCinema", Device="Setup", DeviceId="personal-cinema-setup", Version="0.1"'
        # Private service traffic must not inherit an Internet proxy with credentials.
        values = self._values()
        mode = values.get('network.mode', 'direct')
        if _loopback(url) or mode == 'direct': proxies = {}
        elif mode == 'proxy':
            proxy = _base_url(values.get('network.proxy_url', ''))
            proxies = {'http': proxy, 'https': proxy}
        else: proxies = None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _NoRedirect())
        request = urllib.request.Request(url, headers=headers, method=method,
                                         data=None if data is None else json.dumps(data).encode())
        try:
            with opener.open(request, timeout=12) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise IntegrationError('服务返回内容过大')
                value = json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403):
                raise IntegrationError('服务拒绝授权，请重新登录或检查账号权限', code) from None
            raise IntegrationError(f'服务请求失败（HTTP {code}），请检查服务状态', code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise IntegrationError('无法连接服务，请检查地址、网络和服务状态') from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise IntegrationError('服务未返回有效 JSON，请检查地址和兼容版本') from None
        if provider == 'openlist':
            if not isinstance(value, dict) or value.get('code') != 200:
                code = value.get('code') if isinstance(value, dict) else None
                if code in (401, 403):
                    raise IntegrationError('OpenList 授权已失效或权限不足，请重新登录')
                raise IntegrationError('OpenList 未完成请求，请检查配置与服务状态')
            return value.get('data')
        return value

    def perform(self, provider, action, payload=None):
        if provider not in ('jellyfin', 'openlist', 'mpv', 'ffprobe', 'hypomux'):
            raise ValueError('不支持的服务')
        if not isinstance(payload, (dict, type(None))):
            raise ValueError('请求内容无效')
        payload = payload or {}
        if provider in COMPONENTS and action == 'install_plan':
            return self.installer.plan(provider, payload)
        if provider in COMPONENTS and action == 'install_apply':
            return self.installer.apply(provider, payload)
        if self._mode(provider) == 'disabled':
            raise ValueError('请先在设置中启用该服务')
        allowed = {'jellyfin': {'probe', 'login', 'plan', 'apply', 'start', 'stop', 'libraries'},
                   'openlist': {'probe', 'login', 'plan', 'apply', 'start', 'stop', 'browse', 'drivers'},
                   'mpv': {'probe'}, 'ffprobe': {'probe'}, 'hypomux': {'probe', 'plan'}}
        if action not in allowed[provider]:
            raise ValueError('不支持的操作')
        try:
            if action == 'probe': result = self._probe(provider)
            elif action == 'login': result = self._login(provider, payload)
            elif action == 'plan': return self._plan(provider, payload)
            elif action == 'apply': return self._apply(provider, payload)
            elif action == 'start': return self._start(provider)
            elif action == 'stop':
                if self._mode(provider) != 'managed': raise ValueError('不能停止用户已有服务')
                result = self.services.stop(provider)
                self._save_state(provider, {'state': 'stopped', 'detail': '已停止本程序托管服务'})
                return result
            elif action == 'libraries': return self._libraries()
            elif action == 'browse': return self._browse(payload)
            elif action == 'drivers': return self._drivers()
            self._save_state(provider, result)
            return result
        except IntegrationError:
            self._save_state(provider, {'state': 'unavailable', 'detail': '连接或授权失败，请重新检测或登录'})
            raise

    def _login(self, provider, payload):
        username = _text(payload.get('username'), '用户名', 256)
        password = payload.get('password')
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise ValueError('请输入密码')
        if provider == 'jellyfin':
            value = self._http(provider, '/Users/AuthenticateByName', {'Username': username, 'Pw': password}, authenticated=False)
            token = value.get('AccessToken') if isinstance(value, dict) else None
        else:
            value = self._http(provider, '/api/auth/login', {'username': username, 'password': password}, authenticated=False)
            token = value.get('token') if isinstance(value, dict) else None
        if not isinstance(token, str) or not token:
            raise IntegrationError('登录响应缺少有效凭据')
        self.secrets.set(self._credential_ref(provider), token)
        self.secrets.set(provider + '.password', password)
        key = 'foundation.jellyfin_username' if provider == 'jellyfin' else 'sources.openlist_username'
        current = self.config.read()
        if current['values'].get(key) != username:
            self.config.update({key: username}, current['revision'])
        result = self._probe(provider)
        if provider == 'jellyfin' and isinstance(value.get('User'), dict):
            result['user_id'] = value['User'].get('Id')
            if result.get('user_id'): self.secrets.set('jellyfin.user_id', str(result['user_id']))
            if value.get('ServerId'):
                self.secrets.set('jellyfin.server_id', str(value['ServerId']))
        return result

    def _probe(self, provider):
        if provider == 'jellyfin':
            public = self._http(provider, '/System/Info/Public', authenticated=False)
            if not isinstance(public, dict) or not isinstance(public.get('Version'), str):
                raise IntegrationError('此地址不是受支持的 Jellyfin 服务')
            base = {'version': public['Version'][:80], 'startup_complete': bool(public.get('StartupWizardCompleted')),
                    'playback_verified': False}
            if not base['startup_complete']:
                return {**base, 'state': 'needs_setup', 'detail': '媒体服务尚未完成首次设置'}
            if not self.secrets.get(self._credential_ref(provider)):
                return {**base, 'state': 'needs_authorization', 'detail': '已连接 Jellyfin，请登录以验证媒体库访问'}
            user = self._http(provider, '/Users/Me')
            if not isinstance(user, dict) or not user.get('Id'):
                raise IntegrationError('无法验证 Jellyfin 用户')
            user_id = str(user['Id'])
            views = self._http(provider, '/Users/' + urllib.parse.quote(user_id, safe='') + '/Views')
            self.secrets.set('jellyfin.user_id', user_id)
            if public.get('Id'): self.secrets.set('jellyfin.server_id', str(public['Id']))
            else: self.secrets.delete('jellyfin.server_id')
            return {**base, 'state': 'ready', 'detail': '已验证登录与媒体库读取；实际播放需另行验证',
                    'user_id': user_id, 'username': str(user.get('Name', ''))[:256],
                    'is_admin': bool((user.get('Policy') or {}).get('IsAdministrator')),
                    'library_count': len(views.get('Items', [])) if isinstance(views, dict) else 0}
        if provider == 'openlist':
            settings = self._http(provider, '/api/public/settings', authenticated=False)
            if not isinstance(settings, (list, dict)):
                raise IntegrationError('此地址不是受支持的 OpenList 服务')
            version = None
            if isinstance(settings, list):
                version = next((str(x.get('value'))[:80] for x in settings if isinstance(x, dict) and x.get('key') == 'version'), None)
            initialization = self._openlist_initialization()
            if initialization is False:
                return {'state': 'needs_setup', 'detail': 'OpenList 尚未创建管理员，请完成首次设置', 'version': version, 'playback_verified': False}
            if not self.secrets.get(self._credential_ref(provider)):
                return {'state': 'needs_authorization', 'detail': 'OpenList 可达，请登录验证目录权限', 'version': version, 'playback_verified': False}
            user = self._http(provider, '/api/me')
            return {'state': 'ready', 'detail': '已验证登录；请浏览所选目录并验证实际媒体读取', 'version': version,
                    'username': str((user or {}).get('username', ''))[:256], 'playback_verified': False}
        values = self._values()
        key = {'mpv': 'foundation.mpv_path', 'ffprobe': 'foundation.ffprobe_path', 'hypomux': 'network.hypomux_path'}[provider]
        filename = values.get(key, '')
        if not filename:
            return {'state': 'not_installed', 'detail': '请选择已安装的程序或使用组件安装入口'}
        executable = Path(filename)
        if not executable.is_absolute() or not executable.is_file():
            return {'state': 'not_installed', 'detail': '未找到配置的可执行文件'}
        if provider == 'hypomux':
            return {'state': 'limited', 'detail': '已找到 HypoMux；当前仅提供规则建议，尚未验证受支持的控制接口和实际出口',
                    'routing_verified': False, 'automatic_configuration': False}
        if executable.suffix.lower() not in ('.exe', ''):
            raise ValueError('请选择组件可执行程序')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        try:
            arguments = ['--no-config', '--version'] if provider == 'mpv' else ['-version']
            result = subprocess.run([str(executable), *arguments], capture_output=True, timeout=8,
                                    creationflags=flags, shell=False, text=True, encoding='utf-8', errors='replace')
        except (OSError, subprocess.TimeoutExpired):
            raise IntegrationError('无法检测组件版本，请检查程序路径') from None
        first = next((line for line in result.stdout.splitlines() if line.lower().startswith(provider + ' ')), '')
        if result.returncode or not first:
            raise IntegrationError('所选程序未返回有效组件版本')
        detail = 'MPV 可启动，实际画面输出需播放验证' if provider == 'mpv' else '媒体探测程序可启动，真实影片参数需运行后获取'
        return {'state': 'ready', 'detail': detail, 'version': first[:160], 'playback_verified': False}

    def _libraries(self):
        user = self._http('jellyfin', '/Users/Me')
        if not (user.get('Policy') or {}).get('IsAdministrator'):
            value = self._http('jellyfin', '/Users/' + urllib.parse.quote(str(user['Id']), safe='') + '/Views')
            return {'is_admin': False, 'libraries': [{'id': row.get('Id'), 'name': row.get('Name')} for row in value.get('Items', [])]}
        value = self._http('jellyfin', '/Library/VirtualFolders')
        return {'is_admin': True, 'libraries': [{'name': row.get('Name'), 'paths': row.get('Locations', [])} for row in value]}

    def _browse(self, payload):
        path = _remote_path(payload.get('path', '/'))
        data = self._http('openlist', '/api/fs/list', {'path': path, 'page': 1, 'per_page': 200, 'refresh': False})
        items = []
        for row in (data or {}).get('content') or []:
            if isinstance(row, dict):
                items.append({'name': str(row.get('name', ''))[:512], 'is_dir': bool(row.get('is_dir')),
                              'size': row.get('size') if isinstance(row.get('size'), (int, float)) else None})
        return {'path': path, 'items': items, 'total': (data or {}).get('total', len(items)), 'playback_verified': False}

    def _drivers(self):
        value = self._http('openlist', '/api/admin/driver/list')
        if isinstance(value, dict): names = list(value)
        elif isinstance(value, list): names = [v if isinstance(v, str) else v.get('name') for v in value if isinstance(v, (str, dict))]
        else: names = []
        return {'drivers': sorted({name for name in names if isinstance(name, str) and len(name) < 100}),
                'detail': '驱动存在不等于已授权；播放与整理权限需要分别验证'}

    def _openlist_initialization(self):
        try:
            result = self._http('openlist', '/api/public/init_status', authenticated=False)
        except IntegrationError as error:
            if error.status in (404, 405): return None
            raise
        return result.get('initialized') if isinstance(result, dict) and isinstance(result.get('initialized'), bool) else None

    def _plan(self, provider, payload):
        if provider == 'hypomux':
            values = self._values()
            return {'state': 'manual_configuration', 'changes': ['在 HypoMux 中仅添加影院及其读取进程的出口规则', '保留其他应用与系统代理设置'],
                    'suggested_policy': {'preferred_interface': values.get('network.preferred_interface', ''),
                                         'fallback': values.get('network.fallback', 'direct')},
                    'routing_verified': False, 'automatic_configuration': False}
        mode = self._mode(provider)
        if mode not in ('existing', 'managed'):
            raise ValueError('服务模式无效')
        plan_id = uuid.uuid4().hex
        changes, libraries = [], []
        if mode == 'managed':
            self._managed_spec(provider, prepare=False)
            changes.append('准备独立数据目录并启动本程序托管的 ' + provider)
        request = {'libraries': []}
        if provider == 'jellyfin':
            for row in payload.get('libraries', []):
                if not isinstance(row, dict) or row.get('kind') not in ('movies', 'tvshows', 'music', 'books', 'photos', 'mixed'):
                    raise ValueError('媒体库类型无效')
                paths = row.get('paths')
                if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
                    raise ValueError('请选择至少一个媒体目录')
                libraries.append({'name': _text(row.get('name'), '媒体库名称', 80), 'kind': row['kind'],
                                  'paths': [_library_path(p) for p in paths]})
            if 'libraries' not in payload:
                sources = [s for s in self._values().get('sources.entries', []) if s.get('enabled', True)]
                if sources and not _loopback(self._url('jellyfin')):
                    raise ValueError('远端 Jellyfin 看不到本机目录。请在远端建立媒体库，或明确提供服务器可见的共享目录计划')
                if mode == 'managed' or any(s.get('kind') == 'openlist' for s in sources):
                    libraries = [{'name': '影院电影', 'kind': 'movies', 'paths': [str(self.root / 'runtime' / 'catalog' / 'Movies')]},
                                 {'name': '影院剧集', 'kind': 'tvshows', 'paths': [str(self.root / 'runtime' / 'catalog' / 'Shows')]}]
                for source in sources:
                    if source.get('kind') != 'local': continue
                    local = Path(_library_path(source.get('path')))
                    if not local.is_dir(): raise ValueError('本地来源目录不存在，请修正来源后重新预览')
                    kind = source.get('library_type', 'mixed')
                    if kind not in ('movies', 'tvshows', 'mixed'): raise ValueError('本地来源的媒体类型无效')
                    libraries.append({'name': _text(source.get('name'), '本地媒体库名称', 80),
                                      'kind': kind, 'paths': [str(local.resolve())]})
            if len(libraries) > 20: raise ValueError('一次最多建立20个媒体库')
            if len({row['name'] for row in libraries}) != len(libraries):
                raise ValueError('媒体库名称不能重复')
            request['libraries'] = libraries
            changes += ['建立媒体库：' + row['name'] for row in libraries]
            if payload.get('username') or payload.get('password'):
                if mode != 'managed': raise ValueError('已有服务请使用登录入口，不能重新初始化')
                request['username'] = _text(payload.get('username'), '管理员用户名', 256)
                password = payload.get('password')
                if not isinstance(password, str) or not 8 <= len(password) <= 4096:
                    raise ValueError('新实例管理员密码至少8个字符')
                reference = 'jellyfin.setup.' + plan_id
                self.secrets.set(reference, password)
                request['password_ref'] = reference
                changes.append('仅当新托管实例尚未初始化时创建管理员；保留已有实例设置')
        else:
            if payload.get('username') or payload.get('password'):
                if mode != 'managed': raise ValueError('已有服务请使用登录入口，不能重新初始化')
                request['username'] = _text(payload.get('username'), '管理员用户名', 256)
                password = payload.get('password')
                if not isinstance(password, str) or not 8 <= len(password) <= 4096:
                    raise ValueError('新实例管理员密码至少8个字符')
                reference = 'openlist.setup.' + plan_id
                self.secrets.set(reference, password)
                request['password_ref'] = reference
                changes.append('仅当新托管 OpenList 尚未初始化时创建管理员')
            storage = payload.get('storage')
            if storage is not None:
                if not isinstance(storage, dict): raise ValueError('挂载配置无效')
                addition = storage.get('addition', {})
                if not isinstance(addition, dict) or len(json.dumps(addition)) > 64000:
                    raise ValueError('驱动配置过大或格式无效')
                if _has_secret_fields(addition):
                    raise ValueError('驱动秘密字段请使用凭据引用，不要写入普通挂载配置')
                field = storage.get('credential_field', 'refresh_token')
                if field not in ('refresh_token', 'token', 'cookie', 'password', 'access_token'):
                    raise ValueError('不支持的驱动凭据字段')
                request['storage'] = {'mount_path': _remote_path(storage.get('mount_path')),
                                      'driver': _text(storage.get('driver'), '驱动名称', 100), 'addition': addition,
                                      'credential_ref': _credential_name(storage.get('credential_ref')),
                                      'credential_field': field}
                changes.append('建立已授权的 OpenList 挂载：' + request['storage']['mount_path'])
        if not changes: changes.append('验证现有服务，保留现有库、用户与网络设置')
        record = {'id': plan_id, 'provider': provider, 'created_at': time.time(), 'expires_at': time.time() + 1800,
                  'fingerprint': self._fingerprint(provider), 'mode': mode, 'request': request, 'changes': changes,
                  'completed_steps': [], 'state': 'planned'}
        atomic_json(self.root / 'services' / 'plans' / (plan_id + '.json'), record)
        return {'plan_id': plan_id, 'state': 'planned', 'changes': changes, 'requires_restart': mode == 'managed'}

    def _apply(self, provider, payload):
        plan_id = payload.get('plan_id')
        if not isinstance(plan_id, str) or not re.fullmatch('[0-9a-f]{32}', plan_id):
            raise ValueError('配置计划无效')
        path = self.root / 'services' / 'plans' / (plan_id + '.json')
        with self.lock:
            plan = read_json(path, {})
            if plan.get('provider') != provider: raise ValueError('找不到此服务的配置计划')
            if plan.get('state') == 'complete': return plan['result']
            if plan.get('expires_at', 0) < time.time(): raise ValueError('配置计划已过期，请重新预览')
            if plan.get('fingerprint') != self._fingerprint(provider): raise ValueError('服务配置已变化，请重新生成计划')
            plan['state'] = 'applying'
            atomic_json(path, plan)
            def checkpoint(name):
                if name not in plan['completed_steps']: plan['completed_steps'].append(name)
                atomic_json(path, plan)
            try:
                if plan['mode'] == 'managed':
                    self._start(provider)
                    checkpoint('managed-service')
                if provider == 'jellyfin': self._apply_jellyfin(plan, checkpoint)
                else: self._apply_openlist(plan, checkpoint)
                status = self._probe(provider)
                if status['state'] != 'ready':
                    raise IntegrationError(status['detail'])
                self._save_state(provider, status)
                result = {'plan_id': plan_id, 'state': 'complete', 'detail': '所选配置已完成并验证',
                          'completed_steps': list(plan['completed_steps']), 'playback_verified': False}
                plan.update(state='complete', result=result, completed_at=time.time())
                atomic_json(path, plan)
                reference = plan['request'].get('password_ref')
                if reference: self.secrets.delete(reference)
                return result
            except Exception:
                plan['state'] = 'failed'
                plan['detail'] = '配置未完成，已保留检查点；排除问题后可重试本计划'
                atomic_json(path, plan)
                raise

    def _apply_jellyfin(self, plan, checkpoint):
        public = self._http('jellyfin', '/System/Info/Public', authenticated=False)
        request = plan['request']
        if not public.get('StartupWizardCompleted'):
            if plan['mode'] != 'managed' or not self.services.status('jellyfin')['owned']:
                raise IntegrationError('已有服务尚未初始化，请先在 Jellyfin 官方界面完成设置')
            username = request.get('username')
            password = self.secrets.get(request.get('password_ref', '')) if request.get('password_ref') else None
            if not username or not password:
                raise IntegrationError('新实例尚未初始化，请重新生成包含管理员账号的配置计划')
            self._http('jellyfin', '/Startup/Configuration', {'ServerName': '个人影院', 'UICulture': 'zh-CN',
                       'MetadataCountryCode': 'CN', 'PreferredMetadataLanguage': 'zh'}, authenticated=False)
            self._http('jellyfin', '/Startup/User', {'Name': username, 'Password': password}, authenticated=False)
            self._http('jellyfin', '/Startup/RemoteAccess', {'EnableRemoteAccess': False, 'EnableAutomaticPortMapping': False}, authenticated=False)
            self._http('jellyfin', '/Startup/Complete', {}, method='POST', authenticated=False)
            checkpoint('jellyfin-initialized')
            self._login('jellyfin', {'username': username, 'password': password})
            plan['fingerprint'] = self._fingerprint('jellyfin')
            checkpoint('jellyfin-login')
        if not self.secrets.get(self._credential_ref('jellyfin')) and request.get('password_ref'):
            password = self.secrets.get(request['password_ref'])
            if password: self._login('jellyfin', {'username': request['username'], 'password': password})
            plan['fingerprint'] = self._fingerprint('jellyfin')
            checkpoint('jellyfin-login')
        libraries = request.get('libraries', [])
        if libraries:
            user = self._http('jellyfin', '/Users/Me')
            if not (user.get('Policy') or {}).get('IsAdministrator'):
                raise IntegrationError('建立媒体库需要管理员权限；观看账号可连接已有媒体库')
        for library in libraries:
            existing = self._http('jellyfin', '/Library/VirtualFolders')
            same = [row for row in existing if row.get('Name') == library['name']]
            if same:
                if set(same[0].get('Locations', [])) != set(library['paths']):
                    raise IntegrationError('同名媒体库已存在且目录不同，请更换名称或在原服务中处理')
            else:
                for item in library['paths']:
                    local = Path(item)
                    try: local.resolve().relative_to(self.root / 'runtime' / 'catalog')
                    except ValueError: continue
                    local.mkdir(parents=True, exist_ok=True)
                parameters = {'name': library['name'], 'paths': library['paths'], 'refreshLibrary': 'false'}
                if library['kind'] != 'mixed': parameters['collectionType'] = library['kind']
                query = urllib.parse.urlencode(parameters, doseq=True)
                options = {'EnableRealtimeMonitor': True, 'SaveLocalMetadata': False,
                           'EnableChapterImageExtraction': False, 'EnableTrickplayImageExtraction': False,
                           'PreferredMetadataLanguage': 'zh', 'MetadataCountryCode': 'CN',
                           'PathInfos': [{'Path': p} for p in library['paths']]}
                self._http('jellyfin', '/Library/VirtualFolders?' + query, {'LibraryOptions': options})
            checkpoint('library:' + library['name'])
        checkpoint('jellyfin-verified')

    def _apply_openlist(self, plan, checkpoint):
        request = plan['request']
        initialized = self._openlist_initialization()
        if initialized is False:
            if plan['mode'] != 'managed' or not self.services.status('openlist')['owned']:
                raise IntegrationError('已有 OpenList 尚未初始化，请先在其官方界面完成设置')
            username = request.get('username')
            password = self.secrets.get(request.get('password_ref', '')) if request.get('password_ref') else None
            if not username or not password:
                raise IntegrationError('请重新生成包含新实例管理员账号的配置计划')
            self._http('openlist', '/api/public/init/setup', {'username': username, 'password': password,
                       'site_title': '个人影院媒体来源'}, authenticated=False)
            checkpoint('openlist-initialized')
        if not self.secrets.get(self._credential_ref('openlist')) and request.get('password_ref'):
            password = self.secrets.get(request['password_ref'])
            if password: self._login('openlist', {'username': request['username'], 'password': password})
            plan['fingerprint'] = self._fingerprint('openlist')
            checkpoint('openlist-login')
        storage = request.get('storage')
        if storage:
            drivers = self._drivers()['drivers']
            if storage['driver'] not in drivers:
                raise IntegrationError('当前 OpenList 版本没有所选驱动')
            existing = self._http('openlist', '/api/admin/storage/list')
            rows = existing.get('content', []) if isinstance(existing, dict) else []
            same = [row for row in rows if row.get('mount_path') == storage['mount_path']]
            if same:
                if same[0].get('driver') != storage['driver']:
                    raise IntegrationError('挂载目录已被其他驱动使用，未覆盖原配置')
            else:
                secret = self.secrets.get(storage['credential_ref'])
                if not secret: raise IntegrationError('请先保存该云盘的授权凭据')
                addition = dict(storage['addition'])
                addition[storage['credential_field']] = secret
                self._http('openlist', '/api/admin/storage/create', {'mount_path': storage['mount_path'],
                           'order': 1, 'driver': storage['driver'], 'remark': 'Personal Cinema managed mount',
                           'cache_expiration': 30, 'web_proxy': True, 'webdav_policy': 'native_proxy',
                           'disabled': False, 'addition': json.dumps(addition, ensure_ascii=False)})
            self._browse({'path': storage['mount_path']})
            checkpoint('mount:' + storage['mount_path'])
        checkpoint('openlist-verified')

    def _managed_spec(self, provider, prepare=False):
        values = self._values()
        url = self._url(provider)
        if not _loopback(url): raise ValueError('托管服务必须使用本机回环地址')
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'http' or parsed.path not in ('', '/'):
            raise ValueError('托管服务使用本机 HTTP 地址，不支持自定义路径前缀')
        if parsed.hostname not in ('127.0.0.1', 'localhost'):
            raise ValueError('托管服务请使用 127.0.0.1 或 localhost 地址')
        port = parsed.port or 80
        exe_key = 'foundation.jellyfin_path' if provider == 'jellyfin' else 'sources.openlist_path'
        executable = Path(values.get(exe_key) or '')
        if not executable.is_absolute() or not executable.is_file() or executable.suffix.lower() not in ('.exe', ''):
            raise ValueError('请选择已安装的 ' + provider + ' 可执行文件')
        directory = self.root / 'managed' / provider
        data = directory / 'data'
        if provider == 'jellyfin' and values.get('foundation.jellyfin_data'):
            data = Path(values['foundation.jellyfin_data'])
            if not data.is_absolute(): raise ValueError('托管数据目录必须为绝对路径')
            data = data.resolve()
        marker = data / '.personal-cinema-owner.json'
        if data.exists() and any(data.iterdir()) and not marker.is_file():
            raise ValueError('数据目录已有内容且不属于本程序，请选择空目录或连接已有服务')
        if marker.exists():
            owner = read_json(marker, {})
            if owner != {'root': str(self.root), 'provider': provider}:
                raise ValueError('该数据目录属于另一实例，不能接管')
        if prepare:
            directory.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)
            atomic_json(marker, {'root': str(self.root), 'provider': provider})
        if provider == 'jellyfin':
            if prepare:
                network_file = data / 'config' / 'network.xml'
                if network_file.exists():
                    try: xml = ET.parse(network_file)
                    except ET.ParseError: raise ValueError('托管网络配置损坏，请恢复备份') from None
                    xml_root = xml.getroot()
                else:
                    xml_root = ET.Element('NetworkConfiguration')
                    xml = ET.ElementTree(xml_root)
                for key, val in [('InternalHttpPort', str(port)), ('EnableHttps', 'false'),
                                 ('EnableUPnP', 'false'), ('AutoDiscovery', 'false')]:
                    node = xml_root.find(key)
                    if node is None: node = ET.SubElement(xml_root, key)
                    node.text = val
                binds = xml_root.find('LocalNetworkAddresses')
                if binds is None: binds = ET.SubElement(xml_root, 'LocalNetworkAddresses')
                binds.clear(); ET.SubElement(binds, 'string').text = '127.0.0.1'
                network_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = network_file.with_suffix('.xml.tmp')
                xml.write(temporary, encoding='utf-8', xml_declaration=True)
                os.replace(temporary, network_file)
            argv = [str(executable), '--datadir', str(data), '--cachedir', str(directory / 'cache'),
                    '--logdir', str(directory / 'logs')]
            web = executable.parent / 'jellyfin-web'
            if web.is_dir(): argv += ['--webdir', str(web)]
            ffmpeg = executable.parent / 'ffmpeg.exe'
            if ffmpeg.is_file(): argv += ['--ffmpeg', str(ffmpeg)]
        else:
            if prepare:
                config_file = data / 'config.json'
                settings = read_json(config_file, {})
                settings.setdefault('scheme', {}).update({'address': '127.0.0.1', 'http_port': port, 'https_port': -1})
                settings.setdefault('database', {}).update({'type': 'sqlite3', 'db_file': str(data / 'data.db'), 'table_prefix': 'x_'})
                settings.setdefault('temp_dir', str(data / 'temp'))
                atomic_json(config_file, settings)
            argv = [str(executable), 'server', '--data', str(data)]
        return argv, directory, port

    def _start(self, provider):
        if self._mode(provider) != 'managed': raise ValueError('已有服务由其原有管理器启停')
        argv, directory, port = self._managed_spec(provider, prepare=False)
        spec_path = self.root / 'services' / 'specifications.json'
        specifications = read_json(spec_path, {})
        specification = hashlib.sha256(json.dumps([argv, str(directory), self._url(provider)], ensure_ascii=False).encode()).hexdigest()
        process = self.services.status(provider)
        if process['running']:
            if specifications.get(provider) != specification:
                raise ValueError('托管服务正在使用另一份启动配置，请先停止后再应用')
            state = self._probe(provider)
            self._save_state(provider, state)
            return {**state, 'process': process}
        with socket.socket() as probe:
            probe.settimeout(.3)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise IntegrationError('端口已被其他服务使用，请改用连接已有服务或更换端口')
        argv, directory, port = self._managed_spec(provider, prepare=True)
        env = dict(os.environ)
        network = self._values()
        if network.get('network.mode', 'direct') == 'direct':
            for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
                env.pop(key, None)
            env['NO_PROXY'] = '*'
            env['no_proxy'] = '*'
        elif network.get('network.mode') == 'proxy':
            proxy = _base_url(network.get('network.proxy_url', ''))
            for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
                env[key] = proxy
            env.pop('ALL_PROXY', None)
            env.pop('all_proxy', None)
            env['NO_PROXY'] = env['no_proxy'] = 'localhost,127.0.0.1,::1'
        process = self.services.start(provider, argv, directory, env=env)
        specifications[provider] = specification
        atomic_json(spec_path, specifications)
        # Do not call an unresponsive but launched process "ready".
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not self.services.status(provider)['running']:
                raise IntegrationError('托管进程已退出，请查看日志并重新配置')
            try:
                state = self._probe(provider)
                self._save_state(provider, state)
                return {**state, 'process': process}
            except IntegrationError:
                time.sleep(.25)
        self._save_state(provider, {'state': 'starting', 'detail': '进程已启动，服务尚未就绪，请稍后重新检测'})
        raise IntegrationError('进程已启动但服务尚未就绪，请稍后重新检测')

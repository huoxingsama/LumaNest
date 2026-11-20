"""Loopback administration UI, authenticated before any configuration is exposed."""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .configuration import ConfigStore, RevisionConflict
from .credentials import SecretStore
from .integrations import IntegrationManager
from .schema import schema


def editable_credential(name):
    return (isinstance(name, str) and not name.startswith(('player.', 'settings.'))
            and name not in ('stream.key', 'jellyfin.user_id', 'jellyfin.server_id'))


def credential_status(store):
    return {name: value for name, value in store.status().items() if editable_credential(name)}


def redact(value):
    if isinstance(value, dict):
        return {key: ('[redacted]' if re.search(r'password|token|secret|cookie|api.?key', key, re.I) else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(https?://)[^/@\s]+:[^/@\s]+@', r'\1[redacted]@', value)
        value = re.sub(r'(?i)([?&](?:key|token|access_token|api_key|signature)=)[^&\s]+', r'\1[redacted]', value)
    return value


class Application:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = ConfigStore(self.root)
        self.secrets = SecretStore(self.root)
        self.integrations = IntegrationManager(self.root, self.config, self.secrets)
        self.launch_token = secrets.token_urlsafe(40)
        self.launcher_key = secrets.token_urlsafe(40)
        self.launch_deadline = time.monotonic() + 600
        self.sessions = {}
        self.lock = threading.RLock()
        self._runtime = None
        self.closed = threading.Event()
        self.housekeeping = None

    @property
    def runtime(self):
        if self._runtime is None:
            from .runtime import Runtime
            self._runtime = Runtime(self.root, self.config, self.secrets, self.integrations)
        return self._runtime

    def diagnostics(self):
        return {'version': __version__, 'platform': os.name,
                'configuration_revision': self.config.read()['revision'],
                'integrations': redact(self.integrations.status()),
                'runtime': redact(self.runtime.status()),
                'includes_credentials': False, 'includes_watch_history': False}

    def maintenance(self):
        from .maintenance import MaintenanceManager

        def running():
            status = self.runtime.status()
            return bool(status['playing'] or any(status['processes'].values()) or
                        any(self.integrations.services.status(provider)['running'] for provider in ('jellyfin', 'openlist', 'hypomux')))
        return MaintenanceManager(self.root, running=running)

    def start_housekeeping(self):
        if self.housekeeping is not None:
            return

        def run():
            from .runtime_paths import instance_context
            last_cleanup = 0
            while not self.closed.wait(10):
                try:
                    with instance_context(self.root):
                        result = self.runtime.tick()
                        if result and result.get('state') == 'applied':
                            self.maintenance().sync_autostart()
                        if time.monotonic() - last_cleanup > 3600:
                            self.maintenance().prune_diagnostics()
                            last_cleanup = time.monotonic()
                except Exception:
                    # Failed supervision is visible as an unavailable process;
                    # never log exception strings containing a private URL.
                    pass
        self.housekeeping = threading.Thread(target=run, daemon=True, name='cinema-supervisor')
        self.housekeeping.start()


class HTTPError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def handler(application):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'PersonalCinema'
        sys_version = ''

        def log_message(self, *_):
            # Request paths may contain imported credentials; access logging is opt-in elsewhere.
            pass

        def respond(self, status, payload, extra=None):
            raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def local_request(self):
            port = self.server.server_port
            valid_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            if self.headers.get('Host') not in valid_hosts:
                raise HTTPError(403, 'host_rejected', '管理界面只接受本机访问')
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://{host}' for host in valid_hosts}:
                raise HTTPError(403, 'origin_rejected', '不允许其他网站修改本机设置')

        def session(self, mutation=False):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                token = cookie[f'cinema_session_{self.server.server_port}'].value
            except (KeyError, ValueError):
                token = ''
            entry = application.sessions.get(hashlib.sha256(token.encode()).hexdigest())
            if not entry or entry['expires'] < time.monotonic():
                raise HTTPError(401, 'session_required', '请从本机启动入口重新打开设置')
            if mutation and not hmac.compare_digest(entry['csrf'], self.headers.get('X-Cinema-CSRF', '')):
                raise HTTPError(403, 'csrf_rejected', '页面会话已失效，请刷新后重试')
            return entry

        def body(self):
            if self.headers.get('Transfer-Encoding'):
                raise HTTPError(400, 'bad_request', '不支持此请求格式')
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise HTTPError(400, 'bad_length', '请求长度无效') from None
            if not 0 <= length <= 2 * 1024 * 1024:
                raise HTTPError(413, 'too_large', '配置文件超过大小限制')
            if self.headers.get_content_type() != 'application/json':
                raise HTTPError(415, 'json_required', '请发送JSON格式的配置')
            try:
                value = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise HTTPError(400, 'invalid_json', '配置格式无效') from None
            if not isinstance(value, dict):
                raise HTTPError(400, 'object_required', '配置必须是对象')
            return value

        def handle_request(self):
            from .runtime_paths import instance_context
            with instance_context(application.root):
                self.dispatch_request()

        def dispatch_request(self):
            try:
                self.local_request()
                route = urllib.parse.urlsplit(self.path)
                path = route.path
                if path == '/api/reopen' and self.command == 'POST':
                    payload = self.body()
                    supplied = payload.get('key')
                    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, application.launcher_key):
                        raise HTTPError(401, 'launcher_required', '请从本机启动入口打开设置')
                    with application.lock:
                        application.launch_token = secrets.token_urlsafe(40)
                        application.launch_deadline = time.monotonic() + 600
                        token = application.launch_token
                    self.respond(200, {'token': token})
                    return
                if path == '/api/session' and self.command == 'POST':
                    payload = self.body()
                    with application.lock:
                        supplied = payload.get('token')
                        if not isinstance(supplied, str) or time.monotonic() > application.launch_deadline or not hmac.compare_digest(supplied, application.launch_token):
                            raise HTTPError(401, 'bad_launch_token', '启动链接已失效，请重新打开设置')
                        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
                        application.sessions[hashlib.sha256(token.encode()).hexdigest()] = {'csrf': csrf, 'expires': time.monotonic() + 12 * 3600}
                        application.launch_token = secrets.token_urlsafe(40)
                    self.respond(200, {'csrf': csrf}, {'Set-Cookie': f'cinema_session_{self.server.server_port}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'})
                    return
                if not path.startswith('/api/'):
                    if self.command != 'GET':
                        raise HTTPError(405, 'method_not_allowed', '不支持此操作')
                    self.static(path)
                    return
                session = self.session(mutation=self.command != 'GET')
                query = urllib.parse.parse_qs(route.query)
                if self.command == 'GET':
                    if path == '/api/session':
                        value = {'csrf': session['csrf']}
                    elif path == '/api/schema':
                        value = schema()
                    elif path == '/api/config':
                        value = application.config.read()
                        value['effective_values'] = application.config.get_effective(query.get('scope', ['global'])[0])
                    elif path == '/api/config/export':
                        value = application.config.export()
                    elif path == '/api/credentials':
                        value = {'configured': credential_status(application.secrets)}
                    elif path == '/api/status':
                        value = {'integrations': application.integrations.status(), 'runtime': application.runtime.status(),
                                 'setup': {'complete': (application.root / 'setup-complete.json').exists()}, 'version': __version__}
                    elif path == '/api/diagnostics':
                        value = application.diagnostics()
                    elif path == '/api/tasks':
                        value = application.runtime.tasks()
                    elif path == '/api/maintenance/backups':
                        value = {'items': application.maintenance().list_backups()}
                    elif path == '/api/organizing/plans':
                        from .worker import prepare
                        from .runtime_paths import RuntimePath
                        prepare()
                        import source_policy
                        value = source_policy.list_plans(RuntimePath(application.runtime.folder))
                    else:
                        raise HTTPError(404, 'not_found', '没有此接口')
                elif self.command == 'DELETE' and path.startswith('/api/credentials/'):
                    name = urllib.parse.unquote(path.rsplit('/', 1)[-1])
                    if not editable_credential(name):
                        raise ValueError('这是程序维护的会话或账户状态，请使用对应服务的登录入口')
                    application.secrets.delete(name)
                    value = {'configured': credential_status(application.secrets)}
                elif self.command == 'POST':
                    payload = self.body()
                    if path == '/api/config':
                        value = application.config.update(payload.get('changes', {}), payload.get('revision'), payload.get('scope', 'global'))
                    elif path == '/api/config/import':
                        value = application.config.import_data(payload.get('config'), payload.get('revision'))
                    elif path == '/api/credentials':
                        if not editable_credential(payload.get('name')):
                            raise ValueError('这是程序维护的会话或账户状态，请使用对应服务的登录入口')
                        application.secrets.set(payload.get('name'), payload.get('value'))
                        value = {'configured': credential_status(application.secrets)}
                    elif path.startswith('/api/integrations/'):
                        parts = path.strip('/').split('/')
                        if len(parts) != 4:
                            raise HTTPError(404, 'not_found', '没有此接口')
                        value = application.integrations.perform(parts[2], parts[3], payload)
                    elif path == '/api/setup/complete':
                        value = application.runtime.complete_setup()
                    elif path.startswith('/api/runtime/'):
                        action = path.rsplit('/', 1)[-1]
                        if action not in ('apply', 'start', 'stop', 'launch-player'):
                            raise HTTPError(404, 'not_found', '没有此操作')
                        value = getattr(application.runtime, action.replace('-', '_'))()
                        if action == 'apply' and value.get('state') == 'applied':
                            application.maintenance().sync_autostart()
                    elif path.startswith('/api/maintenance/'):
                        action = path.rsplit('/', 1)[-1]
                        maintenance = application.maintenance()
                        with application.runtime.lock:
                            if action == 'backup':
                                value = maintenance.backup()
                            elif action == 'restore':
                                value = maintenance.restore(payload.get('id'), payload.get('revision'))
                            elif action == 'prune':
                                value = maintenance.prune_diagnostics()
                            elif action == 'autostart':
                                value = maintenance.sync_autostart()
                            else:
                                raise HTTPError(404, 'not_found', '没有此维护操作')
                    elif path.startswith('/api/organizing/'):
                        value = application.runtime.organizing_command(path.rsplit('/', 1)[-1], payload)
                    elif path.startswith('/api/tasks/'):
                        value = application.runtime.task_command(path.rsplit('/', 1)[-1], payload)
                    else:
                        raise HTTPError(404, 'not_found', '没有此接口')
                else:
                    raise HTTPError(405, 'method_not_allowed', '不支持此操作')
                self.respond(200, value)
            except HTTPError as error:
                self.respond(error.status, {'error': {'code': error.code, 'message': error.message}})
            except RevisionConflict:
                self.respond(409, {'error': {'code': 'revision_conflict', 'message': '设置已在另一页面修改，请重新加载后保存'}})
            except (ValueError, PermissionError) as error:
                self.respond(400, {'error': {'code': 'invalid_configuration', 'message': redact(str(error))[:400]}})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self.respond(503, {'error': {'code': type(error).__name__, 'message': '操作未完成，请检查连接、授权及组件状态后重试'}})

        do_GET = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

        def static(self, path):
            files = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
            if path not in files:
                raise HTTPError(404, 'not_found', '没有此页面')
            file = Path(__file__).parent / 'web' / files[path]
            raw = file.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', (mimetypes.guess_type(file.name)[0] or 'text/plain') + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def default_root():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'PersonalCinema'


def create_server(root, port=0):
    application = Application(root)

    class ManagedServer(ThreadingHTTPServer):
        def server_close(self):
            application.closed.set()
            if application.housekeeping is not None:
                application.housekeeping.join(timeout=2)
            super().server_close()
    server = ManagedServer(('127.0.0.1', port), handler(application))
    server.daemon_threads = True
    server.application = application
    return server


def reopen_existing(root, timeout=8):
    """Reopen the owned settings process without starting another supervisor."""
    import urllib.request
    import psutil
    from .runtime import read
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = read(Path(root) / 'settings-process.json')
            process = psutil.Process(record['pid'])
            if abs(process.create_time() - record['created']) > .01 or process.cmdline() != record['command']:
                raise ValueError('Settings process ownership changed')
            port = record['port']
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError('Invalid settings endpoint')
            url = f'http://127.0.0.1:{port}'
            key = SecretStore(root).get('settings.launcher')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(url + '/api/reopen', data=json.dumps({'key': key}).encode(),
                                             headers={'Content-Type': 'application/json'})
            with opener.open(request, timeout=1) as response:
                token = json.loads(response.read(4096))['token']
            return url + '/#token=' + token
        except (OSError, ValueError, KeyError, psutil.Error):
            time.sleep(.2)
    raise ValueError('设置已在运行但暂时无法连接，请稍后重新打开；没有启动重复后台')


def main():
    parser = argparse.ArgumentParser(description='Personal Cinema 设置与首次启动')
    parser.add_argument('--data-dir', type=Path, default=default_root())
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--start-runtime', action='store_true', help='Start configured instance workers after completed setup')
    args = parser.parse_args()
    from .process_lock import InstanceLease
    from .runtime import write
    import psutil
    root = args.data_dir.resolve()
    lease = InstanceLease(root / '.settings.lock')
    if not lease.acquire():
        url = reopen_existing(root)
        if not args.no_browser:
            webbrowser.open(url)
        return
    server = None
    try:
        server = create_server(root, args.port)
        server.application.secrets.set('settings.launcher', server.application.launcher_key)
        process = psutil.Process()
        write(root / 'settings-process.json', {'pid': process.pid, 'created': process.create_time(),
                                               'command': process.cmdline(), 'port': server.server_port})
        server.application.start_housekeeping()
        if args.start_runtime and (root / 'setup-complete.json').exists():
            try:
                from .runtime_paths import instance_context
                with instance_context(root):
                    server.application.runtime.start()
            except (OSError, ValueError):
                # Keep settings available for correcting the service connection.
                pass
        url = f'http://127.0.0.1:{server.server_port}/#token={server.application.launch_token}'
        # Port is public; the session bootstrap token is never printed or logged.
        print(f'Personal Cinema {__version__} — http://127.0.0.1:{server.server_port}/', flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        lease.release()


if __name__ == '__main__':
    main()

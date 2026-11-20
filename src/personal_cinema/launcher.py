"""One installed application entry for settings, background work and the cinema.

All workers are owned by the settings supervisor. This short-lived launcher never
executes scripts from the user's media/data directories or reads their passwords.
"""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from .runtime_paths import application_home
from .settings_launcher import _settings_running


class LaunchError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        response.close()
        raise LaunchError('本机设置接口返回了意外跳转；操作已停止')


def _reopen(root, timeout=15):
    from .server import reopen_existing
    return reopen_existing(root, timeout=timeout)


def _ensure_server(root, port=0):
    if not _settings_running(root):
        executable = Path(sys.executable)
        # A custom executable name can be bound to a network route. Preserve
        # that identity; CREATE_NO_WINDOW already hides the child on Windows.
        environment = os.environ.copy()
        environment['PERSONAL_CINEMA_HOME'] = str(root)
        command = [str(executable), '-m', 'personal_cinema.server', '--data-dir', str(root), '--no-browser']
        if port:
            command += ['--port', str(port)]
        # The server's existing lease and ownership registry serialize parallel
        # launcher clicks; no independent watchdog or legacy script is created.
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, env=environment,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return _reopen(root)


class _LocalSession:
    def __init__(self, bootstrap):
        parsed = urllib.parse.urlsplit(bootstrap)
        try:
            port = parsed.port
        except ValueError:
            raise LaunchError('本机设置地址无效') from None
        fragment = urllib.parse.parse_qs(parsed.fragment)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not port or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.path not in ('', '/') or set(fragment) != {'token'}
                or len(fragment['token']) != 1):
            raise LaunchError('本机设置地址无效')
        self.origin = f'http://127.0.0.1:{port}'
        self.csrf = ''
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect())
        result = self.post('/api/session', {'token': fragment['token'][0]})
        self.csrf = result.get('csrf')
        if not isinstance(self.csrf, str) or not self.csrf:
            raise LaunchError('本机设置会话未建立，请重新打开')

    def post(self, route, payload=None):
        if route not in ('/api/session', '/api/runtime/start', '/api/runtime/launch-player',
                         '/api/tasks/scan-new', '/api/tasks/scan',
                         '/api/integrations/jellyfin/probe', '/api/integrations/openlist/probe'):
            raise LaunchError('不支持此启动操作')
        request = urllib.request.Request(self.origin + route, method='POST',
            data=json.dumps(payload or {}).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Origin': self.origin,
                     'X-Cinema-CSRF': self.csrf})
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise LaunchError('本机设置响应过大；请在设置页查看状态')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise LaunchError('本机设置未返回有效状态')
            return result
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            # Do not echo private paths, URLs or provider errors in terminal
            # output. The authenticated settings page has the detailed state.
            raise LaunchError(f'本机操作未完成（HTTP {status}），请在系统设置检查配置与运行状态', status) from None
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            raise LaunchError('本机接口暂时不可用，请通过系统设置查看状态后重试') from None


def _wait_external_services(root, session, timeout):
    """Wait for configured external dependencies; never start an external script."""
    from .configuration import ConfigStore
    values = ConfigStore(root).get_effective('device')
    waiting = []
    if values.get('foundation.jellyfin_mode') == 'existing':
        waiting.append('jellyfin')
    cloud = any(row.get('enabled', True) and row.get('kind') == 'openlist'
                for row in values.get('sources.entries', []))
    if cloud and values.get('sources.openlist_mode') == 'existing':
        waiting.append('openlist')
    deadline = time.monotonic() + timeout
    while waiting:
        for provider in list(waiting):
            try:
                result = session.post(f'/api/integrations/{provider}/probe')
            except LaunchError as error:
                if error.status not in (None, 400, 503):
                    raise
                continue
            state = result.get('state')
            if state == 'ready':
                waiting.remove(provider)
            elif state in ('needs_setup', 'needs_authorization', 'unauthorized', 'expired'):
                raise LaunchError('外部媒体服务需要设置或重新授权，请打开系统设置完成连接')
        if waiting:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LaunchError('等待外部 Jellyfin / OpenList 就绪超时；请先启动所连接的服务，再重新打开影院')
            time.sleep(min(2, remaining))


def launch(root=None, action='settings', port=0, wait_services=120):
    root = Path(root if root is not None else application_home()).resolve()
    if action not in ('settings', 'background', 'open-cinema', 'scan-new', 'scan-all', 'settings-only'):
        raise LaunchError('不支持此启动操作')
    if action in ('background', 'scan-new', 'scan-all') and not (root / 'setup-complete.json').is_file():
        raise LaunchError('请先打开系统设置完成初始配置；后台没有执行扫描或启动播放器')
    bootstrap = _ensure_server(root, port)
    if action == 'settings-only':
        return {'state': 'running', 'message': '本机设置服务已运行'}
    if action == 'settings' or (action == 'open-cinema' and not (root / 'setup-complete.json').is_file()):
        if not webbrowser.open(bootstrap):
            raise LaunchError('无法打开系统浏览器，请检查默认浏览器后重试')
        return {'state': 'opened', 'message': '已打开系统设置' if action == 'settings' else '请先完成初始配置，再打开影院'}

    # Concurrent desktop clicks can rotate the one-time bootstrap token. Retry
    # authentication only, never silently repeat a runtime or scan mutation.
    for attempt in range(3):
        try:
            session = _LocalSession(bootstrap)
            break
        except LaunchError as error:
            if error.status != 401 or attempt == 2:
                raise
            bootstrap = _reopen(root)
    _wait_external_services(root, session, wait_services)
    if action == 'open-cinema':
        result = session.post('/api/runtime/launch-player')
        if result.get('state') not in ('starting', 'running'):
            raise LaunchError('播放器尚未启动，请在系统设置查看实际状态')
        return {'state': result['state'], 'message': '正在打开本实例影院'}
    result = session.post('/api/runtime/start')
    if result.get('state') not in ('running', 'ready'):
        raise LaunchError('后台尚未就绪，请在系统设置查看实际状态')
    if action == 'background':
        return {'state': 'running', 'message': '本实例后台已启动；后续任务按已保存的自动化设置执行'}
    queued = session.post('/api/tasks/' + ('scan-new' if action == 'scan-new' else 'scan'),
                          {'request_id': secrets.token_hex(16)})
    if queued.get('state') not in ('queued', 'accepted', 'running', 'complete', 'applied'):
        raise LaunchError('扫描请求未被接受，请在工作台查看实际状态')
    return {'state': queued['state'], 'message': '新入库扫描已提交' if action == 'scan-new' else '全库扫描已提交'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='LumaNest：同一程序，独立私人配置')
    parser.add_argument('--data-dir', type=Path, default=application_home(), help='本实例配置和加密凭据目录')
    parser.add_argument('--port', type=int, default=0, help='新设置服务的本机端口；默认自动选择')
    parser.add_argument('--wait-services', type=int, default=120, help='等待已有 Jellyfin / OpenList 就绪的最长秒数，默认 120')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--open-cinema', action='store_const', const='open-cinema', dest='action', help='启动本实例后台并打开内置 MPV 影院')
    actions.add_argument('--background', action='store_const', const='background', dest='action', help='启动本实例后台，不打开播放器或浏览器')
    actions.add_argument('--scan-new', action='store_const', const='scan-new', dest='action', help='启动后台并提交新入库扫描')
    actions.add_argument('--scan-all', action='store_const', const='scan-all', dest='action', help='启动后台并提交全库扫描')
    parser.add_argument('--no-browser', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--start-runtime', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('端口必须介于 0 和 65535')
    if not 0 <= args.wait_services <= 600:
        parser.error('等待服务的时间必须介于 0 和 600 秒')
    action = args.action or ('background' if args.start_runtime else 'settings-only' if args.no_browser else 'settings')
    try:
        result = launch(args.data_dir, action, args.port, args.wait_services)
        if args.start_runtime and not args.no_browser and not args.action:
            launch(args.data_dir, 'settings', args.port)
        if sys.stdout is not None:
            print(result['message'])
        return 0
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, LaunchError) else '启动未完成，请从系统设置检查此实例'
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if action == 'open-cinema':
            try:
                launch(args.data_dir, 'settings', args.port)
            except (OSError, ValueError):
                pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

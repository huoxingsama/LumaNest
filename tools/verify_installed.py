"""Verify the built wheel from a fresh venv, outside the development checkout.

Usage: python tools/verify_installed.py --wheel dist/<wheel>.whl
Only temporary fake services/DPAPI credentials are created. No media is played.
"""
from __future__ import annotations

import argparse
import gettext
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def clean_environment():
    environment = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PERSONAL_CINEMA_HOME', 'MEDIA_LIBRARY_ROOT'):
        environment.pop(name, None)
    environment['PYTHONNOUSERSITE'] = '1'
    environment['PYTHONUTF8'] = '1'
    return environment


def wait_for(action, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = action()
            if value:
                return value
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.1)
    raise RuntimeError('Timed out waiting for isolated service')


def request(url, path, body=None, headers=None, method=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    prepared = urllib.request.Request(url + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        response = opener.open(prepared, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        content = response.read(4 * 1024 * 1024 + 1)
        require(len(content) <= 4 * 1024 * 1024, 'Fixture response exceeds size limit')
        return response.status, dict(response.headers), content


def process_record(pid):
    import psutil
    process = psutil.Process(pid)
    return {'pid': pid, 'created': process.create_time(), 'command': process.cmdline()}


def alive(record):
    import psutil
    try:
        process = psutil.Process(record['pid'])
        return (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                and abs(process.create_time() - record['created']) < .001
                and process.cmdline() == record['command'])
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def child_records(records):
    import psutil
    found = {row['pid']: row for row in records}
    for row in records:
        if not alive(row): continue
        try:
            parent = psutil.Process(row['pid'])
            for child in parent.children(recursive=True):
                if child.create_time() >= row['created']:
                    found[child.pid] = process_record(child.pid)
        except psutil.NoSuchProcess:
            pass
    return list(found.values())


def clean_processes(records):
    from personal_cinema.process_control import terminate_owned_tree
    for row in records:
        if alive(row):
            terminate_owned_tree(row['pid'], lambda row=row: alive(row), timeout=5)


def run_bounded(command, *, timeout, **kwargs):
    """Track only this invocation's children, including venv redirectors.

    The outer verifier needs psutil (a declared application dependency) but does
    not import application code. On timeout no source-tree fallback is used.
    """
    import psutil
    process = subprocess.Popen(command, **kwargs)
    records = []
    try:
        try: records.append(process_record(process.pid))
        except psutil.NoSuchProcess: pass
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            records = child_records(records)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            try: process.wait(timeout=min(1, max(.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired: pass
        return subprocess.CompletedProcess(command, process.returncode)
    finally:
        # Freeze verified parents before the last enumeration so cleanup cannot
        # race with a wrapper spawning another child. Never search by image name.
        records = child_records(records)
        for row in records:
            if alive(row):
                try: psutil.Process(row['pid']).suspend()
                except psutil.NoSuchProcess: pass
        records = child_records(records)
        for row in reversed(records):
            if alive(row):
                try:
                    child = psutil.Process(row['pid'])
                    child.kill()
                    child.wait(timeout=5)
                except psutil.NoSuchProcess: pass
        process.wait(timeout=5)


def resources_check(forbidden):
    cinema = importlib.import_module('personal_cinema')
    native = importlib.import_module('jellyfin_mpv_shim')
    prefix = Path(sys.prefix).resolve()
    for package in (cinema, native):
        path = Path(package.__file__).resolve()
        require(path.is_relative_to(prefix), 'Package was not imported from the fresh virtual environment')
        require(not path.is_relative_to(forbidden), 'Development checkout leaked into installed imports')
    require(all(not Path(p).resolve().is_relative_to(forbidden) for p in sys.path if p), 'Development path is present in sys.path')
    cinema_root, native_root = Path(cinema.__file__).parent, Path(native.__file__).parent
    expected = [(cinema_root, 'web/index.html'), (cinema_root, 'web/app.js'), (cinema_root, 'web/style.css'),
                (cinema_root, 'resources/brightness-control.lua'),
                (native_root, 'themes/superdark.json'), (native_root, 'mpvtk/renderer.lua'),
                (native_root, 'mouse.lua'), (native_root, 'thumbfast.lua'),
                (native_root, 'mpvtk_browser/components/models/lbpcascade_animeface.xml'),
                (native_root, 'messages/zh_Hans/LC_MESSAGES/base.mo')]
    for folder, relative in expected:
        require((folder / relative).is_file() and (folder / relative).stat().st_size > 0,
                'Installed resource missing: ' + relative)
    json.loads((native_root / 'themes/superdark.json').read_text('utf-8'))
    ET.parse(native_root / 'mpvtk_browser/components/models/lbpcascade_animeface.xml')
    with (native_root / 'messages/zh_Hans/LC_MESSAGES/base.mo').open('rb') as translation:
        gettext.GNUTranslations(translation)
    from jellyfin_mpv_shim.mpvtk_browser.components import subject_framing
    detector = subject_framing._local_detector()
    require(detector is not None, 'Installed subject detector dependencies are missing')
    cv2, numpy, classifiers = detector
    for dependency in (cv2, numpy):
        require(Path(dependency.__file__).resolve().is_relative_to(prefix), 'Subject detector imported outside installed venv')
    require(len(classifiers) == 2 and all(not model.empty() for model in classifiers), 'Installed subject models incomplete')
    for model in classifiers:
        require(len(model.detectMultiScale(numpy.zeros((128, 128), dtype=numpy.uint8))) == 0,
                'Native subject detector failed on empty fixture')
    return {'state': 'passed', 'checked': [relative for _, relative in expected], 'imports_from_venv': True,
            'subject_detection': {'opencv': cv2.__version__, 'numpy': numpy.__version__, 'models_loaded': len(classifiers)}}


def settings_check(workspace, outside):
    from personal_cinema.credentials import SecretStore
    home = workspace / 'settings-instance'
    records, process = [], None
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        with (workspace / 'settings-process.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-I', '-m', 'personal_cinema.server',
                '--data-dir', str(home), '--no-browser'], cwd=outside, env=clean_environment(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags)
        records.append(process_record(process.pid))
        registry = wait_for(lambda: json.loads((home / 'settings-process.json').read_text('utf-8')))
        records.append({key: registry[key] for key in ('pid', 'created', 'command')})
        url = 'http://127.0.0.1:' + str(registry['port'])
        wait_for(lambda: request(url, '/')[0] == 200)
        require(request(url, '/api/schema')[0] == 401, 'Settings schema was accessible without a session')
        launcher = SecretStore(home).get('settings.launcher')
        require(bool(launcher), 'Installed settings launcher did not persist DPAPI bootstrap')
        status, _, data = request(url, '/api/reopen', {'key': launcher})
        require(status == 200, 'DPAPI launcher exchange failed')
        token = json.loads(data)['token']
        status, response_headers, data = request(url, '/api/session', {'token': token}, {'Origin': url})
        require(status == 200, 'Installed settings session failed')
        cookie = response_headers['Set-Cookie']
        require('HttpOnly' in cookie and 'SameSite=Strict' in cookie, 'Session cookie protections missing')
        headers = {'Cookie': cookie.split(';', 1)[0], 'Origin': url, 'X-Cinema-CSRF': json.loads(data)['csrf']}
        status, _, data = request(url, '/api/schema', headers=headers)
        require(status == 200 and len(json.loads(data)['fields']) >= 50, 'Installed schema incomplete')
        status, _, data = request(url, '/api/config', headers=headers)
        require(status == 200, 'Installed config read failed')
        revision = json.loads(data)['revision']
        body = {'revision': revision, 'changes': {'appearance.text_scale': 1.1}}
        require(request(url, '/api/config', body, {**headers, 'X-Cinema-CSRF': 'invalid-fixture'})[0] == 403,
                'Installed settings accepted invalid CSRF')
        require(request(url, '/api/config', body, {**headers, 'Origin': 'https://outside.invalid'})[0] == 403,
                'Installed settings accepted cross-origin mutation')
        require(request(url, '/api/config', body, headers)[0] == 200, 'Installed settings could not save configuration')
        for path, content_type in (('/', 'text/html'), ('/app.js', 'javascript'), ('/style.css', 'text/css')):
            status, response_headers, data = request(url, path)
            require(status == 200 and len(data) > 100, 'Installed settings static asset missing: ' + path)
            require(content_type in response_headers.get('Content-Type', ''), 'Static asset MIME type incorrect')
            require('frame-ancestors' in response_headers.get('Content-Security-Policy', ''), 'Static security headers missing')
        records = child_records(records)
        clean_processes(records)
        process.wait(timeout=5)
        require(not any(alive(row) for row in records), 'Installed settings left process descendants running')
        return {'state': 'passed', 'auth': True, 'csrf': True, 'schema': True, 'static_assets': True,
                'process_tree_stopped': True}
    finally:
        clean_processes(records)
        if process is not None:
            process.wait(timeout=5)


def runtime_check(workspace):
    from personal_cinema.configuration import ConfigStore
    from personal_cinema.credentials import SecretStore
    from personal_cinema.integrations import IntegrationManager
    from personal_cinema.runtime import Runtime
    from personal_cinema.runtime_paths import instance_context
    seen = []

    class FakeJellyfin(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            seen.append(self.path)
            route = self.path.split('?', 1)[0]
            require(route.startswith('/fixture-jf/'), 'Installed gateway dropped the configured base path')
            route = route[len('/fixture-jf'):]
            if route == '/System/Info/Public':
                value = {'ServerName': 'Installed verification fixture', 'Name': 'Installed verification fixture',
                         'Version': '10.10.7', 'Id': 'fixture-server', 'StartupWizardCompleted': True}
            elif route == '/Users/Me': value = {'Id': 'fixture-user', 'Name': 'fixture', 'Policy': {'IsAdministrator': True}}
            elif route in ('/Users', '/Sessions', '/Library/VirtualFolders'): value = []
            else: value = {'Items': [], 'TotalRecordCount': 0}
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), FakeJellyfin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = workspace / 'runtime-instance'
    records, runtime = [], None
    try:
        config, credentials = ConfigStore(home), SecretStore(home)
        config.update({'foundation.jellyfin_url': f'http://127.0.0.1:{server.server_port}/fixture-jf',
                       'foundation.jellyfin_username': 'fixture', 'automation.mode': 'manual'}, 0)
        credentials.set('jellyfin.token', 'isolated-verification-token')
        credentials.set('jellyfin.user_id', 'fixture-user')
        credentials.set('jellyfin.server_id', 'fixture-server')
        with instance_context(home):
            runtime = Runtime(home, config, credentials, IntegrationManager(home, config, credentials))
            state = runtime.start()
            registry = json.loads((home / 'runtime-processes.json').read_text('utf-8'))
            records = child_records(list(registry.values()))
            require(state['state'] == 'running', 'Installed runtime did not start its gateway')
            require(state['processes']['gateway'] and state['processes']['portraits'], 'Installed runtime workers exited')
            manifest = json.loads((home / 'runtime-manifest.json').read_text('utf-8'))
            url = manifest['endpoints']['gateway']
            status, _, data = request(url, '/System/Info/Public')
            require(status == 200 and json.loads(data).get('Name') == 'Installed verification fixture',
                    'Installed gateway failed to forward to fake Jellyfin')
            require('/fixture-jf/System/Info/Public' in seen, 'Fake Jellyfin did not receive gateway request')
            status, response_headers, data = request(url, '/MediaCache/')
            require(status == 200 and b'<html' in data.lower(), 'Installed MediaCache static workbench missing')
            require('text/html' in response_headers.get('Content-Type', ''), 'MediaCache MIME type incorrect')
            secret_dir = home / 'runtime/secrets'
            require(not any(secret_dir.iterdir()), 'Runtime materialized plaintext credentials')
            copied_lua = home / 'runtime/data/mpv-shim/scripts/brightness-control.lua'
            require(copied_lua.is_file() and copied_lua.stat().st_size > 0, 'Runtime failed to copy installed Lua resource')
            # Capture the redirector's actual Python children before asking the
            # installed runtime to stop. PID reuse is checked in alive().
            records = child_records(records)
            runtime.stop()
            wait_for(lambda: not any(alive(row) for row in records), timeout=8)
            remaining = json.loads((home / 'runtime-processes.json').read_text('utf-8'))
            require(not remaining, 'Runtime kept live worker registry entries after stop')
            address = urllib.parse.urlsplit(url)
            with socket.socket() as probe:
                probe.settimeout(.3)
                require(probe.connect_ex(('127.0.0.1', address.port)) != 0, 'Gateway still listens after runtime.stop')
            return {'state': 'passed', 'gateway_forwarding': True, 'base_path_preserved': True,
                    'workbench_static': True, 'dpapi_only': True, 'lua_copy': True,
                    'owned_processes_verified': len(records), 'no_processes_after_stop': True}
    finally:
        try:
            if runtime is not None:
                with instance_context(home):
                    try: runtime.stop()
                    finally:
                        registry = home / 'runtime-processes.json'
                        if registry.is_file():
                            records.extend(json.loads(registry.read_text('utf-8')).values())
                        clean_processes(records)
                        for process in runtime.processes.values():
                            process.wait(timeout=5)
        finally:
            server.shutdown()
            server.server_close()


def installed_main(args):
    workspace = Path(args.inside).resolve()
    outside = workspace / 'outside-project'
    report = {'state': 'running', 'python': sys.version.split()[0], 'platform': sys.platform, 'checks': {}}
    try:
        require(os.name == 'nt', 'Installed verification requires Windows DPAPI')
        from personal_cinema.runtime_paths import instance_context
        with instance_context(workspace / 'resources-instance'):
            report['checks']['resources'] = resources_check(Path(args.forbidden_root).resolve())
        report['checks']['settings'] = settings_check(workspace, outside)
        report['checks']['runtime'] = runtime_check(workspace)
        report['state'] = 'passed'
    except Exception as error:
        report['state'] = 'failed'
        report['error_type'] = type(error).__name__
        report['error'] = str(error).replace(str(workspace), '$TEMP').replace(str(args.forbidden_root), '$SOURCE')[:800]
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    return 0 if report['state'] == 'passed' else 1


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path)
    parser.add_argument('--constraints', type=Path, default=project / 'requirements-windows.lock')
    parser.add_argument('--report', type=Path, default=project / 'dist/installed-verification.json')
    parser.add_argument('--inside', help=argparse.SUPPRESS)
    parser.add_argument('--forbidden-root', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        return installed_main(args)
    require(os.name == 'nt', 'This verification targets Windows x64')
    wheels = sorted((project / 'dist').glob('*.whl'), key=lambda p: p.stat().st_mtime, reverse=True)
    wheel = (args.wheel or (wheels[0] if wheels else Path('missing.whl'))).resolve()
    require(wheel.is_file() and wheel.suffix == '.whl', 'Select a freshly rebuilt wheel')
    constraints = args.constraints.resolve()
    require(constraints.is_file(), 'Dependency constraints file missing')
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with wheel.open('rb') as source:
        wheel_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    with tempfile.TemporaryDirectory(prefix='personal-cinema-installed-') as temporary:
        workspace = Path(temporary).resolve()
        require(workspace.parent == Path(tempfile.gettempdir()).resolve(), 'Unexpected temporary workspace')
        outside = workspace / 'outside-project'
        outside.mkdir()
        environment = clean_environment()
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        print('Creating fresh verification environment.', flush=True)
        result = run_bounded([sys.executable, '-I', '-m', 'venv', str(workspace / 'venv')],
                             cwd=outside, env=environment, creationflags=flags, timeout=120)
        require(result.returncode == 0, 'Could not create the isolated virtual environment')
        python = workspace / 'venv/Scripts/python.exe'
        print('Installing the built wheel using dependency constraints.', flush=True)
        with (workspace / 'pip-install.log').open('wb') as log:
            result = run_bounded([str(python), '-I', '-m', 'pip', '--isolated', 'install',
                '--disable-pip-version-check', '--index-url', 'https://pypi.org/simple', '-c', str(constraints), str(wheel)],
                stdout=log, stderr=log, cwd=outside, env=environment, creationflags=flags, timeout=600)
        if result.returncode:
            report = {'state': 'failed', 'stage': 'dependency-install', 'wheel': wheel.name, 'wheel_sha256': wheel_hash,
                      'error': 'Fresh-venv installation with pinned constraints failed'}
            report_path.write_text(json.dumps(report, indent=2), 'utf-8')
            print(json.dumps(report), flush=True)
            return 1
        runner = workspace / 'installed_runner.py'
        shutil.copyfile(__file__, runner)
        internal_report = workspace / 'installed-report.json'
        print('Checking installed resources, authenticated settings and real isolated workers.', flush=True)
        with (workspace / 'verification.log').open('wb') as log:
            result = run_bounded([str(python), '-I', str(runner), '--inside', str(workspace),
                '--forbidden-root', str(project), '--report', str(internal_report)], stdout=log, stderr=log,
                cwd=outside, env=environment, creationflags=flags, timeout=180)
        report = json.loads(internal_report.read_text('utf-8')) if internal_report.is_file() else {
            'state': 'failed', 'stage': 'installed-runner', 'error': 'Installed verifier did not produce a report'}
        report.update(wheel=wheel.name, wheel_sha256=wheel_hash, dependency_constraints=constraints.name,
                      fresh_virtual_environment=True, source_pythonpath_removed=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0 if result.returncode == 0 and report.get('state') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

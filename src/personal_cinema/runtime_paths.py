"""Portable addresses and DPAPI-backed compatibility for the existing workers.

Legacy modules use RuntimePath explicitly; no global pathlib or filesystem hooks.
Credential-shaped legacy paths are virtual and are never materialized as plaintext.
"""
import http.client
import json
import os
import socket
import ssl
import sys
import urllib.parse
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_INSTANCE = ContextVar('personal_cinema_instance', default=None)


@contextmanager
def instance_context(root):
    token = _INSTANCE.set(Path(root).resolve())
    try:
        yield
    finally:
        _INSTANCE.reset(token)


def application_home():
    if _INSTANCE.get() is not None:
        return _INSTANCE.get()
    return Path(os.environ.get('PERSONAL_CINEMA_HOME') or
                str(Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'PersonalCinema'))


def manifest():
    try:
        return json.loads((application_home() / 'runtime-manifest.json').read_text('utf-8'))
    except (OSError, ValueError):
        return {}


def root_path(suffix=''):
    root = _INSTANCE.get() / 'runtime' if _INSTANCE.get() is not None else Path(os.environ.get('MEDIA_LIBRARY_ROOT') or str(application_home() / 'runtime'))
    return str(root / suffix)


def python_path():
    return sys.executable


def endpoint(name):
    defaults = {'jellyfin': 'http://127.0.0.1:8096', 'openlist': 'http://127.0.0.1:5244',
                'gateway': 'http://127.0.0.1:18797', 'stream': 'http://127.0.0.1:18766'}
    return manifest().get('endpoints', {}).get(name, defaults[name]).rstrip('/')


def port(name):
    address = urllib.parse.urlsplit(endpoint(name))
    return address.port or (443 if address.scheme == 'https' else 80)


def settings():
    from .configuration import ConfigStore
    return ConfigStore(application_home()).get_effective('device')


def option(name, fallback=None):
    # ConfigStore performs schema validation on every writer. Old workers receive
    # their effective settings from the same saved revision as the management UI.
    return settings().get(name, fallback)


def cache_path(suffix=''):
    base = manifest().get('cache_directory') or str(Path(root_path()) / 'cache')
    return RuntimePath(base) / suffix


def tool_path(name):
    return manifest().get('tools', {}).get(name, '')


def resource_path(name):
    return Path(__file__).parent / 'legacy' / name


def proxy_handler():
    import urllib.request
    mode = option('network.mode', 'direct')
    if mode == 'direct':
        return urllib.request.ProxyHandler({})
    if mode == 'proxy':
        url = option('network.proxy_url', '')
        return urllib.request.ProxyHandler({'http': url, 'https': url})
    return urllib.request.ProxyHandler()


def launch_metadata_browser(playwright):
    mode = option('network.mode', 'direct')
    options = {'headless': True}
    if mode == 'direct':
        options['args'] = ['--no-proxy-server']
    elif mode == 'proxy':
        options['proxy'] = {'server': option('network.proxy_url', ''), 'bypass': 'localhost,127.0.0.1'}
    # Edge is normally installed on Windows; the optional Chromium runtime is
    # the documented fallback. No personal browser profile is ever opened.
    try:
        return playwright.chromium.launch(channel='msedge', **options)
    except Exception:
        return playwright.chromium.launch(**options)


def vault():
    from .credentials import SecretStore
    return SecretStore(application_home())


SECRET_FILES = {'stream-key.txt': 'stream.key', 'openlist-token.txt': 'openlist.token',
                'jellyfin-admin-token.txt': 'jellyfin.token', 'mdblist-api-key.txt': 'mdblist.api_key',
                'quark-web-cookie.json': 'quark.web_cookie'}


def secret_key(path):
    p = Path(path).absolute()
    managed = Path(root_path('secrets')).absolute()
    if p.parent != managed:
        return None
    mapped = {'jellyfin-admin-token.txt': 'foundation.jellyfin_credential',
              'openlist-token.txt': 'sources.openlist_credential', 'mdblist-api-key.txt': 'ratings.credential_ref'}
    if p.name in mapped:
        return option(mapped[p.name], SECRET_FILES[p.name])
    return SECRET_FILES.get(p.name) or ('legacy.accounts' if p.name == 'accounts.json' else None)


def secret_read(path):
    key = secret_key(path)
    if key is None:
        return None
    store = vault()
    if key == 'legacy.accounts':
        config = settings()
        user = config.get('foundation.jellyfin_username', '')
        account = {'username': user, 'password': store.get('jellyfin.password') or ''}
        return json.dumps({'jellyfin_admin': account, 'jellyfin': account,
                           'openlist': {'username': config.get('sources.openlist_username', ''),
                                        'password': store.get('openlist.password') or ''}})
    value = store.get(key)
    if value is None:
        raise FileNotFoundError('Required credential has not been configured')
    return value


def secret_write(path, value):
    key = secret_key(path)
    if key is None:
        return False
    if key == 'legacy.accounts':
        raise PermissionError('Account configuration must use the authenticated settings interface')
    vault().set(key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return True


class RuntimePath(type(Path())):
    def read_text(self, *args, **kwargs):
        value = secret_read(self)
        return value if value is not None else super().read_text(*args, **kwargs)

    def write_text(self, data, *args, **kwargs):
        if secret_write(self, data):
            return len(data)
        return super().write_text(data, *args, **kwargs)

    def exists(self, **kwargs):
        key = secret_key(self)
        if key is not None:
            return key == 'legacy.accounts' or bool(vault().get(key))
        return super().exists(**kwargs)

    def is_file(self, **kwargs):
        return self.exists() if secret_key(self) else super().is_file(**kwargs)


def jellyfin_connection(timeout=30):
    base = urllib.parse.urlsplit(endpoint('jellyfin'))
    cls = http.client.HTTPSConnection if base.scheme == 'https' else http.client.HTTPConnection
    connection = cls(base.hostname, base.port, timeout=timeout)
    original = connection.request
    prefix = base.path.rstrip('/')

    def request(method, url, *args, **kwargs):
        if not url.startswith('/') or url.startswith('//'):
            raise ValueError('Invalid Jellyfin relative request')
        return original(method, prefix + url, *args, **kwargs)
    connection.request = request
    return connection


def jellyfin_socket(timeout=15):
    base = urllib.parse.urlsplit(endpoint('jellyfin'))
    conn = socket.create_connection((base.hostname, base.port or (443 if base.scheme == 'https' else 80)), timeout)
    if base.scheme == 'https':
        conn = ssl.create_default_context().wrap_socket(conn, server_hostname=base.hostname)
    return conn

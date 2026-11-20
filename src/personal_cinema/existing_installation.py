"""Adopt an existing data directory into the installed LumaNest distribution.

Import does not start processes, move films, run old Python code, or remove old
credentials. The operator stops the previous supervisor before starting the new
distribution. Personal options and reports belong outside the source repository.
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import uuid
from urllib.parse import urlsplit

from .configuration import ConfigStore, _file_lock, _value
from .credentials import SecretStore
from .player_launcher import ProfileVault

GIB = 1024 ** 3


def _json(path, default=None):
    if not path.exists(): return {} if default is None else default
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('迁移输入不是受支持的普通配置文件：' + path.name)
    try: return json.loads(path.read_text('utf-8-sig'))
    except (OSError, ValueError):
        raise ValueError('无法读取迁移输入：' + path.name) from None


def _listener(path):
    """Read a literal old listener without importing or executing old code."""
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024: return None
    try:
        tree = ast.parse(path.read_text('utf-8-sig'))
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in ('ThreadingHTTPServer', 'HTTPServer') and call.args:
                address = call.args[0]
                if isinstance(address, ast.Tuple) and len(address.elts) == 2:
                    host = address.elts[0]
                    if isinstance(host, ast.Constant) and isinstance(host.value, str):
                        return host.value
    except (ValueError, OSError, SyntaxError): pass
    return None


class ExistingInstallation:
    def __init__(self, home, runtime_directory, *, credentials=None, options=None):
        self.home = Path(home).resolve()
        self.runtime = Path(runtime_directory).resolve()
        if not self.runtime.is_dir() or not (self.runtime / 'data').is_dir():
            raise ValueError('请选择包含 data 子目录的已有影院运行目录。')
        self.config = ConfigStore(self.home)
        self.credentials = credentials if credentials is not None else SecretStore(self.home)
        self.options = dict(options or {})

    def _collect(self):
        native = self.runtime / 'data/mpv-shim'
        conf = _json(native / 'conf.json')
        cache = _json(self.runtime / 'data/cache-config.json')
        metadata = _json(self.runtime / 'data/metadata-config.json')
        accounts = _json(self.runtime / 'secrets/accounts.json')
        if any(not isinstance(value, dict) for value in (conf, cache, metadata, accounts)):
            raise ValueError('已有运行配置结构无效，未迁移。')
        values = {'foundation.runtime_directory': str(self.runtime),
                  'foundation.mpv_backend': 'external' if conf.get('mpv_ext') else 'embedded',
                  'foundation.jellyfin_mode': 'existing', 'cache.directory': str(self.runtime / 'cache'),
                  'appearance.theme': 'lumanest' if conf.get('theme', 'cinema-night') == 'cinema-night' else conf.get('theme', 'lumanest'),
                  'appearance.poster_scale': conf.get('poster_scale') or 1.3,
                  'network.mode': 'direct'}
        for old, new in (('mpv_ext_path', 'foundation.mpv_path'), ('hwdec', 'playback.hwdec'),
                         ('hdr_output', 'playback.hdr_output'), ('ui_text_scale', 'appearance.text_scale')):
            if conf.get(old) is not None: values[new] = conf[old]
        for old, new in (('max_cache_bytes', 'cache.offline_gib'), ('reserve_free_bytes', 'cache.reserve_gib'),
                         ('playback_buffer_bytes', 'cache.playback_gib')):
            if old in cache: values[new] = math.ceil(int(cache[old]) / GIB)
        for old, new in (('next_episodes','cache.next_episodes'), ('downloads_paused','cache.downloads_paused'),
                         ('cleanup_only_on_pressure','cache.cleanup_only_on_pressure'), ('stream_base_url','network.stream_url')):
            if old in cache: values[new] = cache[old]
        for old, new in (('retention_seconds','cache.retain_days'), ('recent_playback_seconds','cache.recent_days')):
            if old in cache: values[new] = max(1, math.ceil(int(cache[old]) / 86400))
        if 'scan_interval_seconds' in cache:
            values['automation.scan_interval_minutes'] = max(5, math.ceil(int(cache['scan_interval_seconds']) / 60))
        if 'manual_ingestion_only' in cache:
            values['automation.mode'] = 'manual' if cache['manual_ingestion_only'] else 'scheduled'
        if metadata.get('search_languages'): values['metadata.languages'] = metadata['search_languages']
        mpv_config = native / 'mpv.conf'
        from .mpv_config import global_options
        for key, text in global_options(mpv_config).items():
            if key == 'brightness':
                try: values['playback.brightness'] = int(text)
                except ValueError: pass
            if key in ('alang', 'slang') and text:
                values['playback.audio_languages' if key == 'alang' else 'playback.subtitle_languages'] = list(dict.fromkeys(v.strip() for v in text.split(',') if v.strip()))
        roots = cache.get('source_roots') or ([cache['source_root']] if cache.get('source_root') else [])
        if not isinstance(roots, list) or any(not isinstance(p, str) for p in roots):
            raise ValueError('已有来源目录结构无效。')
        values['sources.entries'] = [{'id': 'imported-' + str(i + 1), 'name': path.rstrip('/').rsplit('/', 1)[-1] or '媒体来源',
            'kind': 'openlist', 'path': path, 'enabled': True, 'allow_write': False} for i, path in enumerate(dict.fromkeys(roots))]
        if roots: values['sources.openlist_mode'] = 'existing'
        for name, filename in (('gateway', 'metadata_proxy.py'), ('stream', 'cache_worker.py')):
            host = _listener(self.runtime / 'scripts' / filename)
            if host is not None: values['network.' + name + '_bind_host'] = host

        protected = {}
        files = {'jellyfin-admin-token.txt': 'jellyfin.token', 'openlist-token.txt': 'openlist.token',
                 'mdblist-api-key.txt': 'ratings.mdblist', 'stream-key.txt': 'stream.key',
                 'quark-web-cookie.json': 'quark.web_cookie'}
        for filename, key in files.items():
            path = self.runtime / 'secrets' / filename
            if path.is_file():
                if path.is_symlink() or path.stat().st_size > 65536:
                    raise ValueError('迁移凭据文件无效：' + filename)
                value = path.read_text('utf-8-sig').strip()
                if value: protected[key] = value
        for provider, alias in (('jellyfin', 'jellyfin_admin'), ('openlist', 'openlist')):
            account = accounts.get(alias) or accounts.get(provider) or {}
            if not isinstance(account, dict): raise ValueError('已有服务账户结构无效。')
            if account.get('username'): values['foundation.jellyfin_username' if provider == 'jellyfin' else 'sources.openlist_username'] = account['username']
            if account.get('password'): protected[provider + '.password'] = account['password']

        profiles = _json(native / 'users.json')
        if not isinstance(profiles, dict): raise ValueError('播放器用户记录无效。')
        if not profiles.get('users'):
            credentials = _json(native / 'cred.json', [])
            if isinstance(credentials, dict): credentials = list(credentials.values())
            if credentials:
                ident = str(uuid.uuid4())
                profiles = {'active': ident, 'users': [{'id': ident, 'name': '影院账户', 'default': True, 'credentials': credentials}]}
        if profiles.get('users'):
            users = profiles['users']
            if not isinstance(users, list) or not all(isinstance(u, dict) for u in users):
                raise ValueError('播放器用户记录无效。')
            active = next((u for u in users if u.get('id') == profiles.get('active')), users[0])
            credentials = active.get('credentials') or []
            credential = next((c for c in credentials if isinstance(c, dict) and c.get('AccessToken') and c.get('Id') and c.get('UserId')), None)
            if credential:
                address = credential.get('address') or credential.get('Address') or ''
                target = urlsplit(address)
                if target.hostname in ('localhost', '127.0.0.1') and target.scheme == 'http':
                    values['foundation.gateway_url'] = address.rstrip('/')
                protected.update({'player.jellyfin.token': credential['AccessToken'], 'player.jellyfin.user_id': credential['UserId'],
                                  'player.jellyfin.server_id': credential['Id']})
                if credential.get('username'): protected['player.jellyfin.username'] = credential['username']
                viewer = accounts.get('jellyfin') or {}
                if (isinstance(viewer, dict) and viewer.get('password') and credential.get('username')
                        and str(viewer.get('username', '')).casefold() == str(credential['username']).casefold()):
                    protected['player.jellyfin.password'] = viewer['password']
                protected.setdefault('jellyfin.token', credential['AccessToken'])
                protected['jellyfin.server_id'] = credential['Id']
                if protected['jellyfin.token'] == credential['AccessToken']:
                    protected['jellyfin.user_id'] = credential['UserId']
                values['foundation.player_credential'] = 'player.jellyfin.token'
                credential['PersonalCinemaCredential'] = 'player.jellyfin.token'
        values.update(self.options)
        values = {key: _value(key, value) for key, value in values.items()}
        # Validate the complete document as well as each value before any write.
        self.config._normalize({'values': values})
        for key, value in protected.items():
            if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 65536 or '\x00' in value:
                raise ValueError('已有凭据格式无效：' + key)
        return values, protected, profiles

    def preview(self):
        values, protected, profiles = self._collect()
        return {'state': 'preview', 'settings': values, 'credential_names': sorted(protected),
                'profiles': len(profiles.get('users', [])), 'starts_processes': False, 'moves_media': False,
                'notes': ['使用同一发行程序读取既有数据；启用前必须停止旧版本后台与监督进程。',
                          '来源整理权限默认只读；已明确指定的来源权限按导入选项保留。',
                          '原播放器账户文件保留；首次启动发行影院后会加密归档并清除明文副本。']}

    def apply(self, expected_revision=0):
        values, protected, profiles = self._collect()
        with _file_lock(self.config.lock_path), _file_lock(self.credentials.lock_path):
            existing = self.config._read()
            if existing['revision'] != expected_revision or existing['revision'] != 0 or self.config.path.exists() or self.credentials.status():
                raise ValueError('导入仅用于新的实例设置目录，不能覆盖已有配置或凭据。')
            original = self.credentials.path.read_bytes() if self.credentials.path.exists() else None
            try:
                for key, value in protected.items(): self.credentials.set(key, value)
                if profiles.get('users'):
                    ProfileVault(self.credentials, self.runtime / 'data/mpv-shim').write(profiles)
                record = self.config.update(values, expected_revision)
            except Exception:
                if original is None: self.credentials.path.unlink(missing_ok=True)
                else: self.credentials.path.write_bytes(original)
                raise
        return {'state': 'imported', 'revision': record['revision'], 'credential_count': len(protected),
                'profile_count': len(profiles.get('users', [])), 'started': False,
                'message': '设置与加密账户已导入；原数据保持原位。停止旧后台后再应用并启动本发行版。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Import existing cinema data into this distribution; preview by default')
    parser.add_argument('--home', required=True)
    parser.add_argument('--runtime-directory', required=True)
    parser.add_argument('--options', help='Local JSON object of explicit settings; never include secret values')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    options = _json(Path(args.options)) if args.options else {}
    importer = ExistingInstallation(args.home, args.runtime_directory, options=options)
    print(json.dumps(importer.apply() if args.apply else importer.preview(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

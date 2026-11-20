"""Materialize validated settings and run only this instance's worker processes."""
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

GIB = 1024 ** 3
WORKER_COMMANDS = {'gateway': ['metadata_proxy'], 'cache': ['cache_worker', 'run'],
                   'portraits': ['person_portrait_worker'], 'matcher': ['metadata_matcher', 'run'],
                   'credits': ['chinese_credits_worker', 'run']}


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text('utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def merge_json(path, updates, defaults=None):
    value = read(path)
    if not isinstance(value, dict):
        raise ValueError('已有运行配置不是有效对象，未覆盖：' + Path(path).name)
    for key, default in (defaults or {}).items():
        value.setdefault(key, default)
    value.update(updates)
    write(path, value)


def merge_mpv_options(path, options):
    """Update managed global keys; retain custom lines and named MPV profiles."""
    path = Path(path)
    lines = path.read_text('utf-8-sig').splitlines() if path.is_file() else []
    split = next((i for i, line in enumerate(lines) if line.strip().startswith('[')), len(lines))
    keys = {line.split('=', 1)[0] for line in options} | {'http-proxy'}
    global_lines = [line for line in lines[:split]
                    if line.lstrip().startswith(('#', ';')) or line.split('=', 1)[0].strip() not in keys]
    path.write_text('\n'.join(global_lines + options + lines[split:]) + '\n', 'utf-8')


class Runtime:
    def __init__(self, root, config, credentials, integrations):
        self.root = Path(root).resolve()
        values = config.get_effective('device')
        selected = read(self.root / 'runtime-manifest.json').get('runtime_directory') or values.get('foundation.runtime_directory')
        self.folder = Path(selected).resolve() if selected else self.root / 'runtime'
        self.config, self.credentials, self.integrations = config, credentials, integrations
        from .process_lock import LifecycleLock
        self.lock = LifecycleLock(self.root / '.runtime.lock')
        self.processes = {}

    def _alive(self, name):
        record = read(self.root / 'runtime-processes.json').get(name)
        if not record:
            return False
        try:
            import psutil
            process = psutil.Process(record['pid'])
            return (abs(process.create_time() - record['created']) < .01
                    and process.cmdline() == record['command'] and process.is_running())
        except Exception:
            return False

    def playing(self):
        path = self.folder / 'data/cache-status.json'
        if path.is_file() and time.time() - path.stat().st_mtime <= 120:
            status = read(path)
            if any(status.get(key) for key in ('playback_present', 'playback_exclusive', 'playback_buffering', 'playback_paused')):
                return True
        if not self._alive('gateway') and not self._alive('player'):
            return False
        # Local/Jellyfin-only libraries have no cache worker. Query the media
        # server too, retaining paused playback and failing closed on outages.
        try:
            import urllib.request
            from .network_security import credential_opener, jellyfin_authorization
            from .runtime_paths import instance_context, proxy_handler
            manifest = read(self.root / 'runtime-manifest.json')
            base = manifest.get('endpoints', {}).get('jellyfin')
            values = self.config.get_effective('device')
            token = self.credentials.get(values.get('foundation.jellyfin_credential', 'jellyfin.token'))
            if not base or not token:
                return True
            request = urllib.request.Request(base.rstrip('/') + '/Sessions', headers={'Authorization': jellyfin_authorization(token)})
            with instance_context(self.root):
                with credential_opener(proxy_handler()).open(request, timeout=2) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                return True
            sessions = json.loads(raw)
            if not isinstance(sessions, list):
                return True
            return any(row.get('NowPlayingItem') for row in sessions if isinstance(row, dict))
        except (OSError, ValueError):
            return True

    def status(self):
        receipt = read(self.root / 'runtime-applied.json')
        saved_revision = self.config.read()['revision']
        return {'state': 'running' if self._alive('gateway') else 'configured' if receipt else 'not_configured',
                'applied_revision': receipt.get('revision'), 'saved_revision': saved_revision,
                'pending': receipt.get('revision') != saved_revision,
                'playing': self.playing(),
                'processes': {name: self._alive(name) for name in (*WORKER_COMMANDS, 'player')},
                'recovery': read(self.root / 'runtime-recovery.json'),
                'message': '设置已生效于本实例运行配置；运行中的播放器在下次启动读取画面默认值。' if receipt else '请完成基础服务配置后应用。'}

    def _manifest(self, values, revision):
        old = read(self.root / 'runtime-manifest.json')
        endpoints = old.get('endpoints', {})
        endpoints.update(jellyfin=values['foundation.jellyfin_url'].rstrip('/'),
                         openlist=values['sources.openlist_url'].rstrip('/'))
        for name in ('gateway', 'stream'):
            selected = values.get('foundation.gateway_url' if name == 'gateway' else 'network.stream_url')
            if selected:
                endpoints[name] = selected.rstrip('/')
            elif name not in endpoints:
                endpoints[name] = f'http://127.0.0.1:{free_port()}'
        cache = values.get('cache.directory') or str(self.folder / 'cache')
        old_cache = old.get('cache_directory')
        if old_cache and os.path.normcase(old_cache) != os.path.normcase(cache):
            if any(Path(old_cache).glob('ready/**/*')) or any(Path(old_cache).glob('partial/*')) or any(Path(old_cache).glob('playback-buffer/**/*')):
                raise ValueError('旧缓存目录仍有文件，请先迁移或清理缓存后修改位置；现有缓存没有被删除')
        return {'version': 1, 'endpoints': endpoints, 'cache_directory': str(Path(cache).resolve()),
                'runtime_directory': str(self.folder),
                'bind_hosts': {name: values.get('network.' + name + '_bind_host', '127.0.0.1') for name in ('gateway', 'stream')},
                'tools': {'mpv': values.get('foundation.mpv_path', ''), 'ffprobe': values.get('foundation.ffprobe_path', '')},
                'revision': revision}

    def _require_unclaimed_listeners(self, values):
        """Do not silently attach to an old version or another instance."""
        endpoints = read(self.root / 'runtime-manifest.json').get('endpoints', {})
        for name, process, key in (('gateway', 'gateway', 'foundation.gateway_url'), ('stream', 'cache', 'network.stream_url')):
            if self._alive(process):
                continue
            address = values.get(key) or endpoints.get(name)
            if not address:
                continue
            host = values.get('network.' + name + '_bind_host', '127.0.0.1')
            if host == '0.0.0.0': host = '127.0.0.1'
            try:
                with socket.create_connection((host, urllib.parse.urlsplit(address).port), timeout=.2):
                    pass
            except OSError:
                continue
            raise ValueError('媒体接口端口已由其他进程占用；请先停止旧版本后台及其监督程序，不能同时运行两套后台。')

    def apply(self):
        with self.lock:
            if self.playing():
                write(self.root / 'runtime-pending.json', {'revision': self.config.read()['revision'], 'requested_at': time.time()})
                return {'state': 'waiting_for_playback', 'message': '影片正在播放、暂停或播放状态暂时无法确认；退出播放后后台会自动应用。'}
            snapshot = self.config.effective_snapshot('device')
            values, revision = snapshot['values'], snapshot['revision']
            self._require_unclaimed_listeners(values)
            selected = Path(values.get('foundation.runtime_directory') or self.root / 'runtime').resolve()
            if selected != self.folder:
                if (self.root / 'runtime-applied.json').exists() or any(self._alive(name) for name in (*WORKER_COMMANDS, 'player')):
                    raise ValueError('运行数据目录已应用；更换需要先完成独立迁移，现有数据未移动。')
                self.folder = selected
            manifest = self._manifest(values, revision)
            was_running = self._alive('gateway') or self._alive('cache')
            if was_running:
                self.stop()
            for name in ('data', 'data/requests', 'data/mpv-shim', 'data/mpv-shim/scripts', 'logs', 'catalog/Movies', 'catalog/Shows', 'secrets'):
                (self.folder / name).mkdir(parents=True, exist_ok=True)
            for name in ('ready', 'partial', 'playback', 'images'):
                (Path(manifest['cache_directory']) / name).mkdir(parents=True, exist_ok=True)
            if not self.credentials.get('stream.key'):
                self.credentials.set('stream.key', secrets.token_urlsafe(40))
            sources = [source for source in values.get('sources.entries', []) if source.get('enabled', True)]
            cloud_sources = [source['path'] for source in sources if source.get('kind') == 'openlist']
            mode = values.get('automation.mode', 'manual')
            cache = {'source_roots': cloud_sources or ['/media'], 'source_write_roots': [s['path'] for s in sources if s.get('kind') == 'openlist' and s.get('allow_write')], 'source_root': (cloud_sources or ['/media'])[0],
                     'sources_configured': bool(cloud_sources),
                     'max_cache_bytes': int(values.get('cache.offline_gib', 100) * GIB),
                     'reserve_free_bytes': int(values.get('cache.reserve_gib', 10) * GIB),
                     'playback_buffer_bytes': int(values.get('cache.playback_gib', 10) * GIB),
                     'retention_seconds': int(values.get('cache.retain_days', 30) * 86400),
                     'next_episodes': values.get('cache.next_episodes', 3),
                     'scan_interval_seconds': values.get('automation.scan_interval_minutes', 30) * 60,
                     'stream_base_url': manifest['endpoints']['stream'],
                     'recent_playback_seconds': values.get('cache.recent_days', 14) * 86400,
                     'cleanup_only_on_pressure': values.get('cache.cleanup_only_on_pressure', True),
                     'downloads_paused': values.get('cache.downloads_paused', False),
                     'cloud_normalization_enabled': bool(values.get('organizing.enabled') and any(s.get('allow_write') for s in sources)),
                     'organizing_preview_only': values.get('organizing.preview_only', True),
                     'cleanup_empty': values.get('organizing.cleanup_empty', False),
                     'manual_ingestion_only': mode != 'scheduled'}
            merge_json(self.folder / 'data/cache-config.json', cache, {'download_connections': 2, 'poll_seconds': 30,
                'smart_cache': True, 'pause_cache_for_steam': False, 'session_poll_seconds': 5})
            merge_json(self.folder / 'data/metadata-config.json', {'enabled': True, 'preferred_provider': values.get('metadata.search_provider', 'TheMovieDb'),
                    'search_languages': values.get('metadata.languages', ['zh-CN', 'en']), 'retry_seconds': [120, 600, 3600, 43200]})
            merge_json(self.folder / 'data/automation-config.json', {'enabled': True, 'automatic_versions': False,
                    'recycle_inferior_sources': False, 'manual_only': mode == 'manual', 'background_cache_yields_to_library': True})
            merge_json(self.folder / 'data/chinese-credits-config.json', {}, {'enabled': True, 'source_priority': ['baidu_baike', 'wikipedia'],
                    'max_cast': 30, 'source_urls': {}, 'source_titles': {}})
            merge_json(self.folder / 'data/task-center-config.json', {}, {'user_ids': [self.credentials.get('jellyfin.user_id')] if self.credentials.get('jellyfin.user_id') else []})
            current_mpv = read(self.folder / 'data/mpv-shim/conf.json')
            current_mpv.update({'mpv_ext': values.get('foundation.mpv_backend', 'embedded') == 'external', 'mpv_ext_path': values.get('foundation.mpv_path') or None,
                'mpv_ext_start': True, 'mpv_ext_ipc': self.ipc_name(), 'lang': current_mpv.get('lang') or 'zh_Hans',
                'check_updates': False, 'notify_updates': 'disabled',
                'hdr_output': values.get('playback.hdr_output', 'auto'), 'hwdec': values.get('playback.hwdec', 'auto-copy'),
                'ui_text_scale': values.get('appearance.text_scale', 1), 'poster_scale': values.get('appearance.poster_scale', 1.3),
                'theme': values.get('appearance.theme', 'lumanest')})
            write(self.folder / 'data/mpv-shim/conf.json', current_mpv)
            # Credential list is stored in the vault; the customized client reads it through the bridge.
            write(self.root / 'runtime-manifest.json', manifest)
            from .mpv_config import global_options
            current_options = global_options(self.folder / 'data/mpv-shim/mpv.conf')
            mpv_lines = [f'brightness={values.get("playback.brightness", 0)}', 'cache=yes',
                         'alang=' + ','.join(values.get('playback.audio_languages', ['zho','jpn','eng'])),
                         'slang=' + ','.join(values.get('playback.subtitle_languages', ['zho','eng']))]
            if 'demuxer-max-bytes' not in current_options:
                mpv_lines.append('demuxer-max-bytes=256MiB')
            if values.get('network.mode') == 'proxy':
                mpv_lines.append('http-proxy=' + values.get('network.proxy_url', ''))
            elif values.get('network.mode') == 'direct':
                mpv_lines.append('http-proxy=')
            merge_mpv_options(self.folder / 'data/mpv-shim/mpv.conf', mpv_lines)
            resource = Path(__file__).parent / 'resources/brightness-control.lua'
            shutil.copyfile(resource, self.folder / 'data/mpv-shim/scripts/brightness-control.lua')
            write(self.root / 'runtime-applied.json', {'revision': revision, 'applied_at': time.time()})
            (self.root / 'runtime-pending.json').unlink(missing_ok=True)
            if was_running:
                self.start(apply=False)
            pending = self.config.read()['revision'] != revision
            return {'state': 'applied', 'revision': revision, 'pending': pending,
                    'message': '当前快照已写入；另有新保存的设置等待应用。' if pending else '设置已写入本实例；播放器画面默认值在下次启动时生效。'}

    def ipc_name(self):
        ident = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        return '\\\\.\\pipe\\personal-cinema-' + ident if os.name == 'nt' else str(self.root / 'mpv.sock')

    def environment(self):
        env = dict(os.environ)
        env.update(PERSONAL_CINEMA_HOME=str(self.root), MEDIA_LIBRARY_ROOT=str(self.folder))
        src = Path(__file__).resolve().parents[1]
        project = src.parent
        paths = [str(src), str(project / 'vendor'), str(Path(__file__).parent / 'legacy')]
        env['PYTHONPATH'] = os.pathsep.join(paths)
        values = self.config.get_effective('device')
        if values.get('foundation.mpv_backend', 'embedded') == 'embedded':
            from .embedded_mpv import library_path, extend_environment
            extend_environment(env, library_path(values))
        mode = values.get('network.mode', 'direct')
        if mode == 'direct':
            for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
                env.pop(key, None)
            env['NO_PROXY'] = '*'
        elif mode == 'proxy':
            env['HTTP_PROXY'] = env['HTTPS_PROXY'] = values.get('network.proxy_url', '')
            env['NO_PROXY'] = 'localhost,127.0.0.1,::1'
        return env

    def _spawn(self, name, command):
        if self._alive(name):
            return
        import psutil
        log = (self.folder / 'logs' / f'{name}.log').open('ab')
        try:
            process = subprocess.Popen(command, cwd=self.folder, env=self.environment(), stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        finally:
            log.close()
        self.processes[name] = process
        registry = read(self.root / 'runtime-processes.json')
        child = psutil.Process(process.pid)
        registry[name] = {'pid': process.pid, 'created': child.create_time(), 'command': child.cmdline()}
        write(self.root / 'runtime-processes.json', registry)

    def start(self, apply=True):
        with self.lock:
            self._require_unclaimed_listeners(self.config.get_effective('device'))
            if apply and self.status()['pending']:
                result = self.apply()
                if result['state'] != 'applied':
                    return result
            if not self.credentials.get(self.config.get_effective().get('foundation.jellyfin_credential', 'jellyfin.token')):
                raise ValueError('请先完成 Jellyfin 登录授权')
            values = self.config.get_effective('device')
            sources = [s for s in values.get('sources.entries', []) if s.get('enabled', True) and s.get('kind') == 'openlist']
            if sources:
                if values.get('sources.openlist_mode') == 'disabled':
                    raise ValueError('请先启用并配置 OpenList，或停用网盘来源')
                if not self.credentials.get(values.get('sources.openlist_credential', 'openlist.token')):
                    raise ValueError('网盘来源需要先完成 OpenList 授权')
            if self.integrations is None:
                from .integrations import IntegrationManager
                self.integrations = IntegrationManager(self.root, self.config, self.credentials)
            for provider, key in (('jellyfin', 'foundation.jellyfin_mode'), ('openlist', 'sources.openlist_mode')):
                if values.get(key) == 'managed' and (provider == 'jellyfin' or sources):
                    self.integrations.perform(provider, 'start', {})
            desired = ['gateway', 'portraits'] + (['cache'] if sources else [])
            if sources and values.get('automation.mode') == 'scheduled':
                desired += ['matcher', 'credits']
            for name in desired:
                self._spawn(name, self.worker_command(name))
            write(self.root / 'runtime-recovery.json', {})
            write(self.root / 'runtime-desired.json', {'enabled': True, 'workers': desired})
            deadline = time.monotonic() + 12
            address = urllib.parse.urlsplit(read(self.root / 'runtime-manifest.json')['endpoints']['gateway'])
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', address.port), timeout=.3):
                        break
                except OSError:
                    if time.monotonic() >= deadline or not self._alive('gateway'):
                        raise ValueError('媒体接口未成功启动，请查看本实例运行日志')
                    time.sleep(.15)
            if values.get('automation.mode') == 'startup' and sources:
                self.task_command('scan-new', {'request_id': 'startup-' + str(int(time.time()))})
            return self.status()

    def stop(self):
        with self.lock:
            if self.playing():
                raise ValueError('影片仍在播放或暂停，请退出播放后停止后台')
            write(self.root / 'runtime-desired.json', {'enabled': False, 'workers': []})
            registry = read(self.root / 'runtime-processes.json')
            from .process_control import terminate_owned_tree
            for name, record in list(registry.items()):
                if name == 'player' or not self._alive(name):
                    continue
                terminate_owned_tree(record['pid'], lambda: self._alive(name))
                registry.pop(name, None)
            write(self.root / 'runtime-processes.json', registry)
            return self.status()

    def worker_command(self, name):
        arguments = list(WORKER_COMMANDS[name])
        if name == 'portraits':
            arguments += ['--root', str(self.folder)]
        return [sys.executable, '-m', 'personal_cinema.worker', *arguments]

    def supervise(self, now=None):
        """Bounded restart of this instance's requested workers, never the player."""
        now = time.time() if now is None else now
        with self.lock:
            desired = read(self.root / 'runtime-desired.json')
            if not desired.get('enabled'):
                return
            retries = self.config.get_effective().get('automation.retry_limit', 3)
            recovery = read(self.root / 'runtime-recovery.json')
            for name in desired.get('workers', []):
                if name not in WORKER_COMMANDS:
                    continue
                row = recovery.get(name, {})
                if self._alive(name):
                    if row.get('attempts') and now - row.get('last_attempt', now) > 300:
                        recovery.pop(name, None)
                    continue
                if row.get('attempts', 0) >= retries or row.get('next_attempt', 0) > now:
                    continue
                attempts = row.get('attempts', 0) + 1
                recovery[name] = {'attempts': attempts, 'last_attempt': now,
                                  'next_attempt': now + min(300, 10 * 2 ** attempts), 'state': 'retrying'}
                try:
                    self._spawn(name, self.worker_command(name))
                except (OSError, ValueError):
                    recovery[name]['state'] = 'failed'
            write(self.root / 'runtime-recovery.json', recovery)

    def tick(self):
        applied = None
        if (self.root / 'runtime-pending.json').exists() and not self.playing():
            try:
                applied = self.apply()
            except (OSError, ValueError) as error:
                write(self.root / 'runtime-apply-error.json', {'state': 'failed', 'error_type': type(error).__name__})
                (self.root / 'runtime-pending.json').unlink(missing_ok=True)
        self.supervise()
        return applied

    def launch_player(self):
        with self.lock:
            return self._launch_player()

    def _launch_player(self):
        values = self.config.get_effective('device')
        if values.get('foundation.mpv_backend', 'embedded') == 'embedded':
            from .embedded_mpv import library_path
            if not library_path(values):
                raise ValueError('内置播放运行库尚未安装；请安装随发行包提供的播放器运行库，或选择已有 libMPV。')
        elif not Path(values.get('foundation.mpv_path') or '').is_file():
            raise ValueError('请先选择并检测外置 MPV 程序')
        self.start()
        self._spawn('player', [sys.executable, '-m', 'personal_cinema.player_launcher', '--config', str(self.folder / 'data/mpv-shim')])
        return {'state': 'starting', 'message': '正在启动本实例影院'}

    def complete_setup(self):
        readiness = self.integrations.validate_setup()
        if readiness.get('state') != 'ready':
            raise ValueError('；'.join(row.get('detail', '') for row in readiness.get('missing', [])) or '服务与来源尚未就绪')
        states = []
        for provider in ('jellyfin', 'mpv'):
            result = self.integrations.perform(provider, 'probe', {})
            states.append(result)
        if any(row.get('state') not in ('ready', 'available', 'connected') for row in states):
            raise ValueError('基础服务尚未通过检测，请先完成 Jellyfin 与 MPV 配置')
        if not self.credentials.get(self.config.get_effective().get('foundation.jellyfin_credential', 'jellyfin.token')):
            raise ValueError('请先完成 Jellyfin 登录')
        applied = self.apply()
        if applied['state'] != 'applied':
            return applied
        write(self.root / 'setup-complete.json', {'version': 1, 'completed_at': time.time()})
        return {'state': 'complete', 'message': '基础配置完成，可以打开影院；资料来源可继续在设置中完善。'}

    def organizing_command(self, action, payload):
        if action not in ('plan', 'apply'):
            raise ValueError('不支持此整理操作')
        if not self._alive('cache'):
            raise ValueError('请先启用媒体来源并启动后台')
        from .worker import prepare
        from .runtime_paths import RuntimePath
        prepare()
        import source_policy
        root = RuntimePath(self.folder)
        entry = source_policy.selected(root, payload.get('source_id'))
        if action == 'apply':
            source_policy.approve_plan(root, entry['id'], payload.get('plan_id'))
        ident = secrets.token_hex(16)
        record = {'action': 'organizing-' + action, 'source_id': entry['id'], 'plan_id': payload.get('plan_id'),
                  'request_id': ident, 'state': 'queued', 'queued_at': time.time(), 'requested_by': 'settings'}
        write(self.folder / 'data/task-receipts' / (ident + '.json'), record)
        write(self.folder / 'data/requests' / ('organizing-' + ident + '.json'), record)
        return {'id': ident, 'state': 'queued', 'message': '整理操作已进入后台队列，请刷新计划查看结果。'}

    def tasks(self):
        events = [read(p) for p in (self.folder / 'data/task-events').glob('*.json')]
        actions = [{'id': 'scan-new', 'label': '扫描新增'}, {'id': 'scan', 'label': '全库检查'}, {'id': 'retry-pending', 'label': '重试未完成'}] if self._alive('cache') else []
        manifest = read(self.root / 'runtime-manifest.json')
        return {'items': events, 'actions': actions,
                'workbench_url': manifest.get('endpoints', {}).get('gateway', '') + '/MediaCache/' if self._alive('gateway') else None}

    def task_command(self, action, payload):
        if action not in ('scan-new', 'scan', 'retry-pending'):
            raise ValueError('不支持此任务')
        if not self._alive('cache'):
            raise ValueError('请先配置媒体来源并启动后台')
        ident = payload.get('request_id') or secrets.token_hex(16)
        if not isinstance(ident, str) or not __import__('re').fullmatch(r'[A-Za-z0-9_-]{8,100}', ident):
            raise ValueError('任务请求编号无效')
        request = self.folder / 'data/requests' / f'settings-{ident}.json'
        receipt = self.folder / 'data/task-receipts' / f'{ident}.json'
        prior = read(receipt)
        if prior and prior.get('requested_action', action) != action:
            raise ValueError('请求编号已用于另一项操作')
        if not prior:
            record = {'action': 'scan-new' if action in ('scan-new', 'retry-pending') else 'scan', 'request_id': ident,
                      'queued_at': time.time(), 'state': 'queued', 'requested_by': 'settings', 'requested_action': action}
            write(receipt, record)
            write(request, record)
        return {'id': ident, 'state': read(receipt).get('state', 'queued')}

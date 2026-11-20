"""Opt-in, checksum-pinned portable components; never run downloaded installers."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from .service_manager import atomic_json, read_json

MAX_DOWNLOAD = 768 * 1024 * 1024
MAX_EXTRACTED = 3 * 1024 * 1024 * 1024
MAX_FILE = 768 * 1024 * 1024
MAX_FILES = 30000
COMPONENTS = {
    'jellyfin': {'key': 'foundation.jellyfin_path', 'exe': 'jellyfin.exe', 'scope': 'global',
                 'homepage': 'https://jellyfin.org/downloads/windows/'},
    'openlist': {'key': 'sources.openlist_path', 'exe': 'openlist.exe', 'scope': 'global',
                 'homepage': 'https://github.com/OpenListTeam/OpenList/releases'},
    'mpv': {'key': 'foundation.mpv_path', 'exe': 'mpv.exe', 'scope': 'device',
            'homepage': 'https://mpv.io/installation/'},
    'ffprobe': {'key': 'foundation.ffprobe_path', 'exe': 'ffprobe.exe', 'scope': 'global',
                'homepage': 'https://ffmpeg.org/download.html'},
}
GITHUB_REPOS = {
    'jellyfin': ('jellyfin/jellyfin',),
    'openlist': ('OpenListTeam/OpenList',),
    'mpv': ('mpv-player/mpv', 'shinchiro/mpv-winbuild-cmake', 'zhongfly/mpv-winbuild'),
    'ffprobe': ('BtbN/FFmpeg-Builds', 'jellyfin/jellyfin-ffmpeg'),
}


def validate_download_url(component, url, *, redirect=False):
    if component not in COMPONENTS or not isinstance(url, str) or len(url) > 16384:
        raise ValueError('下载来源无效')
    value = urllib.parse.urlsplit(url)
    if (value.scheme != 'https' or not value.hostname or value.username or value.password or value.fragment
            or any(ord(ch) < 33 for ch in url) or '\\' in url):
        raise ValueError('组件下载只接受不含账号的 HTTPS 地址')
    try:
        if value.port not in (None, 443): raise ValueError('下载端口必须为 HTTPS 默认端口')
    except ValueError:
        raise ValueError('下载地址端口无效') from None
    path = urllib.parse.unquote(value.path)
    if any(part in ('.', '..') for part in path.split('/')) or '\\' in path or '\x00' in path:
        raise ValueError('下载路径包含非法跳转')
    host = value.hostname.lower()
    if redirect and host in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
        if path.startswith('/github-production-release-asset/'): return url
        raise ValueError('下载重定向不是受支持的发行附件')
    if value.query: raise ValueError('下载地址不能包含查询参数或凭据')
    if not path.lower().endswith('.zip'):
        raise ValueError('请选择 Windows 便携 ZIP 包；暂不执行 EXE/MSI 安装器或解压 7z')
    if any(part.lower() in ('latest', 'latest-stable') for part in path.split('/')):
        raise ValueError('请选择固定版本附件，不能使用 latest 地址')
    allowed = False
    if host == 'github.com':
        allowed = any(re.fullmatch('/' + re.escape(repo) + r'/releases/download/[^/]+/[^/]+', path)
                      for repo in GITHUB_REPOS[component])
    if component == 'jellyfin' and host == 'repo.jellyfin.org':
        allowed = path.startswith(('/files/server/windows/', '/releases/server/windows/'))
    if component == 'ffprobe' and host in ('www.gyan.dev', 'gyan.dev'):
        allowed = path.startswith('/ffmpeg/builds/packages/')
    if not allowed:
        raise ValueError('下载链接不属于该组件的官方或官网推荐构建来源')
    return url


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, component): self.component = component
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        try:
            validate_download_url(self.component, newurl, redirect=True)
        except ValueError:
            fp.close()
            raise
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or '\\' in value or value.startswith('/'):
        raise ValueError('压缩包包含不安全路径')
    parts = value.rstrip('/').split('/')
    reserved = re.compile(r'^(con|prn|aux|nul|clock\$|com[1-9]|lpt[1-9])(?:\.|$)', re.I)
    if any(not part or part in ('.', '..') or ':' in part or part[-1:] in ('.', ' ')
           or reserved.match(part) or any(ord(ch) < 32 for ch in part) for part in parts):
        raise ValueError('压缩包包含不安全路径')
    return Path(*parts)


def _file_hash(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


class ComponentInstaller:
    def __init__(self, root, config_store):
        self.root = Path(root).resolve()
        self.config = config_store
        self.lock = threading.RLock()

    def _directory(self, *parts):
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('组件目录不能跳转到实例目录之外')
        return path

    def _current_path(self, component):
        return self.config.get_effective('device').get(COMPONENTS[component]['key'], '')

    def plan(self, component, payload):
        if component not in COMPONENTS or not isinstance(payload, dict): raise ValueError('不支持的组件')
        url = validate_download_url(component, payload.get('url'))
        digest, version = payload.get('sha256'), payload.get('version')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest):
            raise ValueError('必须填写上游发行附件对应的 64 位 SHA256')
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', version) or version.lower() in ('latest', 'main', 'master'):
            raise ValueError('请输入固定版本或构建日期')
        executable = payload.get('executable', '')
        if executable:
            executable = _relative_path(executable).as_posix()
            if Path(executable).name.lower() != COMPONENTS[component]['exe']:
                raise ValueError('压缩包中的程序文件名与组件不符')
        digest = digest.lower()
        destination = self._directory('components', component, version + '-' + digest[:12])
        plan = {'plan_id': uuid.uuid4().hex, 'component': component, 'state': 'planned',
                'created_at': time.time(), 'download': {'url': url, 'sha256': digest}, 'version': version,
                'destination': str(destination), 'executable': executable, 'previous_path': self._current_path(component),
                'changes': ['从官方或官网推荐来源下载固定 ZIP，并验证 SHA256',
                            '解压到本实例组件目录，保留其他版本和已有安装',
                            '写入组件路径；不执行下载的程序、不安装系统服务、不自动启动'],
                'source_page': COMPONENTS[component]['homepage']}
        atomic_json(self._directory('services', 'install-plans', plan['plan_id'] + '.json'), plan)
        return plan

    def _download(self, url, destination):
        # The provider is selected from the persisted, previously validated plan.
        component = self._active_component
        values = self.config.get_effective('device')
        mode = values.get('network.mode', 'direct')
        if mode == 'direct': proxies = {}
        elif mode == 'proxy':
            proxy = values.get('network.proxy_url', '')
            parsed = urllib.parse.urlsplit(proxy)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('代理地址无效')
            proxies = {'http': proxy, 'https': proxy}
        else: proxies = None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _ReleaseRedirect(component))
        request = urllib.request.Request(url, headers={'User-Agent': 'PersonalCinema-ComponentSetup/0.1', 'Accept-Encoding': 'identity'})
        deadline = time.monotonic() + 600
        try:
            with opener.open(request, timeout=30) as response, destination.open('xb') as output:
                validate_download_url(component, response.url, redirect=True)
                length = response.headers.get('Content-Length')
                if length and (not length.isdigit() or int(length) > MAX_DOWNLOAD): raise ValueError('组件下载大小超出限制')
                total = 0
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_DOWNLOAD or time.monotonic() > deadline: raise ValueError('组件下载超出大小或时间限制')
                    output.write(block)
        except urllib.error.HTTPError as error:
            error.close()
            raise ValueError('组件下载失败，请检查固定版本链接与网络设置') from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise ValueError('组件下载中断，请检查网络后重新应用计划') from None

    def _extract(self, archive, staging):
        try:
            with zipfile.ZipFile(archive) as package:
                entries = package.infolist()
                if len(entries) > MAX_FILES: raise ValueError('压缩包文件数量超出限制')
                total, seen = 0, set()
                for info in entries:
                    relative = _relative_path(info.filename)
                    key = relative.as_posix().casefold()
                    if key in seen: raise ValueError('压缩包含大小写冲突或重复路径')
                    seen.add(key)
                    mode = info.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.external_attr & 0x400:
                        raise ValueError('压缩包不能含链接或特殊文件')
                    if info.flag_bits & 1: raise ValueError('不接受加密压缩包')
                    total += info.file_size
                    if info.file_size > MAX_FILE or total > MAX_EXTRACTED or info.file_size > max(info.compress_size * 1000, 1024 * 1024):
                        raise ValueError('压缩包解压大小或压缩比超出限制')
                if shutil.disk_usage(staging).free < total + 64 * 1024 * 1024: raise ValueError('组件目录磁盘空间不足')
                written = 0
                for info in entries:
                    target = staging / _relative_path(info.filename)
                    if not target.resolve().is_relative_to(staging.resolve()): raise ValueError('压缩包路径越界')
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    count = 0
                    with package.open(info) as source, target.open('xb') as output:
                        while block := source.read(1024 * 1024):
                            count += len(block)
                            written += len(block)
                            if count > info.file_size or written > MAX_EXTRACTED: raise ValueError('压缩包实际大小与声明不符')
                            output.write(block)
                    if count != info.file_size: raise ValueError('压缩包文件不完整')
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
            raise ValueError('ZIP 文件无效、损坏或采用不支持的压缩方法') from None

    def apply(self, component, payload):
        if component not in COMPONENTS or not isinstance(payload, dict): raise ValueError('不支持的组件')
        plan_id = payload.get('plan_id')
        if not isinstance(plan_id, str) or not re.fullmatch('[0-9a-f]{32}', plan_id): raise ValueError('安装计划编号无效')
        path = self._directory('services', 'install-plans', plan_id + '.json')
        with self.lock:
            record = read_json(path, {})
            if record.get('component') != component: raise ValueError('安装计划不存在或组件不符')
            if record.get('state') == 'complete': return record['result']
            if time.time() - record.get('created_at', 0) > 1800: raise ValueError('安装计划已过期，请重新预览')
            if self._current_path(component) != record['previous_path']: raise ValueError('组件路径已改变，请重新生成安装计划')
            download = record['download']
            validate_download_url(component, download['url'])
            destination = self._directory('components', component, record['version'] + '-' + download['sha256'][:12])
            receipt_path = destination / '.personal-cinema-install.json'
            staging = self._directory('components', '.staging-' + plan_id)
            archive_path = self._directory('components', '.download-' + plan_id + '.zip')
            if staging.exists() or archive_path.exists():
                # Only transient paths for this validated plan, never the final component directory.
                if staging.exists(): shutil.rmtree(staging)
                if archive_path.exists(): archive_path.unlink()
            record['state'] = 'applying'
            atomic_json(path, record)
            try:
                if destination.exists():
                    receipt = read_json(receipt_path, {})
                    if receipt.get('component') != component or receipt.get('sha256') != download['sha256']:
                        raise ValueError('目标组件目录不是本程序对应版本，不能覆盖')
                    executable = destination / _relative_path(receipt['executable'])
                    if not executable.is_file(): raise ValueError('已安装组件不完整，请使用新的安装版本目录')
                    if _file_hash(executable) != receipt.get('executable_sha256'):
                        raise ValueError('已安装程序内容已改变，不能直接复用')
                else:
                    staging.mkdir(parents=True)
                    self._active_component = component
                    self._download(download['url'], archive_path)
                    if archive_path.stat().st_size > MAX_DOWNLOAD: raise ValueError('组件下载大小超出限制')
                    digest = _file_hash(archive_path)
                    if digest != download['sha256']: raise ValueError('SHA256 校验不一致，已拒绝安装')
                    self._extract(archive_path, staging)
                    if record.get('executable'):
                        candidate = staging / _relative_path(record['executable'])
                        candidates = [candidate] if candidate.is_file() else []
                    else:
                        candidates = [p for p in staging.rglob('*') if p.is_file() and p.name.lower() == COMPONENTS[component]['exe']]
                    if len(candidates) != 1: raise ValueError('ZIP 中未找到唯一的组件程序，请指定压缩包内程序路径')
                    relative = candidates[0].relative_to(staging).as_posix()
                    with candidates[0].open('rb') as source:
                        if source.read(2) != b'MZ': raise ValueError('压缩包中的组件不是 Windows 可执行文件')
                    receipt = {'component': component, 'version': record['version'], 'sha256': digest,
                               'executable': relative, 'executable_sha256': _file_hash(candidates[0]),
                               'installed_at': time.time(), 'source': download['url']}
                    atomic_json(staging / '.personal-cinema-install.json', receipt)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    staging.rename(destination)
                    executable = destination / relative
                current = self.config.read()
                self.config.update({COMPONENTS[component]['key']: str(executable)}, current['revision'], scope=COMPONENTS[component]['scope'])
                result = {'state': 'complete', 'component': component, 'version': record['version'],
                          'path': str(executable), 'sha256': download['sha256'], 'started': False,
                          'detail': '组件已安装并填写路径；请检测后继续配置服务'}
                record.update(state='complete', result=result)
                atomic_json(path, record)
                return result
            except (ValueError, OSError, KeyError):
                record['state'] = 'failed'
                atomic_json(path, record)
                raise
            finally:
                if staging.exists() and staging.resolve().is_relative_to(self.root): shutil.rmtree(staging)
                if archive_path.exists() and archive_path.resolve().is_relative_to(self.root): archive_path.unlink()

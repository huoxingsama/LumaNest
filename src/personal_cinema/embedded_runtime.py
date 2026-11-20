"""Install an isolated, checksum-pinned libmpv runtime into a Python prefix.

Only the selected DLL members are copied; no downloaded installer is run.
Validation happens in a short hidden subprocess before replacing an existing
LumaNest-owned player directory. Importing this module has no side effects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

CORE_VERSION = '20261017-git-dde0671072'
CORE_URL = 'https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/20261017/mpv-dev-x86_64-20261017-git-dde0671072.7z'
CORE_SHA256 = 'd6638b2b40561e6071627f39ad60a38e15721dc87a49f244b897911ea2edfd9f'
# The published support bundle is separately pinned; it never contains libmpv.
SUPPORT_URL = 'https://github.com/huoxingsama/LumaNest/releases/download/v0.1.0a1/lumanest-player-support-1-win-x64.zip'
SUPPORT_SHA256 = '3e4493277990362e27a93064338cc0be9609b36383b1d9fea1037d9c940b175a'
ALLOWED_DLLS = frozenset({'mpv-2.dll', 'vulkan-1.dll', 'fribidi-0.dll'})
ALLOWED_SUPPORT_FILES = frozenset({'sources/fribidi-1.0.16.tar.xz',
    'licenses/FriBiDi-COPYING.txt', 'licenses/Vulkan-Loader-LICENSE.txt',
    'NOTICE.txt', 'support-manifest.json'})
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXTRACTED = 768 * 1024 * 1024
MAX_MANIFEST = 128 * 1024


def _sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require_hash(path, expected):
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('运行组件缺少固定 SHA256')
    if _sha256(path) != expected:
        raise ValueError('运行组件 SHA256 不匹配，未安装')


def validate_url(url, *, redirect=False):
    if not isinstance(url, str) or len(url) > 16384:
        raise ValueError('运行组件下载地址无效')
    value = urllib.parse.urlsplit(url)
    if (value.scheme != 'https' or value.username or value.password or value.fragment
            or value.port not in (None, 443) or any(ord(ch) < 33 for ch in url) or '\\' in url):
        raise ValueError('运行组件只允许固定 HTTPS 下载地址')
    if url == CORE_URL or (SUPPORT_URL and url == SUPPORT_URL):
        return
    if (redirect and value.hostname in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com')
            and value.path.startswith('/github-production-release-asset/')
            and '..' not in urllib.parse.unquote(value.path).split('/')):
        return
    raise ValueError('运行组件下载来源不在允许列表')


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        try:
            validate_url(newurl, redirect=True)
        except ValueError:
            fp.close()
            raise
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download(url, destination, digest):
    validate_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'LumaNest-RuntimeSetup/0.1', 'Accept-Encoding': 'identity'})
    deadline = time.monotonic() + 600
    with opener.open(request, timeout=20) as response, Path(destination).open('xb') as output:
        validate_url(response.url, redirect=True)
        total = 0
        length = response.headers.get('Content-Length')
        if length and (not length.isdigit() or int(length) > MAX_DOWNLOAD):
            raise ValueError('运行组件下载大小超出限制')
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > MAX_DOWNLOAD or time.monotonic() > deadline:
                raise ValueError('运行组件下载超出大小或时间限制')
            output.write(block)
    require_hash(destination, digest)


def _hidden():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def extract_core(archive, directory):
    # Reading one exact member to stdout avoids archive paths, links, attributes
    # and arbitrary extraction destinations entirely.
    tar = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/tar.exe'
    if not tar.is_file():
        raise RuntimeError('Windows tar.exe 不可用；可使用 --from-directory 离线安装')
    listing = subprocess.run([str(tar), '-tf', str(archive)], capture_output=True,
                             timeout=30, check=True, **_hidden())
    if len(listing.stdout) > 1024 * 1024:
        raise ValueError('运行组件压缩包目录超出限制')
    names = listing.stdout.decode('utf-8').splitlines()
    if names.count('libmpv-2.dll') != 1:
        raise ValueError('固定发行包中找不到唯一 libmpv-2.dll')
    target = Path(directory) / 'mpv-2.dll'
    with target.open('xb') as output:
        subprocess.run([str(tar), '-xOf', str(archive), 'libmpv-2.dll'], stdout=output,
                       stderr=subprocess.PIPE, timeout=120, check=True, **_hidden())
    if target.stat().st_size > MAX_EXTRACTED:
        raise ValueError('运行组件展开大小超出限制')


def extract_support(archive, directory):
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        seen, total = set(), 0
        if len(members) > 32:
            raise ValueError('支持组件文件数量超出限制')
        for member in members:
            name = member.filename
            if (name not in ALLOWED_DLLS - {'mpv-2.dll'} and name not in ALLOWED_SUPPORT_FILES
                    or name.casefold() in seen or member.is_dir()
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError('支持组件包含未允许的文件或路径')
            seen.add(name.casefold())
            total += member.file_size
            if member.file_size < 0 or total > MAX_EXTRACTED:
                raise ValueError('支持组件展开大小超出限制')
        for member in members:
            target = Path(directory) / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, target.open('xb') as output:
                copied = 0
                while block := source.read(1024 * 1024):
                    copied += len(block)
                    if copied > member.file_size:
                        raise ValueError('支持组件实际大小与清单不符')
                    output.write(block)


def require_x64(path):
    with Path(path).open('rb') as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b'MZ':
            raise ValueError('运行组件不是 Windows DLL')
        offset = struct.unpack_from('<I', header, 0x3c)[0]
        if offset < 64 or offset > 1024 * 1024:
            raise ValueError('运行组件 PE 头无效')
        stream.seek(offset)
        pe = stream.read(6)
    if len(pe) != 6 or pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] != 0x8664:
        raise ValueError('运行组件必须为 Windows x86_64')


def verify_library(directory):
    if os.name != 'nt' or struct.calcsize('P') != 8:
        raise RuntimeError('内置播放器需要 Windows 64 位 Python')
    script = """import ctypes,json,os,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve()
with os.add_dll_directory(str(root)):
    library=ctypes.CDLL(str(root/'mpv-2.dll'))
    library.mpv_client_api_version.restype=ctypes.c_ulonglong
    version=int(library.mpv_client_api_version())
    if version<=0: raise RuntimeError('invalid libmpv API')
    library.mpv_create.restype=ctypes.c_void_p
    library.mpv_set_option_string.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_char_p]
    library.mpv_set_option_string.restype=ctypes.c_int
    library.mpv_initialize.argtypes=[ctypes.c_void_p]
    library.mpv_initialize.restype=ctypes.c_int
    library.mpv_terminate_destroy.argtypes=[ctypes.c_void_p]
    context=library.mpv_create()
    if not context: raise RuntimeError('libmpv context creation failed')
    try:
        for key,value in ((b'vo',b'null'),(b'ao',b'null'),(b'config',b'no'),(b'load-scripts',b'no'),(b'terminal',b'no')):
            if library.mpv_set_option_string(context,key,value)<0: raise RuntimeError('libmpv option failed')
        if library.mpv_initialize(context)<0: raise RuntimeError('libmpv initialization failed')
    finally:
        library.mpv_terminate_destroy(context)
    print(json.dumps({'api_version':version,'initialized':True}))
"""
    # Windows venv python.exe may be a redirector. Running the base interpreter
    # directly lets subprocess timeout terminate the actual validator process.
    python = getattr(sys, '_base_executable', None) or sys.executable
    result = subprocess.run([python, '-I', '-c', script, str(directory)],
                            cwd=str(directory), capture_output=True, timeout=20, **_hidden())
    if result.returncode:
        raise RuntimeError('内置播放器 DLL 加载失败；请检查系统图形驱动和支持组件')
    try:
        value = json.loads(result.stdout)
        if type(value['api_version']) is not int or value['api_version'] <= 0 or value.get('initialized') is not True:
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('内置播放器检测未返回有效版本') from None


def _manifest(directory):
    root = Path(directory).resolve()
    path = root / 'manifest.json'
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_MANIFEST:
        raise ValueError('离线播放器目录缺少有效 manifest.json')
    try:
        value = json.loads(path.read_text('utf-8'))
    except (ValueError, UnicodeError):
        raise ValueError('播放器清单无法读取') from None
    if not isinstance(value, dict):
        raise ValueError('播放器清单必须是对象')
    files = value.get('files')
    if (value.get('format') != 1 or value.get('architecture') != 'x86_64'
            or not isinstance(files, dict) or 'mpv-2.dll' not in files or len(files) > 32):
        raise ValueError('播放器清单格式或平台无效')
    total = 0
    for name, digest in files.items():
        if name not in ALLOWED_DLLS and name not in ALLOWED_SUPPORT_FILES:
            raise ValueError('播放器清单包含未允许的文件或路径')
        item = root / name
        if not item.is_file() or item.is_symlink() or item.resolve() != item:
            raise ValueError('播放器文件缺失或跳转到目录外')
        total += item.stat().st_size
        if total > MAX_EXTRACTED:
            raise ValueError('播放器文件总大小超出限制')
        require_hash(item, digest)
        if name in ALLOWED_DLLS:
            require_x64(item)
    return value


def install(prefix=None, from_directory=None):
    """Validate in staging then replace only a manifest-owned player directory."""
    from .process_lock import InstanceLease
    prefix = Path(prefix or sys.prefix).resolve()
    prefix.mkdir(parents=True, exist_ok=True)
    lease = InstanceLease(prefix / '.player-install.lock')
    if not lease.acquire():
        raise RuntimeError('此 Python 环境正在安装播放器，请等待当前安装完成')
    try:
        return _install_locked(prefix, from_directory)
    finally:
        lease.release()


def _remove_owned(directory, parent):
    # Every recursive cleanup remains under the explicitly selected prefix.
    directory, parent = Path(directory), Path(parent).resolve()
    if directory.is_symlink() or directory.resolve().parent != parent:
        raise ValueError('临时播放器目录跳转到了安装目录之外')
    shutil.rmtree(directory)


def _install_locked(prefix, from_directory):
    target = prefix / 'player'
    if target.is_symlink() or target.resolve() != target:
        raise ValueError('播放器目标目录不能是符号链接或目录联接')
    existing = None
    if target.exists():
        if not target.is_dir():
            raise ValueError('播放器目标不是目录')
        existing = _manifest(target)
        paths = set()
        for directory, folders, filenames in os.walk(target, followlinks=False):
            for name in folders + filenames:
                item = Path(directory) / name
                if item.is_symlink() or item.resolve() != item:
                    raise ValueError('已有播放器目录包含符号链接或目录联接')
            paths.update((Path(directory) / name).relative_to(target).as_posix() for name in filenames)
        if paths != set(existing['files']) | {'manifest.json'}:
            raise ValueError('已有播放器目录包含清单外文件，未覆盖')
    # Validate the whole selected directory before loading any of its code.
    offline = Path(from_directory).resolve() if from_directory is not None else None
    source_manifest = _manifest(offline) if offline else None
    if source_manifest and existing and source_manifest['files'] == existing['files']:
        health = verify_library(target)
        return {'state': 'ready', 'directory': str(target), **health}
    if not offline and existing and existing.get('core_sha256') == CORE_SHA256 and existing.get('support_sha256', '') == SUPPORT_SHA256:
        return {'state': 'ready', 'directory': str(target), **verify_library(target)}
    stage = Path(tempfile.mkdtemp(prefix='.player-stage-', dir=prefix))
    backup = prefix / ('.player-backup-' + uuid.uuid4().hex)
    try:
        if offline:
            for name in source_manifest['files']:
                (stage / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(offline / name, stage / name)
            manifest = dict(source_manifest)
        else:
            with tempfile.TemporaryDirectory(prefix='lumanest-player-download-') as temporary:
                archive = Path(temporary) / 'mpv.7z'
                download(CORE_URL, archive, CORE_SHA256)
                extract_core(archive, stage)
                sources = [{'url': CORE_URL, 'sha256': CORE_SHA256}]
                if SUPPORT_URL:
                    support = Path(temporary) / 'support.zip'
                    download(SUPPORT_URL, support, SUPPORT_SHA256)
                    extract_support(support, stage)
                    sources.append({'url': SUPPORT_URL, 'sha256': SUPPORT_SHA256})
            manifest = {'format': 1, 'architecture': 'x86_64', 'version': CORE_VERSION,
                        'core_sha256': CORE_SHA256, 'support_sha256': SUPPORT_SHA256,
                        'sources': sources,
                        'files': {item.relative_to(stage).as_posix(): _sha256(item)
                                  for item in stage.rglob('*') if item.is_file()}}
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        _manifest(stage)
        health = verify_library(stage)
        if target.exists():
            target.rename(backup)
        try:
            stage.rename(target)
        except BaseException:
            if backup.exists():
                backup.rename(target)
            raise
        if backup.exists():
            _remove_owned(backup, prefix)
        return {'state': 'installed', 'directory': str(target), **health}
    finally:
        if stage.exists():
            _remove_owned(stage, prefix)


def main(argv=None):
    parser = argparse.ArgumentParser(description='安装 LumaNest 内置播放器运行组件')
    parser.add_argument('--prefix', help='Python 安装或虚拟环境目录；默认当前 Python')
    parser.add_argument('--from-directory', help='带 manifest.json 的离线运行组件目录')
    options = parser.parse_args(argv)
    try:
        result = install(options.prefix, options.from_directory)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'state': 'failed', 'message': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

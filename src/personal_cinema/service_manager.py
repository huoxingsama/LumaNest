"""Own only processes started by this installation, with PID-reuse protection."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from .process_control import terminate_owned_tree


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text('utf-8'))
    except FileNotFoundError:
        return default


def process_identity(pid: int):
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return {'created': str((created.dwHighDateTime << 32) | created.dwLowDateTime),
                    'executable': os.path.normcase(str(Path(buffer.value).resolve()))}
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return {'created': fields[19], 'executable': str(Path(f'/proc/{pid}/exe').resolve(strict=True))}
    except (OSError, IndexError):
        return None


class ServiceManager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.registry = self.root / 'services' / 'owned.json'
        self.lock = threading.RLock()
        self.children = {}

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}', name):
            raise ValueError('服务名称无效')
        return name

    def _records(self):
        value = read_json(self.registry, {})
        if not isinstance(value, dict):
            raise ValueError('服务所有权记录损坏，请恢复备份')
        return value

    def _matches(self, record):
        actual = process_identity(record.get('pid'))
        return bool(actual and actual.get('created') == record.get('created')
                    and actual.get('executable') == record.get('executable'))

    def status(self, name):
        self._name(name)
        with self.lock:
            child = self.children.get(name)
            if child is not None:
                child.poll()
            record = self._records().get(name, {})
            running = self._matches(record)
            return {'id': name, 'running': running, 'owned': running,
                    'pid': record.get('pid') if running else None,
                    'state': 'running' if running else 'stopped'}

    def start(self, name, argv, cwd, env=None):
        self._name(name)
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\x00' in v for v in argv):
            raise ValueError('启动参数无效')
        executable = Path(argv[0])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError('请选择已安装组件的绝对可执行文件路径')
        cwd = Path(cwd).resolve()
        try:
            cwd.relative_to(self.root / 'managed')
        except ValueError:
            raise ValueError('工作目录必须位于本程序托管目录内') from None
        fingerprint = hashlib.sha256(json.dumps([argv, str(cwd)], ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            records = self._records()
            prior = records.get(name, {})
            if self._matches(prior):
                if prior.get('command_hash') != fingerprint:
                    raise ValueError('该托管服务正在使用其他配置，请先停止再应用')
                return self.status(name)
            cwd.mkdir(parents=True, exist_ok=True)
            log_dir = self.root / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
            with (log_dir / f'{name}.log').open('ab') as log:
                process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, shell=False, creationflags=flags)
            self.children[name] = process
            identity = process_identity(process.pid)
            if not identity:
                process.poll()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                raise RuntimeError('服务启动失败或无法验证进程身份，请查看本机日志')
            record = {'pid': process.pid, **identity, 'command_hash': fingerprint,
                      'started_at': time.time(), 'cwd': str(cwd)}
            records[name] = record
            try:
                atomic_json(self.registry, records)
            except Exception:
                terminate_owned_tree(record['pid'], lambda: self._matches(record), timeout=5)
                process.wait(timeout=5)
                raise
            return self.status(name)

    def stop(self, name):
        self._name(name)
        with self.lock:
            records = self._records()
            record = records.get(name, {})
            if not self._matches(record):
                return {'id': name, 'state': 'not_owned_or_not_running', 'running': False, 'owned': False}
            terminate_owned_tree(record['pid'], lambda: self._matches(record))
            process = self.children.get(name)
            if process is not None and process.pid == record['pid']:
                process.wait(timeout=5)
            records[name] = {**record, 'stopped_at': time.time()}
            atomic_json(self.registry, records)
            return self.status(name)

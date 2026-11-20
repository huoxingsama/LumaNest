"""Stop a verified owned process tree, including Windows Python redirectors."""
from __future__ import annotations

import os
from pathlib import Path
import time

import psutil


def _identity(process):
    return {'pid': process.pid, 'created': process.create_time(),
            'executable': os.path.normcase(str(Path(process.exe()).resolve()))}


def _same(record):
    try:
        current = psutil.Process(record['pid'])
        if not current.is_running() or current.status() == psutil.STATUS_ZOMBIE:
            return None
        if _identity(current) != {key: record[key] for key in ('pid', 'created', 'executable')}:
            return None
        return current
    except psutil.NoSuchProcess:
        return None


def terminate_owned_tree(pid, owns_process, *, timeout=8):
    """Require the caller's PID/creation/command ownership check before any action.

    Descendants are discovered from the verified parent, with creation time and
    executable checks before suspension or termination. On Windows suspension
    prevents a redirector/service spawning more children while shutdown walks
    the tree. Detached or unrelated processes are never searched by name.
    """
    if type(pid) is not int or pid <= 0 or not callable(owns_process):
        raise ValueError('停止进程需要有效的所有权校验')
    if not owns_process():
        return {'state': 'not_owned_or_not_running', 'terminated': []}
    captured, terminated = [], []

    def resume():
        for record in reversed(captured):
            if record.get('suspended_by_us'):
                try:
                    process = _same(record)
                    if process: process.resume()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def capture(process, parent=None):
        if len(captured) >= 4096: raise RuntimeError('托管进程树超出安全处理数量，未继续停止')
        try:
            record = _identity(process)
            if parent is not None:
                if process.ppid() != parent['pid'] or record['created'] < parent['created']:
                    return
                if _same(parent) is None: raise RuntimeError('托管父进程身份发生变化，已取消停止')
            if not owns_process(): raise RuntimeError('托管进程身份发生变化，已取消停止')
            if _same(record) is None: return
            record['suspended_by_us'] = False
            captured.append(record)
            if os.name == 'nt' and process.status() != psutil.STATUS_STOPPED:
                process.suspend()
                record['suspended_by_us'] = True
            for child in process.children():
                capture(child, record)
        except psutil.NoSuchProcess:
            return

    try:
        root = psutil.Process(pid)
        root_identity = _identity(root)
        if not owns_process():
            return {'state': 'not_owned_or_not_running', 'terminated': []}
        capture(root)
        if not captured or captured[0]['pid'] != pid or _same(root_identity) is None:
            raise RuntimeError('托管进程身份发生变化，已取消停止')
        deadline = time.monotonic() + max(0.1, float(timeout))
        for record in reversed(captured):
            process = _same(record)
            if process is None: continue
            # psutil also checks creation time before issuing each process signal.
            try:
                process.terminate()
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except psutil.TimeoutExpired:
                    process = _same(record)
                    if process is not None:
                        process.kill()
                        process.wait(timeout=3)
                terminated.append(record['pid'])
            except psutil.NoSuchProcess:
                pass
        return {'state': 'stopped', 'terminated': terminated}
    except psutil.NoSuchProcess:
        if not captured: return {'state': 'not_owned_or_not_running', 'terminated': []}
        raise RuntimeError('托管进程树在停止前发生变化，请重新检测') from None
    except (psutil.AccessDenied, psutil.TimeoutExpired):
        raise RuntimeError('无法完成托管进程树停止，已保留所有权记录供重试') from None
    finally:
        # A partial failure must not leave an otherwise healthy process frozen.
        resume()

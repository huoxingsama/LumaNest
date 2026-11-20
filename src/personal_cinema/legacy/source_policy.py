"""Selected-source authority for journalled cloud operations.

No personal mount names, no guessed writable roots, and no path-only object IDs.
Every mutation re-reads the saved permissions; preview approval is scoped to the
exact journal, configuration revision and source.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re
import time

from personal_cinema.configuration import ConfigStore
from personal_cinema.runtime_paths import vault, config_home_for_runtime
from library_tasks import read, write

SUPPORTED = {'Quark', 'BaiduNetdisk'}


def _config(root):
    return ConfigStore(config_home_for_runtime(root))


def source_entries(root):
    return _config(root).read()['values'].get('sources.entries', [])


def selected(root, source):
    matches = [s for s in source_entries(root) if s.get('id') == source or s.get('path') == source]
    if len(matches) != 1:
        raise RuntimeError('媒体来源未唯一配置，请在设置页选择来源。')
    entry = matches[0]
    if not entry.get('enabled', True) or entry.get('kind') != 'openlist':
        raise RuntimeError('此来源未启用 OpenList 读取。')
    return entry


def source_roots(root):
    return [s['path'] for s in source_entries(root) if s.get('enabled', True) and s.get('kind') == 'openlist']


def journal_path(root, source):
    entry = selected(root, source)
    return root / 'data' / ('cloud-organizer-plan-source-' + entry['id'] + '.json')


def plan_id(plan):
    fields = [{k: op.get(k) for k in ('fid', 'source', 'target', 'size', 'entry_id', 'video_entry_id')}
              for op in plan.get('operations', [])]
    raw = json.dumps({'source_id': plan.get('source_id'), 'operations': fields}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _permission(root, entry, cleanup=False, approval=None):
    current = selected(root, entry['id'])
    if any(current.get(key) != entry.get(key) for key in ('path', 'driver', 'mount_path', 'native_root', 'credential')):
        raise RuntimeError('来源配置已变化，请重新生成整理计划。')
    values = _config(root).get_effective('source:' + current['id'])
    if not values.get('organizing.enabled') or not current.get('allow_write', False):
        raise RuntimeError('来源为只读，尚未授予整理写权限。')
    if current.get('driver') not in SUPPORTED:
        raise RuntimeError('该来源驱动暂不支持安全整理，仍可只读播放。')
    if cleanup and not values.get('organizing.cleanup_empty', False):
        raise RuntimeError('未启用确认空目录回收。')
    if values.get('organizing.preview_only', True):
        if not approval:
            raise RuntimeError('当前为预览模式，请先在设置页确认本次整理计划。')
        record = read(root / 'data/cloud-organizer-approvals.json').get(current['id'], {})
        if (record.get('plan_id') != approval or record.get('revision') != _config(root).read()['revision']
                or record.get('expires_at', 0) < time.time()
                or plan_id(read(journal_path(root, current['id']))) != approval):
            raise RuntimeError('整理计划批准已过期或内容变化，请重新预览并确认。')


def approve_plan(root, source_id, expected_id):
    entry = selected(root, source_id)
    plan = read(journal_path(root, source_id))
    if not plan.get('operations') or plan_id(plan) != expected_id or plan.get('source_id') != entry['id']:
        raise ValueError('整理计划已变化或不存在，请重新读取。')
    values = _config(root).get_effective('source:' + entry['id'])
    if not values.get('organizing.enabled') or not entry.get('allow_write') or entry.get('driver') not in SUPPORTED:
        raise ValueError('请先启用整理并授予此来源写权限。')
    approvals = read(root / 'data/cloud-organizer-approvals.json')
    approvals[entry['id']] = {'plan_id': expected_id, 'revision': _config(root).read()['revision'],
                              'expires_at': time.time() + 86400}
    write(root / 'data/cloud-organizer-approvals.json', approvals)
    return {'state': 'approved', 'source_id': entry['id'], 'plan_id': expected_id}


def list_plans(root):
    items = []
    for entry in source_entries(root):
        if entry.get('kind') != 'openlist' or not entry.get('enabled', True):
            continue
        plan = read(journal_path(root, entry['id']))
        items.append({'source_id': entry['id'], 'name': entry['name'], 'source_root': entry['path'],
            'supported': entry.get('driver') in SUPPORTED, 'allow_write': bool(entry.get('allow_write')),
            'plan_id': plan_id(plan) if plan.get('operations') else None,
            'phase': plan.get('phase', 'not_planned'), 'operation_count': len(plan.get('operations', [])),
            'review_count': len(plan.get('review', [])), 'changes': [
                {'source': op.get('source'), 'target': op.get('target'), 'kind': op.get('kind', 'move')}
                for op in plan.get('operations', [])]})
    return {'items': items}


class SourceGuard:
    def _init_source(self, root, entry):
        self.root, self.entry = root, dict(entry)
        self.source_root = entry['path'].rstrip('/')
        self.slug = 'source-' + entry['id']
        self._objects = {}
        self.approval = None
        self._bootstrapping = False

    def _remember(self, fid, path, directory, size=0):
        self._objects[str(fid)] = {'path': path, 'dir': bool(directory), 'size': int(size or 0)}

    def _path(self, fid, allow_root=True):
        row = self._objects.get(str(fid))
        if not row:
            raise RuntimeError('对象身份尚未在所选来源中确认。')
        path = row['path']
        if path != self.source_root and not path.startswith(self.source_root + '/'):
            raise RuntimeError('操作超出所选媒体目录。')
        if not allow_root and path == self.source_root:
            raise RuntimeError('不能移动、重命名或删除媒体来源根目录。')
        # A nested source has its own permission and cannot be reached through
        # a more permissive parent source.
        for entry in source_entries(self.root):
            other = entry.get('path', '').rstrip('/')
            if (entry.get('kind') == 'openlist' and entry.get('id') != self.entry['id']
                    and other.startswith(self.source_root + '/') and (path == other or path.startswith(other + '/'))):
                raise RuntimeError('嵌套来源需要单独确认权限。')
        return path

    def _track_listing(self, parent, rows):
        if self._bootstrapping:
            return rows
        base = self._path(parent)
        for row in rows:
            name = self._name(row['file_name'])
            self._remember(row['fid'], base + '/' + name, row.get('dir'), row.get('size', 0))
        return rows

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name or name in ('.', '..') or any(c in name for c in '/\\\x00\r\n'):
            raise RuntimeError('对象名称包含无效路径字符。')
        return name

    def require_write(self, fids=(), cleanup=False, allow_root=False):
        _permission(self.root, self.entry, cleanup=cleanup, approval=self.approval)
        for fid in fids:
            self._path(fid, allow_root=allow_root)

    def _moved(self, fid, path):
        old = self._path(fid)
        for row in self._objects.values():
            if row['path'] == old or row['path'].startswith(old + '/'):
                row['path'] = path + row['path'][len(old):]


def credentials_for(worker, entry):
    ref = entry.get('credential')
    if ref:
        raw = vault().get(ref)
        if not raw:
            raise RuntimeError('来源指定的管理凭据尚未配置。')
        try:
            result = json.loads(raw)
        except ValueError:
            result = {'cookie': raw} if entry.get('driver') == 'Quark' else {'AccessToken': raw}
        if not isinstance(result, dict):
            raise RuntimeError('来源管理凭据格式不正确。')
        return result
    # Reuse only the explicitly selected OpenList mount's account, in memory.
    if worker is None or not entry.get('mount_path'):
        raise RuntimeError('请指定挂载点或独立管理凭据。')
    value = worker.api('openlist', '/api/admin/storage/list?page=1&per_page=100')
    rows = (value.get('data') or {}).get('content') or []
    matches = [r for r in rows if r.get('mount_path') == entry['mount_path'] and r.get('driver') == entry['driver']]
    if len(matches) != 1:
        raise RuntimeError('所选挂载点与云盘驱动不一致，未获取管理权限。')
    addition = matches[0].get('addition')
    return json.loads(addition) if isinstance(addition, str) else dict(addition or {})


def manager_for(worker, source):
    entry = selected(worker.root, source)
    if entry.get('driver') not in SUPPORTED:
        raise RuntimeError('当前驱动仅支持读取；整理需选择受支持的云盘驱动。')
    auth = credentials_for(worker, entry)
    if entry['driver'] == 'Quark':
        from quark_manage import Quark
        return Quark(worker.root, entry=entry, auth=auth)
    from baidu_manage import Baidu
    return Baidu(worker.root, entry=entry, auth=auth)


def process_request(worker, request, payload):
    action = payload.get('action') or (request if isinstance(request, str) else request.get('action', '') if isinstance(request, dict) else '')
    if action not in ('organizing-plan', 'organizing-apply'):
        return False
    source = payload.get('source_id')
    entry = selected(worker.root, source)
    from cloud_organizer import make_plan, metadata, resume_plan, progress
    manager = manager_for(worker, source)
    if action == 'organizing-plan':
        prior = read(journal_path(worker.root, source))
        if prior.get('phase') not in (None, 'complete', 'planned', 'preview'):
            raise RuntimeError('此来源已有执行中的计划，请先恢复原计划。')
        j, by_id, by_path, movies = metadata(worker.root)
        ids = {e['id'] for e in worker.entries.values() if e.get('remote', '').startswith(entry['path'] + '/')}
        plan, _ = make_plan(worker.root, manager, worker, j, by_id, by_path, movies, entry_ids=ids)
        plan.update(source_id=entry['id'], phase='preview')
        write(journal_path(worker.root, source), plan)
        progress(worker.root, 'preview', '整理计划已生成，请在设置页预览确认', state='waiting', source_id=entry['id'])
    else:
        approval = payload.get('plan_id')
        plan = read(journal_path(worker.root, source))
        if not approval or plan_id(plan) != approval:
            raise RuntimeError('整理计划已变化，未执行。')
        manager.approval = approval
        _permission(worker.root, entry, approval=approval)
        resume_plan(worker, entry['path'], manager=manager)
    return True

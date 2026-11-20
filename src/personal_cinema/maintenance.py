"""Instance-only backups, current-user restore, log retention and Windows startup.

Backups contain private configuration and DPAPI ciphertext. They are recovery
artifacts, not the sanitized settings export intended for sharing.
"""
import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

from .configuration import ConfigStore, MAX_CONFIG_BYTES, RevisionConflict, _atomic_write, _file_lock
from .credentials import CredentialError, SecretStore

MAX_ARCHIVE_BYTES = 6 * 1024 * 1024
MEMBERS = frozenset(('manifest.json', 'config.json', 'secrets/credentials.json'))
BACKUP_ID = re.compile(r'^backup-[0-9TZ]+-[a-f0-9]{12}\.zip$')


class WindowsAutostart:
    """Own one HKCU Run value; never enumerate or remove unrelated values."""
    key = r'Software\Microsoft\Windows\CurrentVersion\Run'

    def set(self, name, command):
        if os.name != 'nt': raise ValueError('登录自启当前仅支持 Windows。')
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, command)

    def delete(self, name):
        if os.name != 'nt': raise ValueError('登录自启当前仅支持 Windows。')
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as handle:
                try: winreg.DeleteValue(handle, name)
                except FileNotFoundError: pass
        except FileNotFoundError: pass


class MaintenanceManager:
    def __init__(self, root, running=None, cipher=None, registry=None):
        self.root = Path(root).expanduser().resolve()
        self.config = ConfigStore(self.root)
        self.running = running
        self._cipher = cipher
        self.registry = registry if registry is not None else WindowsAutostart()
        self.autostart_name = 'PersonalCinema-' + hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()[:16]

    def _safe(self, relative, base=None):
        base = self.root if base is None else Path(base).resolve()
        path = base / relative
        try: path.resolve().relative_to(base)
        except ValueError: raise ValueError('实例维护路径越界。') from None
        current = path
        while current != base:
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise ValueError('维护目录不能包含符号链接或目录联接。')
            current = current.parent
        return path

    def _vault(self):
        self._safe('secrets/credentials.json')
        return SecretStore(self.root, cipher=self._cipher)

    def _validate_credentials(self, raw):
        if not isinstance(raw, dict) or set(raw) != {'version', 'protection', 'values'} or raw.get('version') != 1 or raw.get('protection') != 'windows-dpapi-user' or not isinstance(raw.get('values'), dict):
            raise ValueError('备份凭据库格式不兼容。')
        cipher = self._vault().cipher
        for name, encoded in raw['values'].items():
            SecretStore._name(name)
            try:
                if not isinstance(encoded, str) or len(encoded) > 200000: raise ValueError()
                value = cipher.decrypt(base64.b64decode(encoded, validate=True)).decode('utf-8')
                if not value or '\x00' in value or len(value.encode('utf-8')) > 65536: raise ValueError()
            except Exception:
                raise ValueError('此凭据备份不能由当前 Windows 账户解密，请使用原账户恢复。') from None
        return raw

    def _new_id(self):
        return 'backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(6) + '.zip'

    @staticmethod
    def _encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')

    def _write_archive(self, config, credentials):
        # A launch handshake belongs to the current settings process, not to
        # configuration history. Restoring it would orphan a running UI.
        credentials = {**credentials, 'values': {key: value for key, value in credentials['values'].items()
                                                if key != 'settings.launcher'}}
        folder = self._safe('backups')
        folder.mkdir(parents=True, exist_ok=True)
        ident = self._new_id()
        target = self._safe('backups/' + ident)
        content = {'config.json': self._encode(config), 'secrets/credentials.json': self._encode(credentials)}
        if any(len(data) > MAX_CONFIG_BYTES for data in content.values()): raise ValueError('备份记录超过允许大小。')
        manifest = {'format': 'personal-cinema-backup', 'version': 1,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'protection': 'windows-dpapi-user',
                    'files': {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}}
        descriptor, temporary = tempfile.mkstemp(prefix='.backup-', suffix='.tmp', dir=folder)
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', self._encode(manifest))
                for name, data in content.items(): archive.writestr(name, data)
            os.chmod(temporary, 0o600)
            with open(temporary, 'r+b') as stream: os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {'id': ident, 'created_at': manifest['created_at'], 'size': target.stat().st_size,
                'includes_credentials': bool(credentials['values'])}

    def backup(self):
        self._safe('config.json')
        vault = self._vault()
        with ExitStack() as stack:
            for path in (self._safe('.maintenance.lock'), self.config.lock_path, vault.lock_path):
                stack.enter_context(_file_lock(path))
            config, credentials = self.config._read(), vault._read()
            self._validate_credentials(credentials)
            return self._write_archive(config, credentials)

    def _read_archive(self, path, *, validate_credentials=True):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError('请选择大小受限的普通备份 ZIP 文件。')
        try:
            with path.open('rb') as stream: data = stream.read(MAX_ARCHIVE_BYTES + 1)
            if len(data) > MAX_ARCHIVE_BYTES: raise ValueError('备份文件过大。')
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) != len(MEMBERS) or {entry.filename for entry in entries} != MEMBERS:
                    raise ValueError('备份只允许配置和加密凭据；拒绝重复、媒体文件或越界路径。')
                for entry in entries:
                    if entry.file_size > MAX_CONFIG_BYTES or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                        raise ValueError('备份条目无效或过大。')
                members = {entry.filename: archive.read(entry) for entry in entries}
            manifest = json.loads(members['manifest.json'])
            if not isinstance(manifest, dict) or manifest.get('format') != 'personal-cinema-backup' or manifest.get('version') != 1 or manifest.get('protection') != 'windows-dpapi-user':
                raise ValueError('备份格式或版本不兼容。')
            if not isinstance(manifest.get('created_at'), str) or len(manifest['created_at']) > 64:
                raise ValueError('备份时间无效。')
            files = manifest.get('files')
            if not isinstance(files, dict) or set(files) != MEMBERS - {'manifest.json'}:
                raise ValueError('备份清单无效。')
            for name, digest in files.items():
                if not isinstance(digest, str) or hashlib.sha256(members[name]).hexdigest() != digest:
                    raise ValueError('备份校验失败，原配置未改变。')
            config = self.config._normalize(json.loads(members['config.json']))
            credentials = json.loads(members['secrets/credentials.json'])
            if validate_credentials: self._validate_credentials(credentials)
            return manifest, config, credentials
        except (zipfile.BadZipFile, zipfile.LargeZipFile, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError('备份损坏或格式无效。') from error

    def list_backups(self):
        folder = self._safe('backups')
        if not folder.exists(): return []
        records = []
        for path in folder.glob('backup-*.zip'):
            if not BACKUP_ID.fullmatch(path.name): continue
            try:
                manifest, _config, credentials = self._read_archive(path, validate_credentials=False)
                records.append({'id': path.name, 'created_at': manifest['created_at'], 'size': path.stat().st_size,
                                'includes_credentials': bool(credentials.get('values'))})
            except (OSError, ValueError, AttributeError): continue
        return sorted(records, key=lambda row: (row['created_at'], row['id']), reverse=True)

    def import_archive(self, path):
        _manifest, config, credentials = self._read_archive(path)
        with _file_lock(self._safe('.maintenance.lock')):
            return self._write_archive(config, credentials)

    def _require_stopped(self):
        if self.running is None:
            raise ValueError('尚未确认运行状态，不能还原；请通过设置页或维护命令操作。')
        try: active = self.running()
        except Exception: raise ValueError('无法确认运行状态，未执行还原。') from None
        if active is not False:
            raise ValueError('请先退出播放并停止此实例全部后台与托管服务，再还原备份。')

    def restore(self, ident, expected_revision=None):
        if not isinstance(ident, str) or not BACKUP_ID.fullmatch(ident): raise ValueError('备份编号无效。')
        self._require_stopped()
        _manifest, restored, credentials = self._read_archive(self._safe('backups/' + ident))
        self._safe('config.json')
        vault = self._vault()
        with ExitStack() as stack:
            for path in (self._safe('.maintenance.lock'), self.config.lock_path, vault.lock_path):
                stack.enter_context(_file_lock(path))
            current, previous_credentials = self.config._read(), vault._read()
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision != current['revision']):
                raise RevisionConflict(expected_revision, current['revision'])
            self._require_stopped()
            safety = self._write_archive(current, previous_credentials)
            credentials['values'].pop('settings.launcher', None)
            if 'settings.launcher' in previous_credentials['values']:
                credentials['values']['settings.launcher'] = previous_credentials['values']['settings.launcher']
            restored['revision'] = current['revision'] + 1
            try:
                _atomic_write(self.config.path, restored)
                _atomic_write(vault.path, credentials)
            except Exception:
                # A saved copy remains available even if the disk also rejects rollback.
                try:
                    _atomic_write(self.config.path, current)
                    _atomic_write(vault.path, previous_credentials)
                except Exception:
                    raise RuntimeError('还原和回滚未全部完成；安全备份：' + safety['id']) from None
                raise
        return {'state': 'restored', 'revision': restored['revision'], 'safety_backup': safety['id'],
                'message': '设置与当前 Windows 账户的凭据已恢复；请重新应用设置后启动后台。'}

    def sync_autostart(self):
        enabled = self.config.get_effective('device')['maintenance.autostart']
        if enabled:
            executable = Path(sys.executable)
            # Custom windowless executables can be bound to a network interface.
            # Preserve that identity; only standard console Python needs its GUI sibling.
            if executable.name.lower() == 'python.exe':
                executable = executable.with_name('pythonw.exe')
            if not executable.is_file():
                raise ValueError('登录自启程序不存在；请使用已安装的应用重新应用自启规则。')
            # Reuse the normal singleton launcher and its dependency readiness checks.
            command = subprocess.list2cmdline([str(executable), '-m', 'personal_cinema', '--data-dir', str(self.root), '--background'])
            self.registry.set(self.autostart_name, command)
        else:
            self.registry.delete(self.autostart_name)
        return {'enabled': enabled, 'name': self.autostart_name}

    def prune_diagnostics(self, now=None):
        now = time.time() if now is None else float(now)
        values = self.config.get_effective('device')
        days = values['maintenance.diagnostic_retention_days']
        from .runtime import read
        runtime = Path(read(self.root / 'runtime-manifest.json').get('runtime_directory')
                       or values.get('foundation.runtime_directory') or self.root / 'runtime')
        cutoff = now - days * 86400
        removed = skipped = 0
        for base, relative in ((self.root, 'logs'), (runtime, 'logs'), (self.root, 'diagnostics')):
            try: folder = self._safe(relative, base=base)
            except ValueError:
                skipped += 1; continue
            if not folder.is_dir(): continue
            for path in folder.iterdir():
                is_log = bool(re.fullmatch(r'[A-Za-z0-9_.-]+\.log(?:\.\d+)?', path.name))
                is_report = relative == 'diagnostics' and bool(re.fullmatch(r'diagnostics-[A-Za-z0-9_.-]+\.json', path.name))
                if not (is_log or is_report): continue
                try:
                    self._safe(str(path.relative_to(Path(base).resolve())), base=base)
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink(); removed += 1
                except (OSError, ValueError): skipped += 1
        return {'removed': removed, 'skipped': skipped}


def main():
    from .runtime_paths import application_home
    parser = argparse.ArgumentParser(description='LumaNest 私有配置备份与维护（不复制或移动影片）')
    parser.add_argument('--data-dir', type=Path, default=application_home())
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('backup', 'list', 'prune', 'autostart'): commands.add_parser(name)
    restore = commands.add_parser('restore'); restore.add_argument('id'); restore.add_argument('--revision', type=int)
    imported = commands.add_parser('import'); imported.add_argument('file', type=Path)
    args = parser.parse_args()
    try:
        from .integrations import IntegrationManager
        from .runtime import Runtime
        config, vault = ConfigStore(args.data_dir), SecretStore(args.data_dir)
        integrations = IntegrationManager(args.data_dir, config, vault)
        runtime = Runtime(args.data_dir, config, vault, integrations)
        def running():
            status = runtime.status()
            return bool(status['playing'] or any(status['processes'].values()) or any(integrations.services.status(name)['running'] for name in ('jellyfin', 'openlist', 'hypomux')))
        manager = MaintenanceManager(args.data_dir, running=running)
        if args.command == 'backup': result = manager.backup()
        elif args.command == 'list': result = manager.list_backups()
        elif args.command == 'restore': result = manager.restore(args.id, args.revision)
        elif args.command == 'import': result = manager.import_archive(args.file)
        elif args.command == 'autostart': result = manager.sync_autostart()
        else: result = manager.prune_diagnostics()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, CredentialError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__': main()

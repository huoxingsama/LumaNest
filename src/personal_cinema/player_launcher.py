"""Launch the native cinema with per-instance, DPAPI-protected local profiles.

Only paths travel in argv. Tokens are read from the current user's credential
vault and reach the native client through its in-memory profile registry.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.parse
import uuid

from .credentials import CredentialError


class ProfileVault:
    """Atomic encrypted registry snapshots; old snapshot remains a backup.

    Chunks stay below SecretStore's 64 KiB limit even for non-ASCII profiles.
    Publishing a single encrypted manifest makes a partial write invisible.
    """
    def __init__(self, store, config_dir, purpose='users'):
        self.store = store
        identity = os.path.normcase(str(Path(config_dir).resolve())) + ':' + purpose
        self.prefix = 'player.' + hashlib.sha256(identity.encode()).hexdigest()[:24]

    def _pointer(self, slot):
        raw = self.store.get(self.prefix + '.' + slot)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if value.get('version') != 1 or not isinstance(value.get('chunks'), list):
                raise ValueError('invalid registry manifest')
            if not value['chunks'] or len(value['chunks']) > 512:
                raise ValueError('invalid registry chunk count')
            if any(not isinstance(k, str) or not k.startswith(self.prefix + '.part.') for k in value['chunks']):
                raise ValueError('invalid registry namespace')
            return value
        except (ValueError, TypeError, AttributeError) as exc:
            raise CredentialError('播放器账户记录损坏，未覆盖已有账户。') from exc

    def read(self, slot='current'):
        pointer = self._pointer(slot)
        if pointer is None:
            return None
        try:
            chunks = [self.store.get(key) for key in pointer['chunks']]
            if any(chunk is None for chunk in chunks):
                raise ValueError('incomplete encrypted registry')
            raw = ''.join(chunks)
            if hashlib.sha256(raw.encode()).hexdigest() != pointer.get('sha256'):
                raise ValueError('registry checksum mismatch')
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise CredentialError('播放器账户记录不完整，未覆盖已有账户。') from exc

    def write(self, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        chunks = [raw[i:i + 12000] for i in range(0, len(raw), 12000)]
        if len(chunks) > 512:
            raise CredentialError('播放器账户资料过大，未覆盖已有账户。')
        generation = uuid.uuid4().hex
        keys = [self.prefix + '.part.' + generation + '.' + str(i) for i in range(len(chunks))]
        for key, chunk in zip(keys, chunks):
            self.store.set(key, chunk)
        previous, old_backup = None, None
        try:
            previous = self._pointer('current')
            self.read()
        except CredentialError:
            # Recovery may replace a damaged current snapshot with a good backup.
            previous = None
        try:
            old_backup = self._pointer('backup')
        except CredentialError:
            pass
        pointer = {'version': 1, 'chunks': keys, 'sha256': hashlib.sha256(raw.encode()).hexdigest()}
        if previous:
            self.store.set(self.prefix + '.backup', json.dumps(previous))
        self.store.set(self.prefix + '.current', json.dumps(pointer))
        if self.read() != payload:
            raise CredentialError('播放器账户保存校验失败。')
        # Retain exactly the published snapshot plus one known-good backup.
        keep = set(keys + (previous or old_backup or {}).get('chunks', []))
        for key in (old_backup or {}).get('chunks', []):
            if key not in keep:
                self.store.delete(key)

    def archive_legacy(self, path):
        """Keep old bytes encrypted before removing this exact legacy file."""
        path = Path(path)
        data = path.read_bytes()
        archive = ProfileVault(self.store, path.parent, 'legacy:' + path.name)
        payload = {'filename': path.name, 'bytes': base64.b64encode(data).decode('ascii')}
        archive.write(payload)
        if archive.read() != payload:
            raise CredentialError('旧账户文件尚未安全迁移，保留原文件。')
        path.unlink()


def authorized_credential(values, store, gateway):
    target = urllib.parse.urlsplit(gateway)
    if (target.scheme != 'http' or target.hostname not in ('127.0.0.1', '::1', 'localhost')
            or target.username or target.password or target.query or target.fragment):
        raise ValueError('影院只能通过本实例的本地媒体接口启动。')
    selected_player = values.get('foundation.player_credential')
    reference = selected_player or values.get('foundation.jellyfin_credential') or 'jellyfin.token'
    token = store.get(reference)
    namespace = 'player.jellyfin' if selected_player else 'jellyfin'
    user_id, server_id = store.get(namespace + '.user_id'), store.get(namespace + '.server_id')
    if not token or not user_id or not server_id:
        raise ValueError('请在设置页重新完成 Jellyfin 登录，保存媒体库与用户身份后打开影院。')
    return {'Id': server_id, 'UserId': user_id, 'AccessToken': token,
            'address': gateway.rstrip('/'), 'LocalAddress': gateway.rstrip('/'),
            'Name': 'LumaNest', 'username': (store.get('player.jellyfin.username') if selected_player else None) or values.get('foundation.jellyfin_username') or '',
            'uuid': str(uuid.uuid4()), 'PersonalCinemaCredential': reference}


def synchronize_profile(manager, credential):
    """Refresh only the managed login, preserving other profiles and PINs."""
    manager.load()
    ref = credential['PersonalCinemaCredential']
    for user in manager.users:
        for index, old in enumerate(user.get('credentials') or []):
            if old.get('PersonalCinemaCredential') == ref:
                if (old.get('Id'), old.get('UserId')) == (credential['Id'], credential['UserId']):
                    credential['uuid'] = old.get('uuid') or credential['uuid']
                user['credentials'][index] = dict(credential)
                manager.save()
                return
    # Do not inject an admin account into an existing restricted profile.
    target = next((u for u in manager.users if u.get('default') and not u.get('credentials') and not u.get('pin_hash')), None)
    if target is None:
        target = manager.add_user(credential.get('username') or '影院账户')
    target['credentials'].append(dict(credential))
    manager.save()


def main(argv=None):
    from .windows_bootstrap import prepare_process, prepare_window
    prepare_process()
    parser = argparse.ArgumentParser(description='Personal Cinema native player')
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    from .runtime_paths import root_path, settings, vault, endpoint
    expected = Path(root_path('data/mpv-shim')).resolve()
    config_dir = Path(args.config).resolve()
    if config_dir != expected:
        raise ValueError('播放器配置目录不属于当前影院实例。')
    credential = authorized_credential(settings(), vault(), endpoint('gateway'))
    from .embedded_mpv import library_path, extend_environment
    if settings().get('foundation.mpv_backend', 'embedded') == 'embedded':
        extend_environment(os.environ, library_path(settings()))
    prepare_window(config_dir)
    # The native modules parse their own arguments. Never forward credentials.
    sys.argv = ['personal-cinema-player', '--config', str(config_dir), '--gui']
    from jellyfin_mpv_shim import args as native_args
    native_args._args = None
    from jellyfin_mpv_shim.conf import settings as native_settings
    native_settings.load(str(config_dir / 'conf.json'))
    from jellyfin_mpv_shim.users import userManager
    synchronize_profile(userManager, credential)
    from jellyfin_mpv_shim.mpv_shim import main as native_main
    return native_main()


if __name__ == '__main__':
    main()

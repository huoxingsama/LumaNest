"""Read-only Steam content activity detection; never changes Steam settings."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
import time
from personal_cinema.runtime_paths import RuntimePath as Path

def update_states(text, states=None):
    states = dict(states or {})
    for line in text.splitlines():
        match = re.search('AppID (\\d+) (App update changed|state changed|App update canceled|App update finished|scheduler finished)\\s*:\\s*(.*)', line, re.I)
        if not match:
            continue
        app, event, detail = match.groups()
        event = event.casefold()
        detail = detail.casefold()
        if any((word in event for word in ['canceled', 'finished'])):
            states[app] = False
        elif event == 'app update changed':
            states[app] = ('running update' in detail or 'downloading' in detail) and (not any((word in detail for word in ['paused', 'stopping'])))
        elif event == 'state changed':
            states[app] = 'update running' in detail and 'paused' not in detail
    return states

class SteamGate:

    def __init__(self, roots=None):
        self.roots = [Path(p) for p in roots or []]
        self.states = {}

    def sample(self, now=None):
        import psutil
        now = time.time() if now is None else now
        running = []
        for p in psutil.process_iter(['name', 'exe']):
            if (p.info['name'] or '').casefold() == 'steam.exe':
                running.append(p.info.get('exe'))
        if not running:
            self.states.clear()
            return False
        roots = list(dict.fromkeys(self.roots + [Path(p).parent for p in running if p]))
        active = False
        for root in roots:
            path = root / 'logs/content_log.txt'
            try:
                if now - path.stat().st_mtime > 180:
                    continue
                with path.open('rb') as source:
                    source.seek(max(0, path.stat().st_size - 512 * 1024))
                    text = source.read().decode('utf-8', errors='replace')
            except OSError:
                continue
            key = str(root)
            self.states[key] = update_states(text, self.states.get(key))
            active |= any(self.states[key].values())
        return active

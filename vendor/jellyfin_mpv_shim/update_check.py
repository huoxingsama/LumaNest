from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import requests
import datetime
import logging
import re
import time
import webbrowser
from urllib.parse import urljoin, urlsplit
from .constants import CLIENT_VERSION
from .conf import settings
from .i18n import _
from .version import is_newer
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .player import PlayerManager as PlayerManager_type
log = logging.getLogger('update_check')
release_url = 'https://github.com/jellyfin/jellyfin-mpv-shim/releases/'
release_urls = [release_url]
one_day = 86400
GITHUB_HOST = 'github.com'
ALLOWED_OWNERS = frozenset(('jellyfin', 'jellyfin-labs', 'iwalton3'))
MAX_HOPS = 4
_REDIRECTS = (301, 302, 303, 307, 308)
CHAIN_BUDGET = 15.0
_VERSION_TAG = re.compile('^v?\\d')
_RELEASE_PATH = re.compile('^/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+)/releases/(.*)$')

def parse_release_url(url: str):
    """``(base, rest)`` for a GitHub releases URL worth following, else None.

    ``base`` comes back rebuilt as
    ``https://github.com/<owner>/<repo>/releases/`` and ``rest`` as whatever
    followed it (``latest``, ``tag/v3.0.0``), so the
    caller only ever requests a URL this function has vouched for rather than
    the string a server handed us. Query and fragment are dropped with it.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname != GITHUB_HOST:
            return None
        if parts.port not in (None, 443):
            return None
    except ValueError:
        return None
    match = _RELEASE_PATH.match(parts.path)
    if match is None:
        return None
    owner, repo, rest = match.groups()
    if owner.lower() not in ALLOWED_OWNERS:
        return None
    if repo.strip('.') == '':
        return None
    return ('https://%s/%s/%s/releases/' % (GITHUB_HOST, owner, repo), rest)

class UpdateChecker:

    def __init__(self, player_manager: 'PlayerManager_type'):
        self.playerManager = player_manager
        self.has_notified = False
        self.new_version = None
        self.last_check = None
        self.release_url = release_url

    def _resolve_latest(self, base: str):
        """``(version, base)`` for the newest release under ``base``.

        ``base`` comes back because it may not be the one that went in: GitHub
        redirects the old URL of a moved repository, and the answer is only
        meaningful together with the place it came from. ``None`` if the chain
        does not end at a tag under an owner we trust (see ``ALLOWED_OWNERS``).
        """
        url = base + 'latest'
        seen = {url}
        deadline = time.monotonic() + CHAIN_BUDGET
        for hop in range(MAX_HOPS):
            remaining = deadline - time.monotonic()
            if hop and remaining <= 0:
                log.warning('Release lookup ran out of time.')
                return None
            response = requests.get(url, allow_redirects=False, timeout=(3, max(1.0, min(10.0, remaining))))
            if response.status_code not in _REDIRECTS:
                log.warning('Release page returned bad status code %s.', response.status_code)
                return None
            sent = response.headers.get('location')
            if not sent:
                log.warning('Release redirect carried no location.')
                return None
            location = urljoin(url, sent)
            target = parse_release_url(location)
            if target is None:
                log.warning('Refusing to follow release redirect to %s.', location)
                return None
            base, rest = target
            if rest.startswith('tag/'):
                tag = rest[4:].strip('/')
                if not _VERSION_TAG.match(tag):
                    log.warning('Release tag is not a version: %r', tag)
                    return None
                return (tag[1:] if tag[:1] in ('v', 'V') else tag, base)
            if rest != 'latest':
                log.warning('Release redirect went somewhere unexpected: %s', location)
                return None
            url = base + rest
            if url in seen:
                log.warning('Release redirect loops through %s.', url)
                return None
            seen.add(url)
        log.warning('Release page redirected too many times.')
        return None

    def _check_updates(self):
        log.info('Checking for updates...')
        for base in release_urls:
            try:
                found = self._resolve_latest(base)
                if found is None:
                    continue
                version, base = found
                self.release_url = base
                if is_newer(version, CLIENT_VERSION):
                    self.new_version = version
                    break
                log.info('Up to date (running %s, latest release %s).', CLIENT_VERSION, version)
            except Exception:
                log.error('Could not check for updates.', exc_info=True)
        return self.new_version is not None

    def check(self):
        if not settings.check_updates:
            return
        if self.last_check is not None and (datetime.datetime.utcnow() - self.last_check).total_seconds() < one_day:
            log.info('Update check performed in last day. Skipping.')
            return
        self.last_check = datetime.datetime.utcnow()
        if self.new_version is not None or self._check_updates():
            if settings.update_skip_version == self.new_version:
                log.info('Update %s available; the user asked to skip it.', self.new_version)
                return
            if not self.has_notified and settings.notify_updates_wanted():
                self.has_notified = True
                log.info('Update Available: {0}'.format(self.new_version))
                self.notify()

    def notify(self):
        """Surface the available update. When a UI is running (it sets
        ``notify_update``) the notice goes to the browser window; otherwise it
        falls back to an MPV OSD toast for CLI/headless users.

        **Inside a Flatpak that updates itself, the notice says how to
        install it** -- ``flatpak update``, since the releases page cannot
        install anything there -- but it still carries the link. [iw], once
        the notice stopped being on by default there: somebody who switched
        it back on may well want to read the release notes, and that is what
        the page is good for even where it cannot deliver the build.
        """
        from . import hostinfo
        managed = hostinfo.flatpak_managed()
        notify_ui = getattr(self.playerManager, 'notify_update', None)
        if notify_ui is not None:
            try:
                notify_ui(self.new_version, self.release_url + 'latest')
                return
            except Exception:
                log.error('Could not send update notice to the UI.', exc_info=True)
        text = _('MPV Shim v{0} Update Available\nOpen menu (press c) for details.') if not managed else _('MPV Shim v{0} Update Available\nRun "flatpak update" to install it.')
        self.playerManager.show_text(text.format(self.new_version), 5000, 1)

    def open(self):
        self.playerManager.set_fullscreen(False)
        try:
            webbrowser.open(self.release_url + 'latest')
        except Exception:
            log.error('Could not open release URL.', exc_info=True)

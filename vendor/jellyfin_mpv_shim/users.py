"""Local multi-user support ("fast user switching").

Jellyfin has no concept of switching between local accounts on one device;
because this client owns its own UI we can offer it. A *user* here is a local
grouping of one or more server logins that are connected together, only one is
active at a time, and each carries its own Jellyfin **device id** so two users
on the same physical server do not collide on one server-side session.

A user may be PIN-protected -- a parental-control affordance, *not* a security
boundary, since the PIN is only salted-hashed. Persistence uses the current
Windows user DPAPI vault. Legacy users.json preserves all profiles; cred.json
migrates into a default profile with the original device identity. Old files
are archived encrypted before their plaintext copies are removed.

What is per-user versus per-server, and how this meets ``clientManager``:
docs/architecture.md section 7.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from pathlib import Path
from personal_cinema.runtime_paths import vault as _credential_vault
from personal_cinema.credentials import CredentialError
from personal_cinema.player_launcher import ProfileVault
import hashlib
import hmac
import json
import logging
import os
import os.path
import threading
import uuid
from . import conffile
from .conf import settings
from .constants import APP_NAME
from .i18n import _
log = logging.getLogger('users')
USERS_FILE = 'users.json'
DEFAULT_USER_NAME = _('(default)')
BACKUP_SUFFIX = '.bak'

def _has_users(data):
    """Is this a registry payload with somebody in it?

    The one predicate for "usable", asked of the primary and of the backup
    with the same words, because the failure this guards against is a rule
    applied to one of them and not the other.
    """
    return bool(isinstance(data, dict) and data.get('users'))

_PBKDF2_ROUNDS = 200000

def _hash_pin(pin: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', pin.encode('utf-8'), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS).hex()

def _new_id() -> str:
    return str(uuid.uuid4())

class UserManager:

    def __init__(self):
        self._lock = threading.RLock()
        self.users = []
        self.active_id = None
        self._loaded = False
        self.load_failed = False

    @staticmethod
    def _new_user(name, device_id=None, credentials=None, is_default=False):
        return {'id': _new_id(), 'name': name, 'device_id': device_id or _new_id(), 'default': is_default, 'pin_hash': None, 'pin_salt': None, 'require_pin_startup': False, 'last_server': None, 'auto_download': None, 'credentials': list(credentials or [])}

    def _path(self):
        return conffile.get(APP_NAME, USERS_FILE)

    @staticmethod
    def _migrate_legacy_credentials(raw):
        """Bring a raw cred.json payload into the flat list shape clients.py
        expects (mirrors the old ClientManager.load_credentials migration)."""
        if isinstance(raw, dict) and 'Servers' in raw:
            migrated = []
            for server in raw['Servers']:
                server['uuid'] = _new_id()
                server['username'] = ''
                migrated.append(server)
            return migrated
        if isinstance(raw, list):
            return raw
        return []

    def _load_legacy_cred_json(self):
        location = conffile.get(APP_NAME, 'cred.json')
        if not os.path.exists(location):
            return []
        try:
            with open(location, encoding='utf-8-sig') as cf:
                return self._migrate_legacy_credentials(json.load(cf))
        except Exception as exc:
            raise CredentialError('旧播放器账户文件无法读取，原文件未迁移或删除。') from exc

    @staticmethod
    def _read_json(path):
        """The parsed contents of one file, or None if it is not there or
        will not parse. The caller decides what either means."""
        try:
            with open(path, encoding='utf-8-sig') as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception:
            log.warning('Could not read %s', path, exc_info=True)
            return None

    def _protected_registry(self):
        return ProfileVault(_credential_vault(), Path(self._path()).parent)

    def _remove_migrated_plaintext(self, registry):
        folder = Path(self._path()).parent.resolve()
        # Only this instance's known legacy credential files; never recurse.
        for pattern in ('users.json', 'users.json.*', 'cred.json', 'cred.json.*'):
            for file in folder.glob(pattern):
                if file.is_file() and not file.is_symlink() and file.resolve().parent == folder:
                    registry.archive_legacy(file)

    def load(self):
        """Load encrypted profiles, migrating plaintext only after a durable save."""
        with self._lock:
            if self._loaded:
                return
            registry = self._protected_registry()
            recovered = False
            try:
                data = registry.read()
            except CredentialError:
                data = registry.read('backup')
                if not _has_users(data):
                    self.load_failed = True
                    raise CredentialError('无法读取播放器账户，请使用原 Windows 账户恢复；现有记录未覆盖。')
                recovered = True
            migrated = data is None
            if migrated:
                data = self._read_json(self._path())
                if not _has_users(data):
                    data = self._read_json(self._path() + BACKUP_SUFFIX)
                if not _has_users(data):
                    if os.path.exists(self._path()) or os.path.exists(self._path() + BACKUP_SUFFIX):
                        self.load_failed = True
                        raise CredentialError('旧播放器用户记录损坏，原文件未迁移或删除。')
                    default = self._new_user(DEFAULT_USER_NAME, device_id=settings.client_uuid,
                        credentials=self._load_legacy_cred_json(), is_default=True)
                    data = {'users': [default], 'active': default['id']}
            if not _has_users(data):
                self.load_failed = True
                raise CredentialError('播放器账户格式不正确，现有记录未覆盖。')
            self.users = [self._normalize(u) for u in data['users']]
            self.active_id = data.get('active')
            if not any(u['id'] == self.active_id for u in self.users):
                self.active_id = self.users[0]['id']
            self._loaded = True
            adopted = self._adopt_legacy_auto_download()
            if migrated or recovered or adopted:
                try:
                    self.save()
                except Exception:
                    self._loaded = False
                    raise
            self._remove_migrated_plaintext(registry)

    @staticmethod
    def _normalize(u):
        """Fill in any keys missing from an older users.json entry."""
        return {'id': u.get('id') or _new_id(), 'name': u.get('name') or DEFAULT_USER_NAME, 'device_id': u.get('device_id') or _new_id(), 'default': bool(u.get('default')), 'pin_hash': u.get('pin_hash'), 'pin_salt': u.get('pin_salt'), 'require_pin_startup': bool(u.get('require_pin_startup')), 'last_server': u.get('last_server'), 'auto_download': u.get('auto_download'), 'credentials': list(u.get('credentials') or [])}

    def save(self):
        with self._lock:
            payload = {'active': self.active_id, 'users': self.users}
            self._protected_registry().write(payload)
        return True

    def get(self, user_id):
        for u in self.users:
            if u['id'] == user_id:
                return u
        return None

    @property
    def active_user(self):
        return self.get(self.active_id)

    @property
    def active_device_id(self):
        u = self.active_user
        return u['device_id'] if u else settings.client_uuid

    def device_name_for(self, user):
        """Device name reported to Jellyfin. The default user keeps the plain
        player_name; every other user appends its name so the sessions are
        distinguishable in the Jellyfin dashboard."""
        base = settings.player_name
        if user is None or user.get('default'):
            return base
        return '{0} ({1})'.format(base, user['name'])

    @property
    def active_device_name(self):
        return self.device_name_for(self.active_user)

    def is_locked(self, user_id):
        u = self.get(user_id)
        return bool(u and u.get('pin_hash'))

    def startup_needs_unlock(self):
        """True when the active user is locked AND opted into a startup PIN."""
        u = self.active_user
        return bool(u and u.get('pin_hash') and u.get('require_pin_startup'))

    def public_users(self):
        """User list safe to hand to the UI process — no hashes/credentials."""
        return [{'id': u['id'], 'name': u['name'], 'locked': bool(u.get('pin_hash')), 'default': bool(u.get('default')), 'require_startup': bool(u.get('require_pin_startup'))} for u in self.users]

    def known_servers(self):
        """Distinct server addresses already used by any user, so a new/other
        user can be provisioned without retyping the URL. Addresses only (no
        tokens); the URL alone grants nothing without credentials."""
        seen = {}
        for u in self.users:
            for c in u.get('credentials', []):
                addr = (c.get('address') or '').rstrip('/')
                if addr and addr not in seen:
                    seen[addr] = {'address': addr, 'name': c.get('Name') or addr}
        return list(seen.values())

    def server_id_for(self, uuid):
        """The Jellyfin ``ServerId`` a saved login belongs to, or None.

        Our ``uuid`` identifies a saved *login*; Jellyfin's ``ServerId``
        identifies the *server*, and one server can carry several uuids --
        two accounts, or two addresses for one box. The download catalog
        scopes content by the second, because "do we hold this item" is a
        property of the server and the media rather than of who asked, so
        this is the translation between them and the only one.

        Single-valued, and that direction on purpose: many logins map to one
        server but never the reverse, so the inverse lookup (which this
        replaces) had to return a set and every caller had to decide what a
        set meant.

        It spans local users because the catalog does: there is one download
        store per machine rather than per user.

        **None means "this login resolves to no server".** It used to be
        documented as "cannot tell", reading as unscoped, and both halves of
        that are now false: `content_id_for` handles the two cases that wanted
        unscoped *before* reaching here -- no login named at all, and the
        browser's ``"offline"`` pseudo-server, which both answer `ANY_SERVER`
        -- and a `None` arriving at a content read now matches **no rows**
        rather than every one. Removing a server does not come through here
        either: its downloads keep the ``content_server_id`` they were written
        with. See `sync/db.py`'s `ANY_SERVER`.

        Read without ``_lock`` deliberately: ``save()`` holds that lock
        across a file write, and this is reached from under the player lock
        during a playback start. Safe because a credential's ``(Id, uuid)``
        pairing is written once and replaced whole, so an unlocked scan can
        miss an entry or see a superseded one but never a torn pair.
        """
        if not uuid:
            return None
        for u in list(self.users):
            for c in u.get('credentials') or ():
                if c.get('uuid') == uuid:
                    return c.get('Id') or None
        return None

    def actor_for(self, uuid):
        """The person behind a saved login, as ``(ServerId, UserId)``, or None.

        Both halves come straight off the credential -- ``Id`` is Jellyfin's
        server, ``UserId`` its account -- so this needs no network and works
        with every server unreachable, which is exactly when offline
        playback needs it.

        The pair rather than the uuid, because a uuid is a *delivery
        handle*: one server can carry several for several addresses, so two
        of them can be one person, and progress queued under one would be
        undrainable through the other. ``UserDataChanged`` announces itself
        with the same pair, which is the server agreeing about what
        identifies a person.

        None when the login is unknown, or when the credential names no
        account -- both mean "cannot say who", never "nobody". A caller
        recording progress must not file it against a guess.

        Unlocked for the reason in :meth:`server_id_for`.
        """
        if not uuid:
            return None
        for u in list(self.users):
            for c in u.get('credentials') or ():
                if c.get('uuid') == uuid:
                    server_id, user_id = (c.get('Id'), c.get('UserId'))
                    return (server_id, user_id) if user_id else None
        return None

    def server_name_for(self, server_id):
        """What to call a Jellyfin server on screen, or None.

        From any saved credential for it -- its ``Name``, or its address when
        the server never told us one. **Spans profiles**, like
        :meth:`server_id_for` and for the same reason: the download catalog is
        one store per machine, so a row can belong to a server only another
        local profile has a login for.

        None rather than the id when nothing names it: a raw ServerId on a
        tile is worse than no qualifier at all, and the caller is better
        placed to decide that.

        Unlocked for the reason in :meth:`server_id_for`.
        """
        if not server_id:
            return None
        for u in list(self.users):
            for c in u.get('credentials') or ():
                if c.get('Id') == server_id:
                    return c.get('Name') or c.get('address') or None
        return None

    def actor_on(self, server_id):
        """The **active** local profile's Jellyfin account on one server, or
        None.

        The offline counterpart to :meth:`actor_for`. With no live client
        there is no login object to ask, but the person at the keyboard is
        still whoever this profile is, so their credential for the server
        the media came from names them.

        Deliberately the active profile only, unlike every other lookup
        here: the others answer "who does this row belong to", which spans
        profiles because the catalog does, and this one answers "who is
        watching", which does not. Falling back to another profile's account
        would file one person's viewing against another.
        """
        if not server_id:
            return None
        user = self.active_user
        for c in (user or {}).get('credentials') or ():
            if c.get('Id') == server_id and c.get('UserId'):
                return c['UserId']
        return None

    def public_state(self):
        return {'active': self.active_id, 'users': self.public_users(), 'startup_locked': self.startup_needs_unlock(), 'known_servers': self.known_servers()}

    def profile_name_for(self, server_id, account_id):
        """The profile holding a saved login for this server account, or
        None. A download names the person who queued it this way."""
        for user in list(self.users):
            for cred in user.get('credentials') or ():
                if (cred.get('Id'), cred.get('UserId')) == (server_id, account_id):
                    return user.get('name')
        return None

    def credentials_of(self, user_id):
        """A copy of one user's saved credentials, or ``[]`` if there is no
        such user.

        By id rather than only for the active user, because a login is slow
        enough to outlive a user switch: the credential it is replacing then
        belongs to whoever *started* it, and reading the active list instead
        answers about the wrong person. Copied for the same reason as
        :meth:`credentials_for_active`.
        """
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return []
            return [dict(c) for c in u['credentials']]

    def credentials_for_active(self):
        """A copy of the active user's saved credentials for ClientManager to
        connect. Copied so ClientManager's live mutations don't touch the store
        until it explicitly saves back."""
        with self._lock:
            return self.credentials_of(self.active_id)

    def set_active_credentials(self, credentials):
        """Replace the active user's stored credentials (already cleaned of
        volatile runtime keys by the caller) and persist."""
        with self._lock:
            u = self.active_user
            if u is None:
                return
            u['credentials'] = [dict(c) for c in credentials]
        self.save()

    def get_last_server(self):
        """The uuid of the server the active user was last browsing, or None.

        The caller must treat this as a hint: the server may since have been
        removed or failed to connect, so it is only usable if it is still in
        the live server list.
        """
        with self._lock:
            u = self.active_user
            return u.get('last_server') if u else None

    def set_last_server(self, server_uuid):
        """Remember which server the active user is browsing.

        Called on every server switch, so it no-ops when unchanged rather than
        rewriting the encrypted user registry on each navigation.
        """
        with self._lock:
            u = self.active_user
            if u is None or u.get('last_server') == server_uuid:
                return
            u['last_server'] = server_uuid
        self.save()

    @staticmethod
    def _auto_download_pairs(u):
        """One profile's auto-download accounts, as ``(ServerId, UserId)``.

        Tolerant of a hand-edited file: an entry that is not a pair of two
        non-empty strings is skipped, because this is read from the download
        scheduler's thread where raising would stop the whole pass.
        """
        out = []
        for entry in u.get('auto_download') or ():
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            server_id, user_id = entry
            if server_id and user_id:
                out.append((server_id, user_id))
        return out

    def auto_download_accounts(self):
        """Every account on this machine with unattended downloading on.

        **Across profiles on purpose.** R14 first asked for a picker gate --
        *"auto downloads only run when the account that turned it on is
        selected in the picker"* -- and then withdrew it once R11's own
        reason was shown to cover it: the constraint is resource usage, not
        liveness. So the account is the unit and who is at the keyboard does
        not enter into it. Restoring a gate here is a behaviour change that
        needs its own ruling, not a tidy-up.
        """
        with self._lock:
            return {pair for u in list(self.users) for pair in self._auto_download_pairs(u)}

    def auto_download_on(self, uuid):
        """Is unattended downloading on for the account behind this login?

        Asked with a *login* uuid because that is what every caller holds --
        the Servers tab draws one row per credential and the scheduler
        iterates live clients -- while the answer is keyed on the account.
        That is the whole point of R14: one server answering at two
        addresses is two login uuids and one account, and a list of uuids
        left unattended fetching configured, enabled, and silently doing
        nothing for the whole of a trip.
        """
        account = self.actor_for(uuid)
        return bool(account) and account in self.auto_download_accounts()

    def auto_download_logins(self):
        """Every saved login whose **account** has unattended downloading on.

        `auto_download_on`'s answer for every uuid at once, and it exists
        because of what the lock costs. `_lock` is held by `save()` across two
        durable file writes and two directory fsyncs -- which is why
        `server_id_for` and `actor_for` are documented as deliberately never
        taking it -- and the Servers tab asked once per credential per
        repaint, on the render path. Ticking the checkbox calls `save()`, so
        the next frame blocked for the whole of that write, once per row.

        **Still keyed on the account** (R14): one server answering at two
        addresses is two uuids and one permission, so both of its logins come
        back here or neither does. A set of uuids is the shape the caller
        wants; the account is still what decides membership.

        `_lock` is an `RLock`, so calling `auto_download_accounts` inside it
        is free and keeps one definition of which pairs are on rather than a
        second copy of the comprehension.
        """
        with self._lock:
            on = self.auto_download_accounts()
            return {c['uuid'] for u in list(self.users) for c in u.get('credentials') or () if c.get('uuid') and (c.get('Id'), c.get('UserId')) in on}

    def set_auto_download(self, uuid, enabled):
        """Turn unattended downloading on or off for a login's account.

        Returns whether anything changed; False also covers a login this
        registry cannot resolve to an account, which has nothing to store.

        On is written into the profile holding the credential -- derivable
        rather than guessed, since a uuid appears in exactly one profile's
        list, which is the fact R21 leans on. **Off is removed from every
        profile**, so that what the checkbox says is true: the pair is one
        account's setting, and two profiles holding the same account hold
        the same fact about it, not two.
        """
        with self._lock:
            account, owner = (None, None)
            for u in self.users:
                for c in u.get('credentials') or ():
                    if c.get('uuid') == uuid and c.get('Id') and c.get('UserId'):
                        account, owner = ((c['Id'], c['UserId']), u)
                        break
                if account is not None:
                    break
            if account is None:
                return False
            changed = False
            for u in self.users:
                current = self._auto_download_pairs(u)
                want = list(current)
                if enabled:
                    if u is owner and account not in want:
                        want.append(account)
                else:
                    want = [p for p in want if p != account]
                if want != current:
                    u['auto_download'] = [list(p) for p in want]
                    changed = True
        if changed:
            self.save()
        return changed

    def _adopt_legacy_auto_download(self):
        """Move the global auto-download allow-list in here, as accounts.

        R14's real migration. `conf.Settings` has no per-profile dimension
        and `object_types` would not take a map, so the list belongs in the
        file that already holds one credential list per profile -- and it is
        derivable rather than lossy, because a ticked login uuid appears in
        exactly one profile's credentials.

        **`None` per profile means "not adopted", `[]` means "adopted,
        nothing ticked".** That marker, rather than the config key being
        empty, is what makes this idempotent: clearing the key is best
        effort (a session that could not *read* conf.json refuses to write
        it), and without the marker a failed clear would undo an untick on
        the next launch.

        Called with `_lock` held, from `load` only.
        """
        raw = (settings.auto_download_servers or '').strip()
        if not raw:
            return False
        wanted = {s.strip() for s in raw.split(',') if s.strip()}
        changed = False
        for u in self.users:
            if u.get('auto_download') is not None:
                continue
            picked = []
            for c in u.get('credentials') or ():
                if c.get('uuid') not in wanted:
                    continue
                if not (c.get('Id') and c.get('UserId')):
                    continue
                pair = [c['Id'], c['UserId']]
                if pair not in picked:
                    picked.append(pair)
            u['auto_download'] = picked
            changed = True
        if not changed:
            return False
        creds = {c['uuid']: c for u in self.users for c in u.get('credentials') or () if c.get('uuid')}
        for uuid in sorted(wanted - set(creds)):
            log.warning('Auto-download was enabled for server %s, which no saved login matches any more; it is off now.', uuid)
        for uuid in sorted((u for u in wanted & set(creds) if not (creds[u].get('Id') and creds[u].get('UserId')))):
            log.warning('Auto-download was enabled for login %s, whose saved credential names no account; re-tick it in Settings -> Servers.', uuid)
        try:
            settings.auto_download_servers = None
            settings.save()
        except Exception:
            log.warning('Could not clear the legacy auto-download list from the config.', exc_info=True)
        return True

    def append_credentials_for(self, user_id, credential):
        """File one (cleaned) credential under a specific user, **replacing
        any entry that already carries the same uuid**.

        Used when a login finishes after the initiating user was switched
        away: the server must land under the user who added it, not whoever
        is active by then. Returns whether the user still exists.

        Replacing rather than appending because two credentials under one
        uuid is never a valid state, however it is reached -- the switcher
        keys by uuid so the duplicate is invisible, every save writes both,
        and every connect races them. A re-authentication that finished
        after a user switch reached exactly that, because the in-place
        replacement on the other branch of `_finalize_login` does not cover
        this path. Doing it here fixes every caller rather than that one.
        """
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return False
            uuid_ = credential.get('uuid')
            for index, existing in enumerate(u['credentials']):
                if uuid_ and existing.get('uuid') == uuid_:
                    u['credentials'][index] = dict(credential)
                    break
            else:
                u['credentials'].append(dict(credential))
        self.save()
        return True

    def add_user(self, name):
        name = (name or '').strip() or _('New User')
        with self._lock:
            user = self._new_user(name)
            self.users.append(user)
        self.save()
        return user

    def rename_user(self, user_id, name):
        name = (name or '').strip()
        if not name:
            return False
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return False
            u['name'] = name
        self.save()
        return True

    def delete_user(self, user_id):
        """Remove a user and its saved logins. The active user can't be deleted
        (switch away first) and the last remaining user can't be deleted."""
        with self._lock:
            if user_id == self.active_id:
                return (False, _('Switch to another user before deleting this one.'))
            if len(self.users) <= 1:
                return (False, _('At least one user is required.'))
            u = self.get(user_id)
            if u is None:
                return (False, _('User not found.'))
            self.users = [x for x in self.users if x['id'] != user_id]
        self.save()
        return (True, None)

    def set_active(self, user_id):
        """Point active at user_id (data only — ClientManager owns connecting).
        Returns the newly active user dict or None."""
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return None
            self.active_id = user_id
        self.save()
        return u

    def verify_pin(self, user_id, pin):
        u = self.get(user_id)
        if not u or not u.get('pin_hash') or (not u.get('pin_salt')):
            return False
        if pin is None:
            return False
        candidate = _hash_pin(pin, u['pin_salt'])
        return hmac.compare_digest(candidate, u['pin_hash'])

    def set_pin(self, user_id, pin, require_startup=False):
        """Set (or, with a falsy pin, clear) a user's PIN. Returns success."""
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return False
            if not pin:
                u['pin_hash'] = None
                u['pin_salt'] = None
                u['require_pin_startup'] = False
            else:
                salt = os.urandom(16).hex()
                u['pin_hash'] = _hash_pin(pin, salt)
                u['pin_salt'] = salt
                u['require_pin_startup'] = bool(require_startup)
        self.save()
        return True
userManager = UserManager()

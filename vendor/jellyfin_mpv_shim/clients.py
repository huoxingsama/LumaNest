from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from jellyfin_apiclient_python import JellyfinClient
from jellyfin_apiclient_python.connection_manager import CONNECTION_STATE
from .conf import settings
from . import conffile
from .users import userManager
from getpass import getpass
from .constants import CAPABILITIES, CLIENT_VERSION, USER_APP_NAME, USER_AGENT, APP_NAME, CONNECT_BUSY, CONNECT_SIGNED_OUT, CONNECT_UNREACHABLE, REAUTH_WRONG_SERVER
from .utils import answering_server, resolved_host_is_private
from .i18n import _
import os.path
import json
import uuid
import time
import logging
import re
import threading
import socket
import ipaddress
from collections import OrderedDict
from urllib.parse import urlparse

def get_local_ips():
    local_ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            local_ips.append(ipaddress.ip_address(s.getsockname()[0]))
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ipaddress.ip_address(info[4][0])
            if ip not in local_ips and ip.is_private:
                local_ips.append(ip)
    except socket.gaierror:
        pass
    return local_ips

def extract_host(url):
    parsed = urlparse(url)
    return parsed.hostname

def is_ipv4(host):
    try:
        ipaddress.IPv4Address(host)
        return True
    except ipaddress.AddressValueError:
        return False

def is_local_subnet(host, local_ips, prefix_length=24):
    try:
        target_ip = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return False
    if not target_ip.is_private:
        return False
    for local_ip in local_ips:
        net = ipaddress.ip_network(f'{local_ip}/{prefix_length}', strict=False)
        if target_ip in net:
            return True
    return False
log = logging.getLogger('clients')
path_regex = re.compile('^(https?://)?(?:(\\[[^/]+\\])|([^/:]+))(:[0-9]+)?(/.*)?$')
QUICK_CONNECT_POLL_SECS = 3
QUICK_CONNECT_TIMEOUT_SECS = 300
VERIFY_RETRY_WINDOW_SECS = 600

class QuickConnectError(Exception):
    """Raised when a Quick Connect login cannot be completed.

    The message is user-facing (already translated) so callers can surface it
    directly in the CLI or GUI.
    """
from typing import Optional
VOLATILE_CREDENTIAL_KEYS = frozenset({'connected', 'cast_ready'})

def clean_credentials_for_save(credentials):
    """Return a copy of the credentials list with volatile runtime keys removed.

    Copies each server dict so the live dicts (which other threads read) are
    left untouched.
    """
    cleaned = []
    for server in credentials:
        cleaned.append({k: v for k, v in server.items() if k not in VOLATILE_CREDENTIAL_KEYS})
    return cleaned

def expo(max_value: Optional[int]=None):
    n = 0
    while True:
        a = 2 ** n
        if max_value is None or a < max_value:
            yield a
            n += 1
        else:
            yield max_value

class PeriodicHealthCheck(threading.Thread):

    def __init__(self, callback):
        self.halt = False
        self.trigger = threading.Event()
        self.callback = callback
        threading.Thread.__init__(self, daemon=True)

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join(1.0)

    def run(self):
        while not self.halt:
            if not self.trigger.wait(settings.health_check_interval):
                try:
                    self.callback()
                except Exception:
                    log.exception('Client health check failed.')

class ClientManager(object):

    def __init__(self):
        self.callback = lambda client, event_name, data: None
        self.on_servers_changed = lambda: None
        self.on_server_connected = lambda: None
        self.credentials = []
        self.clients = {}
        self.usernames = {}
        self._client_lock = threading.RLock()
        self._connecting = set()
        self._removed_uuids = set()
        self._connect_failures = {}
        self._server_on_lan = {}
        self._lan_probe = {}
        self._lan_seq = 0
        self._stop_event = threading.Event()
        self.device_id = settings.client_uuid
        self.device_name = settings.player_name
        self._switching = threading.Event()
        self._switch_lock = threading.RLock()
        self._user_generation = 0
        self.health_check = None
        if settings.health_check_interval is not None:
            self.health_check = PeriodicHealthCheck(self.check_all_clients)
            self.health_check.start()

    @staticmethod
    def _get_cli_credential_args():
        from .args import get_args
        a = get_args()
        if a.server and a.username:
            return (a.server, a.username, a.password)
        return None

    def _find_existing_credential(self, server: str, username: str):
        """Find an existing credential matching the given server and username."""
        if server.endswith('/'):
            server = server[:-1]
        for cred in self.credentials:
            cred_address = cred.get('address', '').rstrip('/')
            cred_username = cred.get('username', '')
            if cred_address == server and cred_username == username:
                return cred
        return None

    @staticmethod
    def _abandon(client):
        """Tear down a client we built and are not going to keep.

        A `JellyfinClient` owns a `requests.Session` from construction, and a
        Quick Connect exchange has been talking over it -- so a refusal that
        simply returns leaks one per attempt, on exactly the paths a user
        retries. Safe on a client that never started anything: the websocket,
        the session and the ping poller each no-op when absent, and
        `client_factory` passes `allow_multiple_clients`, so this cannot reach
        the apiclient's process-wide websocket stop.
        """
        ClientManager._stop_client(client)

    @staticmethod
    def _stop_client(client):
        """Stop an apiclient client; never raises. Every stop goes through here.

        The apiclient's WSClient assigns its KeepAlive before starting it, so a
        stop landing between the two joins an unstarted thread and raises
        RuntimeError (seen from remove_client in a Windows e2e run). The raise
        also skips the rest of the apiclient's stop -- the websocket close,
        the HTTP session, the ping thread -- so it is retried once: the window
        is two adjacent lines, and by then the thread has started and the join
        succeeds. Unguarded, a raise also abandoned every later client in
        stop_all_clients. tests/test_client_stop.py.
        """
        for attempt in (1, 2):
            try:
                client.stop()
                return
            except Exception:
                if attempt == 2:
                    log.warning('could not stop a client cleanly', exc_info=True)
                    return
                log.debug('client stop raised; retrying once', exc_info=True)
                time.sleep(0.05)

    def _update_account(self, server: str, username: str, password: str):
        """Sign a saved account back in from the CLI, keeping its identity.

        **Not remove-then-add**, which is what this was: it deleted the
        credential and saved that *before* trying the password, so a typo
        destroyed a working login -- and a success minted a fresh uuid, the key
        the download catalog's `server_uuid` column and the auto-download
        allow-list are written in. `reauthenticate` is the same gesture done
        properly, and the browser has used it since it existed; this was the
        last site on the old path. CX4.

        False is a log line and nothing else: `cli_connect` clears
        `cli_creds` either way, so a failed update does **not** fall through to
        adding the server -- which is the point, since the credential it would
        have added is the one that is already there.
        """
        existing = self._find_existing_credential(server, username)
        if existing is None:
            return False
        ok, _reason = self.reauthenticate(existing['uuid'], username, password, address=server)
        return ok

    def _cli_quick_connect(self, server: str):
        """Run a Quick Connect login from the terminal, printing the code."""

        def show_code(code):
            print(_('Open Jellyfin, go to your user menu -> Quick Connect, and enter this code:'))
            print('    ' + code)
            print(_('Waiting for authorization...'))
        try:
            return self.login_with_quick_connect(server, code_callback=show_code)
        except QuickConnectError as e:
            log.warning(str(e))
            return False

    def cli_connect(self):
        from .args import get_args
        cli_commands = set(get_args().command or [])
        is_logged_in = self.try_connect()
        add_another = 'add' in cli_commands
        clear_accounts = 'clear' in cli_commands
        use_quick_connect = get_args().quick_connect
        server_arg = get_args().server
        cli_creds = self._get_cli_credential_args()
        if clear_accounts:
            log.info(_('Clearing all existing accounts.'))
            self.remove_all_clients()
            is_logged_in = False
        if use_quick_connect and server_arg:
            cli_creds = None
            if not is_logged_in or add_another or clear_accounts:
                if self._cli_quick_connect(server_arg):
                    log.info(_('Successfully added server.'))
                    is_logged_in = True
                else:
                    log.warning(_('Adding server failed.'))
        if cli_creds:
            server, username, password = cli_creds
            if not clear_accounts:
                existing = self._find_existing_credential(server, username)
                if existing is not None:
                    log.info(_('Account already exists. Updating credentials.'))
                    if self._update_account(server, username, password):
                        log.info(_('Successfully updated server credentials.'))
                        is_logged_in = True
                    else:
                        log.warning(_('Updating server credentials failed.'))
                    cli_creds = None
            if cli_creds and (not is_logged_in or add_another or clear_accounts):
                is_logged_in = self.login(server, username, password)
                if is_logged_in:
                    log.info(_('Successfully added server.'))
                else:
                    log.warning(_('Adding server failed.'))
        while not is_logged_in or add_another:
            server = input(_('Server URL: '))
            if use_quick_connect:
                is_logged_in = self._cli_quick_connect(server)
            else:
                username = input(_('Username: '))
                try:
                    password = getpass(_('Password: '))
                except (EOFError, OSError):
                    password = ''
                is_logged_in = self.login(server, username, password)
            if is_logged_in:
                log.info(_('Successfully added server.'))
                add_another = input(_('Add another server?') + ' [y/N] ')
                add_another = add_another in ('y', 'Y', 'yes', 'Yes')
            else:
                log.warning(_('Adding server failed.'))

    def client_factory(self):
        client = JellyfinClient(allow_multiple_clients=True)
        client.config.data['app.default'] = True
        client.config.app(USER_APP_NAME, CLIENT_VERSION, self.device_name, self.device_id)
        client.config.data['http.user_agent'] = USER_AGENT
        client.config.data['auth.ssl'] = not settings.ignore_ssl_cert
        if settings.tls_client_cert:
            client.config.data['auth.tls_client_cert'] = settings.tls_client_cert
            client.config.data['auth.tls_client_key'] = settings.tls_client_key
            client.config.data['auth.tls_server_ca'] = settings.tls_server_ca
            client.auth.create_session_with_client_auth()
        return client

    def credential_client(self, server_id, user_id):
        """A client for one person built from their saved credential alone.

        D2 (ruled 2026-09-26): a download queued by a profile that is not
        active is fetched with THAT profile's saved credential. So this looks
        in every profile, not just the active one, for a login whose server
        and account are exactly this pair, and brings up an HTTP session with
        its token -- no websocket and no posted capabilities (Izzie: "it
        doesn't need a websocket connection or posted session capabilities,
        just a credential"). It is not registered in `self.clients`, so
        nothing else in the app sees it or speaks through it.

        Returns ``(uuid, client)`` or None when no such credential exists.
        The caller owns the client and must `stop()` it.
        """
        from .users import userManager
        for user in list(userManager.users):
            for cred in user.get('credentials') or ():
                if (cred.get('Id'), cred.get('UserId')) != (server_id, user_id):
                    continue
                token = cred.get('AccessToken')
                address = cred.get('address')
                if not (token and address):
                    continue
                client = self.client_factory()
                client.config.auth(address, user_id, token, not settings.ignore_ssl_cert)
                client.logged_in = True
                client.start(websocket=False)
                return (cred.get('uuid'), client)
        return None

    def _connect_all(self):
        is_logged_in = False
        local_ips = get_local_ips()

        def connection_priority(server):
            host = extract_host(server['address'])
            if is_ipv4(host) and is_local_subnet(host, local_ips):
                return 0
            if is_ipv4(host):
                try:
                    if ipaddress.IPv4Address(host).is_private:
                        return 1
                except ipaddress.AddressValueError:
                    pass
            return 2
        sorted_credentials = sorted(self.credentials, key=connection_priority)
        chains = OrderedDict()
        for server in sorted_credentials:
            chains.setdefault(server['Id'], []).append(server)
        chains = list(chains.values())
        if not chains:
            return False
        prefer_uuid = None
        try:
            prefer_uuid = userManager.get_last_server()
        except Exception:
            log.debug('Could not read the remembered server.', exc_info=True)
        preferred = None
        if prefer_uuid:
            for index, chain in enumerate(chains):
                if any((s.get('uuid') == prefer_uuid for s in chain)):
                    preferred = index
                    break
        ready = threading.Event()
        done = threading.Event()
        state = {'connected': 0, 'finished': 0}
        lock = threading.Lock()

        def run(index, chain):
            ok = False
            try:
                for server in chain:
                    if self.is_stopping:
                        break
                    if self.connect_client(server):
                        ok = True
                        break
            except Exception:
                log.error('Connecting to server failed.', exc_info=True)
            with lock:
                if ok:
                    state['connected'] += 1
                state['finished'] += 1
                all_done = state['finished'] == len(chains)
                late = ok and ready.is_set()
                release = ok if preferred is None else ok and index == preferred
                if release or all_done:
                    ready.set()
                if all_done:
                    done.set()
            if late:
                try:
                    self.on_server_connected()
                except Exception:
                    log.debug('Late-connect notification failed.', exc_info=True)
        threads = []
        for index, chain in enumerate(chains):
            thread = threading.Thread(target=run, args=(index, chain), daemon=True, name='connect-%d' % index)
            threads.append(thread)
            thread.start()
        ready.wait()
        with lock:
            is_logged_in = state['connected'] > 0
            pending = len(chains) - state['finished']
        if pending > 0:
            log.info('Rendering with the preferred server; %d still connecting.', pending)
        return is_logged_in

    def load_credentials(self):
        """Load the active user's saved credentials. Fast (no network) — call
        this before connecting so callers know which servers exist up front.

        userManager owns persistence now (users.json); on first run it migrates
        the legacy cred.json into a "(default)" user. We adopt that user's
        device id / name so its servers see the right device."""
        userManager.load()
        self._adopt_active_user()

    def _adopt_active_user(self):
        """Point our live credential list + device identity at the active user."""
        self.credentials = userManager.credentials_for_active()
        self.device_id = userManager.active_device_id
        self.device_name = userManager.active_device_name

    def connect_all(self):
        """Connect to all loaded credentials (call load_credentials first),
        honouring connect_retry_mins. Returns whether any server connected."""
        is_logged_in = self._connect_all()
        if settings.connect_retry_mins and (not is_logged_in):
            log.warning('Connection failed. Will retry for {0} minutes.'.format(settings.connect_retry_mins))
            for attempt in range(settings.connect_retry_mins * 2):
                if self._stop_event.wait(30):
                    break
                is_logged_in = self._connect_all()
                if is_logged_in:
                    break
        return is_logged_in

    def try_connect(self):
        self.load_credentials()
        return self.connect_all()

    def save_credentials(self):
        userManager.set_active_credentials(clean_credentials_for_save(self.credentials))

    @staticmethod
    def _normalize_server(server: str) -> str:
        if server.endswith('/'):
            server = server[:-1]
        protocol, ipv6_host, ipv4_host, port, path = path_regex.match(server).groups()
        if not protocol:
            log.warning('Adding http:// because it was not provided.')
            protocol = 'http://'
        if protocol == 'http://' and (not port):
            log.warning('Adding port 8096 for insecure local http connection.')
            log.warning('If you want to connect to standard http port 80, use :80 in the url.')
            port = ':8096'
        return ''.join(filter(bool, (protocol, ipv6_host, ipv4_host, port, path)))

    @staticmethod
    def _answering_server(client):
        """The server dict a connect has just filled in, or ``{}``.

        ``connect_to_address`` fetches ``/System/Info/Public`` and writes
        ``Id``, ``Name`` and ``Version`` into the credential store, so which
        server is on the other end is known before any password or Quick
        Connect code is exchanged. The same dict ``_finalize_login`` reads
        later, which is what keeps the early and late checks comparing the
        same thing.
        """
        return answering_server(client)

    @staticmethod
    def _replaces_the_same_server(replacing_uuid, server, credentials):
        """Whether re-authenticating ``replacing_uuid`` reached the server
        that uuid already stands for.

        The re-auth form lets the address be edited -- deliberately, because
        a server that moved is what it is for -- so the answer can come from
        a different server entirely. The uuid is what the download catalog's
        ``server_uuid`` column and the auto-download allow-list are written
        in, and what ``UserManager.server_id_for`` resolves to the catalog's
        content scope, so handing it over attributes every download from the
        old server to one that never had it.

        One rule, one function, three application points, and the ranking
        between them matters. The call inside ``_finalize_login`` is the
        **authority**: it is the one holding the switch lock, the one that
        knows which user the credential is being filed under, and the one
        standing between the answer and the write. Searching the *active*
        user's list there is what refused legitimate re-auths and got the
        first attempt reverted, because a login that outlived a user switch
        is filed under its initiator.

        The two calls in the re-auth entry points run earlier, against the
        credential those already looked up, and exist for what happens
        before the write rather than to protect it: they keep a password or
        a Quick Connect approval from being spent on a server that is about
        to be refused, and they are where the refusal can say *why*.

        Says yes whenever it cannot prove otherwise -- the credential is
        gone, or either side records no ``Id``. Refusing there would lock a
        user out of a server with no way back except remove-and-re-add,
        which mints a fresh uuid: the loss this whole path exists to
        prevent. The replacement writes the ``Id``, so a credential saved
        before it was kept becomes checkable after one re-auth.
        """
        prior = next((c for c in credentials or () if c.get('uuid') == replacing_uuid), None)
        if prior is None:
            return True
        old_id, new_id = (prior.get('Id'), server.get('Id'))
        if not old_id or not new_id or old_id == new_id:
            return True
        log.error('Refusing to sign back in to %s: that address answered as server %s, not %s. Add it as a new server instead.', replacing_uuid, new_id, old_id)
        return False

    def _finalize_login(self, client: 'JellyfinClient', username: str, owner_id=None, replacing_uuid=None):
        """Stash a freshly-authenticated client into our credential store.

        The client must already hold a valid AccessToken for its first server
        (either from a password login or a Quick Connect exchange).
        ``owner_id`` is the local user who initiated the login; if the active
        user changed while the (slow) login ran, the credential is filed under
        that user instead of leaking into whoever is active now.

        ``replacing_uuid`` re-authenticates a server that is already saved: the
        new token replaces the old credential *in place*, under the identity
        the rest of the app has written down (see ``reauthenticate``). Shared
        with the add-a-server path rather than written beside it, because
        everything else this does -- the user-switch handover, the removed-uuid
        interaction, tearing the client down again if a switch slipped in --
        applies identically and is not obvious from either call site.
        """
        credentials = client.auth.credentials.get_credentials()
        server = credentials['Servers'][0]
        if replacing_uuid:
            server['uuid'] = replacing_uuid
        else:
            server['uuid'] = str(uuid.uuid4())
        server['username'] = username
        with self._switch_lock:
            handed_off = owner_id is not None and owner_id != userManager.active_id
            if replacing_uuid and (not self._replaces_the_same_server(replacing_uuid, server, userManager.credentials_of(owner_id) if handed_off else self.credentials)):
                return False
            if handed_off:
                log.warning('Login finished after a user switch; filing the server under the original user.')
                return userManager.append_credentials_for(owner_id, clean_credentials_for_save([server])[0])
            with self._client_lock:
                self._removed_uuids.discard(server['uuid'])
            replaced = False
            if replacing_uuid:
                for index, cred in enumerate(self.credentials):
                    if cred.get('uuid') == replacing_uuid:
                        self.credentials[index] = server
                        replaced = True
                        break
            if not replaced:
                self.credentials.append(server)
            self.save_credentials()
        if replacing_uuid:
            self._disconnect_client(uuid=replacing_uuid)
        self.connect_client(server)
        if owner_id is not None:
            with self._switch_lock:
                if owner_id != userManager.active_id:
                    self._disconnect_client(server=server)
        return True

    def login(self, server: str, username: str, password: str, owner_id=None):
        """Add a server under a **new** identity. `reauthenticate` is the one
        that signs back in to a server already saved, keeping its uuid.

        ``owner_id``: the profile it is for, when that is not the active one
        -- an add made while a profile switch is in flight belongs to the
        profile that was on screen (Izzie, 2026-09-28)."""
        server = self._normalize_server(server)
        owner_id = owner_id or userManager.active_id
        client = self.client_factory()
        try:
            client.auth.connect_to_address(server)
            result = client.auth.login(server, username, password)
            if 'AccessToken' not in result:
                return False
            return self._finalize_login(client, username, owner_id=owner_id)
        finally:
            self._abandon(client)

    def quick_connect_initiate(self, server: str):
        """Start a Quick Connect request against ``server``.

        Returns ``(client, secret, code)``. The ``code`` must be shown to the
        user to enter in their (already authenticated) Jellyfin session.
        Raises QuickConnectError if the server is unreachable or has Quick
        Connect disabled.
        """
        server = self._normalize_server(server)
        client = self.client_factory()
        try:
            client.auth.connect_to_address(server)
            servers = client.auth.credentials.get_credentials().get('Servers')
            if not servers:
                raise QuickConnectError(_('Could not connect to the server.'))
            address = servers[0]['address']
            session = client.auth.session
            if not client.auth.API.quick_connect_enabled(address, session):
                raise QuickConnectError(_('Quick Connect is not enabled on this server.'))
            data = client.auth.API.quick_connect_initiate(address, session)
            if not data:
                raise QuickConnectError(_('Could not start Quick Connect.'))
        except Exception:
            self._abandon(client)
            raise
        return (client, data['Secret'], data['Code'])

    def quick_connect_wait(self, client, secret: str, should_cancel=None, replacing_uuid=None, owner_id=None):
        """Poll until the Quick Connect request is authorized, then log in.

        Returns True on success, False on timeout/cancellation/failure.
        ``should_cancel`` is an optional callable polled between attempts.
        ``replacing_uuid`` re-authenticates a server already saved under that
        identity rather than adding a new one -- see ``_finalize_login``.

        **Takes ownership of ``client``**: it is torn down here whatever
        happens, because the credential is what the caller wanted and the
        registry's client is built by `connect_client`. A wait the user walked
        away from ran for up to five minutes on that session. CR11.
        """
        try:
            return self._quick_connect_wait(client, secret, should_cancel, replacing_uuid, owner_id)
        finally:
            self._abandon(client)

    def _quick_connect_wait(self, client, secret, should_cancel, replacing_uuid, owner_id=None):
        address = client.auth.credentials.get_credentials()['Servers'][0]['address']
        session = client.auth.session
        owner_id = owner_id or userManager.active_id
        deadline = time.time() + QUICK_CONNECT_TIMEOUT_SECS
        authorized = False
        while time.time() < deadline:
            if should_cancel is not None and should_cancel():
                return False
            state = client.auth.API.quick_connect_state(address, secret, session)
            if state.get('Authenticated'):
                authorized = True
                break
            time.sleep(QUICK_CONNECT_POLL_SECS)
        if not authorized:
            log.warning('Quick Connect timed out waiting for authorization.')
            return False
        result = client.auth.login_with_quick_connect(address, secret)
        if 'AccessToken' not in result:
            log.warning('Quick Connect authentication failed.')
            return False
        return self._finalize_login(client, result['User']['Name'], owner_id=owner_id, replacing_uuid=replacing_uuid)

    def login_with_quick_connect(self, server: str, code_callback=None, should_cancel=None, owner_id=None):
        """High-level Quick Connect login.

        Initiates the request, hands the user-facing code to ``code_callback``,
        then blocks polling until authorized. Raises QuickConnectError on setup
        failure; returns True/False from the wait phase.
        """
        client, secret, code = self.quick_connect_initiate(server)
        try:
            if code_callback is not None:
                code_callback(code)
        except Exception:
            self._abandon(client)
            raise
        return self.quick_connect_wait(client, secret, should_cancel=should_cancel, owner_id=owner_id)

    def validate_client(self, client: 'JellyfinClient', dry_run=False, server=None):
        try:
            client_list = client.jellyfin.sessions(timeout=10, retry=1)
        except Exception:
            log.warning('Health check session query failed; treating as disconnected.', exc_info=True)
            client_list = []
        if client_list is None:
            log.warning('Client check failed, proceeding anyways. (Client list is unset.)')
            return True
        for f_client in client_list:
            if f_client.get('DeviceId') == self.device_id:
                break
        else:
            if not dry_run:
                log.warning('Client is not actually connected. (It does not show in the client list.)')
                client.callback = lambda *_: None
                client.callback_ws = lambda *_: None
                removed = server is not None and self._disconnect_client(server=server, expected_client=client)
                if not removed:
                    self._stop_client(client)
            return False
        return True

    def setup_client(self, client: 'JellyfinClient', server, do_retries=True):
        ws_error_backoff = [None]

        def event(event_name, data):
            if event_name == 'WebSocketError':
                gen = ws_error_backoff[0]
                if gen is None:
                    gen = ws_error_backoff[0] = expo(60)
                self._stop_event.wait(next(gen))
                self.callback(client, event_name, data)
                return
            if event_name == 'WebSocketDisconnect':
                timeout_gen = expo(100)
                if self.clients.get(server['uuid']) is client:
                    while not self.is_stopping and (not settings.work_offline):
                        timeout = next(timeout_gen)
                        log.info('No connection to server. Next try in {0} second(s)'.format(timeout))
                        self._disconnect_client(server=server, expected_client=client)
                        if self._stop_event.wait(timeout):
                            break
                        if self.connect_client(server, False):
                            self.on_server_connected()
                            break
            elif event_name == 'WebSocketConnect':
                ws_error_backoff[0] = None
                log.info('WebSocket connected, posting capabilities')
                for i in range(6):
                    if self.is_stopping:
                        break
                    try:
                        client.jellyfin.post_capabilities(CAPABILITIES)
                        break
                    except Exception:
                        if i == 5:
                            log.warning('Failed to post capabilities on connect', exc_info=True)
                        else:
                            time.sleep(2)
                self.callback(client, event_name, data)
            else:
                self.callback(client, event_name, data)
        client.callback = event
        client.callback_ws = event
        client.start(websocket=True)
        if do_retries:
            threading.Thread(target=self._verify_connected, args=(client, server), name='verify-connected', daemon=True).start()

    def _verify_connected(self, client, server):
        """Background confirmation that the websocket session registered with
        the server (needed only for cast / remote control; browsing already
        works via the token). On a confirmed "halfway connected" client — one
        that never shows up in the server's session list — rebuild it with
        bounded retries, the same remedy the startup path used to run
        synchronously, now off the UI's critical path."""
        if self._is_session_live(client, server):
            self._mark_cast_ready(server)
            return
        partial_reconnect_attempts = 3
        for i in range(partial_reconnect_attempts):
            if self.is_stopping:
                return
            log.warning(f'Partially connected. Retrying {i + 1}/{partial_reconnect_attempts}.')
            self._disconnect_client(server=server)
            if self._stop_event.wait(1):
                return
            if not self.connect_client(server, False):
                continue
            if self._is_session_live(self.clients.get(server['uuid']), server):
                self._mark_cast_ready(server)
                return
        self.on_servers_changed()
        backoff = expo(60)
        deadline = time.time() + VERIFY_RETRY_WINDOW_SECS
        while time.time() < deadline:
            if self._stop_event.wait(next(backoff)):
                return
            if settings.work_offline or self._switching.is_set():
                return
            with self._client_lock:
                if server['uuid'] in self._removed_uuids:
                    return
            if self._server_is_connected(server):
                return
            if self.connect_client(server, False):
                client = self.clients.get(server['uuid'])
                if client is not None and self.validate_client(client, dry_run=True):
                    self._mark_cast_ready(server)
                else:
                    self.on_servers_changed()
                self.on_server_connected()
                return
        self.on_servers_changed()

    def _mark_cast_ready(self, server):
        if not server.get('cast_ready'):
            server['cast_ready'] = True
            self.on_servers_changed()

    def _is_session_live(self, client, server=None):
        """True once this device shows in the server's session list. Probes
        once, then once more after a short delay (the session can take a moment
        to register after the websocket connects). The second probe is
        non-dry-run: pass the server dict so a failed client is removed from
        the registry (identity-checked), not just stopped in place."""
        if client is None:
            return False
        if self.validate_client(client, True):
            return True
        log.info('Not connected yet, waiting 3 seconds...')
        if self._stop_event.wait(3):
            return False
        if self.validate_client(client, server=server):
            log.info('Actually connected now.')
            return True
        return False

    def remove_client(self, uuid: str):
        with self._client_lock:
            self._removed_uuids.add(uuid)
            self._forget_server_state(uuid)
        with self._switch_lock:
            self.credentials = [server for server in self.credentials if server['uuid'] != uuid]
            self.save_credentials()
        self._disconnect_client(uuid=uuid)

    def _forget_server_state(self, uuid=None):
        """Drop the per-uuid answers about a server: why its last connect
        failed, whether it is on this network, and any locality lookup in
        flight. With `uuid`, that one server; without, all of them.

        One place because the three have one lifetime -- they are answers
        *about a credential*, and the Servers tab reads all three in the window
        between a credential list changing and the next connect landing. One of
        them surviving that is the tab saying something untrue about a server
        nothing has tried: "Signed out", with Sign In Again emphasised, or the
        wrong home/away icon. CR9.

        A removed-and-re-added server does get a fresh uuid, which would hide
        the first of those -- but that is a property of `_finalize_login` and
        not one to depend on from here, and it does nothing for the profile
        switch, where the uuids come back.

        The caller holds `_client_lock`.
        """
        if uuid is None:
            self._connect_failures.clear()
            self._server_on_lan.clear()
            self._lan_probe.clear()
            return
        self._connect_failures.pop(uuid, None)
        self._server_on_lan.pop(uuid, None)
        self._lan_probe.pop(uuid, None)

    def _record_connect_failure(self, uuid, client, server):
        """Work out *why* a connect failed, and remember it for the UI.

        The apiclient cannot be asked. `connect_to_server` answers
        `Unavailable` both when the address does not respond and when it
        responds and refuses our token -- the revoked-token branch
        (`validate_authentication_token` returning nothing) falls into the
        same return as a socket error. So the two are told apart here, by
        asking the one endpoint that needs no credential: if the server
        answers `/System/Info/Public`, it is up, and what it rejected was
        the saved login.

        Unauthenticated and on the apiclient's own session, so this adds no
        request path of its own and no place for a token to leak into
        (`docs/auth-headers.md`).
        """
        reason = CONNECT_UNREACHABLE
        try:
            if client.auth.API.get_public_info(server.get('address'), client.auth.session):
                reason = CONNECT_SIGNED_OUT
        except Exception:
            log.debug('Could not probe %s for a reachability verdict.', server.get('address'), exc_info=True)
        with self._client_lock:
            self._connect_failures[uuid] = reason
        log.info('Server %s did not connect (%s).', server.get('Name') or server.get('address'), reason)

    def connection_problem(self, uuid):
        """Why this server is not connected, or None if nothing is wrong with
        it as far as we know. See the CONNECT_* constants.

        `CONNECT_BUSY` is answered from live state rather than from the ledger:
        a connect in flight is not a failure, and storing it would leave the
        verdict standing if that connect never came back. `_connecting` is the
        same reservation that refuses the second caller, so the two answers
        cannot disagree.
        """
        with self._client_lock:
            if uuid in self.clients:
                return None
            if uuid in self._connecting:
                return CONNECT_BUSY
            return self._connect_failures.get(uuid)

    def uuid_for_client(self, client):
        """Our uuid for a live `JellyfinClient`, or None.

        The registry is uuid -> client, so this is the reverse read. It is
        the only way back from an object the media layer is holding to the
        server it came from, and `None` for a client we do not have
        registered -- including the fully-offline case, where there is no
        client at all.
        """
        if client is None:
            return None
        with self._client_lock:
            for uuid, candidate in self.clients.items():
                if candidate is client:
                    return uuid
        return None

    def server_is_local(self, uuid):
        """Is this server on our own network? ``True``/``False``, or ``None``
        when we have not connected to it and so have not asked.

        Resolved rather than read off the URL, which is the only way to be
        right about split-horizon DNS: a self-hoster's own domain answers
        with a LAN address at home and a public one from a hotel, and the
        address the user typed is identical in both.
        """
        with self._client_lock:
            return self._server_on_lan.get(uuid)

    def reauthenticate(self, uuid, username, password, address=None):
        """Sign back in to a server we already have, keeping its identity.

        **The uuid has to survive**, which is why this exists at all instead
        of "remove it and add it again" -- the only route there was. That
        route mints a fresh uuid, and the uuid is what the download catalog's
        `server_uuid` column and the auto-download allow-list are written in:
        re-adding a signed-out server therefore orphaned every download made
        from it, from the user's point of view by deleting them, while they
        were doing the one thing the UI offered.

        Returns ``(ok, reason)``, the shape ``retry_server`` already uses: on
        failure ``reason`` is a ``REAUTH_*`` constant or None, and it is what
        lets the form say the address named a different server rather than
        blaming the password.
        """
        with self._switch_lock:
            existing = next((c for c in self.credentials if c.get('uuid') == uuid), None)
        if existing is None:
            return (False, None)
        address = self._normalize_server(address or existing.get('address'))
        owner_id = userManager.active_id
        client = self.client_factory()
        try:
            client.auth.connect_to_address(address)
            if not self._replaces_the_same_server(uuid, self._answering_server(client), [existing]):
                return (False, REAUTH_WRONG_SERVER)
            result = client.auth.login(address, username, password)
            if 'AccessToken' not in result:
                return (False, None)
            return (bool(self._finalize_login(client, username, owner_id=owner_id, replacing_uuid=uuid)), None)
        finally:
            self._abandon(client)

    def reauthenticate_with_quick_connect(self, uuid, code_callback=None, should_cancel=None, address=None):
        """The passwordless half of `reauthenticate`, with the same promise
        about the uuid and the same ``(ok, reason)`` return."""
        with self._switch_lock:
            existing = next((c for c in self.credentials if c.get('uuid') == uuid), None)
        if existing is None:
            return (False, None)
        client, secret, code = self.quick_connect_initiate(address or existing.get('address'))
        if not self._replaces_the_same_server(uuid, self._answering_server(client), [existing]):
            self._abandon(client)
            return (False, REAUTH_WRONG_SERVER)
        try:
            if code_callback is not None:
                code_callback(code)
        except Exception:
            self._abandon(client)
            raise
        return (bool(self.quick_connect_wait(client, secret, should_cancel=should_cancel, replacing_uuid=uuid)), None)

    @staticmethod
    def _spawn(target, name, args=()):
        """Start a detached worker. The one place a thread is started for
        background work here, so a test about the *decision* such a worker
        makes can run it inline instead of racing the scheduler; the tests
        about the concurrency itself use the real one.
        """
        threading.Thread(target=target, args=args, name=name, daemon=True).start()

    def _start_lan_probe(self, uuid, address):
        """Ask, off this thread, whether `address` is on our own network.

        **Never on the connect path.** `getaddrinfo` takes no timeout and
        cannot be given one -- `socket.setdefaulttimeout` does not reach it
        on glibc and is process-global besides -- so a stalled resolver
        asked inline holds a client that is already registered and usable,
        keeps the uuid's in-flight reservation occupied so every other
        connector for that server is turned away, and delays every later
        address in the same `_connect_all` chain. To choose an icon.

        One outstanding lookup per (server, address), so N reconnects
        against a stuck resolver leave one thread rather than N. A different
        address is a different question -- a server that moved -- so it gets
        its own token, and the older probe finds itself superseded rather
        than answering for an address this server no longer has.
        """
        with self._client_lock:
            current = self._lan_probe.get(uuid)
            if current is not None and current[1] == address:
                return
            self._lan_seq += 1
            token = self._lan_seq
            self._lan_probe[uuid] = (token, address)

        def work():
            self._finish_lan_probe(uuid, address, token, resolved_host_is_private(address))
        self._spawn(work, 'lan-probe')

    def _finish_lan_probe(self, uuid, address, token, on_lan):
        """Record a locality answer, if it is still the one being waited on.

        Two ways it may not be. The lookup can be **superseded** -- a later
        connect for the same server at a different address -- which the
        token settles. Or the server can be **gone**: stop, removal and a
        user switch all take the client out of the registry, so asking
        whether it is still there covers the three with one question.
        """
        with self._client_lock:
            if self._lan_probe.get(uuid) != (token, address):
                return
            del self._lan_probe[uuid]
            if uuid not in self.clients:
                return
            if on_lan is None:
                self._server_on_lan.pop(uuid, None)
            else:
                self._server_on_lan[uuid] = on_lan

    def connect_client(self, server, do_retries=True):
        uuid = server['uuid']
        with self._client_lock:
            if self.is_stopping:
                return False
            if uuid in self.clients:
                return True
            if uuid in self._connecting:
                return False
            self._connecting.add(uuid)
            generation = self._user_generation
        try:
            client = self.client_factory()
            state = client.authenticate({'Servers': [server]}, discover=False)
            server['connected'] = state['State'] == CONNECTION_STATE['SignedIn']
            if not server['connected']:
                self._record_connect_failure(uuid, client, server)
                self._abandon(client)
                return False
            if do_retries:
                server['cast_ready'] = False
            self.setup_client(client, server, do_retries)
            registered = False
            with self._client_lock:
                if not self.is_stopping and uuid not in self._removed_uuids and (generation == self._user_generation):
                    self.clients[uuid] = client
                    if server.get('username'):
                        self.usernames[uuid] = server['username']
                    registered = True
            if not registered:
                self._stop_client(client)
                return False
            with self._client_lock:
                self._connect_failures.pop(uuid, None)
            self._start_lan_probe(uuid, server.get('address'))
            return True
        finally:
            with self._client_lock:
                self._connecting.discard(uuid)

    def _disconnect_client(self, uuid: Optional[str]=None, server=None, expected_client=None):
        """Remove and stop the registered client. With expected_client, only
        act if that exact instance is still the registered one — a probe that
        raced a reconnect must not tear down the healthy replacement. Returns
        True if a client was removed."""
        with self._client_lock:
            if uuid is None and server is not None:
                uuid = server['uuid']
            client = self.clients.get(uuid)
            if client is None:
                return False
            if expected_client is not None and client is not expected_client:
                return False
            if server is not None:
                server['connected'] = False
            del self.clients[uuid]
        client.callback = lambda *_: None
        client.callback_ws = lambda *_: None
        self._stop_client(client)
        try:
            self.on_servers_changed()
        except Exception:
            log.debug('servers-changed notification failed', exc_info=True)
        return True

    def switch_user(self, user_id):
        """Disconnect the active user's servers and connect ``user_id``'s.

        The heavy connect_all() runs after the swap with the switch flag
        cleared, so the health check can help it along and shutdown stays
        responsive. Returns True on a successful (attempted) switch, False if
        the target user doesn't exist. A target with no servers is still a
        successful switch — it simply lands on the login screen."""
        with self._switch_lock:
            if self.is_stopping:
                return False
            target = userManager.get(user_id)
            if target is None:
                return False
            if user_id == userManager.active_id and self.clients:
                return True
            self._switching.set()
            try:
                with self._client_lock:
                    self._user_generation += 1
                self.save_credentials()
                self.stop_all_clients()
                userManager.set_active(user_id)
                self._adopt_active_user()
                with self._client_lock:
                    self._removed_uuids.clear()
                    self._forget_server_state()
            finally:
                self._switching.clear()
        self.connect_all()
        return True

    def remove_all_clients(self):
        self.stop_all_clients()
        with self._switch_lock:
            self.credentials = []
            self.save_credentials()

    def stop_all_clients(self):
        with self._client_lock:
            clients, self.clients = (dict(self.clients), {})
        for client in clients.values():
            self._stop_client(client)

    def _server_is_connected(self, server):
        """Whether this *server* already has a live client — not whether this
        particular credential does.

        ``_connect_all`` groups credentials by server ``Id`` into serial
        fallback chains and stops at the first address that answers,
        deliberately: several addresses for one server (a LAN IP and a
        hostname, say) are alternative routes to it rather than several
        servers, and racing them lets a worse route win. Everything that
        reconnects afterwards has to ask that same question or it quietly
        undoes it — the registry, the in-flight reservation and the removal
        tombstones are all keyed by ``uuid``, so the *other* address of a
        server that is already up looks disconnected and gets connected too.

        The result is two clients and two websockets on one ``device_id``
        under a single server session, so which socket a remote-control
        command reaches becomes whichever the server saw last; and
        ``_collect_servers`` is uuid-keyed too, so the same box appears twice
        in the switcher and on the home screen. Nothing ever collapses the
        pair: ``remove_client`` takes one uuid.

        This does mean two accounts on one server share one client, which is
        already true of startup — the chain covers them both. Consistent and
        limited beats inconsistent.
        """
        if server['uuid'] in self.clients:
            return True
        server_id = server.get('Id')
        if not server_id:
            return False
        return any((other.get('Id') == server_id and other['uuid'] in self.clients for other in list(self.credentials)))

    def check_all_clients(self):
        if settings.work_offline:
            return
        if self._switching.is_set():
            return
        log.info('Performing client health check...')
        for server in list(self.credentials):
            client = self.clients.get(server['uuid'])
            if client is not None:
                self.validate_client(client, server=server)
        reconnected = False
        for server in list(self.credentials):
            if not self._server_is_connected(server) and (not self.is_stopping):
                log.info('Health check: retrying disconnected server %s', server.get('address'))
                if self.connect_client(server, do_retries=False):
                    reconnected = True
        if reconnected:
            self.on_server_connected()

    @property
    def is_stopping(self):
        return self._stop_event.is_set()

    def stop(self):
        self._stop_event.set()
        if self.health_check:
            self.health_check.stop()
            self.health_check = None
        self.stop_all_clients()

    def get_username_from_client(self, client):
        for uuid, client2 in self.clients.items():
            if client2 is client:
                if uuid in self.usernames:
                    return self.usernames[uuid]
                for server in self.credentials:
                    if server['uuid'] == uuid:
                        return server.get('username', 'Unknown')
                break
        return 'Unknown'
clientManager = ClientManager()

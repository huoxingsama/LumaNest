import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Config:
    def __init__(self, **values):
        self.values = values
        self.revision = 0
    def get_effective(self, scope='global'):
        return dict(self.values)
    def read(self):
        return {'revision': self.revision, 'values': dict(self.values)}
    def update(self, changes, expected_revision, scope='global'):
        if expected_revision != self.revision:
            raise ValueError('revision conflict')
        self.values.update(changes)
        self.revision += 1
        return self.read()


class Secrets:
    def __init__(self): self.values = {}
    def set(self, name, value): self.values[name] = value
    def get(self, name): return self.values.get(name)
    def delete(self, name): self.values.pop(name, None)
    def status(self): return {key: True for key in self.values}


class FakeService(BaseHTTPRequestHandler):
    requests = []
    libraries = []
    fail_reads = False
    jellyfin_initialized = True
    openlist_initialized = True
    redirect_authorized = False
    def log_message(self, *args): pass
    def do_GET(self): self.respond()
    def do_POST(self): self.respond()
    def respond(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        self.requests.append((self.command, self.path, data, dict(self.headers)))
        route = self.path.split('?')[0]
        if route == '/Users/Me' and self.redirect_authorized:
            self.send_response(302)
            self.send_header('Location', '/unexpected-credential-target')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        status = 200
        if route == '/System/Info/Public':
            result = {'Version': '10.10.7', 'Id': 'server-id', 'ServerName': 'Test Library', 'StartupWizardCompleted': self.jellyfin_initialized}
        elif route == '/Startup/Complete':
            type(self).jellyfin_initialized = True
            result = None
        elif route.startswith('/Startup/'):
            result = None
        elif route == '/Users/AuthenticateByName':
            result = {'AccessToken': 'test-token-do-not-return', 'User': {'Id': 'viewer-id', 'Name': 'viewer', 'Policy': {'IsAdministrator': True}}}
        elif route == '/Users/Me':
            result = {'Id': 'viewer-id', 'Name': 'viewer', 'Policy': {'IsAdministrator': True}}
        elif route == '/Library/VirtualFolders':
            if self.command == 'POST':
                from urllib.parse import parse_qs, urlsplit
                query = parse_qs(urlsplit(self.path).query)
                self.libraries.append({'Name': query['name'][0], 'Locations': query['paths']})
                result = None
            else: result = self.libraries
        elif route == '/Users/viewer-id/Views': result = {'Items': []}
        elif route == '/api/public/settings': result = {'code': 200, 'data': [{'key': 'version', 'value': 'v4.2.9'}]}
        elif route == '/api/public/init_status': result = {'code': 200, 'data': {'initialized': self.openlist_initialized}}
        elif route == '/api/public/init/setup':
            type(self).openlist_initialized = True
            result = {'code': 200, 'data': None}
        elif route == '/api/auth/login': result = {'code': 200, 'data': {'token': 'openlist-secret'}}
        elif route == '/api/me': result = {'code': 200, 'data': {'username': 'viewer', 'role': 2}}
        elif route == '/api/fs/list':
            if self.fail_reads: status, result = 503, {'error': 'fake unavailable'}
            else: result = {'code': 200, 'data': {'content': [{'name': 'Movies', 'is_dir': True}, {'name': 'sample.mkv', 'is_dir': False, 'size': 100}], 'total': 2}}
        elif route == '/api/admin/storage/list': result = {'code': 200, 'data': {'content': []}}
        elif route == '/api/admin/driver/list': result = {'code': 200, 'data': ['WebDav', 'BaiduNetdisk']}
        else: status, result = 404, {'error': 'missing'}
        body = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        from personal_cinema.integrations import IntegrationManager
        FakeService.requests = []
        FakeService.libraries = []
        FakeService.fail_reads = False
        FakeService.jellyfin_initialized = True
        FakeService.openlist_initialized = True
        FakeService.redirect_authorized = False
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeService)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        url = f'http://127.0.0.1:{self.server.server_port}'
        self.config = Config(**{'foundation.jellyfin_mode': 'existing', 'foundation.jellyfin_url': url,
                               'foundation.jellyfin_credential': 'jellyfin.token', 'sources.openlist_mode': 'existing',
                               'sources.openlist_url': url, 'sources.openlist_credential': 'openlist.token',
                               'network.mode': 'direct'})
        self.secrets = Secrets()
        self.manager = IntegrationManager(Path(self.temp.name), self.config, self.secrets)

    def test_login_stores_token_without_returning_it(self):
        value = self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'private-password'})
        self.assertEqual('ready', value['state'])
        self.assertEqual('test-token-do-not-return', self.secrets.get('jellyfin.token'))
        serialized = json.dumps(value)
        self.assertNotIn('test-token-do-not-return', serialized)
        self.assertNotIn('private-password', serialized)

    def test_repeated_library_plan_apply_does_not_duplicate_library(self):
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        plan = self.manager.perform('jellyfin', 'plan', {'libraries': [{'name': 'Cinema Movies', 'kind': 'movies', 'paths': ['/media/Movies']}]})
        first = self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})
        second = self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})
        self.assertEqual('complete', first['state'])
        self.assertEqual(first, second)
        self.assertEqual(1, len(FakeService.libraries))

    def test_login_records_jellyfin_identity_for_safe_player_launch(self):
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        self.assertEqual('viewer-id', self.secrets.get('jellyfin.user_id'))
        self.assertEqual('server-id', self.secrets.get('jellyfin.server_id'))

    def test_local_sources_are_planned_as_real_jellyfin_libraries(self):
        media = Path(self.temp.name) / 'LocalMovies'
        media.mkdir()
        self.config.values['sources.entries'] = [{'id': 'local', 'enabled': True, 'kind': 'local', 'path': str(media), 'name': '本地电影', 'library_type': 'movies'}]
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        plan = self.manager.perform('jellyfin', 'plan', {})
        self.assertTrue(any('本地电影' in text for text in plan['changes']))
        self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})
        self.assertEqual([{'Name': '本地电影', 'Locations': [str(media.resolve())]}], FakeService.libraries)

    def test_remote_jellyfin_does_not_receive_client_local_paths_automatically(self):
        self.config.values['foundation.jellyfin_url'] = 'https://media.example.com'
        self.config.values['sources.entries'] = [{'enabled': True, 'kind': 'local', 'path': str(Path(self.temp.name)), 'name': 'local'}]
        with self.assertRaises(ValueError): self.manager.perform('jellyfin', 'plan', {})
        self.assertEqual([], FakeService.requests)

    def test_cloud_source_uses_runtime_catalog_not_unrelated_empty_directory(self):
        self.config.values['sources.entries'] = [{'enabled': True, 'kind': 'openlist', 'path': '/Movies', 'name': 'Cloud'}]
        plan = self.manager.perform('jellyfin', 'plan', {})
        record = json.loads((Path(self.temp.name) / 'services/plans' / (plan['plan_id'] + '.json')).read_text('utf-8'))
        libraries = record['request']['libraries']
        self.assertEqual(['Movies', 'Shows'], [Path(row['paths'][0]).name for row in libraries])
        self.assertTrue(all(Path(row['paths'][0]).is_relative_to(self.manager.root / 'runtime/catalog') for row in libraries))

    def test_changing_sources_invalidates_library_plan(self):
        plan = self.manager.perform('jellyfin', 'plan', {})
        self.config.values['sources.entries'] = [{'enabled': True, 'kind': 'openlist', 'path': '/Movies', 'name': 'Cloud'}]
        with self.assertRaises(ValueError): self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})

    def test_setup_blocks_sources_before_jellyfin_library_mapping_is_applied(self):
        media = Path(self.temp.name) / 'Movies'
        media.mkdir()
        self.config.values['sources.entries'] = [{'enabled': True, 'kind': 'local', 'path': str(media), 'name': 'My movies', 'library_type': 'movies'}]
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        self.assertEqual('needs_setup', self.manager.validate_setup()['state'])
        plan = self.manager.perform('jellyfin', 'plan', {})
        self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})
        self.assertEqual('ready', self.manager.validate_setup()['state'])

    def test_setup_cloud_source_requires_openlist_and_real_catalog_mapping(self):
        self.config.values['sources.entries'] = [{'enabled': True, 'kind': 'openlist', 'path': '/Movies', 'name': 'Cloud'}]
        self.config.values['sources.openlist_mode'] = 'disabled'
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        result = self.manager.validate_setup()
        self.assertEqual('needs_setup', result['state'])
        self.assertTrue(any(row['id'] == 'openlist' for row in result['missing']))
        self.config.values['sources.openlist_mode'] = 'existing'
        self.manager.perform('openlist', 'login', {'username': 'viewer', 'password': 'secret'})
        plan = self.manager.perform('jellyfin', 'plan', {})
        self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})
        self.assertEqual('ready', self.manager.validate_setup()['state'])
        self.assertTrue(next(row for row in self.manager.status() if row['id'] == 'openlist')['required'])

    def test_setup_remote_server_rejects_client_strm_source_without_network_calls(self):
        self.config.values['foundation.jellyfin_url'] = 'https://media.example.com'
        self.config.values['sources.entries'] = [{'enabled': True, 'kind': 'openlist', 'path': '/Movies', 'name': 'Cloud'}]
        result = self.manager.validate_setup()
        self.assertEqual('needs_setup', result['state'])
        self.assertEqual([], FakeService.requests)

    def test_setup_existing_library_only_does_not_require_openlist_or_admin_library_paths(self):
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        FakeService.requests.clear()
        result = self.manager.validate_setup()
        self.assertEqual('ready', result['state'])
        self.assertFalse(result['playback_verified'])
        self.assertFalse(any(row[1].startswith('/api/') or row[1] == '/Library/VirtualFolders' for row in FakeService.requests))

    def test_setup_token_only_openlist_requires_webdav_login_credentials(self):
        self.config.values['sources.entries'] = [{'enabled': True, 'kind': 'openlist', 'path': '/Movies', 'name': 'Cloud'}]
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        self.manager.perform('openlist', 'login', {'username': 'viewer', 'password': 'secret'})
        plan = self.manager.perform('jellyfin', 'plan', {})
        self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})
        self.secrets.delete('openlist.password')
        result = self.manager.validate_setup()
        self.assertEqual('needs_setup', result['state'])
        self.assertTrue(any(row['id'] == 'openlist' and row['action'] == 'login' for row in result['missing']))

    def test_configuration_changed_after_plan_rejects_apply(self):
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        plan = self.manager.perform('jellyfin', 'plan', {})
        self.config.update({'foundation.jellyfin_url': 'http://127.0.0.1:1'}, self.config.revision)
        with self.assertRaises(ValueError):
            self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})

    def test_existing_server_plan_never_runs_startup_wizard(self):
        self.manager.perform('jellyfin', 'login', {'username': 'viewer', 'password': 'secret'})
        plan = self.manager.perform('jellyfin', 'plan', {})
        self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})
        self.assertFalse(any('/Startup/' in row[1] for row in FakeService.requests))

    def test_openlist_login_and_browse_do_not_claim_playback_verified(self):
        self.manager.perform('openlist', 'login', {'username': 'viewer', 'password': 'secret'})
        result = self.manager.perform('openlist', 'browse', {'path': '/Movies'})
        self.assertEqual(2, len(result['items']))
        self.assertFalse(result['playback_verified'])
        self.assertNotIn('openlist-secret', json.dumps(result))

    def test_disabled_openlist_rejects_network_actions(self):
        self.config.values['sources.openlist_mode'] = 'disabled'
        with self.assertRaises(ValueError):
            self.manager.perform('openlist', 'login', {'username': 'viewer', 'password': 'secret'})
        self.assertEqual([], FakeService.requests)

    def test_unknown_action_and_path_escape_rejected(self):
        with self.assertRaises(ValueError): self.manager.perform('jellyfin', 'run-shell', {})
        with self.assertRaises(ValueError): self.manager.perform('openlist', 'apply', {'plan_id': '../bad'})

    def test_status_only_uses_last_probe_and_does_not_perform_network_calls(self):
        self.manager.status()
        self.assertEqual([], FakeService.requests)

    def test_http_error_is_redacted(self):
        self.manager.perform('openlist', 'login', {'username': 'viewer', 'password': 'secret'})
        FakeService.fail_reads = True
        with self.assertRaises(Exception) as caught:
            self.manager.perform('openlist', 'browse', {'path': '/Movies'})
        self.assertNotIn('openlist-secret', str(caught.exception))
        self.assertNotIn('fake unavailable', str(caught.exception))

    def test_new_owned_openlist_initializes_once_and_keeps_password_out_of_plan(self):
        import sys
        self.config.values.update({'sources.openlist_mode': 'managed', 'sources.openlist_path': sys.executable})
        FakeService.openlist_initialized = False
        plan = self.manager.perform('openlist', 'plan', {'username': 'owner', 'password': 'setup-secret-123'})
        stored = (Path(self.temp.name) / 'services' / 'plans' / (plan['plan_id'] + '.json')).read_text('utf-8')
        self.assertNotIn('setup-secret-123', stored)
        with patch.object(self.manager, '_start', return_value={'state': 'needs_setup'}), patch.object(self.manager.services, 'status', return_value={'owned': True, 'running': True}):
            self.manager.perform('openlist', 'apply', {'plan_id': plan['plan_id']})
            self.manager.perform('openlist', 'apply', {'plan_id': plan['plan_id']})
        self.assertEqual(1, sum(row[1] == '/api/public/init/setup' for row in FakeService.requests))

    def test_uninitialized_existing_openlist_is_never_initialized(self):
        FakeService.openlist_initialized = False
        plan = self.manager.perform('openlist', 'plan', {})
        with self.assertRaises(Exception):
            self.manager.perform('openlist', 'apply', {'plan_id': plan['plan_id']})
        self.assertFalse(any(row[1] == '/api/public/init/setup' for row in FakeService.requests))

    def test_nested_secret_in_driver_plan_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.perform('openlist', 'plan', {'storage': {'mount_path': '/Movies', 'driver': 'WebDav',
                'credential_ref': 'webdav.password', 'addition': {'nested': {'password': 'do-not-store'}}}})

    def test_new_owned_jellyfin_initializes_and_creates_default_libraries(self):
        import sys
        self.config.values.update({'foundation.jellyfin_mode': 'managed', 'foundation.jellyfin_path': sys.executable})
        FakeService.jellyfin_initialized = False
        plan = self.manager.perform('jellyfin', 'plan', {'username': 'owner', 'password': 'setup-secret-123'})
        with patch.object(self.manager, '_start', return_value={'state': 'needs_setup'}), patch.object(self.manager.services, 'status', return_value={'owned': True, 'running': True}):
            result = self.manager.perform('jellyfin', 'apply', {'plan_id': plan['plan_id']})
        self.assertEqual('complete', result['state'])
        self.assertEqual(2, len(FakeService.libraries))
        self.assertTrue(FakeService.jellyfin_initialized)

    def test_authorized_redirect_never_forwards_credentials(self):
        self.secrets.set('jellyfin.token', 'private-token')
        FakeService.redirect_authorized = True
        with self.assertRaises(Exception): self.manager.perform('jellyfin', 'probe', {})
        self.assertFalse(any(row[1] == '/unexpected-credential-target' for row in FakeService.requests))

    def test_existing_mode_cannot_start_or_stop_services(self):
        with self.assertRaises(ValueError): self.manager.perform('jellyfin', 'start', {})
        with self.assertRaises(ValueError): self.manager.perform('jellyfin', 'stop', {})

    def test_managed_running_service_does_not_rewrite_changed_network_config(self):
        import sys
        self.config.values.update({'foundation.jellyfin_mode': 'managed', 'foundation.jellyfin_path': sys.executable})
        self.manager._managed_spec('jellyfin', prepare=True)
        network = Path(self.temp.name) / 'managed/jellyfin/data/config/network.xml'
        before = network.read_bytes()
        self.config.values['foundation.jellyfin_url'] = 'http://127.0.0.1:1'
        with patch.object(self.manager.services, 'status', return_value={'running': True, 'owned': True}), patch.object(self.manager.services, 'start', return_value={}), patch.object(self.manager, '_probe', return_value={'state': 'ready'}):
            with self.assertRaises(ValueError): self.manager.perform('jellyfin', 'start', {})
        self.assertEqual(before, network.read_bytes())


if __name__ == '__main__': unittest.main()

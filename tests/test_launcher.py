import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from personal_cinema import launcher
from personal_cinema.server import Application, handler


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lumanest-launch-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def server(self, handler_type):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_type)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def configured_application(self):
        (self.root / 'setup-complete.json').write_text('{}', encoding='utf-8')
        app = Application(self.root)
        app._runtime = Mock()
        app._runtime.start.return_value = {'state': 'running', 'private_value': 'not-for-launch-output'}
        app._runtime.launch_player.return_value = {'state': 'starting'}
        app._runtime.task_command.return_value = {'state': 'queued', 'id': 'fixture-request'}
        app.integrations = Mock()
        app.integrations.perform.return_value = {'state': 'ready'}
        server = self.server(handler(app))
        url = lambda *_args: f'http://127.0.0.1:{server.server_port}/#token={app.launch_token}'
        return app, url

    def test_real_session_dispatches_only_requested_actions(self):
        app, url = self.configured_application()
        with patch.object(launcher, '_ensure_server', side_effect=url), \
             patch.object(launcher.webbrowser, 'open') as browser:
            background = launcher.launch(self.root, 'background')
            self.assertEqual(background['state'], 'running')
            self.assertNotIn('not-for-launch-output', json.dumps(background))
            app._runtime.start.assert_called_once()
            app._runtime.task_command.assert_not_called()
            app._runtime.launch_player.assert_not_called()
            app._runtime.reset_mock()

            self.assertEqual(launcher.launch(self.root, 'open-cinema')['state'], 'starting')
            app._runtime.launch_player.assert_called_once()
            app._runtime.start.assert_not_called()  # launch_player owns its start.
            app._runtime.task_command.assert_not_called()
            app._runtime.reset_mock()

            for action, requested in [('scan-new', 'scan-new'), ('scan-all', 'scan')]:
                self.assertEqual(launcher.launch(self.root, action)['state'], 'queued')
                self.assertEqual(app._runtime.task_command.call_args.args[0], requested)
                self.assertRegex(app._runtime.task_command.call_args.args[1]['request_id'], '^[a-f0-9]{32}$')
            self.assertEqual(app._runtime.start.call_count, 2)
            browser.assert_not_called()
            self.assertEqual(app.integrations.perform.call_args.args[:2], ('jellyfin', 'probe'))

    def test_first_run_opens_setup_without_launching_workers(self):
        with patch.object(launcher, '_ensure_server', return_value='http://127.0.0.1:5000/#token=fixture') as ensure, \
             patch.object(launcher.webbrowser, 'open', return_value=True) as browser, \
             patch.object(launcher, '_LocalSession') as session:
            result = launcher.launch(self.root, 'open-cinema')
            self.assertEqual(result['state'], 'opened')
            browser.assert_called_once()
            session.assert_not_called()
            ensure.reset_mock()
            with self.assertRaisesRegex(launcher.LaunchError, '初始配置'):
                launcher.launch(self.root, 'background')
            ensure.assert_not_called()

    def test_spawn_uses_installed_module_no_private_worker_or_token(self):
        with patch.object(launcher, '_settings_running', return_value=False), \
             patch.object(launcher.subprocess, 'Popen') as spawn, \
             patch.object(launcher, '_reopen', return_value='fixture'):
            self.assertEqual(launcher._ensure_server(self.root, 1234), 'fixture')
            command = spawn.call_args.args[0]
            self.assertEqual(command[1:], ['-m', 'personal_cinema.server', '--data-dir', str(self.root), '--no-browser', '--port', '1234'])
            self.assertNotIn('/scripts/', ' '.join(command).replace('\\', '/'))
            self.assertNotIn('token', ' '.join(command))
            self.assertEqual(spawn.call_args.kwargs['env']['PERSONAL_CINEMA_HOME'], str(self.root))
        with patch.object(launcher, '_settings_running', return_value=True), \
             patch.object(launcher.subprocess, 'Popen') as spawn, \
             patch.object(launcher, '_reopen', return_value='fixture'):
            launcher._ensure_server(self.root)
            spawn.assert_not_called()

    def test_concurrent_bootstrap_rotation_retries_authentication_not_mutation(self):
        app, url = self.configured_application()
        invalid = url().split('#')[0] + '#token=expired-fixture'
        with patch.object(launcher, '_ensure_server', return_value=invalid), \
             patch.object(launcher, '_reopen', side_effect=url) as reopen:
            launcher.launch(self.root, 'background')
            reopen.assert_called_once()
            app._runtime.start.assert_called_once()

    def test_custom_interpreter_identity_is_preserved_for_network_routing(self):
        custom = str(self.root / 'network-runtime/Scripts/media-worker.exe')
        with patch.object(launcher.sys, 'executable', custom), \
             patch.object(launcher, '_settings_running', return_value=False), \
             patch.object(launcher.subprocess, 'Popen') as spawn, \
             patch.object(launcher, '_reopen', return_value='fixture'):
            launcher._ensure_server(self.root)
            self.assertEqual(spawn.call_args.args[0][0], custom)
        from personal_cinema import settings_launcher
        with patch.object(settings_launcher.sys, 'executable', custom), \
             patch.object(settings_launcher, '_settings_running', return_value=False), \
             patch.object(settings_launcher.subprocess, 'Popen') as spawn:
            settings_launcher.open_settings(self.root)
            self.assertEqual(spawn.call_args.args[0][0], custom)

    def test_external_dependencies_must_be_ready_before_runtime_start(self):
        app, url = self.configured_application()
        app.integrations.perform.return_value = {'state': 'needs_authorization'}
        with patch.object(launcher, '_ensure_server', side_effect=url):
            with self.assertRaisesRegex(launcher.LaunchError, '重新授权'):
                launcher.launch(self.root, 'background', wait_services=0)
        app._runtime.start.assert_not_called()
        app.integrations.perform.return_value = {'state': 'unconfigured'}
        with patch.object(launcher, '_ensure_server', side_effect=url):
            with self.assertRaisesRegex(launcher.LaunchError, '超时'):
                launcher.launch(self.root, 'background', wait_services=0)
        app._runtime.start.assert_not_called()

    def test_external_services_readiness_retries_and_unused_openlist_is_skipped(self):
        session = Mock()
        session.post.side_effect = [launcher.LaunchError('not ready', 503), {'state': 'ready'}]
        with patch.object(launcher.time, 'sleep'):
            launcher._wait_external_services(self.root, session, 5)
        self.assertEqual([call.args[0] for call in session.post.call_args_list],
                         ['/api/integrations/jellyfin/probe', '/api/integrations/jellyfin/probe'])

    def test_local_client_rejects_redirects_and_nonlocal_urls(self):
        for address in ('http://example.com/#token=x', 'https://127.0.0.1:9000/#token=x',
                        'http://' + 'user:secret@127.0.0.1:9000/#token=x', 'http://127.0.0.1:9000/?api_key=x#token=x'):
            with self.assertRaises(launcher.LaunchError):
                launcher._LocalSession(address)
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(307)
                self.send_header('Location', 'http://example.com/collect')
                self.end_headers()
            def log_message(self, *_):
                pass
        server = self.server(Redirect)
        with self.assertRaisesRegex(launcher.LaunchError, '跳转'):
            launcher._LocalSession(f'http://127.0.0.1:{server.server_port}/#token=fixture-secret')

    def test_cli_dispatch_and_errors_do_not_echo_arbitrary_exception_details(self):
        output = io.StringIO()
        with patch.object(launcher, 'launch', return_value={'state': 'running', 'message': '后台已启动'}) as launch, \
             contextlib.redirect_stdout(output):
            self.assertEqual(launcher.main(['--data-dir', str(self.root), '--background', '--wait-services', '30']), 0)
            launch.assert_called_once_with(self.root, 'background', 0, 30)
        with patch.object(launcher, 'launch', side_effect=OSError('secret-from-provider')), \
             contextlib.redirect_stderr(output):
            self.assertEqual(launcher.main(['--background']), 1)
        self.assertNotIn('secret-from-provider', output.getvalue())


if __name__ == '__main__':
    unittest.main()

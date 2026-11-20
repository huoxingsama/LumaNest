import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from personal_cinema import settings_launcher

BASE = Path(__file__).resolve().parents[1]
for directory in (BASE / 'vendor', BASE / 'src/personal_cinema/legacy'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


class SettingsLauncherTests(unittest.TestCase):
    def test_owned_process_reuses_secure_reopen_without_child_or_returning_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = {'pid': 42, 'created': 10, 'command': ['pythonw', '-m', 'personal_cinema.server']}
            (root / 'settings-process.json').write_text(json.dumps(record), encoding='utf-8')
            process = Mock(create_time=Mock(return_value=10), cmdline=Mock(return_value=record['command']))
            with patch.object(settings_launcher.psutil, 'Process', return_value=process), \
                 patch('personal_cinema.server.reopen_existing', return_value='http://127.0.0.1:1000/#token=fixture-secret') as reopen, \
                 patch.object(settings_launcher.webbrowser, 'open', return_value=True) as browser, \
                 patch.object(settings_launcher.subprocess, 'Popen') as spawn:
                result = settings_launcher.open_settings(root)
                reopen.assert_called_once_with(root.resolve())
                browser.assert_called_once_with('http://127.0.0.1:1000/#token=fixture-secret')
                spawn.assert_not_called()
                self.assertEqual(result['state'], 'opened')
                self.assertNotIn('fixture-secret', json.dumps(result))

    def test_absent_process_launches_same_instance_hidden_without_secret_arguments(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(settings_launcher.subprocess, 'Popen') as spawn:
            root = Path(tmp)
            result = settings_launcher.open_settings(root)
            self.assertEqual(result['state'], 'starting')
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[1:], ['-m', 'personal_cinema.server', '--data-dir', str(root.resolve())])
            self.assertEqual(spawn.call_args.kwargs['env']['PERSONAL_CINEMA_HOME'], str(root.resolve()))
            if os.name == 'nt':
                self.assertEqual(spawn.call_args.kwargs['creationflags'], settings_launcher.subprocess.CREATE_NO_WINDOW)
            self.assertEqual(list(root.iterdir()), [])

    def test_live_server_reopen_failure_does_not_start_duplicate_process(self):
        with patch.object(settings_launcher, '_settings_running', return_value=True), \
             patch('personal_cinema.server.reopen_existing', side_effect=ValueError('not ready')), \
             patch.object(settings_launcher.subprocess, 'Popen') as spawn:
            with self.assertRaises(ValueError):
                settings_launcher.open_settings(Path.cwd())
            spawn.assert_not_called()

    def test_native_settings_entry_has_system_and_player_actions_and_honors_lock(self):
        from jellyfin_mpv_shim.mpvtk_browser.settings import SettingsMixin
        browser = SimpleNamespace(_locked=False, _close_dialog=Mock(), open_settings=Mock(),
                                  _open_system_settings=Mock(), _show_dialog=Mock())
        SettingsMixin._open_settings(browser)
        dialog = browser._show_dialog.call_args.args[0]()
        elements = []
        def collect(node):
            elements.append(node)
            if getattr(node, 'child', None) is not None:
                collect(node.child)
            for child in getattr(node, 'children', []):
                collect(child)
        collect(dialog)
        buttons = {item.id: item for item in elements if getattr(item, 'id', None)}
        self.assertIn('open-system-settings', buttons)
        self.assertIn('open-player-settings', buttons)
        browser._show_dialog.reset_mock()
        browser._locked = True
        SettingsMixin._open_settings(browser)
        browser._show_dialog.assert_not_called()

    def test_workbench_requires_admin_and_is_idempotent_without_exposing_auth(self):
        from task_center_api import APIError, Center
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'runtime'
            center = Center(root)
            payload = {'action': 'open-settings', 'request_id': 'a' * 32}
            admin = {'Id': 'administrator', 'Policy': {'IsAdministrator': True}}
            with patch.object(settings_launcher, 'open_settings', return_value={'state': 'opened', 'message': '已打开'}) as opener:
                with self.assertRaises(APIError) as error:
                    center.command({}, [], {'Id': 'viewer', 'Policy': {}}, payload)
                self.assertEqual(error.exception.status, 403)
                opener.assert_not_called()
                first = center.command({}, [], admin, payload)
                self.assertEqual(center.command({}, [], admin, payload), first)
                opener.assert_called_once_with(root.parent)
                self.assertEqual(first['state'], 'applied')
                self.assertEqual(first['message'], '已打开')
                self.assertNotIn('token', json.dumps(first))
                self.assertFalse((root / 'data/requests').exists())
                with self.assertRaises(APIError):
                    center.command({}, [], {'Id': 'other', 'Policy': {'IsAdministrator': True}}, payload)


if __name__ == '__main__':
    unittest.main()

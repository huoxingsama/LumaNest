import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


class ServiceTests(unittest.TestCase):
    def setUp(self):
        from personal_cinema.service_manager import ServiceManager
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ServiceManager(self.root)

    def test_owned_process_start_is_idempotent_and_stop_only_it(self):
        cwd = self.root / 'managed' / 'test'
        cwd.mkdir(parents=True)
        argv = [sys.executable, '-c', 'import time; time.sleep(60)']
        first = self.manager.start('test', argv, cwd)
        self.addCleanup(lambda: self.manager.stop('test'))
        second = self.manager.start('test', argv, cwd)
        self.assertEqual(first['pid'], second['pid'])
        self.assertTrue(self.manager.status('test')['running'])
        self.manager.stop('test')
        self.assertFalse(self.manager.status('test')['running'])

    def test_different_command_cannot_reuse_existing_service(self):
        cwd = self.root / 'managed' / 'test'; cwd.mkdir(parents=True)
        self.manager.start('test', [sys.executable, '-c', 'import time; time.sleep(60)'], cwd)
        self.addCleanup(lambda: self.manager.stop('test'))
        with self.assertRaises(ValueError):
            self.manager.start('test', [sys.executable, '-c', 'print(1)'], cwd)

    def test_unowned_pid_manifest_never_terminates_foreign_process(self):
        path = self.root / 'services' / 'owned.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'foreign': {'pid': os.getpid(), 'created': 'wrong', 'executable': sys.executable, 'command_hash': 'bad'}}))
        result = self.manager.stop('foreign')
        self.assertFalse(result['running'])
        self.assertEqual('not_owned_or_not_running', result['state'])

    def test_cwd_must_be_inside_owned_tree(self):
        with self.assertRaises(ValueError):
            self.manager.start('test', [sys.executable, '-c', 'print(1)'], Path(tempfile.gettempdir()))

    def test_unsafe_service_id_rejected(self):
        with self.assertRaises(ValueError): self.manager.stop('../foreign')

    def test_stop_terminates_verified_wrapper_descendants_but_not_unrelated_process(self):
        from personal_cinema.service_manager import process_identity, ServiceManager
        cwd = self.root / 'managed' / 'tree'
        cwd.mkdir(parents=True)
        script = cwd / 'wrapper.py'
        script.write_text('import pathlib, subprocess, sys, time\n'
                          'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                          'pathlib.Path("child.pid").write_text(str(child.pid))\n'
                          'time.sleep(60)\n', 'utf-8')
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def stop_unrelated():
            if unrelated.poll() is None: unrelated.terminate()
            unrelated.wait(timeout=5)
        self.addCleanup(stop_unrelated)
        self.manager.start('tree', [sys.executable, str(script)], cwd)
        def stop_wrapper():
            self.manager.stop('tree')
            self.manager.children['tree'].wait(timeout=5)
        self.addCleanup(stop_wrapper)
        deadline = time.monotonic() + 5
        while not (cwd / 'child.pid').is_file() and time.monotonic() < deadline:
            time.sleep(.02)
        child_pid = int((cwd / 'child.pid').read_text())
        child_identity = process_identity(child_pid)
        # Cleanup this isolated test child even if the assertion exposes a bug.
        def stop_test_child():
            import psutil
            try:
                if process_identity(child_pid) != child_identity: return
                child = psutil.Process(child_pid)
                child.terminate()
                child.wait(timeout=5)
            except psutil.NoSuchProcess: pass
        self.addCleanup(stop_test_child)
        self.assertIsNotNone(process_identity(child_pid))
        reopened = ServiceManager(self.root)
        reopened.stop('tree')
        self.assertIsNone(process_identity(child_pid), 'Managed wrapper child was left running')
        self.assertIsNone(unrelated.poll(), 'Unrelated process must remain running')
        self.manager.children['tree'].wait(timeout=5)

    def test_process_tree_helper_refuses_missing_owner_identity(self):
        from personal_cinema.process_control import terminate_owned_tree
        result = terminate_owned_tree(os.getpid(), lambda: False)
        self.assertEqual('not_owned_or_not_running', result['state'])
        self.assertEqual([], result['terminated'])


if __name__ == '__main__': unittest.main()

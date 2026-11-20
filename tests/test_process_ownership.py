import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from personal_cinema.process_lock import InstanceLease, LifecycleLock


class OwnershipTests(unittest.TestCase):
    def test_lease_excludes_another_process_and_recovers_after_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lease'
            lease = InstanceLease(path)
            self.assertTrue(lease.acquire())
            code = 'import sys; from personal_cinema.process_lock import InstanceLease; x=InstanceLease(sys.argv[1]); print(x.acquire()); x.release()'
            try:
                result = subprocess.check_output([sys.executable, '-c', code, str(path)], text=True)
                self.assertEqual(result.strip(), 'False')
            finally:
                lease.release()
            result = subprocess.check_output([sys.executable, '-c', code, str(path)], text=True)
            self.assertEqual(result.strip(), 'True')

    def test_lifecycle_is_reentrant_across_runtime_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = LifecycleLock(Path(directory) / 'run'), LifecycleLock(Path(directory) / 'run')
            with a:
                with b:
                    self.assertEqual(a.state['depth'], 2)
            self.assertEqual(a.state['depth'], 0)

    @unittest.skipUnless(os.name == 'nt', 'Uses Windows credential vault')
    def test_second_settings_launch_reuses_server_and_does_not_start_workers(self):
        import psutil
        from personal_cinema.server import reopen_existing
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = [sys.executable, '-m', 'personal_cinema.server', '--data-dir', str(root), '--no-browser']
            primary = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while not (root / 'settings-process.json').exists() and time.monotonic() < deadline:
                    if primary.poll() is not None:
                        self.fail('Isolated settings server failed to start')
                    time.sleep(.05)
                before = json.loads((root / 'settings-process.json').read_text())
                second = subprocess.run(command, capture_output=True, timeout=15)
                self.assertEqual(second.returncode, 0, second.stderr.decode(errors='replace'))
                self.assertEqual(json.loads((root / 'settings-process.json').read_text()), before)
                owned = {primary.pid, *(child.pid for child in psutil.Process(primary.pid).children(recursive=True))}
                self.assertIn(before['pid'], owned)
                self.assertIn('/#token=', reopen_existing(root))
                self.assertFalse((root / 'runtime-processes.json').exists())
                self.assertFalse((root / 'runtime-desired.json').exists())
            finally:
                try:
                    children = psutil.Process(primary.pid).children(recursive=True)
                except psutil.Error:
                    children = []
                for child in reversed(children):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                primary.terminate()
                primary.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()

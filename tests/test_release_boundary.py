import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('release_check', Path(__file__).resolve().parents[1] / 'tools/release_check.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class BoundaryTests(unittest.TestCase):
    def test_allowlist_excludes_instance_state_and_build_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('src/app.py', 'src/__pycache__/bad.py', 'dist/private.py', '.venv/token.py', 'state.json'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('')
            with patch.object(gate, 'ROOT', root):
                self.assertEqual([p.relative_to(root).as_posix() for p in gate.files()], ['src/app.py'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction boundary')
    def test_nested_junction_is_rejected_before_reading_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'release'
            outside = Path(directory) / 'outside'
            (root / 'src').mkdir(parents=True)
            outside.mkdir()
            (outside / 'secret.py').write_text('PRIVATE_MARKER')
            junction = root / 'src' / 'redirect'
            quote = lambda p: "'" + str(p).replace("'", "''") + "'"
            command = 'New-Item -ItemType Junction -Path ' + quote(junction) + ' -Target ' + quote(outside) + ' | Out-Null'
            subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command], check=True, capture_output=True)
            try:
                with patch.object(gate, 'ROOT', root):
                    self.assertEqual(gate.check(), ['src/redirect: redirected release path'])
            finally:
                os.rmdir(junction)
            self.assertTrue((outside / 'secret.py').is_file())


if __name__ == '__main__':
    unittest.main()

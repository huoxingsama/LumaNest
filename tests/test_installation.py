import base64
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from personal_cinema.installation import verify_distribution


class Entry(str):
    @property
    def hash(self):
        return SimpleNamespace(mode='sha256', value=base64.urlsafe_b64encode(hashlib.sha256(b'original').digest()).rstrip(b'=').decode())


class InstallationTests(unittest.TestCase):
    def test_package_drift_is_detected_without_inspecting_user_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / 'personal_cinema'
            package.mkdir()
            file = package / 'ui.py'
            file.write_bytes(b'original')
            entries = [Entry('personal_cinema/ui.py'), Entry('../private/config.json')]
            distribution = SimpleNamespace(version='test', files=entries, locate_file=lambda entry: root / entry)
            self.assertTrue(verify_distribution(distribution)['verified'])
            file.write_bytes(b'changed')
            result = verify_distribution(distribution)
            self.assertFalse(result['verified'])
            self.assertEqual(result['changed'], ['personal_cinema/ui.py'])
            file.unlink()
            self.assertEqual(verify_distribution(distribution)['missing'], ['personal_cinema/ui.py'])

    def test_editable_or_empty_installation_cannot_claim_wheel_parity(self):
        self.assertFalse(verify_distribution(SimpleNamespace(version='dev', files=[]))['verified'])

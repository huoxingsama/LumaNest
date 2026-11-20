import base64
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class TestCipher:
    def encrypt(self, value): return bytes(x ^ 91 for x in value)
    def decrypt(self, value): return bytes(x ^ 91 for x in value)


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        try: self.module = importlib.import_module('personal_cinema.credentials')
        except ImportError: self.fail('Protected credentials have not been implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.module.SecretStore(self.root, cipher=TestCipher())

    def test_set_get_status_delete_and_no_plaintext_file(self):
        self.store.set('jellyfin.token', 'fake-fixture-private-value')
        self.assertEqual(self.store.get('jellyfin.token'), 'fake-fixture-private-value')
        self.assertEqual(self.store.status(), {'jellyfin.token': True})
        for file in self.root.rglob('*.json'):
            self.assertNotIn('fake-fixture-private-value', file.read_text('utf-8'))
        other = self.module.SecretStore(self.root, cipher=TestCipher())
        self.assertEqual(other.get('jellyfin.token'), 'fake-fixture-private-value')
        self.store.delete('jellyfin.token')
        self.assertIsNone(self.store.get('jellyfin.token'))
        self.assertEqual(self.store.status(), {})

    def test_invalid_name_and_empty_secret_rejected(self):
        for name in ('../token', '/root', 'a b', '__proto__', ''):
            with self.assertRaises(ValueError): self.store.set(name, 'fake')
        with self.assertRaises(ValueError): self.store.set('valid.token', '')

    def test_wrong_cipher_never_returns_ciphertext_or_empty_success(self):
        self.store.set('fixture.token', '值-with-unicode')
        class BadCipher:
            def decrypt(self, value): raise OSError('wrong user')
        other = self.module.SecretStore(self.root, cipher=BadCipher())
        with self.assertRaises(self.module.CredentialError): other.get('fixture.token')

    def test_non_windows_has_no_plaintext_fallback(self):
        with patch.object(self.module.os, 'name', 'posix'):
            with self.assertRaises(self.module.CredentialError): self.module.DPAPICipher()

    def test_default_windows_cipher_real_roundtrip(self):
        if self.module.os.name != 'nt': self.skipTest('Windows DPAPI test')
        store = self.module.SecretStore(self.root / 'native')
        store.set('sample.token', 'fixture-only-中文')
        self.assertEqual(store.get('sample.token'), 'fixture-only-中文')


if __name__ == '__main__': unittest.main()

import hashlib
import io
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import package_player_support as packer
import verify_archive as gate


class ArchiveVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lumanest-archive-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.members = {name: ('fixture:' + name).encode() for name in gate.PLAYER_SUPPORT_MEMBERS}
        self.digests = {name: hashlib.sha256(data).hexdigest() for name, data in self.members.items()}

    def make_zip(self, members=None, filename=None, link=None, duplicate=None):
        path = self.root / (filename or gate.PLAYER_SUPPORT_NAME)
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in (members if members is not None else self.members).items():
                entry = zipfile.ZipInfo(name)
                entry.external_attr = ((stat.S_IFLNK if name == link else stat.S_IFREG) | 0o644) << 16
                archive.writestr(entry, data)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(duplicate, self.members[duplicate])
        return path

    def inspect_fixture(self, path, *, member_hashes=True):
        # Deliberately re-pin the tiny synthetic container so membership and
        # payload verification are exercised independently of the outer hash.
        with patch.object(gate, 'PLAYER_SUPPORT_SHA256', hashlib.sha256(path.read_bytes()).hexdigest()):
            if not member_hashes:
                return gate.inspect_archive(path)
            with patch.dict(gate.PLAYER_SUPPORT_MEMBERS, self.digests, clear=True):
                return gate.inspect_archive(path)

    def test_pins_are_exact_and_match_reviewed_build_inputs(self):
        self.assertEqual(gate.PLAYER_SUPPORT_NAME, 'lumanest-player-support-1-win-x64.zip')
        self.assertEqual(gate.PLAYER_SUPPORT_SHA256, '3e4493277990362e27a93064338cc0be9609b36383b1d9fea1037d9c940b175a')
        self.assertEqual(len(gate.PLAYER_SUPPORT_MEMBERS), 7)
        for name, digest in packer.DLL_HASHES.items():
            self.assertEqual(gate.PLAYER_SUPPORT_MEMBERS[name], digest)
        self.assertEqual(gate.PLAYER_SUPPORT_MEMBERS['sources/fribidi-1.0.16.tar.xz'], packer.SOURCE_HASH)
        self.assertEqual(gate.PLAYER_SUPPORT_MEMBERS['licenses/Vulkan-Loader-LICENSE.txt'], packer.VULKAN_LICENSE_HASH)
        self.assertEqual(gate.PLAYER_SUPPORT_MEMBERS['NOTICE.txt'], hashlib.sha256(packer.NOTICE.encode()).hexdigest())

    def test_exact_members_with_matching_all_hashes_are_accepted(self):
        self.assertEqual(self.inspect_fixture(self.make_zip()), 7)

    def test_same_filename_does_not_bypass_whole_archive_hash(self):
        path = self.make_zip()
        with self.assertRaisesRegex(ValueError, 'archive checksum mismatch'):
            gate.inspect_archive(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open('ab') as stream:
            stream.write(b'tampered trailing content')
        with patch.object(gate, 'PLAYER_SUPPORT_SHA256', digest):
            with self.assertRaisesRegex(ValueError, 'archive checksum mismatch'):
                gate.inspect_archive(path)

    def test_each_payload_is_checked_even_with_matching_container_hash(self):
        for name in self.members:
            with self.subTest(name=name):
                members = {**self.members, name: self.members[name] + b'tamper'}
                with self.assertRaisesRegex(ValueError, 'member checksum mismatch'):
                    self.inspect_fixture(self.make_zip(members))

    def test_extra_missing_duplicate_and_renamed_members_are_rejected(self):
        changes = [
            {**self.members, 'extra.txt': b'extra'},
            {k: v for k, v in self.members.items() if k != 'NOTICE.txt'},
            {('FRIBIDI-0.DLL' if k == 'fribidi-0.dll' else k): v for k, v in self.members.items()},
            {('../NOTICE.txt' if k == 'NOTICE.txt' else k): v for k, v in self.members.items()},
        ]
        for index, members in enumerate(changes):
            with self.subTest(index=index):
                with self.assertRaisesRegex(ValueError, 'member list'):
                    self.inspect_fixture(self.make_zip(members))
        with self.assertRaisesRegex(ValueError, 'member list'):
            self.inspect_fixture(self.make_zip(duplicate='NOTICE.txt'))

    def test_symlinks_are_rejected_even_when_names_and_hashes_match(self):
        with self.assertRaisesRegex(ValueError, 'unsafe player support member'):
            self.inspect_fixture(self.make_zip(link='fribidi-0.dll'))

    def test_dlls_still_rejected_in_source_zip_wheel_and_sdist(self):
        for filename in ('lumanest-0.1.0a1-source.zip', 'lumanest-0.1.0a1-py3-none-any.whl',
                         'lumanest-player-support-2-win-x64.zip', 'LumaNest-player-support-1-win-x64.zip'):
            with self.subTest(filename=filename):
                path = self.make_zip({'package/fribidi-0.dll': b'MZ fixture'}, filename)
                with self.assertRaisesRegex(ValueError, 'unbundled component'):
                    gate.inspect_archive(path)
        path = self.root / 'lumanest-0.1.0a1.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            member = tarfile.TarInfo('package/fribidi-0.dll')
            data = b'MZ fixture'
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, 'unbundled component'):
            gate.inspect_archive(path)

    def test_source_archive_privacy_and_link_rules_are_unchanged(self):
        for name, payload in [('package/secrets/token.txt', b'fixture'), ('package/users.json', b'{}'),
                              ('../escape.txt', b'fixture'), ('package/data.bin', b'\xff\x00')]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    gate.inspect_archive(self.make_zip({name: payload}, 'source.zip'))
        with self.assertRaisesRegex(ValueError, 'unsafe archive entry'):
            gate.inspect_archive(self.make_zip({'README.md': b'# Sample'}, 'source.zip', link='README.md'))
        self.assertEqual(gate.inspect_archive(self.make_zip({'README.md': b'# Sample'}, 'source.zip')), 1)


if __name__ == '__main__':
    unittest.main()

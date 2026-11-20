"""Inspect built archives without extraction; reject private state and links."""
import argparse
import hashlib
import io
from pathlib import Path, PurePosixPath
import stat
import tarfile
import zipfile

from release_check import PRIVATE_NAMES, PRIVATE_TEXT


PLAYER_SUPPORT_NAME = 'lumanest-player-support-1-win-x64.zip'
PLAYER_SUPPORT_SHA256 = '3e4493277990362e27a93064338cc0be9609b36383b1d9fea1037d9c940b175a'
PLAYER_SUPPORT_MEMBERS = {
    'fribidi-0.dll': '7b5add237f08bad7c6075fa056892dcf4e0fb5093c9205814ceb3dde257e87ff',
    'vulkan-1.dll': 'e13eb45be42a7fd23d39368a8941e5f6b10766fe6cd7c7602336d239d51a9869',
    'sources/fribidi-1.0.16.tar.xz': '1b1cde5b235d40479e91be2f0e88a309e3214c8ab470ec8a2744d82a5a9ea05c',
    'licenses/FriBiDi-COPYING.txt': '32434afcc8666ba060e111d715bfdb6c2d5dd8a35fa4d3ab8ad67d8f850d2f2b',
    'licenses/Vulkan-Loader-LICENSE.txt': '43c0a37e6a0fa7ff3c843b3ec5a4fac84b712558ddac103fbd4c1649662a9ece',
    'NOTICE.txt': '7940a2140bbe497f4eb9e17c18c9dff9dfa62caf4000bc334189678720458913',
    'support-manifest.json': '62ccf4982d6c402732013f481e521f5cc09d1d70574ba23bf40862f9c9560de5',
}


def inspect_player_support(path):
    # This exception is for one named, pinned artifact, not for DLLs in arbitrary
    # archives. Inspect the same bounded bytes that were hashed (no reopen race).
    with path.open('rb') as stream:
        payload = stream.read(16 * 1024 * 1024 + 1)
    if len(payload) > 16 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != PLAYER_SUPPORT_SHA256:
        raise ValueError(path.name + ': player support archive checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        if len(entries) != len(PLAYER_SUPPORT_MEMBERS) or {entry.filename for entry in entries} != set(PLAYER_SUPPORT_MEMBERS):
            raise ValueError(path.name + ': unexpected player support member list')
        for entry in entries:
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or entry.flag_bits & 1:
                raise ValueError(entry.filename + ': unsafe player support member')
            if hashlib.sha256(archive.read(entry)).hexdigest() != PLAYER_SUPPORT_MEMBERS[entry.filename]:
                raise ValueError(entry.filename + ': player support member checksum mismatch')
        return len(entries)


def inspect_member(name, data, link=False):
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or ':' in name or link:
        raise ValueError(name + ': unsafe archive entry')
    if (path.name in PRIVATE_NAMES or any(part in {'.venv', 'secrets', '.git', '__pycache__'} for part in path.parts)
            or path.suffix.lower() in {'.key', '.pem', '.pyc', '.exe', '.dll', '.strm', '.mkv', '.mp4'}):
        raise ValueError(name + ': private state or unbundled component')
    if path.suffix.lower() not in {'.png', '.mo'}:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeError:
            raise ValueError(name + ': unexpected binary entry') from None
        if PRIVATE_TEXT.search(text):
            raise ValueError(name + ': private deployment literal')


def inspect_archive(path):
    if path.name == PLAYER_SUPPORT_NAME:
        return inspect_player_support(path)
    count = 0
    if path.suffix in ('.zip', '.whl'):
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                inspect_member(entry.filename, archive.read(entry), stat.S_ISLNK(entry.external_attr >> 16))
                count += 1
    else:
        with tarfile.open(path, 'r:gz') as archive:
            for entry in archive.getmembers():
                if entry.isdir():
                    continue
                if not entry.isfile():
                    raise ValueError(entry.name + ': unexpected archive member type')
                inspect_member(entry.name, archive.extractfile(entry).read())
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='*', type=Path)
    args = parser.parse_args()
    paths = args.archives or sorted(path for path in (Path(__file__).resolve().parents[1] / 'dist').iterdir()
                                   if path.suffix in ('.zip', '.whl', '.gz'))
    if not paths:
        raise SystemExit('No built archives found')
    for path in paths:
        print(f'{path.name}: {inspect_archive(path)} entries checked')


if __name__ == '__main__':
    main()

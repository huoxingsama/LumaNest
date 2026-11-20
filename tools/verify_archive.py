"""Inspect built archives without extraction; reject private state and links."""
import argparse
from pathlib import Path, PurePosixPath
import stat
import tarfile
import zipfile

from release_check import PRIVATE_NAMES, PRIVATE_TEXT


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

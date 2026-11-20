"""Check installed package files against pip's wheel RECORD, excluding user data."""
import base64
import hashlib
import importlib.metadata
import json
from pathlib import PurePosixPath


def verify_distribution(distribution=None):
    distribution = distribution or importlib.metadata.distribution('lumanest')
    checked, changed, missing = 0, [], []
    for entry in distribution.files or []:
        relative = PurePosixPath(str(entry).replace('\\', '/'))
        if not relative.parts or relative.parts[0] not in ('personal_cinema', 'jellyfin_mpv_shim'):
            continue
        if relative.suffix == '.pyc' or '__pycache__' in relative.parts:
            continue
        file_hash = entry.hash
        if file_hash is None:
            changed.append(str(relative))
            continue
        file = distribution.locate_file(entry)
        if not file.is_file():
            missing.append(str(relative))
            continue
        digest = hashlib.new(file_hash.mode)
        with file.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        actual = base64.urlsafe_b64encode(digest.digest()).rstrip(b'=').decode('ascii')
        checked += 1
        if actual != file_hash.value:
            changed.append(str(relative))
    return {'version': distribution.version, 'verified': checked > 0 and not changed and not missing,
            'checked_package_files': checked, 'changed': changed, 'missing': missing}


def main():
    try:
        result = verify_distribution()
    except importlib.metadata.PackageNotFoundError:
        result = {'verified': False, 'message': 'LumaNest is not installed in this environment'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Build a deterministic, allowlisted source archive without local state."""
import hashlib
import json
import tomllib
import zipfile
from pathlib import Path
from release_check import ROOT, check, files


def main():
    issues = check()
    if issues:
        raise SystemExit('\n'.join(issues))
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text('utf-8'))['project']
    version, name = project['version'], project['name']
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    target = output / f'{name}-{version}-source.zip'
    manifest = []
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in files():
            relative = source.relative_to(ROOT).as_posix()
            data = source.read_bytes()
            info = zipfile.ZipInfo(f'{name}-{version}/' + relative, (2026, 11, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
            manifest.append({'path': relative, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    (output / 'source-manifest.json').write_text(json.dumps(manifest, indent=2), 'utf-8')
    (output / 'SHA256SUMS.txt').write_text('\n'.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name
        for p in sorted(output.iterdir()) if p.suffix in ('.whl', '.zip', '.gz')) + '\n', 'utf-8')
    print(f'Created {target.name}: {len(manifest)} files, {target.stat().st_size} bytes')


if __name__ == '__main__':
    main()

"""Release allowlist and privacy boundary checks; prints paths, never secrets."""
import ast
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {'README.md', 'LICENSE.md', 'THIRD_PARTY_NOTICES.md', 'SECURITY.md',
             'CONTRIBUTING.md', 'CHANGELOG.md', 'pyproject.toml', 'MANIFEST.in',
             '.gitignore', 'requirements-windows.lock'}
FOLDERS = {'src', 'vendor', 'tests', 'tools', 'docs', 'scripts', '.github'}
EXTENSIONS = {'.py', '.md', '.json', '.html', '.css', '.js', '.cjs', '.lua',
              '.png', '.mo', '.xml', '.glsl', '.ps1', '.yml', '.yaml', '.txt'}
EXCLUDED = {'__pycache__', '.git', 'build', 'dist', '.venv', 'node_modules'}
PRIVATE_NAMES = {'cred.json', 'users.json', 'accounts.json', 'credentials.json',
                 'cache-state.json', 'policy.json', 'runtime-manifest.json', 'config.json'}
PRIVATE_TEXT = re.compile(r'(?i)(?:[A-Z]:[\\/]Users[\\/](?!Public\b|Example\b)[^\\/\s]+|'
                          r'[A-Z]:[\\/]MediaLibrary\b|100\.64\.\d+\.\d+|'
                          r'tail[a-z0-9]{6,}\.ts\.net|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----|'
                          r'(?:https?://)[^/@\s]+:[^/@\s]+@)')


def safe_path(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not path.resolve().is_relative_to(ROOT.resolve())):
        raise ValueError(path.relative_to(ROOT).as_posix() + ': redirected release path')


def files():
    # Prune before traversing: never enter virtualenvs, generated data, links,
    # junctions or another filesystem subtree through an allowlisted directory.
    selected = []
    for name in sorted(TOP_FILES | FOLDERS):
        top = ROOT / name
        if not top.exists() and not top.is_symlink():
            continue
        safe_path(top)
        if name in TOP_FILES:
            if top.is_file():
                selected.append(top)
            continue
        for base, directories, names in os.walk(top, followlinks=False):
            directories[:] = [d for d in directories if d not in EXCLUDED and not d.endswith('.egg-info')]
            for directory in directories:
                safe_path(Path(base) / directory)
            for file in names:
                path = Path(base) / file
                if path.suffix.lower() in EXTENSIONS:
                    safe_path(path)
                    if path.is_file():
                        selected.append(path)
    yield from sorted(selected)


def check():
    problems = []
    try:
        selected = list(files())
    except ValueError as error:
        return [str(error)]
    for path in selected:
        relative = path.relative_to(ROOT).as_posix()
        if path.is_symlink() or path.name in PRIVATE_NAMES or path.name.endswith(('.key', '.pem')):
            problems.append(relative + ': private file or symbolic link')
        if path.suffix in {'.png', '.mo'}:
            continue
        try:
            text = path.read_text('utf-8-sig')
        except UnicodeError:
            problems.append(relative + ': unexpected binary file')
            continue
        if PRIVATE_TEXT.search(text):
            problems.append(relative + ': private deployment literal')
        if path.suffix == '.py':
            try:
                ast.parse(text, relative)
            except SyntaxError as error:
                problems.append(relative + ': syntax error at line ' + str(error.lineno))
    for path in TOP_FILES - {'CONTRIBUTING.md', 'CHANGELOG.md', 'requirements-windows.lock'}:
        if not (ROOT / path).is_file():
            problems.append(path + ': required release file missing')
    return problems


if __name__ == '__main__':
    problems = check()
    print('\n'.join(problems) if problems else f'Release boundary passed: {len(list(files()))} allowlisted files')
    sys.exit(bool(problems))

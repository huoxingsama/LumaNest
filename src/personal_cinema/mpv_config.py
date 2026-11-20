"""Read global MPV options, including bounded local includes, without execution."""
import os
from pathlib import Path


def global_options(path, *, _seen=None, _depth=0):
    path = Path(path).expanduser().resolve()
    seen = set() if _seen is None else _seen
    if path in seen or _depth > 8 or not path.is_file() or path.stat().st_size > 1024 * 1024:
        return {}
    seen.add(path)
    values = {}
    for line in path.read_text('utf-8-sig').splitlines():
        line = line.strip()
        if line.startswith('['): break
        if not line or line.startswith(('#', ';')) or '=' not in line: continue
        key, value = (part.strip() for part in line.split('=', 1))
        value = value.strip('"\'')
        if key == 'include':
            if '://' in value or '\x00' in value: continue
            include = Path(os.path.expandvars(value)).expanduser()
            if value.startswith('~~/'): include = path.parent / value[3:]
            if not include.is_absolute(): include = path.parent / include
            values.update(global_options(include, _seen=seen, _depth=_depth + 1))
        else:
            values[key] = value
    return values

"""UI scale factor: the logical/physical pixel boundary.

Every number in Python view code is logical; bitmap rasterization and the
Lua side are the only physical things. See `mpvtk/GUIDE.md` section 8 for
why images are the leak in that abstraction, and for the exact-vs-rounded
split below.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import math
_scale = 1.0

def set_scale(value):
    """Set the global factor. Values <= 0 or non-finite fall back to 1.0."""
    global _scale
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 1.0
    if not math.isfinite(v) or v <= 0:
        v = 1.0
    _scale = v
    return _scale

def scale():
    return _scale

def px(dip_value):
    """Logical -> physical. THE rounding rule; never inline this."""
    if _scale == 1.0:
        return int(dip_value) if isinstance(dip_value, int) else _round(dip_value)
    return _round(dip_value * _scale)

def dip(px_value):
    """Physical -> logical. For anything coming back from mpv in real
    pixels (mouse positions, surface size)."""
    return px_value / _scale

def raster(w, h):
    """Physical (w, h) for a producer about to rasterize a logical box."""
    return (px(w), px(h))

def logical_size(size):
    """Physical surface size -> the logical size views lay out against.

    Deliberately float: truncating here would lose up to a pixel of usable
    width per axis, and layout rounds at the end anyway.
    """
    if _scale == 1.0:
        return size
    return (size[0] / _scale, size[1] / _scale)

def _round(v):
    return int(math.floor(v + 0.5))
_PX_KEYS = ('x', 'y', 'w', 'h', 'radius', 'bw', 'pw', 'cw', 'ch', 'rh', 'snap', 'snap_off', 'off0', 'isz')
_EXACT_KEYS = ('size',)
_PX_LIST_KEYS = ('snaps',)
_NESTED = ('hover',)
_NESTED_PX_KEYS = ('bw', 'radius')

def scale_scene(nodes):
    """Convert a laid-out scene from logical to physical, in place."""
    if _scale == 1.0:
        return nodes
    for node in nodes:
        for key in _PX_KEYS:
            v = node.get(key)
            if v is not None:
                node[key] = px(v)
        for key in _EXACT_KEYS:
            v = node.get(key)
            if v is not None:
                node[key] = v * _scale
        for key in _PX_LIST_KEYS:
            v = node.get(key)
            if v is not None:
                node[key] = [px(x) for x in v]
        for nest in _NESTED:
            d = node.get(nest)
            if not isinstance(d, dict):
                continue
            if not any((d.get(k) is not None for k in _NESTED_PX_KEYS)):
                continue
            d = dict(d)
            for key in _NESTED_PX_KEYS:
                v = d.get(key)
                if v is not None:
                    d[key] = px(v)
            node[nest] = d
    return nodes

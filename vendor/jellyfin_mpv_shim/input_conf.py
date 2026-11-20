"""Writing the user's own key choices into mpv's ``input.conf`` (#16).

#16 gives the player keys back to mpv, but a user who *changed* one made a
real choice, so a one-time migration turns the settings mpv can express into
real mpv bindings in the shim's own config directory and clears the settings
so nothing binds them twice.

**Two things are load-bearing.** *Where it writes*: before the first ``[``,
because everything after a section header belongs to that section until the
next, so appending to the end of the file would put the bindings somewhere
they apply conditionally or never while the file looked right.
(``mpv_options.hwdec_pinned_by_config`` reads the same rule from the other
side.) *What it declines to write*: a setting whose meaning mpv cannot
express -- ``use_web_seek`` and ``skip_intro_on_seek`` -- because a migration
that quietly dropped a feature would be worse than none.

See docs/architecture.md section 6, and docs/mpv-backends.md section 5 for
which keys the shim claims at all.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import os
log = logging.getLogger('input_conf')
MARKER = '# --- migrated from jellyfin-mpv-shim key settings ---'
FIXED = (('kb_pause', 'cycle pause'), ('kb_fullscreen', 'cycle fullscreen'))
SEEKS = (('up', 'seek_up', 'seek_v_exact'), ('down', 'seek_down', 'seek_v_exact'), ('right', 'seek_right', 'seek_h_exact'), ('left', 'seek_left', 'seek_h_exact'))
LEGACY_SEEK_DEFAULTS = {'seek_up': 60, 'seek_down': -60, 'seek_right': 5, 'seek_left': -5, 'seek_v_exact': False, 'seek_h_exact': False}

def _cleared(value):
    """Whether the user turned this binding off.

    **[iw]**: "unless of course they set the config options to null, that
    means they were probably parking our nav interception away and eating
    the penalty." Re-binding that key during a migration would undo the
    exact thing #16 is for. ``"None"`` is included because that is what a
    cleared binding was written as before the settings became Optional.
    """
    return value in (None, '', 'None')

def _changed(settings, name):
    """Whether ``name`` holds something other than the shim's own default.

    **Not ``__fields_set__``.** That says "this key was in the file", and
    ``save()`` writes all 186 of them — so after a single save every key is
    in the file and the whole config reads as deliberately chosen. The first
    version of this migrated `space cycle pause`, `f cycle fullscreen` and
    the four arrows into somebody's input.conf: every one of them mpv's own
    default, written back as an explicit binding for nothing. **[iw]**:
    "these are default bindings, we don't need to set them."

    Comparing to the class default is the honest question. It cannot tell a
    user who deliberately typed the default from one who never touched it —
    but for these settings those two want the same thing, because our
    defaults ARE mpv's for every key here.
    """
    return getattr(settings, name, None) != getattr(type(settings), name, None)

def _legacy(raw, name):
    """A dropped seek setting's value from the raw config, or its default."""
    return (raw or {}).get(name, LEGACY_SEEK_DEFAULTS[name])

def _legacy_changed(raw, name):
    """Whether the raw config holds something other than the old default."""
    return _legacy(raw, name) != LEGACY_SEEK_DEFAULTS[name]

def plan(settings, raw=None):
    """``[(setting, key, command)]`` to write, for the bindings the user
    changed.

    Only ones that differ from our default, only ones that are not cleared,
    and only where mpv can express what the shim did.

    ``raw`` is the config file's own dict. The seek distances are read from
    it rather than from ``settings`` because they are no longer part of the
    schema -- see LEGACY_SEEK_DEFAULTS. Absent, only the `kb_*` half runs,
    which is what a caller with no file to offer should get.
    """
    out = []
    for name, command in FIXED:
        if not _changed(settings, name):
            continue
        value = getattr(settings, name, None)
        if not _cleared(value):
            out.append((name, value, command))
    if getattr(settings, 'use_web_seek', False):
        return out
    for key, amount_key, exact_key in SEEKS:
        if not (_legacy_changed(raw, amount_key) or _legacy_changed(raw, exact_key)):
            continue
        amount = _legacy(raw, amount_key)
        if _cleared(amount):
            continue
        out.append((amount_key, key, 'seek %d%s' % (amount, ' exact' if _legacy(raw, exact_key) else '')))
    return out

def render(entries):
    """The block to insert, marker included."""
    lines = [MARKER, '# Written once, when this client stopped binding these keys', '# itself. Edit or delete them freely -- nothing rewrites this.']
    lines += ['%s %s' % (key, command) for _n, key, command in entries]
    return '\n'.join(lines) + '\n\n'

def insert_before_first_section(existing, block):
    """``existing`` with ``block`` inserted **above the first ``[``**.

    Not appended. Everything after a section header belongs to that section
    until the next one, so appending to a file that has any section puts
    these bindings inside it -- written, looking right, and never firing.
    """
    lines = existing.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith('['):
            return ''.join(lines[:i]) + block + ''.join(lines[i:])
    if existing and (not existing.endswith('\n')):
        existing += '\n'
    return existing + block

def migrate(settings, path, raw=None):
    """Write the plan into ``path`` and clear the settings it carried.

    Returns the entries written (empty if there was nothing to do), so the
    caller can decide whether the config needs saving.
    """
    entries = plan(settings, raw)
    if not entries:
        return []
    if getattr(settings, 'mpv_ext', False) and getattr(settings, 'mpv_ext_no_ovr', False):
        log.info('mpv_ext_no_ovr is set, so mpv reads your own config directory and not this one -- not writing %s. To keep these, add them to your own input.conf:\n%s', path, '\n'.join(('%s %s' % (key, cmd) for _n, key, cmd in entries)))
        return []
    migrated = [name for name, _k, _c in entries]
    try:
        existing = ''
        if os.path.isfile(path):
            with open(path, encoding='utf-8') as fh:
                existing = fh.read()
        if MARKER in existing:
            return []
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(insert_before_first_section(existing, render(entries)))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        log.warning('Could not write %s; keeping the key settings.', path, exc_info=True)
        return []
    for name in migrated:
        if not name.startswith('kb_'):
            continue
        setattr(settings, name, None)
    log.info('Migrated %d key binding(s) to %s', len(entries), path)
    return entries

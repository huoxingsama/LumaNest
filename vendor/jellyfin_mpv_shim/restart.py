"""Restarting the app in place, for settings that only apply at startup.

**The relaunch happens as the process's last act**, registered with
``exit_watchdog.set_final_action`` so it runs on *both* ways out and not as an
``exec`` from wherever the button was pressed. A line at the end of ``main``
would be dead code below ``os._exit`` and would miss the wedged exit -- which
is exactly when a user who pressed *Restart Now* needs the app to come back.
See docs/architecture.md section 4.

So the new process starts with the shutdown finished: state flushed, the
instance lock free, the tray child and mpv gone, and no ``execv`` from a
multithreaded process. The cost is that the app really does go away and come
back, which is what the user asked for.

``command()`` rebuilds the launch from **parsed** arguments rather than
copying ``sys.argv``, and the allowlist there is deliberate: a flag nobody
has taught this function about is dropped rather than repeated. Dropping is
the safe direction -- ``--reset-shaders`` would re-run a recovery action,
``add``/``clear`` are one-shot commands, and ``--password`` would put the
user's password back on the process list of a launch they did not type.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import os
import subprocess
import sys
log = logging.getLogger('restart')
_requested = False
_pending_settings = frozenset()

def command():
    """The argv that would start this app again, or None if unknown.

    ``--config`` above all must survive: without it a restart from a
    non-default configuration directory would come back against the default
    one, which is a different app with different servers.
    """
    exe = sys.executable
    if not exe:
        return None
    if getattr(sys, 'frozen', False):
        base = [exe]
    else:
        script = sys.argv[0] if sys.argv else ''
        if not script or not os.path.exists(script):
            return None
        base = [exe, script]
    return base + _durable_flags()

def _durable_flags():
    """The command-line options that describe *how this copy is running*.

    Everything else is left out by construction -- see the module docstring
    for why the allowlist is the safe direction.
    """
    from .args import CLI_OVERRIDES, get_args
    try:
        args = get_args()
    except Exception:
        log.debug('could not read the arguments for a restart', exc_info=True)
        return []
    out = []
    if getattr(args, 'config', None):
        out += ['--config', args.config]
    pending = _pending_settings
    for override in CLI_OVERRIDES:
        value = getattr(args, override.dest, None)
        if value is not None and override.key not in pending:
            out += override.spell(value)
    if getattr(args, 'disable_hwdec', False):
        out.append('--disable-hwdec')
    if getattr(args, 'debug', False):
        out.append('--debug')
    return out
_PYI_MARKERS = ('_MEIPASS2',)
_PYI_PREFIX = '_PYI_'
_PYI_PATH_VARS = ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH', 'LIBPATH')

def child_env():
    """The environment to start the new copy with.

    A plain copy of ours, **except** under a frozen build, where the
    PyInstaller bootloader has left marks describing *this* launch that would
    misdirect the next one (see the two tables above).

    **Gated on ``sys.frozen``, and that gate is load-bearing**: run from
    source, `LD_LIBRARY_PATH` is the user's own and there is no `_ORIG` to
    restore it from, so the same code would silently delete it.
    """
    env = dict(os.environ)
    if not getattr(sys, 'frozen', False):
        return env
    for name in list(env):
        if name in _PYI_MARKERS or name.startswith(_PYI_PREFIX):
            env.pop(name, None)
    for name in _PYI_PATH_VARS:
        original = env.pop(name + '_ORIG', None)
        if original is not None:
            env[name] = original
        else:
            env.pop(name, None)
    return env

def supported():
    """Whether the restart button can be offered at all.

    Asked **before** anything is shut down, so a machine this cannot work on
    gets a banner saying "restart to apply" rather than a button that quietly
    does nothing after taking the app away.

    **Deliberately not memoized here.** It reads `sys.argv[0]` and the
    filesystem, both of which a test legitimately patches, so a module-level
    memo would hand a value computed under one patch to the next. The caller
    caches it instead, where the scope is one browser session
    (`MpvtkBrowser.can_restart`).
    """
    return command() is not None

def request(pending=()):
    """Ask for a relaunch after the shutdown that is about to happen.

    Does not itself stop anything: the caller triggers the ordinary exit,
    which is the only way the shutdown sequence runs in full.

    ``pending`` is the settings this restart is for. A command-line override
    naming one of them is dropped from the relaunch, because ``main``
    applies those on top of the saved config and would undo the change the
    user is restarting to get. See :func:`_durable_flags`.
    """
    global _requested, _pending_settings
    _requested = True
    _pending_settings = frozenset(pending or ())
    log.info('Restart armed; it will happen after the shutdown completes.')

def cancel():
    global _requested, _pending_settings
    if _requested:
        log.info('Restart disarmed.')
    _requested = False
    _pending_settings = frozenset()

def requested():
    return _requested
PREVIOUS_LOG = 'log.prev.txt'

def _preserve_log():
    """Move this run's log aside so the replacement does not truncate it.

    ``configure_log_file`` opens ``log.txt`` with ``mode="w"`` -- right for an
    app that starts once, but a restart is two runs telling one story and the
    successor would overwrite the half that matters. Anyone reading it is
    reading it precisely because the replacement failed to come up.

    Renamed rather than copied, so the parent's handler keeps writing to the
    same inode and the rest of its shutdown lands in the preserved file.
    Best-effort: on Windows an open file cannot be renamed, and losing the
    previous log is not a reason to skip the restart.
    """
    try:
        from . import conffile
        from .conf import settings
        from .constants import APP_NAME
        if not getattr(settings, 'write_logs', False):
            return
        path = conffile.get(APP_NAME, 'log.txt')
        if os.path.exists(path):
            os.replace(path, os.path.join(os.path.dirname(path), PREVIOUS_LOG))
    except Exception:
        log.debug('could not preserve the log across the restart', exc_info=True)

def relaunch_if_requested():
    """Start a fresh copy, if one was asked for. Returns True if spawned.

    Called from ``exit_watchdog``'s final-action hook, immediately before
    ``os._exit`` on whichever exit path got there. Failure is logged and
    swallowed: by this point the app has already shut down, so raising would
    turn "the restart did not happen" into a traceback on the way out and
    change nothing else.
    """
    global _requested
    if not _requested:
        log.debug('No restart was requested; exiting normally.')
        return False
    _requested = False
    cmd = command()
    if cmd is None:
        log.error('Cannot restart: this launch cannot be reconstructed. Start %s again by hand.', os.path.basename(sys.argv[0] or ''))
        return False
    kwargs = {'close_fds': True, 'env': child_env()}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    else:
        kwargs['start_new_session'] = True
    log.info('Restarting: %s', ' '.join(cmd))
    _preserve_log()
    try:
        subprocess.Popen(cmd, **kwargs)
        return True
    except Exception:
        log.exception('Could not restart; start %s again by hand.', os.path.basename(sys.argv[0] or ''))
        return False

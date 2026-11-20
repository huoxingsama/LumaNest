"""Make quitting actually quit, and say what stopped it if it didn't.

Two things can leave the app running with no window and no way to quit it,
and CPython waits for both: a shutdown step that never returns (which the
deadline in :func:`arm` catches) and a thread that outlives its ``stop()``
(which :func:`finish` reports). Both report stacks rather than names, because
the frame a thread is parked in is what identifies the call needing bounding.

The forced exit is safe because it runs only *after* the orderly shutdown;
what it skips is the interpreter's own wait. Everything here also tolerates
having nowhere to write — a Windows GUI build (pythonw, the frozen installer
build) has no console, so both streams are ``None`` — hence :func:`_write` /
:func:`_flush` / :func:`_dump_to` and never a stream touched directly.

The derivation, including the two-minute wedge this was built for, is in
docs/architecture.md section 4.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
import traceback
log = logging.getLogger('exit_watchdog')
GRACE_SECONDS = 3.0
FINAL_ACTION_SECONDS = 10.0
SHUTDOWN_DEADLINE = 20.0
_watchdog = None
_disarm = threading.Event()
_final_action = None
_final_lock = threading.Lock()
_final_done = False

def _flush(stream):
    """Flush a stream that may not exist; a Windows GUI build has no console,
    so both of them are ``None`` and flushing raises AttributeError."""
    if stream is None:
        return
    try:
        stream.flush()
    except Exception:
        pass

def _write(stream, text):
    """Write to a stream that may not exist or may be closed."""
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except Exception:
        pass

def _dump_to(stream):
    """faulthandler-dump every thread to ``stream``, if it can take one.

    faulthandler writes through a file descriptor, so a stream is only
    usable if ``fileno()`` works — which rules out ``None``, and also the
    wrappers a frozen GUI build can leave in place of a real console.
    """
    if stream is None:
        return False
    try:
        if stream.fileno() < 0:
            return False
    except Exception:
        return False
    try:
        faulthandler.dump_traceback(file=stream, all_threads=True)
        _flush(stream)
        return True
    except Exception:
        return False

def enable_manual_dumps():
    """``kill -USR1 <pid>`` dumps every thread's stack to stderr.

    A hang is only diagnosable while it is hanging, at which point it is
    too late to add instrumentation. No-op where the signal does not
    exist (Windows).
    """
    if not hasattr(signal, 'SIGUSR1'):
        return
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)
    except Exception:
        log.debug('could not register SIGUSR1 stack dumps', exc_info=True)

def _dump_all_threads(why):
    """Every thread's Python stack, to stderr and to the log file.

    ``faulthandler`` rather than the walk in :func:`_describe`: this runs
    while the rest of the process is blocked, and it covers the main
    thread — which, when a step is wedged, is the one parked in the call
    that names it.
    """
    banner = '\n===== %s =====\n' % why
    _write(sys.stderr, banner)
    dumped = _dump_to(sys.stderr)
    for handler in logging.getLogger().handlers:
        stream = getattr(handler, 'stream', None)
        if stream is None or stream in (sys.stderr, sys.stdout):
            continue
        _write(stream, banner)
        dumped = _dump_to(stream) or dumped
    if not dumped:
        log.warning('%s\n%s', why, _describe(_survivors()))

def set_final_action(fn):
    """Register the last thing this process does before ``os._exit``.

    **The point is that it runs on both exits, not just the tidy one.**
    This module has two: :func:`finish`, after an orderly shutdown, and the
    deadline in :func:`arm`, which force-kills a wedged one. Anything that
    has to happen however the process ends belongs here rather than in
    ``main``, where it can only cover the first.

    The restart is exactly that. A relaunch written into ``main`` after the
    shutdown covers the ordinary case and silently does not happen when a
    step wedges -- so somebody who pressed *Restart Now* would watch the app
    disappear, on the one occasion the process needed the most help coming
    back.

    Deliberately **not** a reason to soften the deadline. The old process
    still has to die for the new one to take the instance lock, so "make
    sure we end" stays exactly as it was; what changes is that ending is no
    longer the last word.

    Runs once, whichever path gets there first, and its failure is logged
    rather than raised: it is the last statement of a process that is
    already leaving.
    """
    global _final_action
    _final_action = fn

def _run_final_action():
    """Run the registered action once, bounded, whichever path gets here.

    **The lock is held across the call, not just around the flag.** Both
    exits reach this: `finish` disarms first, but the watchdog may already
    be past that check and mid-dump, so the two really do race. With the
    action outside the lock the loser returned immediately and carried on to
    `os._exit` -- killing the process while the winner was still inside
    `subprocess.Popen`. The log would say "Restarting:", the once-only flag
    would be set so nothing retried, and no replacement would exist: the
    exact failure the hook was added to prevent, on the path it was added
    for. The dump above is slower than a zero-straggler grace, so that
    interleaving is the likely one rather than the exotic one.

    Bounded by a watchdog of its own, because holding a lock across foreign
    work on the *forced* exit path would otherwise hand this module's one
    guarantee to that work. The action releases the instance lock and spawns
    a process; either can block on a dead network mount, and its logging
    takes handler locks that the wedged main thread may be holding.
    """
    global _final_done
    with _final_lock:
        if _final_action is None or _final_done:
            return
        _final_done = True
        action = _final_action
        bail = threading.Timer(FINAL_ACTION_SECONDS, _final_action_timeout)
        bail.daemon = True
        bail.start()
        try:
            action()
        except Exception:
            log.exception('the final shutdown action failed')
        finally:
            bail.cancel()

def _final_action_timeout():
    """The final action outstayed its welcome; end the process anyway.

    Deliberately terse and lock-free: the reason this fires may be that
    logging itself is wedged, so it writes through `_write` rather than
    through a handler.
    """
    _write(sys.stderr, '\nfinal shutdown action did not finish within %.0fs; exiting anyway\n' % FINAL_ACTION_SECONDS)
    os._exit(1)

def arm(deadline=None):
    """Start the shutdown deadline.

    Call at the *start* of the shutdown sequence, not the end: the failure
    this guards against is a step that never returns, and anything placed
    after such a step is unreachable by definition.
    """
    global _watchdog
    if _watchdog is not None:
        return
    seconds = SHUTDOWN_DEADLINE if deadline is None else deadline

    def watch():
        if _disarm.wait(seconds):
            return
        _dump_all_threads('shutdown did not finish within %.0fs - all thread stacks follow; the main thread shows which step is wedged' % seconds)
        _run_final_action()
        logging.shutdown()
        _flush(sys.stderr)
        os._exit(1)
    _watchdog = threading.Thread(target=watch, name='exit-watchdog', daemon=True)
    _watchdog.start()

def _survivors():
    """Non-daemon threads still running, excluding the main thread (which
    is the caller). These are exactly the threads that would keep the
    interpreter alive after ``main`` returns; a daemon straggler costs
    nothing at exit and would only be noise here."""
    me = threading.current_thread()
    return [t for t in threading.enumerate() if t is not me and t is not threading.main_thread() and t.is_alive() and (not t.daemon)]

def _describe(threads):
    """Name each straggler and where it is parked, a few frames each.

    Deliberately briefer than :func:`_dump_all_threads`: a leaked thread
    at the end of an otherwise clean shutdown warrants a line in the log,
    not a full-process dump.
    """
    frames = sys._current_frames()
    lines = []
    for t in threads:
        lines.append('  %s (id=%s)' % (t.name, t.ident))
        frame = frames.get(t.ident)
        if frame is None:
            lines.append('      <no stack available>')
            continue
        for entry in traceback.format_stack(frame)[-4:]:
            for line in entry.rstrip().splitlines():
                lines.append('      ' + line.strip())
    return '\n'.join(lines)

def _await_stragglers(threads):
    """Give the whole set GRACE_SECONDS between them to end on their own."""
    deadline = time.monotonic() + GRACE_SECONDS
    for t in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(timeout=remaining)

def finish(status=0):
    """Wait briefly for stragglers, report any, then end the process.

    Call as the last statement of ``main``, after the orderly shutdown.
    """
    _disarm.set()
    stuck = _survivors()
    if stuck:
        _await_stragglers(stuck)
        stuck = _survivors()
    if stuck:
        log.warning('%d thread(s) did not stop during shutdown; exiting anyway. This is a leak — the stacks below show what they are parked on:\n%s', len(stuck), _describe(stuck))
    _run_final_action()
    logging.shutdown()
    _flush(sys.stdout)
    _flush(sys.stderr)
    os._exit(status)

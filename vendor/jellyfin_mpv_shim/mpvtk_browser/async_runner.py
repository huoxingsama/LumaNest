"""Off-thread work with epoch-guarded staleness.

Owns the epoch — a monotonic counter meaning "navigation has moved on" — its
lock and the worker pool. Reading the epoch from anywhere is fine and
expected; *advancing* it is this module's job alone, which
``tests/test_source_invariants.py`` enforces.

**The lock protects writers from each other, not from the reader.** The
browser's ``build()`` reads route data unlocked, which is safe only because
every writer ends with ``invalidate()``. Do not "fix" it by locking the
reader.

``run()`` has three arms and all three have bitten. The rule to have before
writing a callback: a guard that must not outlive the call goes in
``always=``, never in ``on_done`` or ``on_error`` -- past the epoch BOTH of
those are dropped. ``on_error`` is deliberately not epoch-gated at all, which
puts the burden on the handler. Each arm and the bug behind it: see
docs/browser-shell.md section 2; ``tests/test_async_runner.py`` pins them.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
log = logging.getLogger('mpvtk_browser.async_runner')
WORKERS = 4

class AsyncRunner:
    """Owns the epoch, its lock, and the worker pool."""

    def __init__(self, invalidate=None, workers=WORKERS, thread_name_prefix='mpvtk-api'):
        self.invalidate = invalidate or (lambda: None)
        self._epoch = 0
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix)

    @property
    def epoch(self):
        """Current epoch. Read it on the loop thread and pass it down; do not
        re-read it inside a worker, which would be racing the navigation the
        value is meant to guard."""
        return self._epoch

    def bump(self):
        """Invalidate every in-flight result. Returns the new epoch."""
        with self._lock:
            self._epoch += 1
            return self._epoch

    @property
    def pool(self):
        """The executor. Exposed because a few callers submit bare jobs that
        need no epoch guard (the cast compositor, a fire-and-forget client
        mutation), and because tests substitute a synchronous stand-in."""
        return self._pool

    @pool.setter
    def pool(self, value):
        self._pool = value

    def submit(self, fn):
        """Run ``fn()`` off the loop thread with no epoch guard and no
        callbacks. For work whose result nothing waits on."""
        return self._pool.submit(fn)

    def shutdown(self, wait=False, cancel_futures=True):
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)

    def run(self, work, on_done, epoch, on_error=None, always=None):
        """Run ``work()`` off the loop thread; apply ``on_done(result)`` only
        if ``epoch`` still matches. See the module docstring for the full
        contract — every clause of it is load-bearing."""

        def task():
            try:
                try:
                    result = work()
                except Exception as exc:
                    log.warning('async work failed', exc_info=True)
                    if on_error is None:
                        return
                    with self._lock:
                        try:
                            on_error(exc)
                        except Exception:
                            log.warning('async on_error failed', exc_info=True)
                    return
                with self._lock:
                    if epoch != self._epoch:
                        return
                    try:
                        on_done(result)
                    except Exception:
                        log.warning('async on_done failed', exc_info=True)
            finally:
                if always is not None:
                    with self._lock:
                        try:
                            always()
                        except Exception:
                            log.warning('async always failed', exc_info=True)
                self.invalidate()
        try:
            self._pool.submit(task)
        except RuntimeError:
            log.debug('pool is shut down; dropping async work')
            if always is not None:
                try:
                    always()
                except Exception:
                    log.warning('async always failed', exc_info=True)

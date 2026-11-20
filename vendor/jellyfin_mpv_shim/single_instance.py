"""Single-instance guard.

Primary election is an OS file lock held on a persistent fd for the process
lifetime; the same channel carries the ``stop`` command, which is why that is
a subcommand rather than a signal — it reaches exactly the instance owning
*this* config directory.

``acquire()`` fails open, so returning True does not by itself promise there
is only one of us: ``holds_lock`` is the stronger statement (the OS lock was
really taken), and anything whose safety rests on that uniqueness rather than
merely preferring it must read that instead.

Why two files, why the token, and why the guard file is never unlinked: see
docs/architecture.md section 1.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import os
import secrets
import socket
import sys
import threading
from . import conffile
from .constants import APP_NAME
log = logging.getLogger('single_instance')
_MAGIC = b'JMS1'

class SingleInstance:

    def __init__(self):
        self.on_activate = lambda: None
        self.on_stop = lambda: None
        self._sock = None
        self._guard_fd = None
        self.holds_lock = False
        self._token = secrets.token_hex(16).encode('ascii')
        self._lockpath = conffile.get(APP_NAME, 'instance.lock')
        self._guardpath = self._lockpath + '.guard'

    def acquire(self) -> bool:
        """Return True if we are the primary instance. Return False if another
        instance is already running (which we've asked to show its window)."""
        fd = self._try_lock()
        if fd is None:
            log.warning('Could not open the instance guard file; continuing without the single-instance guard.')
            return True
        if fd is False:
            if not self._handoff_to_existing():
                log.warning('%s is already running (instance lock is held) but did not respond to the activation request.', APP_NAME)
            return False
        self._guard_fd = fd
        self.holds_lock = True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('127.0.0.1', 0))
            sock.listen(5)
        except OSError:
            log.warning('Single-instance activation socket unavailable; running without it.', exc_info=True)
            return True
        self._sock = sock
        port = sock.getsockname()[1]
        self._write_endpoint(port)
        threading.Thread(target=self._serve, daemon=True).start()
        return True

    def release(self):
        owned = self.holds_lock
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._guard_fd is not None:
            try:
                os.close(self._guard_fd)
            except OSError:
                pass
            self._guard_fd = None
        self.holds_lock = False
        if owned:
            try:
                os.remove(self._lockpath)
            except OSError:
                pass

    def _try_lock(self):
        """Try to take the election lock on the guard file. Returns the open
        fd on success, False if another live process holds it, None if the
        file can't be opened at all."""
        try:
            fd = os.open(self._guardpath, os.O_RDWR | os.O_CREAT, 384)
        except OSError:
            return None
        try:
            if sys.platform.startswith(('win32', 'cygwin')):
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return False
        return fd

    def _write_endpoint(self, port: int):
        try:
            fd = os.open(self._lockpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
            with os.fdopen(fd, 'w') as fh:
                fh.write('%d\n%s\n' % (port, self._token.decode('ascii')))
        except OSError:
            log.debug('Could not write instance lock endpoint', exc_info=True)

    def _read_lock(self):
        try:
            with open(self._lockpath) as fh:
                port = int(fh.readline().strip())
                token = fh.readline().strip().encode('ascii')
            return (port, token)
        except (OSError, ValueError):
            return None

    def _request(self, command: bytes) -> bool:
        """Send one command to the running primary. True if it acknowledged.

        ``SHOW`` goes on the wire in the original wordless form so a newer
        client can still activate an older running copy across an upgrade —
        that instance would reject anything with a command word appended,
        because it compares the whole payload against its token."""
        info = self._read_lock()
        if not info:
            return False
        port, token = info
        payload = token if command == b'SHOW' else token + b' ' + command
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
                conn.sendall(_MAGIC + payload + b'\n')
                reply = conn.recv(8)
        except OSError:
            return False
        return reply.startswith(_MAGIC)

    def request_stop(self) -> bool:
        """Ask the instance owning this config directory to shut down.

        Returns False both when nothing is running and when something holds
        the lock but will not answer; the caller has the lock state needed to
        tell those apart."""
        return self._request(b'STOP')

    def is_running(self) -> bool:
        """True if another process holds the election lock right now.

        Takes and immediately drops the lock, so it must not be called by an
        instance that intends to *be* the primary — use acquire() for that."""
        fd = self._try_lock()
        if fd is False:
            return True
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return False

    def _handoff_to_existing(self) -> bool:
        if self._request(b'SHOW'):
            log.info('%s is already running; asked it to show its window.', APP_NAME)
            return True
        return False

    def _serve(self):
        while True:
            try:
                sock = self._sock
                if sock is None:
                    break
                conn, _addr = sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve_one, args=(conn,), daemon=True).start()

    def _serve_one(self, conn):
        try:
            conn.settimeout(5)
            data = conn.recv(64)
            if not data.startswith(_MAGIC):
                return
            parts = data[len(_MAGIC):].strip().split()
            if not parts or parts[0] != self._token:
                return
            command = parts[1] if len(parts) > 1 else b'SHOW'
            handler = {b'SHOW': self.on_activate, b'STOP': self.on_stop}.get(command)
            if handler is None:
                log.debug('Ignoring unknown instance command %r', command)
                return
            conn.sendall(_MAGIC + b'OK')
            try:
                handler()
            except Exception:
                log.error('Single-instance %s handler failed', command.decode('ascii', 'replace'), exc_info=True)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

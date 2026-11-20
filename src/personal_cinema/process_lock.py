"""Instance ownership and reentrant cross-process lifecycle serialization."""
import os
import threading
from pathlib import Path

from .configuration import _file_lock

_STATES = {}
_GUARD = threading.Lock()


class LifecycleLock:
    def __init__(self, path):
        self.path = Path(path).resolve()
        key = os.path.normcase(str(self.path))
        with _GUARD:
            self.state = _STATES.setdefault(key, {'lock': threading.RLock(), 'depth': 0})

    def __enter__(self):
        state = self.state
        state['lock'].acquire()
        try:
            if not state['depth']:
                context = _file_lock(self.path)
                context.__enter__()
                state['context'] = context
            state['depth'] += 1
            return self
        except BaseException:
            state['lock'].release()
            raise

    def __exit__(self, *error):
        state = self.state
        try:
            state['depth'] -= 1
            if not state['depth']:
                state.pop('context').__exit__(*error)
        finally:
            state['lock'].release()


class InstanceLease:
    """A non-blocking OS lease released even if its owning process crashes."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('实例锁不能是符号链接')
        stream = self.path.open('a+b')
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

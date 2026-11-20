from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import threading
from .player import playerManager
log = logging.getLogger('action_thread')

class ActionThread(threading.Thread):

    def __init__(self):
        self.trigger = threading.Event()
        self.halt = False
        threading.Thread.__init__(self)
    JOIN_TIMEOUT = 15.0

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join(timeout=self.JOIN_TIMEOUT)
        if self.is_alive():
            log.warning('Action thread did not stop within %.0fs; continuing shutdown without it. A queued player task is still running — see the stack dump from the exit watchdog.', self.JOIN_TIMEOUT)

    def run(self):
        force_next = False
        while not self.halt:
            try:
                if playerManager.is_active() or force_next:
                    playerManager.update()
            except Exception:
                log.exception('Error in action thread.')
            force_next = False
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()
        try:
            playerManager.update()
        except Exception:
            log.exception('Error in final action thread drain.')
actionThread = ActionThread()

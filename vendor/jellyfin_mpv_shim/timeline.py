from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import threading
import os
import jellyfin_apiclient_python.exceptions
from .conf import settings
from .player import playerManager, _mpv_errors
from .utils import Timer
log = logging.getLogger('timeline')

class TimelineManager(threading.Thread):

    def __init__(self):
        self.idleTimer = Timer()
        self.halt = False
        self.trigger = threading.Event()
        self.is_idle = True
        self._pushed_stopped = False
        threading.Thread.__init__(self)
    JOIN_TIMEOUT = 15.0

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join(timeout=self.JOIN_TIMEOUT)
        if self.is_alive():
            log.warning('Timeline thread did not stop within %.0fs; continuing shutdown without it.', self.JOIN_TIMEOUT)

    def run(self):
        while not self.halt:
            try:
                if playerManager.is_active():
                    self.send_timeline()
                    playerManager.push_playstate()
                    playerManager._maybe_save_volume()
                    self._pushed_stopped = False
                    if not settings.idle_when_paused or not playerManager.is_paused():
                        if self.is_idle and settings.idle_ended_cmd:
                            os.system(settings.idle_ended_cmd)
                        self.delay_idle()
                if not playerManager.is_active() and (not self._pushed_stopped):
                    playerManager.push_playstate(stopped=True)
                    self._pushed_stopped = True
                if self.idleTimer.elapsed() > settings.idle_cmd_delay and (not self.is_idle):
                    if settings.idle_when_paused and settings.stop_idle and playerManager.has_video():
                        playerManager.stop()
                    if settings.idle_cmd:
                        os.system(settings.idle_cmd)
                    self.is_idle = True
                if settings.mpv_idle_quit and (not playerManager.is_active()) and (self.idleTimer.elapsed() > settings.mpv_idle_quit_secs):
                    playerManager.idle_quit()
            except Exception:
                log.exception('Error in timeline thread.')
            if self.trigger.wait(5):
                self.trigger.clear()

    def delay_idle(self):
        self.idleTimer.restart()
        self.is_idle = False

    @staticmethod
    def send_timeline():
        try:
            playerManager.send_timeline()
        except jellyfin_apiclient_python.exceptions.HTTPException:
            pass
        except _mpv_errors:
            pass
timelineManager = TimelineManager()

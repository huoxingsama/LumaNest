"""Open this instance's unified settings without exposing its launcher secret."""
import json
import os
from pathlib import Path
import subprocess
import sys
import webbrowser

import psutil

from .runtime_paths import application_home


def _settings_running(root):
    try:
        record = json.loads((root / 'settings-process.json').read_text('utf-8'))
        process = psutil.Process(record['pid'])
        return (abs(process.create_time() - record['created']) < .01
                and process.cmdline() == record['command'])
    except (OSError, ValueError, TypeError, KeyError, psutil.Error):
        return False


def open_settings(root=None):
    """Return a public result; the authenticated URL stays on the local desktop."""
    root = Path(root if root is not None else application_home()).resolve()
    if _settings_running(root):
        from .server import reopen_existing
        url = reopen_existing(root)
        if not webbrowser.open(url):
            raise ValueError('无法打开系统浏览器，请通过桌面入口重新打开设置')
        return {'state': 'opened', 'message': '已在影院电脑的浏览器打开系统设置'}

    executable = Path(sys.executable)
    if os.name == 'nt':
        windowless = executable.with_name('pythonw.exe')
        if windowless.is_file():
            executable = windowless
    environment = os.environ.copy()
    environment['PERSONAL_CINEMA_HOME'] = str(root)
    # The server owns an instance lease, so concurrent launch clicks cannot
    # create duplicate supervisors. Bootstrap credentials never enter argv.
    subprocess.Popen([str(executable), '-m', 'personal_cinema.server', '--data-dir', str(root)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, env=environment,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return {'state': 'starting', 'message': '正在启动系统设置，将在影院电脑的浏览器打开'}

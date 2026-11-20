"""Keep original playback online while cloud organization temporarily owns metadata."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import threading
import time
from personal_cinema.runtime_paths import RuntimePath as Path
from http.server import ThreadingHTTPServer
from cache_worker import Worker, handler_for
from library_routing import restore_progress
ROOT = Path(_cinema_path(''))

def main():
    w = Worker(ROOT)
    w.config['downloads_paused'] = True

    def refresh():
        while True:
            with w.lock:
                w.source_bindings.apply(w.entries)
                w.save()
            try:
                restore_progress(w)
                w.sync_playback()
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(5)
    threading.Thread(target=refresh, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8766), handler_for(w)).serve_forever()
if __name__ == '__main__':
    main()

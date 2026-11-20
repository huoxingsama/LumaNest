"""Offline download / sync support.

The catalog (:class:`.db.SyncDB`) and download manager (:mod:`.manager`) run in
the main process; the library browser opens the same SQLite file read-only for
offline browsing and download indicators.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python

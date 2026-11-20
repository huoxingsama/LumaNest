"""Shaping the sync catalog into the downloads manager's display tree.

This is view logic over plain rows, and it used to live in ``ui.py`` — the
player bridge — where it was ~110 lines of grouping wedged between thin
controller methods, and untestable without a live ``syncManager``. Here it
is pure: rows in, display tree out.

``ui.py`` keeps the seam (reaching the sync db, catching its failures) and
calls these; ``SettingsMixin`` renders what comes back.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import json
from typing import Any
from ..books import AUDIOBOOK_TYPE, BOOK_TYPE
from ..i18n import _
from ..sync.db import ORIGIN_AUTO_NEXT_UP, ORIGIN_AUTO_LOOKAHEAD, is_auto
AUTO_GROUPS = ((ORIGIN_AUTO_NEXT_UP, _('Automatic: Next Up')), (ORIGIN_AUTO_LOOKAHEAD, _('Automatic: Actively Watched')))
AUTO_OTHER_TITLE = _('Automatic')
VIDEO_TYPES = ('Movie', 'Episode', 'Video')
AUDIOBOOK_TITLE = _('Audiobooks')
BOOK_TITLE = _('Books')
UNGROUPED_BOOK_TITLE = _('Ungrouped')

def season_title(row):
    """Display name for a downloaded episode's season."""
    try:
        name = (json.loads(row.get('item_json') or '{}').get('SeasonName') or '').strip()
    except (ValueError, TypeError):
        name = ''
    if name:
        return name
    idx = row.get('parent_index')
    if idx is None:
        return _('Episodes')
    return _('Specials') if idx == 0 else _('Season %s') % idx

def row_size(row):
    """Bytes to show for a row.

    ``size_bytes`` is the *expected* size and is only known once the source
    has been probed; ``downloaded_bytes`` is what is actually on disk.
    Reading a non-existent "size" key is why this used to show 0 B.
    """
    return (row.get('downloaded_bytes') or 0) or (row.get('size_bytes') or 0)

def row_watched(row):
    """Whether a downloaded item has been played by anyone on this machine.

    Reads a key the gateway stamps on, not the row's own columns: watched
    state lives in the per-actor table now, and answering it here would be
    a query per row and per group total. **Any** actor, deliberately, so
    this agrees with what Remove Watched will delete -- one file on disk,
    one decision (docs/offline-sync.md section 1).

    False for a row nobody stamped, which is the honest answer for a caller
    that did not go through the gateway.
    """
    return bool(row.get('played'))

def audiobook_group(row):
    """Which book an audiobook chapter belongs to.

    ``Album`` first: it is what actually joined a rip when this was measured
    against a real server, and it is what the metadata is *for*. Then the
    item's own name, which for a single-file audiobook is the book. Nothing
    else is available -- ``SeriesName`` is null on audiobooks (only
    `BookResolver` populates it, and that runs for `Book`) and there is no
    album entity to point at.
    """
    try:
        item = json.loads(row.get('item_json') or '{}')
    except (ValueError, TypeError):
        item = {}
    album = (item.get('Album') or '').strip()
    if album:
        return album
    return (row.get('name') or '').strip() or UNGROUPED_BOOK_TITLE

def qualified_title(row):
    """Episode title with enough context to identify it on its own.

    Under a series/season heading the bare name is fine — the tree supplies
    the rest. The automatic groups are flat and mix shows together, so a row
    reading "Chapter Four" says nothing about what it belongs to. Formats as
    "Show - S01E04 - Chapter Four", dropping whatever is missing rather than
    rendering "S01ENone" for an item with no numbering.
    """
    name = row.get('name') or row.get('item_id') or ''
    if (row.get('type') or '') != 'Episode':
        return name
    parts = []
    series = (row.get('series_name') or '').strip()
    if series:
        parts.append(series)
    season, index = (row.get('parent_index'), row.get('index_number'))
    if season is not None and index is not None:
        parts.append('S%02dE%02d' % (season, index))
    elif index is not None:
        parts.append('E%02d' % index)
    if name:
        parts.append(name)
    return ' - '.join(parts) or name

def _entry(row, qualified=False):
    return {'kind': 'item', 'id': row.get('item_id'), 'title': qualified_title(row) if qualified else row.get('name') or row.get('item_id'), 'qualified': qualified, 'status': row.get('status') or '', 'size': row_size(row), 'index': row.get('index_number'), 'done': row.get('downloaded_bytes') or 0, 'total': row.get('size_bytes') or 0, 'watched': row_watched(row), 'waiting_for': row.get('waiting_for')}

def status_text(entry):
    """What to show next to a downloading item.

    The raw catalog values ("pending", "downloading") were rendered verbatim
    and untranslated. Tk turned them into "Queued" / "Downloading 42%", which
    is the difference between a status column and a debug dump.
    """
    from ..sync.db import STATUS_COMPLETE, STATUS_DOWNLOADING, STATUS_ERROR, STATUS_PENDING
    status = entry.get('status') or ''
    if status == STATUS_COMPLETE:
        return ''
    if status == STATUS_DOWNLOADING:
        total = entry.get('total') or 0
        done = entry.get('done') or 0
        if total:
            return _('Downloading %d%%') % int(done * 100 / total)
        return _('Downloading')
    if status == STATUS_PENDING:
        waiting = entry.get('waiting_for')
        if waiting is not None:
            return _('Waiting for %s to sign in…') % waiting if waiting else _('Waiting for its profile to sign in…')
        return _('Queued')
    if status == STATUS_ERROR:
        return _('Failed')
    return status

def group_downloads(rows, playlists, playlist_items, owned):
    """Downloads grouped for display, mirroring the Tk DownloadsPanel::

        [{"kind": "playlist"|"series"|"movies", "title", "id",
          "size", "count", "children": [...]}]

    ``rows`` is the flat catalog, ``playlists`` the playlist records,
    ``playlist_items(playlist_id, server_id)`` yields one playlist's rows, and
    ``owned`` maps item_id -> (playlist_id, server_id).

    **A playlist is identified by (id, server), not by id.** Jellyfin hashes a
    playlist id from its *name*, so two servers hand out the same one for
    "Example Playlist" -- measured, not hypothetical. The entry carries
    ``server_id`` so the delete gesture can name which of them the user is
    looking at.

    Playlists come first. A *music* playlist is listed collapsed — hundreds
    of tracks nobody wants enumerated — but a video playlist is a handful of
    films or episodes, so it expands like a series does. Either way its items
    are owned by the playlist and must not also appear below. Series nest
    their seasons; everything left over lands in one flat group.
    """
    live = {(pl['playlist_id'], pl.get('server_id')) for pl in playlists}
    out = []
    for pl in playlists:
        items = playlist_items(pl['playlist_id'], pl.get('server_id'))
        video = bool(items) and all(((r.get('type') or '') in VIDEO_TYPES for r in items))
        out.append({'kind': 'playlist', 'id': pl['playlist_id'], 'server_id': pl.get('server_id'), 'title': pl.get('name') or _('Playlist'), 'size': sum((row_size(r) for r in items)), 'count': len(items), 'watched_count': sum((1 for r in items if row_watched(r))), 'children': [_entry(r) for r in items] if video else []})
    auto: dict[Any, list[Any]] = {}
    series: dict[Any, dict[str, Any]] = {}
    audiobooks: dict[Any, list[Any]] = {}
    books: list[Any] = []
    loose: list[dict[str, Any]] = []
    for r in rows:
        if owned.get(r.get('item_id')) in live:
            continue
        if is_auto(r.get('origin')):
            auto.setdefault(r.get('origin'), []).append(r)
            continue
        rtype = r.get('type') or ''
        if rtype == AUDIOBOOK_TYPE:
            audiobooks.setdefault(audiobook_group(r), []).append(r)
            continue
        if rtype == BOOK_TYPE:
            books.append(r)
            continue
        sid = r.get('series_id')
        if not sid:
            loose.append(_entry(r))
            continue
        show = series.setdefault(sid, {'kind': 'series', 'id': sid, 'title': r.get('series_name') or _('Unknown Series'), 'size': 0, 'count': 0, 'watched_count': 0, 'children': {}})
        show['size'] += row_size(r)
        show['count'] += 1
        show['watched_count'] = show.get('watched_count', 0) + (1 if row_watched(r) else 0)
        season_id = r.get('season_id') or sid
        season = show['children'].setdefault(season_id, {'kind': 'season', 'id': season_id, 'series_id': sid, 'title': season_title(r), 'size': 0, 'count': 0, 'watched_count': 0, 'children': []})
        season['size'] += row_size(r)
        season['count'] += 1
        season['watched_count'] = season.get('watched_count', 0) + (1 if row_watched(r) else 0)
        season['children'].append(_entry(r))
    known = {origin for origin, _t in AUTO_GROUPS}
    ordered = list(AUTO_GROUPS) + [(o, AUTO_OTHER_TITLE) for o in sorted(auto) if o not in known]
    for origin, title in ordered:
        items = auto.get(origin)
        if not items:
            continue
        items = sorted(items, key=lambda r: (str(r.get('series_name') or ''), r.get('parent_index') or 0, r.get('index_number') or 0, str(r.get('name') or '')))
        out.append({'kind': 'auto', 'id': None, 'origin': origin, 'title': title, 'size': sum((row_size(r) for r in items)), 'count': len(items), 'watched_count': sum((1 for r in items if row_watched(r))), 'children': [_entry(r, qualified=True) for r in items]})
    out += _book_sections(audiobooks, books)
    shows = []
    for show in series.values():
        seasons = sorted(show['children'].values(), key=lambda x: str(x['title']))
        for s2 in seasons:
            s2['children'].sort(key=lambda e: (e['index'] is None, e['index'], e['title']))
        show['children'] = seasons
        shows.append(show)
    shows.sort(key=lambda g: str(g['title']))
    out += shows
    if loose:
        loose.sort(key=lambda e: str(e['title']))
        out.append({'kind': 'movies', 'id': None, 'title': _('Movies & Videos'), 'size': sum((e['size'] for e in loose)), 'count': len(loose), 'watched_count': sum((1 for e in loose if e['watched'])), 'children': loose})
    return out

def _book_sections(audiobooks, books):
    """The Audiobooks and Books groups, in that order.

    Audiobooks lead because they are the larger of the two by orders of
    magnitude -- an audiobook is hours of audio and a book is a few hundred
    kilobytes -- so it is the section anyone opening this screen to reclaim
    space came for.
    """
    out = []
    if audiobooks:
        children = []
        size = count = watched = 0
        for title in sorted(audiobooks, key=str):
            entries = sorted(audiobooks[title], key=lambda r: (r.get('parent_index') or 0, r.get('index_number') or 0, str(r.get('name') or '')))
            children.append({'kind': 'audiobook', 'id': None, 'title': title, 'size': sum((row_size(r) for r in entries)), 'count': len(entries), 'watched_count': sum((1 for r in entries if row_watched(r))), 'children': [_entry(r) for r in entries]})
            size += children[-1]['size']
            count += children[-1]['count']
            watched += children[-1]['watched_count']
        out.append({'kind': 'audiobooks', 'id': None, 'title': AUDIOBOOK_TITLE, 'size': size, 'count': count, 'watched_count': watched, 'children': children})
    if books:
        entries = sorted(books, key=lambda r: str(r.get('name') or ''))
        out.append({'kind': 'books', 'id': None, 'title': BOOK_TITLE, 'size': sum((row_size(r) for r in entries)), 'count': len(entries), 'watched_count': sum((1 for r in entries if row_watched(r))), 'children': [_entry(r) for r in entries]})
    return out

def progress_summary(pending_rows):
    """Status-bar line for downloads still in flight:
    ``{"pending": n, "name": str, "percent": int|None}``, or None when
    ``pending_rows`` is empty."""
    if not pending_rows:
        return None
    active = next((r for r in pending_rows if (r.get('downloaded_bytes') or 0) > 0), pending_rows[0])
    total = active.get('size_bytes') or 0
    done = active.get('downloaded_bytes') or 0
    return {'pending': len(pending_rows), 'name': active.get('name') or '', 'percent': int(done * 100 / total) if total else None}

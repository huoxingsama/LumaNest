"""SQLite catalog of offline downloads.

Single writer (the main process via :class:`SyncDB`), many readers (the browser
opens the same file read-only). WAL mode lets a reader and the writer coexist
across processes. Read-only handles tolerate a missing file (empty catalog).
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
import logging
import os
import pathlib
import sqlite3
import threading
import time
log = logging.getLogger('sync.db')
COLUMNS = ['item_id', 'server_uuid', 'type', 'name', 'series_id', 'series_name', 'season_id', 'parent_index', 'index_number', 'media_source_id', 'file_path', 'ext', 'size_bytes', 'downloaded_bytes', 'status', 'runtime_ticks', 'item_json', 'source_json', 'added_at', 'origin', 'completed_at', 'library_id', 'content_server_id', 'requested_server_id', 'requested_user_id']
NOT_UPSERTED = ['watched_at']

class _AnyServer:
    """The type of :data:`ANY_SERVER`; never instantiated elsewhere."""
    __slots__ = ()

    def __repr__(self):
        return 'ANY_SERVER'

    def __bool__(self):
        return True
ANY_SERVER = _AnyServer()
ORIGIN_USER = 'user'
ORIGIN_AUTO_NEXT_UP = 'auto:nextup'
ORIGIN_AUTO_LOOKAHEAD = 'auto:lookahead'
AUTO_PREFIX = 'auto'

def is_auto(origin):
    """Was this row queued by the scheduler rather than asked for?"""
    return bool(origin) and str(origin).startswith(AUTO_PREFIX)
_DOWNLOADS_TABLE = '\nCREATE TABLE IF NOT EXISTS downloads (\n    item_id TEXT PRIMARY KEY,\n    -- There was a `server_id` here and it is GONE as of 3.0.0 (CX8). It was\n    -- the on-disk path key, NULL on every row any build ever wrote, and a\n    -- value in it moved a download\'s directory out from under the row naming\n    -- it -- so the store is one folder called `server`, spelled as a literal\n    -- now rather than as `row["server_id"] or "server"`. Scoping is\n    -- `content_server_id` and always was. docs/do-not-fix.md 1.\n    -- The LOGIN that enqueued this row. It is a delivery address, not an\n    -- identity: one account reachable at two addresses is two uuids, and a\n    -- deleted-and-recreated server connection mints a third. Kept because it\n    -- is what the migrations resolve from; scope on `content_server_id` and\n    -- attribute with `requested_*` below.\n    server_uuid TEXT,\n    -- **Who asked, as an account** -- the `(ServerId, UserId)` pair, the same\n    -- shape `pending_playstate` carries. Two uses of one record and the\n    -- distinction is load-bearing: this is a record of *who asked*, which the\n    -- sweep scoping relies on, while resolving a live *client* by it is the\n    -- defect F48 names. State both or the next round deletes one.\n    --\n    -- NOT NULL is safe against a writer that omits them only because `upsert`\n    -- is INSERT OR **REPLACE**, which SQLite resolves by substituting the\n    -- column default instead of raising. A plain INSERT would break every\n    -- writer that does not know the account -- the orphan adopter, whose item\n    -- comes off the disk with no login at all.\n    --\n    -- `\'\'` means "not migrated yet" and is what the backfill selects on;\n    -- NO_ACTOR means "asked, and we cannot say by whom". A download is a file\n    -- on disk, so it is never dropped for being unattributable the way a\n    -- queued playstate entry is. R19, docs/rulings-log.md.\n    requested_server_id TEXT NOT NULL DEFAULT \'\',\n    requested_user_id TEXT NOT NULL DEFAULT \'\',\n    type TEXT,\n    name TEXT,\n    series_id TEXT,\n    series_name TEXT,\n    season_id TEXT,\n    parent_index INTEGER,\n    index_number INTEGER,\n    media_source_id TEXT,\n    file_path TEXT,\n    ext TEXT,\n    size_bytes INTEGER DEFAULT 0,\n    downloaded_bytes INTEGER DEFAULT 0,\n    status TEXT,\n    runtime_ticks INTEGER,\n    item_json TEXT,\n    source_json TEXT,\n    -- There was a `userdata_json` here and it is GONE as of 3.0.0 (CX8). It\n    -- held the download-time watched snapshot, which `item_userdata` has held\n    -- per actor since 2026-09-12; nothing read the blob, two writers still\n    -- filled it, and what it was being kept for was a downgrade.\n    added_at INTEGER,\n    -- See ORIGIN_*. NULL on rows written before auto-download existed.\n    -- What actually keeps those rows safe is three-valued logic, not the\n    -- backfill: NULL GLOB \'auto*\' is NULL, so auto_size/list_auto exclude\n    -- them, and is_auto(None) is False. _migrate backfills them to \'user\'\n    -- anyway so the column reads honestly, but do NOT rewrite those queries\n    -- into a form that matches NULL (e.g. `origin IS NOT \'user\'`) -- that\n    -- would make every un-backfilled legacy row reapable.\n    origin TEXT,\n    completed_at INTEGER,\n    -- When this app first observed the item as played, for the watched\n    -- grace period (auto_download_keep_watched_hours). NULL means "not\n    -- watched as far as we have seen", which is also what it is reset to\n    -- when an item is un-watched -- so the window starts again rather than\n    -- counting from a viewing that was taken back.\n    --\n    -- Stamped by the reaper rather than by the playback path, because the\n    -- question it answers is "how long have we known", and the reaper is\n    -- the only thing that asks. Deliberately NOT the server\'s\n    -- LastPlayedDate: that is absent with the server away and on some\n    -- Mark Played paths, and a clock that stops existing is worse here\n    -- than one that starts late.\n    watched_at INTEGER,\n    -- The Jellyfin `ServerId` this row\'s media belongs to: the CONTENT key.\n    -- Not `server_uuid`, which names a saved *login* -- one server can carry\n    -- several of those (two accounts, or two addresses for one box) and they\n    -- must all get one answer to "do we hold this". Not `server_id` either;\n    -- see that column. Backfilled from `item_json` by _migrate, so a row\n    -- written before this column existed still answers.\n    content_server_id TEXT,\n    -- The CollectionFolder this item lives in. Captured at download time,\n    -- where a request is already normal, so the shader-profile library\n    -- scope can be resolved for downloaded media with the server away --\n    -- and without the play path making a call at all. NULL on rows written\n    -- before this, and on anything the lookup could not answer.\n    library_id TEXT\n)\n'
_DOWNLOADS_INDEXES = ('CREATE INDEX IF NOT EXISTS idx_downloads_series ON downloads(series_id)', 'CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)')
_SCHEMA = _DOWNLOADS_TABLE + ';\n' + ';\n'.join(_DOWNLOADS_INDEXES) + ';\n' + '\n-- What a server has not been told yet, and on whose behalf.\n--\n-- Keyed on the ACTOR (server_id, user_id), not on the saved login that\n-- happened to be current: a login is a delivery handle, and one person can\n-- have several for several addresses of one server, so an entry queued\n-- under one used to be undrainable through another.\n--\n-- `server_uuid` is the login that queued it, and **it is not dead**: it is the\n-- only input `_migrate_playstate_actors` has for an entry written before the\n-- actor columns existed. CX8 listed it with the two columns 3.0.0 drops and\n-- that classification is wrong -- nothing writes it (`upsert_playstate` names\n-- six columns and this is not one), which is what made it look dead.\n--\n-- It is not dropped once the backfill has run, either, and the reason is worth\n-- the lines: an open with no resolver skips that pass entirely, so a later open\n-- cannot tell "already migrated" from "not yet" -- `user_id = \'\'` is both the\n-- unmigrated state and R18\'s kept-local one. Dropping it would rebuild the one\n-- table here whose contents are **not a clone**, to lose the person half of a\n-- queued viewing on exactly the catalog that still needed it. G2, which\n-- `_migrate`\'s docstring says no schema judgement may touch.\n--\n-- Both actor columns are NOT NULL. They cannot use NULL for "unknown", and\n-- not only for tidiness: this table\'s lookup is `WHERE ... AND item_id=?`,\n-- `NULL = NULL` is false in SQL, and a NULL key therefore inserted a fresh\n-- duplicate on every write instead of advancing the row. Nothing is queued\n-- unattributed at all now -- see `upsert_playstate` -- so the sentinel does\n-- not appear here either.\nCREATE TABLE IF NOT EXISTS pending_playstate (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    server_uuid TEXT,\n    server_id TEXT NOT NULL DEFAULT \'\',\n    user_id TEXT NOT NULL DEFAULT \'\',\n    item_id TEXT,\n    position_ticks INTEGER,\n    played INTEGER,\n    created_at INTEGER,\n    marked_at INTEGER\n);\n-- The unique index on (item_id, server_id, user_id) is created by\n-- `_migrate_playstate_actors`, NOT here. `CREATE TABLE IF NOT EXISTS` is a\n-- no-op on an existing catalog, so on one of those the columns do not exist\n-- yet when this script runs and the index fails -- taking the whole open\n-- with it. Measured on a real catalog; a fresh one cannot show it.\n-- Watched state and resume position, per PERSON rather than per machine.\n--\n-- Jellyfin\'s userdata is per user; this store is one per machine. Keeping\n-- one blob on the downloads row conflated them, so two accounts shared a\n-- resume position and pressing Resume on the second told that account\'s\n-- server it had watched what the first watched.\n--\n-- The actor is (server_id, user_id) -- a Jellyfin ServerId and a Jellyfin\n-- UserId. Deliberately NOT our saved-login uuid: one server can carry\n-- several of those for several addresses, so two uuids can be one person,\n-- and the queue would then be undrainable through the other route. Both key\n-- columns are NOT NULL and use NO_ACTOR rather than NULL for "unknown".\n--\n-- This replaced a `userdata_json` blob on the downloads row. That column\n-- was kept, unread, so a catalog this build touched still opened in an older\n-- one -- and it went in 3.0.0 with the downgrade guarantee it served (CX8).\n-- Nothing holds watched state but this table.\nCREATE TABLE IF NOT EXISTS item_userdata (\n    item_id TEXT NOT NULL,\n    server_id TEXT NOT NULL,\n    user_id TEXT NOT NULL,\n    played INTEGER,\n    position_ticks INTEGER,\n    play_count INTEGER,\n    is_favorite INTEGER,\n    last_played_date TEXT,\n    updated_at INTEGER,\n    PRIMARY KEY (item_id, server_id, user_id)\n);\n-- A playlist id is a hash of its NAME, so two unrelated servers hand out the\n-- same one: `Example Playlist` came back cf0ce7fc72247deaa755cc40b9219e0d from\n-- both the QA 10.11 container and a real personal server (measured;\n-- docs/offline-sync.md section 4). So the identity is\n-- (playlist_id, server_id) and **there is no PRIMARY KEY here on purpose**:\n-- with one, the second server\'s download overwrote the first\'s row.\nCREATE TABLE IF NOT EXISTS playlists (\n    playlist_id TEXT,\n    server_id TEXT,\n    server_uuid TEXT,\n    name TEXT,\n    added_at INTEGER\n);\n-- Its unique index is **not** here: it is created by\n-- `_migrate_playlist_scopes`, because this script runs before the migration\n-- and an existing catalog\'s `playlist_items` has no `server_id` yet -- naming\n-- it in an index here failed the whole schema run, on every catalog that\n-- already exists. (Caught by tests/test_playlist_collision.py\'s migration\n-- class, which is what that class is for.)\n-- Membership of a downloaded playlist. `owned` marks the items this playlist\n-- download is responsible for pulling down: deleting the playlist removes only\n-- those, so an item that was already downloaded another way (owned=0) keeps its\n-- original grouping and survives.\n-- Scoped for the same reason as the table above, and **stored rather than\n-- joined through `downloads`**: `delete_playlist` deletes membership, and a\n-- join cannot see a membership row whose item row is already gone (see\n-- `playlist_owned_ids` on dangling claims), so deleting one server\'s playlist\n-- would strip the other\'s members.\nCREATE TABLE IF NOT EXISTS playlist_items (\n    playlist_id TEXT,\n    server_id TEXT,\n    item_id TEXT,\n    sort_index INTEGER,\n    owned INTEGER DEFAULT 0\n);\n-- Same: the scoped index is the migration\'s, not this script\'s.\nCREATE INDEX IF NOT EXISTS idx_playlist_items_item ON playlist_items(item_id);\n-- Auto-downloads the scheduler is done with. Without this, dropping an\n-- unwatched episode after `keep_days` accomplishes nothing: it is still the\n-- server\'s Next Up (unwatched is exactly why it is there), so the very next\n-- pass re-downloads it, and the cycle repeats forever. A tombstone is the\n-- memory of "we fetched this and decided against it". Cleared when the user\n-- asks for the item explicitly, which is the one signal that overrides the\n-- decision.\n--\n-- Two writers, for the same reason: the reaper\'s age rule, and a download\n-- that failed permanently (SyncManager._record_permanent_failure). Both are\n-- decisions about an item whose *row* is about to be deleted, so the row\n-- cannot be what remembers them.\n-- **One table, saying which server each tombstone is about.** There was a\n-- second one, `auto_discarded`, keyed on the item id alone -- and a tombstone\n-- deliberately outlives the row it describes, so once server A\'s copy of an id\n-- was discarded AND deleted, server B\'s *different* film with the same id was\n-- invisible to the scheduler for good, with nothing left in the catalog to\n-- explain why. Item ids are not unique across servers\n-- (docs/jellyfin-api-notes.md 13b).\n--\n-- A column on that table could not fix it: `item_id` was its PRIMARY KEY, so\n-- there was room for exactly one tombstone per id whatever columns it carried.\n-- This table was added beside it, and for a while the old one was still read as\n-- "any server" -- which is what a row written before it means, and was also\n-- where a discard went when the row\'s own content server was unknown.\n--\n-- `_migrate_discard_scopes` folds those rows in where a held download names the\n-- server and **drops the rest**, because that is the bug this table was added\n-- for: an unattributable tombstone suppresses an id on every server, forever.\n-- A discard that cannot name a server now records nothing at all -- see\n-- `mark_discarded` for what that costs, which is one redundant fetch.\nCREATE TABLE IF NOT EXISTS auto_discarded_scoped (\n    item_id TEXT NOT NULL,\n    server_id TEXT NOT NULL,\n    discarded_at INTEGER,\n    PRIMARY KEY (item_id, server_id)\n);\n'

def _advance(a, b):
    """Merge two nullable flags the way this store merges one actor's own
    state: set wins over unset, and unknown stays unknown rather than
    becoming a decision. `None` and `0` are different answers here --
    "nobody has said" versus "said no".
    """
    if a or b:
        return 1
    return 0 if a is not None or b is not None else None
_ID_CHARS = frozenset('0123456789abcdefABCDEF-')
UNSCOPED_ART_DIR = 'unscoped'
STORE_DIR = 'server'

def playlist_art_dir(root, content_server_id, playlist_id):
    """Where a playlist's cached poster lives:
    ``<root>/server/playlist/<content_server_id>/<playlist_id>/``.

    **Both the writer (`SyncManager._download_playlist_art`) and the offline
    reader (`repository._art_path`) call this.** They carried separate copies
    of this layout, and the copies each held a comment explaining why the
    server was *not* in the path -- which is how a layout change becomes a
    silently blank tile. One function, in the one module both of them already
    import.

    ``server`` is a literal, not a server id: the whole store sits under one
    directory, which is `STORE_DIR` above and was never anything else
    (CX8 dropped the column that spelled it). The **content** server goes below
    ``playlist/``, where it scopes the poster the way the catalog scopes the row
    -- two servers can hold a playlist with the same id, and one poster was
    overwriting the other.

    A scope that is not a plain id becomes `UNSCOPED_ART_DIR` rather than being
    joined: this builds a filesystem path out of a value a server supplies.
    """
    scope = content_server_id or ''
    if not scope or not set(scope) <= _ID_CHARS:
        scope = UNSCOPED_ART_DIR
    return os.path.join(root, STORE_DIR, 'playlist', scope, _store_component(playlist_id))
_UNSAFE_PATH_CHARS = frozenset('/\\:\x00')

def _store_component(value):
    """One server-supplied id, safe to join into a store path.

    The docstring above says why: *this builds a filesystem path out of a
    value a server supplies*. That was applied to the scope and not to the id
    beside it, nor at any of the sibling caches -- so a DTO whose `Id` held
    path separators or `..` put `os.makedirs`, `os.replace` and `rmtree`
    outside the store.

    **A containment test, not the shape test the scope gets**, and the two
    differ on purpose. A scope has to *look like a ServerId* to mean anything
    as a bucket, so `_ID_CHARS` is right there. An id only has to stay inside
    the directory it is joined to -- so this refuses what can leave (a
    separator, a drive letter, `.` and `..`, a name Windows strips a trailing
    dot or space from) and keeps everything else, including ids no Jellyfin
    server would issue. Narrower than `_ID_CHARS` deliberately: the stricter
    test rewrites the path of an id that was never dangerous, and this
    function stands between a server's answer and `rmtree`.

    **Replaced by a digest of the value, not by a constant.** A constant is
    right for the scope, where many playlists share one bucket by design, and
    wrong for an id, where it would collapse two items into one directory and
    make deleting either take the other's files.

    The digest is 41 characters, so `manager._looks_like_item_id` answers
    False for it and the orphan sweep leaves the directory standing rather
    than deleting it -- the direction that helper's own docstring chooses,
    where a false negative costs a stale directory and a false positive costs
    somebody's files.
    """
    value = value or ''
    if value and (not _UNSAFE_PATH_CHARS & set(value)) and value.strip('.') and (not value.endswith(('.', ' '))):
        return value
    return 'x' + hashlib.sha1(value.encode('utf-8', 'replace')).hexdigest()

def series_art_dir(root, series_id):
    """Where a series' cached artwork lives: ``<root>/server/series/<id>/``.

    **Both the writer (`SyncManager._download_series_art`) and the offline
    reader (`repository._art_path`) call this**, for the reason
    `playlist_art_dir` gives: they carried separate copies of the layout once
    and that is how a layout change becomes a silently blank tile. It is also
    what makes the id sanitising above safe to add -- applied at the writer
    alone it would blank the tile for every id it rewrote.

    **Not scoped by content server, and that is deliberate** -- R26. A series
    id is derived from the media path, so two servers handing out the same one
    are describing the same folder and one poster for it is correct. Do not
    "fix" this to match `playlist_art_dir`, whose ids hash a *name* and really
    do collide.
    """
    return os.path.join(root, STORE_DIR, 'series', _store_component(series_id))

def season_art_dir(root, season_id):
    """Where a season's cached artwork lives: ``<root>/server/season/<id>/``.

    Same pair of callers and the same R26 note as `series_art_dir`.
    """
    return os.path.join(root, STORE_DIR, 'season', _store_component(season_id))

def legacy_playlist_art_dir(root, playlist_id):
    """Where a playlist's poster lived **before** R23 scoped it by server:
    ``<root>/server/playlist/<playlist_id>/``.

    Here rather than at its one caller (`SyncManager._rehome_playlist_art`)
    because every store path is spelled in this module, which is what keeps
    the two ends of each of them from drifting -- and because it is joining a
    server-supplied id like all the others. It escapes in the read direction:
    `os.replace` would move files *out of* whatever it named and into the
    store. An ordinary id is unchanged, so a real legacy directory is still
    found.
    """
    return os.path.join(root, STORE_DIR, 'playlist', _store_component(playlist_id))

def item_dir(root, item_id):
    """Where one download's files live: ``<root>/server/<item_id>/``.

    The one that is not a cache: `_remove_files` runs `rmtree` over it, so an
    id that escaped the store escaped it with a recursive delete.
    """
    return os.path.join(root, STORE_DIR, _store_component(item_id))

def filing_state(row_server, actor):
    """C1 for a row we already hold: ``(verdict, key)``.

    Split out of `SyncDB._row_sync_state` so the offline browser can apply
    the identical rule to a **bulk** snapshot -- it holds every row's
    `content_server_id` already, and one query per row to re-ask would undo
    the point of reading them in one go. A second copy of this arithmetic in
    the browser is exactly what C2 forbids, and it agreed with this one only
    by construction.

    Takes the row's content server rather than an item id, so it needs no
    database and no lock. `_row_sync_state` adds the one thing it cannot
    know: whether the row exists at all.
    """
    actor_server, user_id = actor or (None, None)
    if not row_server:
        return ('local', (NO_ACTOR, NO_ACTOR))
    if actor_server and actor_server != NO_ACTOR and (actor_server != row_server):
        return ('refuse', None)
    if not user_id or user_id == NO_ACTOR:
        return ('local', (row_server, NO_ACTOR))
    return ('sync', (row_server, user_id))
NO_ACTOR = '@none'
STATUS_PENDING = 'pending'
STATUS_DOWNLOADING = 'downloading'
STATUS_COMPLETE = 'complete'
STATUS_ERROR = 'error'

class SyncDB:

    def __init__(self, db_path, read_only=False, actor_for=None):
        """``actor_for`` maps a saved-login uuid to ``(ServerId, UserId)``,
        or None when it cannot say -- ``UserManager.actor_for``.

        Injected rather than imported because resolving a person is the
        credential layer's job and this is storage. **Omitting it does not
        mean "drop everything":** without a resolver the userdata migration
        is skipped entirely, because a handle that cannot ask the question
        has not had it answered. Read-only handles never migrate anyway.
        """
        self.path = db_path
        self.read_only = read_only
        self._actor_for = actor_for
        self._lock = threading.Lock()
        self._conn = None
        self._refusals = {}
        if read_only:
            if os.path.exists(db_path):
                uri = pathlib.Path(db_path).as_uri() + '?mode=ro'
                self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                log.info('No download catalog at %s; it reads as empty.', db_path)
        else:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.commit()
            self._migrate()
        self._playlist_items_scoped = self._has_scoped_playlist_items()
        if self._conn is not None:
            self._conn.row_factory = sqlite3.Row
    _ADDED_COLUMNS = (('origin', 'TEXT'), ('completed_at', 'INTEGER'), ('library_id', 'TEXT'), ('watched_at', 'INTEGER'), ('content_server_id', 'TEXT'), ('requested_server_id', "TEXT NOT NULL DEFAULT ''"), ('requested_user_id', "TEXT NOT NULL DEFAULT ''"))
    _ADDED_PLAYSTATE_COLUMNS = (('server_id', "TEXT NOT NULL DEFAULT ''"), ('user_id', "TEXT NOT NULL DEFAULT ''"), ('marked_at', 'INTEGER'))

    def _migrate(self):
        """Bring an existing catalog up to the current schema.

        Runs on every open; `PRAGMA table_info` is the check, so it is a no-op
        once the shape is current.

        **There is no downgrade guarantee, and that is a decision rather than
        an oversight** -- CX8, and `docs/do-not-fix.md` carries it. A
        catalog a 3.0.0
        build has migrated does not open in an older one: the playlist rebuild
        takes away a uniqueness those builds rely on, and the `downloads`
        rebuild takes away two columns their own `INSERT` still names, so their
        writes fail outright. What that costs is a **clone** -- everything in
        the catalog can be downloaded again, which is why the goals document
        files the guarantee as serving no goal. What it bought was the
        additive-only rule, a `user_version` refusal that never existed, and a
        large fraction of two audits.

        **The one place a downgrade still costs something real is G2**: queued
        watched state is the only thing here that is not a clone. Nothing in
        this file may delete an entry of that on a schema judgement -- R13 and
        R18 govern it, and the fold below moves what it cannot attribute to
        `item_userdata` rather than dropping it.

        Three properties that are NOT the downgrade guarantee and still hold,
        because each was written against a failure this has seen:

        * **Backfills are unconditional and WHERE-guarded**, never gated on
          "did this run add the column" -- DDL autocommits, so a crash between
          the ALTER and the UPDATE would otherwise leave a column wrong
          forever.
        * **One bad row must not cost the catalog an open.** A row whose
          manifest will not parse is skipped, not fatal.
        * **`catalog.db.bak` plus the restore path** (docs/offline-sync.md
          section 5) is what covers a rebuild that goes wrong, and it is
          written *after* a clean open rather than before.
        """
        try:
            have = {r[1] for r in self._conn.execute('PRAGMA table_info(downloads)')}
        except sqlite3.Error:
            log.warning('Could not inspect the catalog schema', exc_info=True)
            return
        added = [c for c, _t in self._ADDED_COLUMNS if c not in have]
        try:
            for col, decl in self._ADDED_COLUMNS:
                if col in have:
                    continue
                self._conn.execute('ALTER TABLE downloads ADD COLUMN %s %s' % (col, decl))
            self._conn.execute('UPDATE downloads SET origin = ? WHERE origin IS NULL', (ORIGIN_USER,))
            self._backfill_content_server_id()
            self._backfill_requested_by()
            self._migrate_playstate_actors()
            self._migrate_playlist_scopes()
            self._migrate_discard_scopes()
            self._migrate_drop_vestigial()
            self._conn.commit()
            if added:
                log.info('Catalog migrated: added %s', ', '.join(added))
        except sqlite3.Error:
            self._conn.rollback()
            log.error('Catalog migration failed', exc_info=True)
    _DROPPED_COLUMNS = ('server_id', 'userdata_json')

    def _migrate_drop_vestigial(self):
        """Take `server_id` and `userdata_json` off `downloads`. CX8.

        **This is the migration boundary** `_migrate`'s docstring states: an
        older build's `INSERT` names both columns, so once they are gone its
        writes fail rather than quietly disagree. Deliberate, and what it costs
        is a clone.

        Neither column is load-bearing. `server_id` was the on-disk path key
        and NULL on every row any build ever wrote -- a value in it moved a
        download's directory out from under its own row -- so `_item_dir` and
        the orphan sweep now spell the one directory as a literal.
        `userdata_json` held the download-time watched snapshot, which
        `item_userdata` has held per actor since 2026-09-12; nothing read the
        blob, and R24 already ruled that not carrying it forward is acceptable.

        **A rebuild rather than `ALTER TABLE ... DROP COLUMN`**, which needs
        SQLite 3.35. The Windows bundle and the Flatpak ship their own SQLite,
        and this file already declines `json_extract` for wanting 3.38 -- a
        migration that works on one platform and leaves the schema wrong on
        another is the failure every test on Linux would pass.

        The new table comes from `_DOWNLOADS_TABLE`, the same constant a fresh
        catalog is built from, so the two cannot drift. Columns are copied by
        name, intersected with what is actually there, because this may be
        rebuilding a catalog several versions old.

        Caller holds the write; this does not commit. A failure rolls the whole
        thing back -- unlike an `ALTER TABLE ADD COLUMN`, which autocommits --
        so this one is all-or-nothing rather than self-healing.
        """
        try:
            have = [r[1] for r in self._conn.execute('PRAGMA table_info(downloads)')]
        except sqlite3.Error:
            log.warning('Could not inspect the catalog schema', exc_info=True)
            return
        if not set(have) & set(self._DROPPED_COLUMNS):
            return
        keep = [c for c in have if c not in self._DROPPED_COLUMNS]
        names = ', '.join(keep)
        self._conn.execute('DROP TABLE IF EXISTS downloads_old')
        self._conn.execute('ALTER TABLE downloads RENAME TO downloads_old')
        self._conn.execute('DROP INDEX IF EXISTS idx_downloads_series')
        self._conn.execute('DROP INDEX IF EXISTS idx_downloads_status')
        self._conn.execute(_DOWNLOADS_TABLE)
        for statement in _DOWNLOADS_INDEXES:
            self._conn.execute(statement)
        self._conn.execute('INSERT INTO downloads (%s) SELECT %s FROM downloads_old' % (names, names))
        self._conn.execute('DROP TABLE downloads_old')
        log.info('Catalog migrated: dropped %s from downloads', ', '.join(sorted(set(have) & set(self._DROPPED_COLUMNS))))

    def _backfill_requested_by(self):
        """Give every pre-R19 row the account that asked for it.

        Resolved from `server_uuid`, the login that enqueued it. Measured
        before this was written: 14 of 14 rows in the live catalog still
        resolve to a saved login (2026-09-19), so this is recording a fact that
        is available today rather than rescuing one that is already gone --
        which is the whole argument for doing it now, because the derivation
        stops working the first time a server connection is deleted and added
        back.

        **A row is never dropped for being unattributable.** That is where this
        differs from `_migrate_playstate_actors`, which drops: a queued
        playstate entry nobody can be named for is worth nothing to anybody,
        while a download is a file on disk. An unresolvable login keeps the
        server half -- recovered from the row itself -- and takes `NO_ACTOR`
        for the person, which is R18's shape.

        Selects on `requested_user_id = ''`, the DDL default, so it is a no-op
        once clean and idempotent on a row it could not resolve: such a row is
        written `NO_ACTOR`, which this no longer selects.
        """
        try:
            rows = self._conn.execute("SELECT item_id, server_uuid, content_server_id FROM downloads WHERE requested_user_id = ''").fetchall()
        except sqlite3.Error:
            log.debug('could not read the catalog to record who asked', exc_info=True)
            return
        if not rows:
            return
        resolved = 0
        for item_id, server_uuid, content_server_id in rows:
            actor = None
            if self._actor_for is not None and server_uuid:
                try:
                    actor = self._actor_for(server_uuid)
                except Exception:
                    log.debug('could not resolve who asked for %s', item_id, exc_info=True)
            if actor and actor[1]:
                pair = (content_server_id or actor[0] or NO_ACTOR, actor[1])
                resolved += 1
            else:
                pair = (content_server_id or NO_ACTOR, NO_ACTOR)
            self._conn.execute('UPDATE downloads SET requested_server_id=?, requested_user_id=? WHERE item_id=?', (pair[0], pair[1], item_id))
        log.info('Recorded who asked for %d download(s); %d named a person.', len(rows), resolved)

    def _migrate_discard_scopes(self):
        """Fold the unscoped tombstones into the scoped table, and drop the rest.

        A tombstone keyed on the item id alone binds **every** server, and item
        ids are not unique across servers -- so once server A's copy was
        discarded and deleted, server B's different film of the same id was
        invisible to the scheduler forever, with nothing in the catalog left to
        explain why. That is what the scoped table was added for, and the old
        one has to go rather than be read alongside it.

        A row is folded in where a download still names the server it was about,
        and **dropped otherwise**: keeping it means keeping the bug. What the
        drop costs is one redundant fetch, for the reason `mark_discarded`
        spells out.

        Idempotent by the table's absence: once dropped there is nothing to do.
        Caller holds the write; this does not commit.
        """
        try:
            have = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auto_discarded'").fetchone()
        except sqlite3.Error:
            log.debug('could not inspect the tombstone tables', exc_info=True)
            return
        if not have:
            return
        folded = self._conn.execute("INSERT OR REPLACE INTO auto_discarded_scoped (item_id, server_id, discarded_at) SELECT a.item_id, d.content_server_id, a.discarded_at FROM auto_discarded a JOIN downloads d ON d.item_id = a.item_id WHERE d.content_server_id IS NOT NULL   AND d.content_server_id != ''").rowcount
        self._conn.execute('DROP TABLE auto_discarded')
        log.info('Tombstones: one table now; %d unscoped row(s) named a server and were kept.', folded)

    def _migrate_playlist_scopes(self):
        """Let two servers hold a playlist with the same id.

        A playlist id is a hash of its **name**, so two unrelated servers hand
        out the same one -- `Example Playlist` came back
        `cf0ce7fc72247deaa755cc40b9219e0d` from both the QA 10.11 container and
        a real personal server. With `playlist_id TEXT PRIMARY KEY` the second
        download silently overwrote the first's name, membership and art.

        **Rebuilds both tables**, because SQLite cannot drop a PRIMARY KEY.
        `_migrate` carries the cost.

        Each row's server is derived from its **members**: the one distinct
        `content_server_id` among the downloads it lists. None, or more than
        one, leaves it NULL -- which every content read already admits on
        every scope, so such a playlist stays visible exactly where it is
        visible today. Derived here and at `_record_playlist`, never at read
        time: reading it from members would make a playlist's visibility
        follow which members happen to be *complete*, and those come and go.

        Idempotent through the index: once `idx_playlists_scoped` exists the
        rebuild has happened, and a second open does nothing. Caller holds the
        write; this does not commit.
        """
        try:
            done = {r[1] for r in self._conn.execute('PRAGMA index_list(playlists)')}
        except sqlite3.Error:
            log.warning('Could not inspect the playlist tables', exc_info=True)
            return
        if 'idx_playlists_scoped' in done:
            self._repair_playlist_member_scopes()
            return
        try:
            have = {r[1] for r in self._conn.execute('PRAGMA table_info(playlist_items)')}
        except sqlite3.Error:
            log.warning('Could not inspect the playlist tables', exc_info=True)
            return
        if 'server_id' in have:
            self._repair_playlist_member_scopes()
            self._create_playlist_indexes()
            return
        derived = {}
        for playlist_id, server_id in self._conn.execute('SELECT DISTINCT pi.playlist_id, d.content_server_id FROM playlist_items pi JOIN downloads d ON d.item_id = pi.item_id'):
            derived.setdefault(playlist_id, set()).add(server_id)
        scoped = {pid: next(iter(servers)) for pid, servers in derived.items() if len(servers) == 1 and next(iter(servers))}
        self._conn.execute('DROP TABLE IF EXISTS playlists_scoped_new')
        self._conn.execute('CREATE TABLE playlists_scoped_new (playlist_id TEXT, server_id TEXT, server_uuid TEXT, name TEXT, added_at INTEGER)')
        self._conn.execute('INSERT INTO playlists_scoped_new (playlist_id, server_id, server_uuid, name, added_at) SELECT playlist_id, server_id, server_uuid, name, added_at FROM playlists')
        self._conn.execute('DROP TABLE playlists')
        self._conn.execute('ALTER TABLE playlists_scoped_new RENAME TO playlists')
        for playlist_id, server_id in scoped.items():
            self._conn.execute('UPDATE playlists SET server_id=? WHERE playlist_id=? AND server_id IS NULL', (server_id, playlist_id))
        self._conn.execute('DROP TABLE IF EXISTS playlist_items_scoped_new')
        self._conn.execute('CREATE TABLE playlist_items_scoped_new (playlist_id TEXT, server_id TEXT, item_id TEXT, sort_index INTEGER, owned INTEGER DEFAULT 0)')
        self._conn.execute('INSERT INTO playlist_items_scoped_new (playlist_id, server_id, item_id, sort_index, owned) SELECT pi.playlist_id, p.server_id, pi.item_id,        pi.sort_index, pi.owned FROM playlist_items pi LEFT JOIN playlists p ON p.playlist_id = pi.playlist_id')
        self._conn.execute('DROP TABLE playlist_items')
        self._conn.execute('ALTER TABLE playlist_items_scoped_new RENAME TO playlist_items')
        self._create_playlist_indexes()
        if scoped:
            log.info('Playlists: scoped %d to the server their members came from.', len(scoped))

    def _repair_playlist_member_scopes(self):
        """Put a stranded membership row back on its playlist's scope.

        A playlist and its members are read as a pair, at the playlist's
        scope, so a membership row carrying anything else is invisible -- with
        its file undeletable through the playlist and its `owned=1` claim
        outliving it. See `_migrate_playlist_scopes`.

        **It may not guess which playlist a row belongs to.** This used to
        correlate on `playlist_id` alone, and a playlist id hashes its *name*
        (R26), so two servers holding an `Example Playlist` is the ordinary
        case rather than an exotic one -- and then the scalar subquery
        returned whichever row SQLite yielded first and **every** membership
        row for that id was rewritten to it. Reproduced: one server's members
        became both films, the other's none, and `playlist_owned_ids` for the
        first returned the second's item, so deleting one server's playlist
        deleted the other's download off disk. On every open, so a catalog
        healed by hand re-broke on the next launch.

        So a row is left alone unless it is **stranded** -- its server matches
        no `playlists` row for its id -- and the id names exactly one
        playlist, which is the only case with one answer. Stranded under an
        ambiguous id is reported rather than decided.

        **The `IS` below is not the refuted fix.** Adding
        `AND p.server_id IS playlist_items.server_id` to the *subquery that
        supplies the new value* makes the statement a permanent no-op, because
        every row it should touch is one where those two differ. Here it sits
        in a `NOT EXISTS` that asks whether the row is stranded at all, which
        is the opposite question.

        A collision on the unique index -- two rows for one item moving onto
        one scope -- lands in the `except` below and leaves the catalog as it
        was, which is the same disposition as a table too old to have the
        column.

        Caller holds the write; this does not commit.
        """
        try:
            self._conn.execute('UPDATE playlist_items SET server_id = (  SELECT p.server_id FROM playlists p  WHERE p.playlist_id = playlist_items.playlist_id) WHERE NOT EXISTS (  SELECT 1 FROM playlists p  WHERE p.playlist_id = playlist_items.playlist_id    AND p.server_id IS playlist_items.server_id)  AND (SELECT COUNT(*) FROM playlists p       WHERE p.playlist_id = playlist_items.playlist_id) = 1')
            stranded = self._conn.execute('SELECT COUNT(*) FROM playlist_items WHERE NOT EXISTS (  SELECT 1 FROM playlists p  WHERE p.playlist_id = playlist_items.playlist_id    AND p.server_id IS playlist_items.server_id)  AND (SELECT COUNT(*) FROM playlists p       WHERE p.playlist_id = playlist_items.playlist_id)      > 1').fetchone()[0]
        except sqlite3.Error:
            log.debug('could not align the playlist membership scopes', exc_info=True)
            return
        if stranded:
            log.warning('Playlists: %d membership row(s) name a server none of their playlists do, and the id is held by more than one -- left as they are, because there is no way to tell whose they were.', stranded)

    def _merge_local_playstate(self, item_id, position_ticks, played):
        """Put a viewing nobody can be named for where the local readers look.

        R18 said such a viewing is kept and is worth something **locally**;
        the 2026-09-19 ruling says that means `item_userdata`, under the
        `(server, @none)` key `filing_state` gives a row whose server is
        known and whose reader cannot be named. With no server recoverable
        either, both halves are the sentinel -- one machine-wide bucket,
        because no server means no account.

        **This bucket is not the one homing drops** (R15,
        `_drop_local_userdata`): that one is `(@none, @none)`, and this has a
        server precisely because the download row could supply it.

        Merged rather than written: something may already be there, and the
        advance rule is the store's ordinary one. `play_count` is left alone
        deliberately -- a queued entry never carried one, and inventing one
        here would put a number in front of the user that nothing measured.

        Caller holds the write; this does not commit. Runs from `_migrate`,
        so `row_factory` is not installed and rows are plain tuples.
        """
        key = (item_id, self._playstate_row_server(item_id) or NO_ACTOR, NO_ACTOR)
        where = 'WHERE item_id=? AND server_id=? AND user_id=?'
        cur = self._conn.execute('SELECT played, position_ticks FROM item_userdata ' + where, key).fetchone()
        if cur is None:
            self._conn.execute('INSERT INTO item_userdata (item_id, server_id, user_id, played, position_ticks, updated_at) VALUES (?,?,?,?,?,?)', key + (1 if played else None, position_ticks, int(time.time())))
            return
        self._conn.execute('UPDATE item_userdata SET played=?, position_ticks=?, updated_at=? ' + where, (_advance(cur[0], 1 if played else None), max(cur[1] or 0, position_ticks or 0) or None, int(time.time())) + key)

    def _create_playlist_indexes(self):
        """The scoped indexes, once, for both branches of the migration.

        `COALESCE`, not a plain composite key: `server_id` is nullable, SQLite
        allows NULLs in an ordinary table's PRIMARY KEY, and a UNIQUE index
        treats two NULLs as distinct -- so either of the obvious spellings
        would let `INSERT OR REPLACE` pile up a fresh unscoped row per write
        instead of replacing the one that is there.
        """
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_items_scoped ON playlist_items(playlist_id, COALESCE(server_id, ''), item_id)")
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_playlist_items_item ON playlist_items(item_id)')
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_scoped ON playlists(playlist_id, COALESCE(server_id, ''))")

    def _playstate_row_server(self, item_id):
        """The content server of the download a queued entry is about, or ``''``.

        R18's recovery: an entry whose login no longer resolves still knows
        which *item* it is about, and that item's row knows which server it
        came from. `''` rather than None because the column is NOT NULL and
        because "we could not say" is one value here, not two.

        The caller turns a falsy answer into `NO_ACTOR` for the
        `item_userdata` key, where no server means no account and both halves
        are the sentinel. This still answers `''` because that is what the
        `pending_playstate` column holds; the two spellings of "nobody" are
        one table apart, not a disagreement.
        """
        try:
            row = self._conn.execute('SELECT content_server_id FROM downloads WHERE item_id=?', (item_id,)).fetchone()
        except sqlite3.Error:
            log.debug('could not read the server of %s', item_id, exc_info=True)
            return ''
        return (row[0] if row and row[0] else '') or ''

    def _migrate_playstate_actors(self):
        """Give queued entries the actor they owe, **moving the ones nobody
        can be named for to local watched state**.

        R18: *"Keep it locally, no person, newer database entries should track
        remote user and server id so it can be re-homed"*. So a viewing whose
        saved login no longer resolves -- you watched an episode offline, then
        removed that server and added it back -- survives, with its **server**
        half recovered from the item's own download row and no person named.

        **This used to delete it**, on a rule that appears in no register:
        *"if it names an actor migrate it, otherwise drop it"*. Measured on a
        real catalog before R18 was asked for, that deleted a local watched
        mark whose file is still on disk.

        **Where it survives was ruled separately, on 2026-09-19, and it is not
        this table.** R18 kept the entry here and said what it was worth was
        local -- "watched" on screen, `played_by_anyone`, the reaper's grace
        period. All three of those read `item_userdata`, and nothing outside
        this module reads `pending_playstate` at all, so the kept entry
        delivered none of them; `_open_and_run` then pruned an orphan row's
        entry on every launch, three statements after this logged the keep. So
        the mark goes to `item_userdata` under `(server, @none)` and the queue
        row goes with the promise it can no longer make. R18's property
        stands; the table it named does not. R15 still drops the bucket when
        the copy is re-homed.

        Older catalogs also carried the duplicates the NULL key produced,
        one per write; those collapse here, keeping the furthest position
        and any watched mark, which is what a single row would have held.

        Caller holds the write; this does not commit.
        """
        try:
            have = {r[1] for r in self._conn.execute('PRAGMA table_info(pending_playstate)')}
        except sqlite3.Error:
            log.warning('Could not inspect the replay queue', exc_info=True)
            return
        for col, decl in self._ADDED_PLAYSTATE_COLUMNS:
            if col not in have:
                self._conn.execute('ALTER TABLE pending_playstate ADD COLUMN %s %s' % (col, decl))
        if self._actor_for is None:
            return
        rows = self._conn.execute("SELECT id, server_uuid, item_id, position_ticks, played FROM pending_playstate WHERE user_id = '' ORDER BY id").fetchall()
        merged, folded, local, retired = ({}, [], {}, [])
        for erid, eitem, eserver, euser, epos, eplayed in self._conn.execute("SELECT id, item_id, server_id, user_id, position_ticks, played FROM pending_playstate WHERE user_id != ''"):
            merged[eitem, eserver, euser] = [erid, epos, eplayed, False]
        for rid, server_uuid, item_id, position_ticks, played in rows:
            try:
                actor = self._actor_for(server_uuid)
            except Exception:
                log.debug("could not resolve a queued entry's actor", exc_info=True)
                actor = None
            if not actor or not actor[1]:
                held = local.setdefault(item_id, [0, None])
                held[0] = max(held[0] or 0, position_ticks or 0)
                held[1] = held[1] or played
                retired.append((rid,))
                continue
            key = (item_id,) + tuple(actor)
            keep = merged.get(key)
            if keep is None:
                merged[key] = [rid, position_ticks, played, True]
                continue
            keep[1] = max(keep[1] or 0, position_ticks or 0) or None
            keep[2] = keep[2] or played
            keep[3] = True
            folded.append((rid,))
        for (item_id, server_id, user_id), (rid, pos, played, dirty) in merged.items():
            if not dirty:
                continue
            self._conn.execute('UPDATE pending_playstate SET server_id=?, user_id=?, position_ticks=?, played=? WHERE id=?', (server_id, user_id, pos, played, rid))
        for item_id, (pos, played) in local.items():
            self._merge_local_playstate(item_id, pos or None, played)
        if retired:
            self._conn.executemany('DELETE FROM pending_playstate WHERE id=?', retired)
        if folded:
            self._conn.executemany('DELETE FROM pending_playstate WHERE id=?', folded)
            log.info('Replay queue: folded %d duplicate entry/entries.', len(folded))
        if local:
            log.info('Replay queue: moved %d viewing(s) whose login is gone to local watched state; nothing is owed to a server for them.', len(local))
        self._conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_actor ON pending_playstate(item_id, server_id, user_id)')

    def _backfill_content_server_id(self):
        """Fill the content key from the manifest each row already carries.

        Every download row stores the item DTO it was created from, and every
        DTO names its server -- measured 2000/2000 across 16 types on 12.0.0
        and 14/14 on a real catalog. So this is a local read, not a network
        pass: there is no such thing here as a row whose server is unknown.

        **Deliberately not `json_extract`.** JSON1 is only compiled in by
        default from SQLite 3.38, and the Windows bundle and the Flatpak ship
        their own SQLite. A missing function would leave the column NULL on
        one platform and nowhere else, which every test on Linux would pass.

        Advance from NULL only, and unconditional rather than gated on "did
        this run add the column", for the reason spelled out above the
        `origin` backfill. A row whose manifest is unreadable is skipped and
        stays NULL: one bad row must not cost the whole catalog an open,
        which is the same rule `repository._item_from_row` reads them under.

        Caller holds the write; this does not commit.
        """
        rows = self._conn.execute('SELECT item_id, item_json FROM downloads WHERE content_server_id IS NULL AND item_json IS NOT NULL').fetchall()
        filled = []
        for item_id, item_json in rows:
            try:
                server_id = (json.loads(item_json) or {}).get('ServerId')
            except (TypeError, ValueError):
                continue
            if server_id:
                filled.append((server_id, item_id))
        if filled:
            self._conn.executemany('UPDATE downloads SET content_server_id = ? WHERE item_id = ?', filled)
            for _server_id, item_id in filled:
                self._drop_local_userdata(item_id)
            log.info('Catalog: homed %d row(s) to their server.', len(filled))
    _USERDATA_FIELDS = ('played', 'position_ticks', 'play_count', 'is_favorite', 'last_played_date')

    def _row_sync_state(self, item_id, actor):
        """What may be done for ``item_id`` on behalf of ``actor``, and under
        which key. **The one answer to C1**, consulted by every filing
        decision in this file, including the outgoing queue.

        ``actor`` is the ``(ServerId, UserId)`` pair of whoever is *acting* --
        passed as an assertion to be **checked**, never as a key to file
        under. **The row decides the server half, and no caller may say
        otherwise.** Two things could answer it and they disagree: the row's
        own `content_server_id`, and whatever server the writing login is on
        -- a watched mark fanned out over a series that spans servers, or a
        push whose payload names one server while the row belongs to
        another. Written under one and read back under the other, the mark
        lands and is invisible: no error, no missing write, just a tick that
        never appears. Deriving it here for the write *and* the read makes
        them agree by construction.

        What is new is that the caller's half is compared instead of
        discarded. Every call site already computed the pair and threw the
        server away, which is how a push about server B came to be filed onto
        server A's row. (This replaces `_server_of`, which five sites called
        separately, each pairing it with its own SQL -- one rule, five
        implementations, which is the shape that keeps failing here.)

        Four verdicts, and the pair of them that used to be one is the point:

        - ``no_row`` -- we hold no copy. Nothing is written. **Distinct from
          an orphan**, and `_server_of` alone cannot tell them apart: it
          answers `NO_ACTOR` for both, so an item this catalog has never held
          got userdata filed under the orphan key.
        - ``refuse`` -- the row belongs to another server. Ids are not unique
          across servers (docs/jellyfin-api-notes.md 13b), so this is not our
          film and the honest answer is to do nothing. Not "we know it
          differs" -- we cannot tell, and only the download door has evidence
          that could.
        - ``local`` -- record it here, never send it. Either the row's server
          is unknown (an orphan: it plays, it records, it never syncs --
          docs/offline-sync.md section 1), or the row's server is
          known but nobody can be named on it, in which case the person half
          is the sentinel and there is still no account to sync as.
        - ``sync`` -- the roles agree; file it and it may be queued.

        Returns ``(verdict, key)``; ``key`` is None exactly when nothing may
        be written. Aggregate reads (`played_by_anyone`, `all_userdata`) do
        not come through here -- they select no actor and must not.
        """
        row = self._conn.execute('SELECT content_server_id FROM downloads WHERE item_id=?', (item_id,)).fetchone()
        if row is None:
            return ('no_row', None)
        state, key = filing_state(row[0], actor)
        if state == 'refuse':
            self._note_refusal(item_id, row[0], actor)
        return (state, key)
    _REFUSAL_SUMMARY_INTERVAL = 600

    def _note_refusal(self, item_id, row_server, actor):
        """Say a refusal out loud, because it is otherwise invisible.

        Refusing is correct -- item ids are not unique across servers
        (docs/jellyfin-api-notes.md 13b), so state we cannot show belongs to
        this account's content is not filed -- but `_write_userdata` just
        returns, `upsert_playstate` just answers False, and `userdata` just
        answers "nothing stored". Nothing anywhere said why. That silence is
        what let the e2e suite sit red for fourteen commits, and in the field
        it covers the identical symptom with no evidence at all: watched
        state quietly stops syncing and the log is clean.

        Every refusal is a DEBUG line naming the item. The summary is INFO
        because **the default configuration logs INFO**, and an issue report
        has to carry this without the reporter having been told to turn
        anything on first. Rate-limited per (row server, acting server) pair,
        since a mismatch that fires once fires again on every progress report
        for that item -- and in a healthy single-server setup it never fires
        at all, which is what makes one line at INFO affordable.
        """
        actor_server = (actor or (None, None))[0]
        log.debug('Catalog: refused userdata for %s -- the row is content server %s, the acting account is on %s.', item_id, row_server, actor_server)
        now = time.time()
        shape = (row_server, actor_server)
        count, last = self._refusals.get(shape, (0, 0.0))
        count += 1
        if last and now - last < self._REFUSAL_SUMMARY_INTERVAL:
            self._refusals[shape] = (count, last)
            return
        self._refusals[shape] = (0, now)
        log.info('Catalog: %d watched-state request(s) refused -- the rows belong to content server %s and the acting account is on %s. Ids are not unique across servers, so that state is neither recorded nor synced. Debug logging names the items.', count, row_server, actor_server)

    def _file_under(self, item_id, actor):
        """Where one actor's userdata for one item physically goes, or None
        when it may not be written at all. `_row_sync_state`'s key half."""
        return self._row_sync_state(item_id, actor)[1]

    def _write_userdata(self, item_id, actor, **fields):
        """Upsert one actor's userdata. Caller holds the write and the lock.

        Only the fields passed as non-None are touched, so a position report
        does not blank a watched flag written by something else. A row we do
        not hold, or one belonging to another server, writes nothing.
        """
        key = self._file_under(item_id, actor)
        if key is None:
            return
        server_id, user_id = key
        sets, params = ([], [])
        for name in self._USERDATA_FIELDS:
            value = fields.get(name)
            if value is not None:
                sets.append(name)
                params.append(value)
        if not sets:
            return
        cols = ', '.join(['item_id', 'server_id', 'user_id'] + sets + ['updated_at'])
        marks = ', '.join('?' * (len(sets) + 4))
        updates = ', '.join(('%s=excluded.%s' % (c, c) for c in sets))
        self._conn.execute('INSERT INTO item_userdata (%s) VALUES (%s) ON CONFLICT(item_id, server_id, user_id) DO UPDATE SET %s, updated_at=excluded.updated_at' % (cols, marks, updates), [item_id, server_id, user_id] + params + [int(time.time())])
    _NO_USERDATA = {'played': False, 'position_ticks': 0, 'play_count': 0, 'is_favorite': False, 'last_played_date': None}

    def userdata(self, item_id, *, actor):
        """One actor's watched state and resume position for one item.

        Never None: an actor who has not watched something reads as
        unwatched at position zero, which is what no row means -- and so
        does an item we hold no copy of, or one belonging to another server.

        Resolved through `_row_sync_state`, the same call the write uses, so
        a read cannot look somewhere a write would not have landed. That
        disagreement is the whole defect: a mark written under
        `(@none, a real person)` and read back under `(@none, @none)` lands,
        is invisible, and reports success.
        """
        with self._lock:
            if self._conn is None:
                return dict(self._NO_USERDATA)
            try:
                key = self._file_under(item_id, actor)
            except sqlite3.Error:
                key = None
        if key is None:
            return dict(self._NO_USERDATA)
        rows = self._query('SELECT * FROM item_userdata WHERE item_id=? AND server_id=? AND user_id=?', (item_id,) + key)
        if not rows:
            return dict(self._NO_USERDATA)
        row = rows[0]
        return {'played': bool(row['played']), 'position_ticks': row['position_ticks'] or 0, 'play_count': row['play_count'] or 0, 'is_favorite': bool(row['is_favorite']), 'last_played_date': row['last_played_date']}

    def set_userdata(self, item_id, *, actor, played=None, position_ticks=None, play_count=None, is_favorite=None, last_played_date=None):
        """Write one actor's userdata **verbatim**, for the fields given.

        Verbatim because this is the deliberate-choice path -- Mark played,
        Mark unplayed, a seek backwards the person meant. `update_userdata`
        is the playback path and is the one that only ever moves forward.
        """
        with self._lock:
            if self._conn is None or self.read_only:
                return False
            try:
                self._write_userdata(item_id, actor, played=None if played is None else 1 if played else 0, position_ticks=position_ticks, play_count=play_count, is_favorite=None if is_favorite is None else 1 if is_favorite else 0, last_played_date=last_played_date)
                self._conn.commit()
                return True
            except sqlite3.Error:
                self._conn.rollback()
                log.debug('could not write userdata for %s', item_id, exc_info=True)
                return False

    def userdata_actors(self, item_id):
        """Every actor holding state for this item, as (server_id, user_id).

        Ordered, so a caller comparing two catalogs gets a stable answer.
        """
        return [(r['server_id'], r['user_id']) for r in self._query('SELECT server_id, user_id FROM item_userdata WHERE item_id=? ORDER BY server_id, user_id', (item_id,))]

    def all_userdata(self):
        """Every actor's state, as ``{(item_id, server_id, user_id): {...}}``.

        One query for the whole catalog, because the offline browser
        overlays this onto every row it renders and a point query per row
        would be a round trip per tile.
        """
        return {(r['item_id'], r['server_id'], r['user_id']): {'played': bool(r['played']), 'position_ticks': r['position_ticks'] or 0, 'play_count': r['play_count'] or 0, 'is_favorite': bool(r['is_favorite']), 'last_played_date': r['last_played_date']} for r in self._query('SELECT * FROM item_userdata')}

    def played_by_anyone(self, item_id):
        """Has ANY actor on this machine watched it?

        The aggregate the deletion paths ask, by [iw]'s ruling: one file, so
        one decision, and requiring every account to have watched it would
        stop auto-download reaping the moment one person fell behind. The
        accepted cost is that one person watching can reap another's
        unwatched copy when auto-delete is on; making that per-actor needs a
        claim system recording who *requested* each download, which is a
        third key again. docs/offline-sync.md section 1.
        """
        return bool(self._query('SELECT 1 FROM item_userdata WHERE item_id=? AND played=1 LIMIT 1', (item_id,)))

    def art_owners(self):
        """``(series ids, season ids, {(server_id, playlist_id)})`` that some
        row still references -- every status, so a pending download keeps its
        art. **Strict**: an unreadable catalog raises rather than answering
        "nothing", because the caller deletes what is not listed here."""
        rows = self._query('SELECT series_id, season_id FROM downloads', strict=True)
        playlists = self._query('SELECT server_id, playlist_id FROM playlists', strict=True)
        return ({r['series_id'] for r in rows if r['series_id']}, {r['season_id'] for r in rows if r['season_id']}, {(p['server_id'], p['playlist_id']) for p in playlists})

    def healthy(self):
        """Can the catalog's own rows actually be read?

        Every read here answers ``[]`` on a ``sqlite3.Error`` so a caller
        cannot crash on a bad catalog -- which means "unreadable" and "empty"
        arrive as the same value. That is survivable for a screen and is
        **not** survivable for `SyncManager._reconcile_disk`, which reads an
        empty catalog as "none of this media is known" and deletes all of it.
        One corrupted 4 KiB page (the `downloads` b-tree root) is enough: the
        file still opens, the schema still reads, and every download on disk
        is swept.

        So the distinction has to exist somewhere, and this is it -- the one
        read that reports failure instead of absorbing it.
        """
        try:
            self.list(strict=True)
            return True
        except sqlite3.Error:
            log.warning('The catalog at %s is unreadable.', self.path)
            return False

    def backup(self, dest):
        """Snapshot the catalog to ``dest``. Returns whether it was written.

        The catalog is not a cache: it is the only record of what the files
        on disk *are*, and losing it turns a download folder into unlabelled
        media that the UI cannot list, play or delete. sqlite's own backup
        API rather than a file copy, because a plain copy of a WAL database
        mid-write is how you manufacture the corruption this exists for.

        Written to a temporary file and renamed, so an interrupted backup
        cannot leave a half-written one where a good one used to be.
        """
        with self._lock:
            if self._conn is None or self.read_only:
                return False
            tmp = dest + '.tmp'
            out = None
            try:
                out = sqlite3.connect(tmp)
                self._conn.backup(out)
                out.close()
                out = None
                os.replace(tmp, dest)
                return True
            except (sqlite3.Error, OSError):
                log.debug('Could not back up the catalog', exc_info=True)
                if out is not None:
                    try:
                        out.close()
                    except sqlite3.Error:
                        pass
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False

    def close(self):
        with self._lock:
            if self._conn is None:
                return
            if not self.read_only:
                try:
                    self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                except sqlite3.Error:
                    log.debug('WAL checkpoint on close failed', exc_info=True)
            self._conn.close()
            self._conn = None

    def upsert(self, row: dict):
        values = [row.get(col) for col in COLUMNS]
        placeholders = ','.join(('?' for _ in COLUMNS))
        cols = ','.join(COLUMNS)
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute('INSERT OR REPLACE INTO downloads (%s) VALUES (%s)' % (cols, placeholders), values)
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
    UPDATABLE = frozenset(('status', 'file_path', 'downloaded_bytes', 'origin', 'watched_at', 'completed_at', 'ext', 'media_source_id', 'size_bytes', 'source_json'))

    def update(self, item_id, **fields):
        if not fields:
            return
        refused = [k for k in fields if k not in self.UPDATABLE]
        if refused:
            raise ValueError('cannot update %s through db.update: only %s may be set here. See SyncDB.UPDATABLE.' % (', '.join(sorted(refused)), ', '.join(sorted(self.UPDATABLE))))
        assignments = ','.join(('%s=?' % k for k in fields))
        params = list(fields.values()) + [item_id]
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute('UPDATE downloads SET %s WHERE item_id=?' % assignments, params)
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def drop_unsyncable_playstate(self):
        """Discard queued entries that may never be sent, and say how many.

        `upsert_playstate` refuses these at the door now, but the door does
        not reach what is already inside: `_sync_playstate` never consults
        the download row, so an entry stored by an older build -- or carried
        in by an existing catalog -- is still drained through any client
        matching its actor. Two kinds cannot legitimately be sent, and **both
        require a download row to exist**:

        - the row is an **orphan**. Its copy cannot be shown to be that
          server's content, so reporting progress on it could tell server A
          about a film that is really B's; sending is forbidden outright (11c).
        - the row's server is not the entry's. Ids collide across servers, so
          that is a report about somebody else's film.

        **A missing row is not a third kind**, and reading it as one was a
        data-loss bug. This said "the row is an orphan *or gone*. There is no
        server to send to" -- and for *gone* that is false: the entry carries
        its own ``(server_id, user_id)``, and deleting a download removes the
        copy, not the server. `SyncDB.delete` keeps the queue entry on purpose
        for exactly this reason (see it, and docs/offline-sync.md section 1),
        and this ran on the next launch and threw it away. Watch something
        offline, delete it, relaunch still offline: the server never heard.
        Queued progress is the only state here the server does not already
        have, so it is the only loss that is not a re-download away.

        Deleting rather than parking the two real kinds: the queue is
        advance-only and drains oldest-first, so an entry that can never go
        does not wait, it blocks the accounting and reappears in every log
        line forever. Returns how many went.
        """
        with self._lock:
            if self._conn is None or self.read_only:
                return 0
            try:
                cur = self._conn.execute('DELETE FROM pending_playstate WHERE EXISTS (  SELECT 1 FROM downloads d   WHERE d.item_id = pending_playstate.item_id     AND (d.content_server_id IS NULL          OR d.content_server_id             <> pending_playstate.server_id))')
                self._conn.commit()
                dropped = cur.rowcount or 0
            except sqlite3.Error:
                self._conn.rollback()
                log.debug('could not prune the replay queue', exc_info=True)
                return 0
        if dropped:
            log.info('Replay queue: dropped %d entry/entries that could never be sent.', dropped)
        return dropped

    def home_content_server(self, item_id, server_id):
        """Give a row its content server, and **discard** the watched state it
        recorded without one. **One transaction**, both statements.

        R15 and R2, delivered 2026-09-20; `_drop_local_userdata` carries the
        argument. Learning the server does not learn who watched it, so the
        anonymous bucket does not survive the homing -- the loss is ruled and
        accepted.

        Split across two transactions, a crash between them leaves a homed
        row still carrying the bucket, and nothing clears it afterwards --
        the migration's backfill visits only rows that are still orphans.
        It reads as nothing until a catalog restore orphans the row again,
        and then the state this was supposed to drop is back.
        """
        with self._lock:
            if self._conn is None or self.read_only:
                return False
            if not server_id:
                log.debug('refusing to home %s to an empty server', item_id)
                return False
            try:
                self._conn.execute('UPDATE downloads SET content_server_id=? WHERE item_id=?', (server_id, item_id))
                self._drop_local_userdata(item_id)
                self._conn.commit()
                return True
            except sqlite3.Error:
                self._conn.rollback()
                log.debug('could not home %s', item_id, exc_info=True)
                return False

    def _drop_local_userdata(self, item_id):
        """Discard the `(@none, @none)` bucket when the row learns its server.
        Caller holds `_lock` and the write. Both homing sites call this.

        **R15 delivered, and it reverses what stood here.** This used to move
        the bucket onto `(server, @none)` and merge where something was
        already there. The argument for the move was that both keys are
        anonymous, so nothing is attributed to a *person* by it -- true, and
        not the objection. `(@none, @none)` is one bucket for the whole
        machine; `(server, @none)` is one per server, read by every local
        profile that holds no account there. So the carry takes marks left by
        a profile that *does* have an account on that server -- which reads
        its own key and never sees the bucket either way -- and hands them to
        the profiles that cannot be named. Homing is the moment the machine
        starts being able to tell those apart, and the merge was the one
        thing that reached across it.

        What it costs is real and was ruled with the case in hand ([iw], R15):
        a copy watched while its origin was unknown, that no named account
        ever watched, comes back unwatched. *"The view can't go anywhere...
        So it would be lost."* Single-user machines would be safe to merge and
        that conditional was considered and declined, 2026-09-20 -- see R32.

        **Not `DELETE ... WHERE item_id=?`**: that takes every named account's
        state with it, and R28's `(server, @none)` mark, neither of which
        homing has anything to say about.
        """
        self._conn.execute('DELETE FROM item_userdata WHERE item_id=? AND server_id=? AND user_id=?', (item_id, NO_ACTOR, NO_ACTOR))

    def delete(self, item_id):
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute('DELETE FROM downloads WHERE item_id=?', (item_id,))
                self._conn.execute('DELETE FROM playlist_items WHERE item_id=?', (item_id,))
                self._conn.execute('DELETE FROM item_userdata WHERE item_id=?', (item_id,))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def _has_scoped_playlist_items(self):
        if self._conn is None:
            return False
        try:
            return any((r[1] == 'server_id' for r in self._conn.execute('PRAGMA table_info(playlist_items)')))
        except sqlite3.Error:
            return False

    def _playlist_scope(self, server_id, alias=''):
        """``(sql, params)`` selecting one playlist row, scoped.

        ``alias`` qualifies the column for a joined query (``"pi"``), because
        `playlist_items` and `downloads` are both in those.

        A **keyword-only, defaultless** `server_id` at every caller, and this
        is why: the identity is (playlist_id, server_id) now, so a query that
        forgot the scope would answer for -- or delete -- another server's
        playlist of the same name. `None` is the representable "could not
        tell" row and matches only itself; it is never "any".
        """
        if not self._playlist_items_scoped:
            return ('', ())
        if server_id is ANY_SERVER:
            return ('', ())
        column = '%s.server_id' % alias if alias else 'server_id'
        if server_id is None:
            return (' AND %s IS NULL' % column, ())
        return (' AND %s = ?' % column, (server_id,))

    def upsert_playlist(self, playlist_id, server_id, server_uuid, name):
        with self._lock:
            if self._conn is None:
                return
            try:
                scope = None if server_id is ANY_SERVER else server_id or None
                self._conn.execute('INSERT OR REPLACE INTO playlists (playlist_id, server_id, server_uuid, name, added_at) VALUES (?,?,?,?,?)', (playlist_id, scope, server_uuid, name, int(time.time())))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def replace_playlist_items(self, playlist_id, entries, *, server_id):
        """Set one playlist's membership to ``entries`` (list of
        ``(item_id, sort_index, owned)``), replacing any prior membership so a
        re-download reflects the current order and removals.

        ``server_id`` is which server's playlist -- see `_playlist_scope`.

        **An entry is only written for an item the catalog actually has, and
        the check is part of THIS transaction** -- the caller cannot do it,
        because nothing serialises `_record_playlist`'s `pre_existing` snapshot
        against a concurrent delete (docs/offline-sync.md section 5, "Ownership
        never outlives its row").

        **And therefore this decides whether the playlist exists offline at
        all.** Filtering can write zero rows for a non-empty `entries`, which
        no caller can predict, and `list_playlists` needs a member -- so an
        emptied `playlists` row is a record nothing lists and nothing deletes,
        holding its cached poster art. It goes in the same transaction. The
        count returned lets a caller skip work; the invariant does not depend
        on it being read.
        """
        with self._lock:
            if self._conn is None:
                return 0
            try:
                server_id = None if server_id is ANY_SERVER else server_id
                clause, params = self._playlist_scope(server_id)
                self._conn.execute('DELETE FROM playlist_items WHERE playlist_id=?' + clause, (playlist_id,) + params)
                if self._playlist_items_scoped:
                    self._conn.executemany('INSERT INTO playlist_items (playlist_id, server_id, item_id, sort_index, owned) SELECT ?,?,?,?,? WHERE EXISTS (SELECT 1 FROM downloads WHERE item_id=?)', [(playlist_id, server_id, iid, idx, 1 if owned else 0, iid) for iid, idx, owned in entries])
                else:
                    self._conn.executemany('INSERT INTO playlist_items (playlist_id, item_id, sort_index, owned) SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM downloads WHERE item_id=?)', [(playlist_id, iid, idx, 1 if owned else 0, iid) for iid, idx, owned in entries])
                written = self._conn.execute('SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?' + clause, (playlist_id,) + params).fetchone()[0]
                if not written:
                    self._conn.execute('DELETE FROM playlists WHERE playlist_id=?' + clause, (playlist_id,) + params)
                self._conn.commit()
                return written
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def delete_if_auto(self, item_id):
        """Delete ``item_id`` only while it is *still* an auto-download.

        Returns the row that was deleted, or None if it no longer qualifies
        (promoted to user-owned, or already gone).

        The check and the delete share ONE lock acquisition, because the
        window between them is the entire bug. The reaper lists its rows
        once and acts on every one of them against that snapshot, and
        `enqueue` can promote a row with `set_origin` at any point in there.
        (The window used to be seconds wide -- one blocking
        get_userdata_for_item per row -- and is now as narrow as the deletes
        before it. Narrower is not closed.) Re-reading the origin without
        holding the lock only makes the window smaller; holding it makes the
        claim atomic against every writer, since `update` takes the same
        lock.
        """
        with self._lock:
            if self._conn is None:
                return None
            try:
                found = self._conn.execute('SELECT * FROM downloads WHERE item_id=?', (item_id,)).fetchone()
                if found is None:
                    return None
                row = dict(found)
                if not is_auto(row.get('origin')):
                    return None
                self._conn.execute('DELETE FROM downloads WHERE item_id=?', (item_id,))
                self._conn.execute('DELETE FROM playlist_items WHERE item_id=?', (item_id,))
                self._conn.execute('DELETE FROM item_userdata WHERE item_id=?', (item_id,))
                self._conn.commit()
                return row
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def delete_playlist(self, playlist_id, *, server_id):
        """Drop one server's playlist and its membership.

        Scoped, and that is the whole reason this argument exists: two servers
        can hold a playlist with the same id, and an unscoped delete took both
        -- including the membership of the one the user did not ask about.
        """
        with self._lock:
            if self._conn is None:
                return
            try:
                server_id = None if server_id is ANY_SERVER else server_id
                clause, params = self._playlist_scope(server_id)
                self._conn.execute('DELETE FROM playlists WHERE playlist_id=?' + clause, (playlist_id,) + params)
                self._conn.execute('DELETE FROM playlist_items WHERE playlist_id=?' + clause, (playlist_id,) + params)
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def disown_playlist_items(self, item_id):
        """Drop every playlist's *ownership* of ``item_id``, keeping the
        membership rows so the item still lists under the playlist offline.

        Ownership answers one question only: may deleting the playlist delete
        this file. Asking for the item by hand answers it -- no.
        """
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute('UPDATE playlist_items SET owned=0 WHERE item_id=?', (item_id,))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def playlist_owned_ids(self, playlist_id, *, server_id):
        """Item ids this playlist download is responsible for (owned=1).

        **Joined, like every other read of this table.** `owned=1` answers
        "may deleting this playlist delete this file", and a claim over an
        item the catalog does not have is a claim on whatever writes that row
        next. `replace_playlist_items` will not write one, but that binds only
        the rows this build wrote: a catalog older than it, or one an older
        build opens afterwards, holds whatever it holds. The join is on the
        row's existence and not on its status, because a playlist owns what it
        is still fetching -- `_record_playlist` reads this mid-download.

        A repair tool that needs to *see* dangling claims needs its own query
        and should say so in its name; this one exists to be acted on.

        Scoped: what one server's playlist may delete is not what another's
        may, even when they share an id.
        """
        clause, params = self._playlist_scope(server_id, 'pi')
        return {r['item_id'] for r in self._query('SELECT pi.item_id FROM playlist_items pi JOIN downloads d ON d.item_id = pi.item_id WHERE pi.playlist_id=? AND pi.owned=1' + clause, (playlist_id,) + params)}

    def list_playlists(self, server_id=ANY_SERVER):
        """Playlists that still have at least one completely-downloaded item,
        each as a dict with ``playlist_id``/``name``/``server_id``/``server_uuid``.

        **``playlists.server_id`` is the Jellyfin ``ServerId``** -- the content
        key. `downloads` carried a `server_id` of its own, meaning the opposite
        (an on-disk path key), until CX8 dropped it; scoping on this one is
        correct, and read like a mistake for as long as both existed.

        ``ANY_SERVER`` asks unscoped, which is what the Downloads screen
        wants: it browses a pseudo-server, and its whole job is to show every
        download this machine holds. The badge caller passes a real scope,
        because a playlist held from server A must not tick a tile on server
        B -- and if the login it passes resolves to nothing, this answers with
        nothing rather than with everything. See `ANY_SERVER`.
        """
        scope, params = ('', [STATUS_COMPLETE])
        if server_id is ANY_SERVER:
            pass
        elif not server_id:
            scope, params = (' AND 0', [STATUS_COMPLETE])
        else:
            scope = ' AND (p.server_id=? OR p.server_id IS NULL)'
            params.append(server_id)
        member = ' AND pi.server_id IS p.server_id' if self._playlist_items_scoped else ''
        return self._query('SELECT p.playlist_id, p.name, p.server_id, p.server_uuid FROM playlists p WHERE EXISTS (SELECT 1 FROM playlist_items pi               JOIN downloads d ON d.item_id = pi.item_id               WHERE pi.playlist_id = p.playlist_id                 AND d.status=?' + member + ')' + scope + ' ORDER BY p.name', tuple(params))

    def playlist_item_rows(self, playlist_id, *, server_id):
        """One playlist's completely-downloaded items as full download rows, in
        playlist order. Scoped -- see `_playlist_scope`."""
        clause, params = self._playlist_scope(server_id, 'pi')
        return self._query('SELECT d.* FROM playlist_items pi JOIN downloads d ON d.item_id = pi.item_id WHERE pi.playlist_id=? AND d.status=?' + clause + ' ORDER BY pi.sort_index', (playlist_id, STATUS_COMPLETE) + params)

    def playlist_ownership(self):
        """Map of item_id -> (playlist_id, server_id) for owned items, for
        grouping the Downloads screen. Only one owner per item.

        **Deliberately unscoped, unlike its neighbours**, and it can be: the
        Downloads screen browses every server at once, and `downloads.item_id`
        is unique machine-wide, so one item has one owner however many servers
        hold a playlist of that name. The server travels in the value because
        the grouping has to be able to tell two same-named playlists apart.

        Joined for the same reason as `playlist_owned_ids` above."""
        column = 'pi.server_id' if self._playlist_items_scoped else 'NULL'
        return {r['item_id']: (r['playlist_id'], r['server_id']) for r in self._query('SELECT pi.item_id, pi.playlist_id, %s AS server_id FROM playlist_items pi JOIN downloads d ON d.item_id = pi.item_id WHERE pi.owned=1' % column)}

    def upsert_playstate(self, item_id, *, actor, position_ticks=None, played=None, deliberate=False):
        """Queue what one person still owes one server. One row per pair.

        Position advances (max), played sticks True -- the queue is a floor,
        so a client that has been offline cannot rewind the place another
        device reached.

        **Except a deliberate mark** (``deliberate=True``: Mark played /
        unplayed), which is the one signal authoritative in both directions.
        It is stored verbatim -- ``played = 0`` is a queued unwatch, distinct
        from NULL (progress only) -- and stamps ``marked_at``, which replay
        compares with the server's LastPlayedDate: an unwatch older than a
        watch made elsewhere loses (D1, docs/offline-sync.md section 3a).
        Playback finishing after a queued unwatch is later than it, so it
        sets ``played = 1`` as ever.

        **Only a `sync` row may be queued**, and that is `_row_sync_state`'s
        verdict, not a rule re-derived here. The two tables share one
        predicate on purpose: C1 governs both, and a contract with two
        implementations is this repository's recurring defect installed on
        purpose. What it refuses:

        - a row whose content server is unknown -- an orphan is a local file
          and never syncs in either direction (11c);
        - a row belonging to another server -- not our film;
        - an item we hold no copy of, which could never be drained;
        - an unattributed entry, because there is no account to send it as.
          [iw]'s ruling is that such progress is recorded locally, where
          resume needs it, and never queued. That also retires the
          duplicate-row bug at the root: the old key allowed NULL, and
          `NULL = NULL` is false in SQL, so every write inserted.

        Returns whether anything was queued.
        """
        if not item_id:
            return False
        with self._lock:
            if self._conn is None or self.read_only:
                return False
            state, key = self._row_sync_state(item_id, actor)
            if state != 'sync':
                if state == 'local':
                    log.debug('Catalog: %s is local-only for this actor; progress is recorded but never queued.', item_id)
                return False
            server_id, user_id = key
            try:
                existing = self._conn.execute('SELECT id, position_ticks, played, marked_at FROM pending_playstate WHERE item_id=? AND server_id=? AND user_id=?', (item_id, server_id, user_id)).fetchone()
                marked = deliberate and played is not None
                now = int(time.time())
                if existing:
                    new_pos = existing['position_ticks']
                    if position_ticks is not None:
                        new_pos = max(new_pos or 0, position_ticks)
                    new_played = existing['played']
                    new_marked = existing['marked_at']
                    if marked:
                        new_played = 1 if played else 0
                        new_marked = now
                    elif played:
                        new_played = 1
                    self._conn.execute('UPDATE pending_playstate SET position_ticks=?, played=?, marked_at=? WHERE id=?', (new_pos, new_played, new_marked, existing['id']))
                else:
                    self._conn.execute('INSERT INTO pending_playstate (server_id, user_id, item_id, position_ticks, played, created_at, marked_at) VALUES (?,?,?,?,?,?,?)', (server_id, user_id, item_id, position_ticks, (1 if played else 0) if marked else 1 if played else None, now, now if marked else None))
                self._conn.commit()
                return True
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def _owes_a_watched_mark(self, item_id, server_id, user_id):
        """Is there an undelivered `played = 1` for this actor and item?

        Caller holds ``_lock``. See :meth:`update_userdata` for why the
        nullable column means this cannot be "is there a pending row".
        """
        row = self._conn.execute('SELECT 1 FROM pending_playstate WHERE item_id=? AND server_id=? AND user_id=? AND played=1', (item_id, server_id, user_id)).fetchone()
        return row is not None

    def _owes_an_unwatch(self, item_id, server_id, user_id):
        """Is there an undelivered deliberate unwatch (`played = 0`) for this
        actor and item? Caller holds ``_lock``."""
        row = self._conn.execute('SELECT 1 FROM pending_playstate WHERE item_id=? AND server_id=? AND user_id=? AND played=0', (item_id, server_id, user_id)).fetchone()
        return row is not None

    def update_userdata(self, item_id, *, actor, played=None, position_ticks=None, allow_retreat=False):
        """Merge playback progress into **one actor's** state.

        The playback rule, and the only writer that follows it: played
        sticks True, the position only moves forward -- except that a finish
        clears the resume point, matching what the server does, so the
        browser does not offer "Resume from the very end" of a finished
        item.

        ``allow_retreat`` lifts the sticking, for the **pull only**. The
        catalog sweep is the one caller that has just been told what the
        server itself believes, so ``played=False`` from it is information
        rather than the absence of it -- and without this an item un-watched
        on another device stayed watched here forever, which is also what
        made it reapable forever. docs/offline-sync.md section 1.

        **The retreat is refused while the queue still owes this actor a
        watched advance** -- a `pending_playstate` row with `played = 1`.
        That entry is a mark this machine made and has not delivered, so the
        server's "unwatched" predates it and clearing on it would delete the
        user's own viewing. Not "any pending row": `played` is nullable and
        ordinary offline progress queues position-only entries, which say
        nothing about an unsent mark and must not block the retreat.

        The look and the clear are one critical section rather than two
        calls, because playback can create local state and its queue row on
        either side of a separate look. ``_lock`` is what makes that true:
        `upsert_playstate` is the only other writer of that table and takes
        the same lock.

        Per actor because watched state is a property of a person and this
        store is shared by a machine. Before it was, two accounts shared one
        resume position, and the second one to press Resume told its own
        server it had watched what the first watched.

        Returns whether anything actually moved, which the userdata refresh
        in ``manager`` counts: a pull that changed nothing must not tell the
        browser to redraw.
        """
        with self._lock:
            if self._conn is None or self.read_only:
                return False
            key = self._file_under(item_id, actor)
            if key is None:
                return False
            server_id, user_id = key
            row = self._conn.execute('SELECT runtime_ticks FROM downloads WHERE item_id=?', (item_id,)).fetchone()
            if row is None:
                return False
            runtime = row['runtime_ticks'] or 0
            cur = self._conn.execute('SELECT played, position_ticks FROM item_userdata WHERE item_id=? AND server_id=? AND user_id=?', (item_id, server_id, user_id)).fetchone()
            was_played = bool(cur and cur['played'])
            was_pos = (cur['position_ticks'] if cur else 0) or 0
            fields, changed = ({}, False)
            if allow_retreat and played is not None and (not played) and was_played and (not self._owes_a_watched_mark(item_id, server_id, user_id)):
                fields['played'] = 0
                changed = True
                was_played = False
            if played and allow_retreat and self._owes_an_unwatch(item_id, server_id, user_id):
                played = None
            if played:
                if not was_played:
                    fields['played'] = 1
                    changed = True
                if was_pos:
                    fields['position_ticks'] = 0
                    changed = True
            elif position_ticks is not None:
                near_end = runtime and (position_ticks >= runtime * 0.95 or runtime - position_ticks <= 10 * 10000000)
                if was_played and near_end:
                    pass
                elif position_ticks > was_pos:
                    fields['position_ticks'] = position_ticks
                    changed = True
            if changed:
                try:
                    self._write_userdata(item_id, (server_id, user_id), **fields)
                    self._conn.commit()
                except sqlite3.Error:
                    self._conn.rollback()
                    raise
            return changed

    def watched_targets(self, item_id, *, server_id=ANY_SERVER):
        """Which downloaded rows a watched mark for ``item_id`` covers, and
        **which server each of them belongs to**.

        An id from the library is not always a leaf: a tick on a series or a
        season means every episode under it, as the server does with the same
        request (``Folder.MarkPlayed`` fans out). The catalog holds leaves only,
        so the fan-out is a scan of it -- there is nobody to ask with the server
        away, which is the point of holding the rows locally.

        ``server_id`` is a content scope like every other read here, through
        the one clause, so a mark made on server B cannot reach server A's
        row. ``ANY_SERVER`` asks unscoped, which is what the downloads screen
        does: it browses a pseudo-server, there is no second server to confuse
        a row with, and the catalog is the only source of items there is.

        The second member of each pair is the row's **content** server, so a
        caller resolving who a mark belongs to has the row's side of that
        question without inferring it. It was the saved login for a while,
        and a login on a row names whoever *downloaded* the copy -- on a
        shared machine, somebody else.

        Answers with nothing for an item we hold no copy of, which is what lets
        callers stay unconditional. See docs/offline-sync.md section 1.
        """
        if not item_id:
            return []
        clause, params = self._content_clause(server_id, (STATUS_COMPLETE, item_id, item_id, item_id))
        return [(row['item_id'], row['content_server_id']) for row in self._query('SELECT item_id, content_server_id FROM downloads WHERE status=? AND (item_id=? OR series_id=? OR season_id=?)' + clause + ' ORDER BY series_name, parent_index, index_number, name', params)]

    def set_watched(self, item_id, played, *, actor):
        """Store **one actor's** deliberate watched mark, verbatim, both ways.

        The one non-advancing writer of playback state, and the reason it is
        a separate method rather than a flag on ``update_userdata``: every
        caller of that one is playback, where a value that went backwards is
        a stale report rather than a change of mind.

        Before this existed every writer was advance-only, so an item
        un-watched from this app's own menu stayed watched on disk forever.
        What the server's own MarkPlayed/ResetPlayedState write is in
        docs/jellyfin-api-notes.md section 10; all six local writers and
        their directions are in docs/offline-sync.md section 1.
        """
        with self._lock:
            if self._conn is None or self.read_only:
                return False
            key = self._file_under(item_id, actor)
            if key is None:
                return False
            server_id, user_id = key
            cur = self._conn.execute('SELECT played, position_ticks, play_count FROM item_userdata WHERE item_id=? AND server_id=? AND user_id=?', (item_id, server_id, user_id)).fetchone()
            if played:
                after = (1, 0, max((cur['play_count'] if cur else 0) or 0, 1))
            else:
                after = (0, 0, 0)
            before = (cur['played'] or 0, cur['position_ticks'] or 0, cur['play_count'] or 0) if cur else None
            if before == after:
                return False
            try:
                self._conn.execute('INSERT INTO item_userdata (item_id, server_id, user_id, played, position_ticks, play_count, last_played_date, updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(item_id, server_id, user_id) DO UPDATE SET played=excluded.played, position_ticks=excluded.position_ticks, play_count=excluded.play_count, last_played_date=CASE WHEN excluded.played=0 THEN NULL ELSE item_userdata.last_played_date END, updated_at=excluded.updated_at', (item_id, server_id, user_id) + after + (None, int(time.time())))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
            return True

    def set_reading_position(self, item_id, position_ticks, *, actor):
        """Store **one actor's** reader cursor, verbatim, not advance-only.

        ``update_userdata`` is the playback rule: a position only ever moves
        forward, because a progress report that went backwards is a stop
        report or a stale one. A book is not that. Turning back a chapter is
        an ordinary thing to do, the number is a cursor rather than a
        high-water mark, and the server agrees -- a `Book` is excluded from
        both arms of ``UserDataManager.UpdatePlayState`` and stores what it
        is given. Kept apart from ``update_userdata`` rather than added to it
        as a flag, because every caller of that one is playback and none of
        them wants this.

        Local only. What is *sent* to the server on reconnect stays
        advance-only (``upsert_playstate``), so a client that has been
        offline cannot rewind the place another device reached.
        """
        with self._lock:
            if self._conn is None or self.read_only:
                return
            key = self._file_under(item_id, actor)
            if key is None:
                return
            server_id, user_id = key
            cur = self._conn.execute('SELECT position_ticks FROM item_userdata WHERE item_id=? AND server_id=? AND user_id=?', (item_id, server_id, user_id)).fetchone()
            if cur is not None and cur['position_ticks'] == position_ticks:
                return
            try:
                self._conn.execute('INSERT INTO item_userdata (item_id, server_id, user_id, position_ticks, updated_at) VALUES (?,?,?,?,?) ON CONFLICT(item_id, server_id, user_id) DO UPDATE SET position_ticks=excluded.position_ticks, updated_at=excluded.updated_at', (item_id, server_id, user_id, position_ticks, int(time.time())))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def drop_playstate(self, item_id, actor):
        """Discard what one person had queued for one item, unsent.

        For a deliberate mark made online: it is newer than everything the
        entry holds, position included, so replaying the entry could only
        undo it (item 13, docs/offline-sync.md section 3a)."""
        server_id, user_id = actor
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute('DELETE FROM pending_playstate WHERE item_id=? AND server_id=? AND user_id=?', (item_id, server_id or '', user_id or ''))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def clear_playstate(self, entries):
        """Retire replayed rows -- but only those still holding the values
        that were actually sent.

        Acknowledged **by value, not by id**. There is one row per item and
        `upsert_playstate` advances it in place, so a report landing while the
        replay is on the network leaves the same id holding data nobody has
        uploaded. Deleting by id alone discarded exactly that: the newer
        position, and a final watched mark, neither of which the server ever
        heard. A row that moved on simply stays pending and goes out on the
        next sweep.

        ``entries`` is an iterable of ``(id, position_ticks, played)`` as they
        were read. ``IS`` rather than ``=`` because both columns are nullable
        and ``NULL = NULL`` is NULL in SQL, which would match nothing and
        leave the queue undrainable.
        """
        entries = [tuple(e) for e in entries or ()]
        if not entries:
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.executemany('DELETE FROM pending_playstate WHERE id=? AND position_ticks IS ? AND played IS ?', entries)
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def _query(self, sql, params=(), strict=False):
        with self._lock:
            if self._conn is None:
                if strict:
                    raise sqlite3.ProgrammingError('the catalog is closed')
                return []
            try:
                return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
            except sqlite3.Error:
                log.warning('Catalog query failed: %s', sql, exc_info=True)
                if strict:
                    raise
                return []

    def library_id(self, lookup, *, server_id):
        """The recorded CollectionFolder for an item id **or a series id**,
        or None.

        Two lookups because the caller has whichever it has: the shader
        scope asks about a series, and a film has only its own id. Both
        columns are indexed (``item_id`` is the primary key,
        ``idx_downloads_series``), so this is two point queries.

        ``server_id`` is the Jellyfin ``ServerId`` -- the CONTENT key, not a
        saved login. Scoping is not decoration: Jellyfin derives an item id
        from the media's path, so two servers over one library hand out the
        *same* ids, and the series fallback would otherwise answer with a
        library that exists only on the other one. Two logins on one server
        get one answer, which is the whole point of the key.

        Keyword-only, and required. It used to take a set of saved logins in
        the same position; same arity, different meaning, so a call site
        that was not revisited would have scoped on the wrong key silently.

        **A falsy value matches no rows**, via `_content_clause`. It reads as
        a floor that was removed and is not: the two cases once cited as
        needing it -- a removed-but-still-downloaded server, and the
        fully-offline browser -- both carry a ``ServerId`` and are answered
        *scoped*. A removed server's rows keep the ``content_server_id`` they
        were written with, and the offline browser's items come from that same
        ``item_json``. So the unscoped floor was justified by two cases that
        never reach it, and the only thing it served was a caller with no
        scope to name -- which now says ``ANY_SERVER`` out loud.
        Ruled 2026-09-19.
        """
        if not lookup:
            return None
        scope, params = self._content_clause(server_id, first=(lookup,))
        with self._lock:
            if self._conn is None:
                return None
            try:
                for sql in ('SELECT library_id FROM downloads WHERE item_id = ?' + scope, 'SELECT library_id FROM downloads WHERE series_id = ? AND library_id IS NOT NULL' + scope + ' LIMIT 1'):
                    row = self._conn.execute(sql, params).fetchone()
                    if row and row[0]:
                        return row[0]
            except sqlite3.Error:
                log.debug('could not read library_id', exc_info=True)
        return None

    def get(self, item_id, *, server_id=ANY_SERVER):
        """One download row by item id, optionally scoped to a content server.

        **Item ids collide across servers** -- Jellyfin derives one from the
        media's path (docs/jellyfin-api-notes.md 13b) -- so a primary-key read
        can answer with a row another server's copy wrote. Unscoped by default
        because that is what almost every caller wants: the ~13 readers inside
        `sync/` already hold the row's identity and are asking about *this*
        file on disk.

        The caller that is not asking that is the browser's
        `book_download_state`, where the question really is a server's: a
        colliding id on server B would open server A's downloaded file, and a
        book's downloaded copy is the only way to read it at all. That caller
        takes its scope **without a default**, so no site can ask the
        permissive question by omission.

        Scoped through `_content_clause`, like every other content read: a
        NULL `content_server_id` answers (it cannot be shown to be somebody
        else's), `ANY_SERVER` is unscoped, and a falsy scope matches nothing.
        """
        clause, params = self._content_clause(server_id, (item_id,))
        rows = self._query('SELECT * FROM downloads WHERE item_id=?' + clause, params)
        return rows[0] if rows else None

    def list(self, status=None, series_id=None, strict=False, *, server_id=ANY_SERVER):
        """Rows, optionally narrowed by status, series and content server.

        ``server_id`` is the Jellyfin ``ServerId`` and goes through
        ``_content_clause`` like every other content read -- keyword-only, and
        **falsy matches no rows**: it means a login that resolves to no server.
        The unscoped ask is ``ANY_SERVER``, which is this parameter's default,
        so a caller predating the scope still gets every row. It exists so a
        caller asking
        a content question does not filter the answer itself in Python: this
        file has already carried two different NULL policies at once, and a
        second copy of that policy is how it happened.
        """
        sql = 'SELECT * FROM downloads'
        clauses, params = ([], [])
        if status is not None:
            clauses.append('status=?')
            params.append(status)
        if series_id is not None:
            clauses.append('series_id=?')
            params.append(series_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        scope, params = self._content_clause(server_id, tuple(params))
        if scope and (not clauses):
            sql += ' WHERE 1=1'
        sql += scope
        if status == STATUS_PENDING:
            sql += ' ORDER BY added_at, rowid'
        else:
            sql += ' ORDER BY series_name, parent_index, index_number, name'
        return self._query(sql, tuple(params), strict=strict)

    @staticmethod
    def _content_clause(server_id, first=()):
        """SQL scoping a downloads query to one Jellyfin server, and params.

        **One clause for every content read**, because the recurring defect
        in this repo is a right rule applied at N-1 of N sites -- and this
        file has already carried two different NULL policies at once.

**A NULL ``content_server_id`` is no longer included**, and the reason
        it was is now served better elsewhere. Such a row's manifest could not
        be read, so nothing can show whose content it is -- and answering a
        *named* server with it is how one server's tile ticks for another's
        film, and how the wrong local copy substitutes for a stream. What it
        used to buy was that the row stayed visible and deletable, and that is
        real: it is why this branch existed. It is now bought without the
        permissive read -- the offline library lists such rows under
        **Orphaned Items** ([iw]'s ruling), and every unscoped caller (the
        downloads manager, the offline browse, the reaper) asks with
        ``ANY_SERVER`` and still sees them.

        ``ANY_SERVER`` runs unscoped, and is the only thing that does.
        **``None`` matches nothing**, because the one way to arrive here with
        it is `content_id_for` failing to resolve a login that was named --
        and answering every server's rows to a question asked about a login
        we do not have is the wrong direction to be permissive in. See
        `ANY_SERVER`.
        """
        if server_id is ANY_SERVER:
            return ('', tuple(first))
        if not server_id:
            return (' AND 0', tuple(first))
        return (' AND content_server_id=?', tuple(first) + (server_id,))

    def downloaded_item_ids(self, *, server_id):
        clause, params = self._content_clause(server_id, (STATUS_COMPLETE,))
        return {r['item_id'] for r in self._query('SELECT item_id FROM downloads WHERE status=?' + clause, params)}

    def downloaded_series_ids(self, *, server_id):
        clause, params = self._content_clause(server_id, (STATUS_COMPLETE,))
        return {r['series_id'] for r in self._query('SELECT DISTINCT series_id FROM downloads WHERE status=? AND series_id IS NOT NULL' + clause, params)}

    def downloaded_season_ids(self, *, server_id):
        """Seasons with at least one completed episode.

        A Season is never itself a downloads row — manager.download expands it
        into its episodes — so without this a fully downloaded season could
        never read as downloaded anywhere in the UI.
        """
        clause, params = self._content_clause(server_id, (STATUS_COMPLETE,))
        return {r['season_id'] for r in self._query('SELECT DISTINCT season_id FROM downloads WHERE status=? AND season_id IS NOT NULL' + clause, params)}

    def total_size(self):
        rows = self._query('SELECT COALESCE(SUM(downloaded_bytes),0) AS s FROM downloads')
        return rows[0]['s'] if rows else 0

    def auto_size(self):
        """Bytes held by auto-downloads alone.

        The cap deliberately measures only these: it is a budget for what the
        app decided to fetch on its own, not a ceiling on the library the user
        built by hand. Sizing the cap against everything would make one large
        manual download switch auto-download off.
        """
        rows = self._query('SELECT COALESCE(SUM(downloaded_bytes),0) AS s FROM downloads WHERE origin GLOB ?', (AUTO_PREFIX + '*',))
        return rows[0]['s'] if rows else 0

    def list_auto_incomplete(self):
        """Auto rows stuck in ERROR. They keep their partial bytes (counted
        by auto_size), so nothing else would ever reclaim them.

        Deleting one also destroys the only record that we tried it, which is
        what auto_discarded exists to survive — see
        SyncManager._record_permanent_failure."""
        return self._query('SELECT * FROM downloads WHERE origin GLOB ? AND status=?', (AUTO_PREFIX + '*', STATUS_ERROR))

    def list_auto(self, status=STATUS_COMPLETE):
        """Auto-downloads, oldest completion first — the reaper's eviction
        order. completed_at is NULL for rows finished before it existed, and
        COALESCE falls back to added_at so those sort sensibly rather than
        all landing at the front."""
        return self._query('SELECT * FROM downloads WHERE origin GLOB ? AND status=? ORDER BY COALESCE(completed_at, added_at, 0), rowid', (AUTO_PREFIX + '*', status))

    def mark_discarded(self, item_id, *, server_id):
        """Remember that the scheduler is done with this item, so the planner
        does not immediately fetch it again. See the auto_discarded tables.

        ``server_id`` is the Jellyfin ``ServerId`` and is keyword-only and
        required, for the reason `is_complete` gives: the unscoped spelling
        is still meaningful (it suppresses everywhere), so a caller that
        forgot to revisit would have gone on writing the broad tombstone
        silently. Pass ``None`` to mean it deliberately -- which is right
        when the row's own content server is unknown.

        **A discard that cannot name a server records nothing.** It used to
        write a broad tombstone -- "we cannot say who it was about, so suppress
        it everywhere" -- and that is the bug the scoped table exists for: it
        hid a *different* server's film of the same id from the scheduler
        forever. There is no longer a table to write it to.

        What that costs is bounded and worth stating, because "records nothing"
        reads like the tombstone loop coming back: the planner only ever asks
        on behalf of a **connected** server, and a candidate it finds there
        names that server, so the fetch it is not suppressed from produces a
        row that *does* name one -- and the next discard is scoped and sticks.
        One redundant fetch, not a cycle. Reachable only for a row whose own
        server is unknown, which is an adopted orphan.
        """
        if server_id is ANY_SERVER or not server_id:
            log.debug('Not recording a discard for %s: its row names no server, and a tombstone that cannot name one binds every server forever.', item_id)
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute('INSERT OR REPLACE INTO auto_discarded_scoped VALUES (?, ?, ?)', (item_id, server_id, int(time.time())))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def clear_discarded(self, item_id, *, server_id):
        """Forget a tombstone — the user asked for this item by hand, which
        overrides the reaper's decision.

        Clears **this server's** tombstone. Another server's stays: the user
        asked for *this* item from *this* server, and said nothing about the
        different film that shares its id.

        **The exact mirror of `mark_discarded`**, which is the point, and the
        mirror is what changed here: that no longer writes an unscoped row, so
        there is none to clear. This docstring used to promise it cleared "any
        unscoped one" as well, and that had already stopped being the whole
        truth once the scoped table arrived -- it is now simply gone, and a
        catalog carrying legacy broad rows has had them scoped or dropped by
        `_migrate_discard_scopes` before any of this runs.

        A falsy ``server_id`` clears nothing, for the same reason its mirror
        writes nothing. Keyword-only and required so that is asked for out
        loud rather than arrived at by leaving an argument off.
        """
        if server_id is ANY_SERVER or not server_id:
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute('DELETE FROM auto_discarded_scoped WHERE item_id=? AND server_id=?', (item_id, server_id))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def discarded_ids(self, *, server_id):
        """Item ids the scheduler has given up on **for this server**.

        This server's own tombstones, and **only** those. Keyword-only and
        required, as `mark_discarded`.

        There used to be a second, unscoped table whose rows were in *every*
        answer, which is what "binds everybody" meant -- and is what hid one
        server's film behind another's giving-up. The three scopes now differ
        only in how much of the one table they take: ``ANY_SERVER`` all of it,
        a ``ServerId`` that server's rows, and ``None`` -- a login that
        resolves to no server -- nothing. Before `ANY_SERVER` existed the first
        and the third were one spelling, and the planner asking on behalf of an
        unknown login inherited every other server's giving-up.
        """
        if server_id is ANY_SERVER:
            return {r['item_id'] for r in self._query('SELECT item_id FROM auto_discarded_scoped')}
        if not server_id:
            return set()
        return {r['item_id'] for r in self._query('SELECT item_id FROM auto_discarded_scoped WHERE server_id=?', (server_id,))}

    def set_origin(self, item_id, origin):
        """Promote an auto-download to user-owned (see ORIGIN_*). Only ever
        called in that direction."""
        self.update(item_id, origin=origin)

    def is_complete(self, item_id, *, server_id):
        """Do we hold a finished copy of ``item_id`` **for this server**?

        ``server_id`` is the Jellyfin ``ServerId``. The scope is required
        because item ids are not unique across servers: Jellyfin derives one
        from the media's path with no server component in it, so two
        installs both mounting their library at `/media` hand out the same
        id for *different files* (docs/jellyfin-api-notes.md 13b).
        Unscoped, this answered "yes, we have it" for a row belonging to
        somebody else, and the caller went on to play that file.

        Scoped on the *server*, not the login: two accounts on one box hold
        one copy between them and must get one answer. Keyword-only for the
        reason in ``library_id``.

        ``ANY_SERVER`` asks unscoped, which is right fully offline: there is
        no second server to confuse a row with, and the catalog is the only
        source of items there is. ``None`` means the login named does not
        resolve, and answers False.

        **A row whose own server is unknown answers only the unscoped ask.**
        It used to answer every named one -- so an orphan could substitute for
        a stream on any server at all, which is the direction this whole scope
        exists to refuse. Offline it still plays: that path asks with
        ``ANY_SERVER``. See `_in_scope`, which this shares its rule with.
        """
        row = self.get(item_id)
        if not row or row['status'] != STATUS_COMPLETE:
            return False
        return self._in_scope(row['content_server_id'], server_id)

    @staticmethod
    def _in_scope(row_server_id, server_id):
        """`_content_clause`, for a row already in hand rather than a query.

        **Two forms of one rule, and neither may be edited alone.** They are
        adjacent for that reason: the clause narrows a SELECT, this tests a
        row the caller already has, and they must agree about all three
        scopes. `is_complete` carried its own copy of the old two-way
        version, which is why `ANY_SERVER` read as "a server named
        ANY_SERVER" there and answered False for every row.

        A row with no server recorded answers **only** the unscoped ask, as
        in the clause: nothing can show whose content it is, so it is not an
        answer to a named server. It stays reachable through every unscoped
        caller, and the offline library gives it a home of its own -- see the
        clause for where that ruling is written down.
        """
        if server_id is ANY_SERVER:
            return True
        if not server_id:
            return False
        return row_server_id == server_id

    def owner_of(self, item_id):
        """Which Jellyfin server holds this row, or None if we hold no row.

        For telling a refusal from an absence: an enqueue that cannot have
        an item because another server already claimed the id is a different
        thing from one that simply has not run. The server rather than the
        login, so a second account on the owning server is not refused a
        film its own machine is holding.
        """
        row = self.get(item_id)
        return row['content_server_id'] if row else None

    def list_playstate(self):
        return self._query('SELECT * FROM pending_playstate ORDER BY created_at')

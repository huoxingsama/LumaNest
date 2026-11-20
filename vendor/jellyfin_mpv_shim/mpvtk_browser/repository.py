"""Browse-only data access for the library browser.

``LibrarySource`` is the seam the UI depends on. Today it is backed by live
Jellyfin connections; a future offline build can provide an object with the same
method surface backed by the local sync catalog without touching the views.

Every method returns plain Jellyfin item DTO dicts (or lists of them) so the
same shapes work whether they came from the server or a local cache.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import datetime
import json
import logging
from urllib.parse import urlparse
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from jellyfin_apiclient_python import JellyfinClient
from .. import items_api
from ..books import AUDIOBOOK_TYPE, BOOK_TYPE
from ..constants import offline_playlist_id, split_offline_playlist_id, USER_APP_NAME, CLIENT_VERSION, USER_AGENT, OFFLINE_SERVER_UUID
from ..i18n import _
from ..sync.db import NO_ACTOR, STORE_DIR, SyncDB, STATUS_COMPLETE, filing_state, playlist_art_dir, season_art_dir, series_art_dir
from . import home_sections
from . import live_tv
from . import user_prefs
from . import view_prefs

def _iso_utc(when):
    """An aware datetime as the ISO 8601 the Live TV endpoints require.

    With the ``Z``, always: the server binds these dates with
    ``AdjustToUniversal``, which accepts an offset-less string *without*
    shifting it — so a bare ``str(datetime)`` queries a window that is out by
    the local UTC offset and answers successfully with the wrong programmes.
    """
    return when.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
log = logging.getLogger('mpvtk_browser.repository')
LIST_FIELDS = 'PrimaryImageAspectRatio,Overview,MediaSourceCount,CanDelete'
_LANDSCAPE_ART = 0.8
GRID_FIELDS = 'PrimaryImageAspectRatio,MediaSourceCount,CanDelete'
BOOKS_GRID_FIELDS = GRID_FIELDS + ',Overview'
LIBRARY_ITEM_TYPES = {'movies': 'Movie', 'tvshows': 'Series', 'music': 'MusicAlbum', 'musicvideos': 'MusicVideo', 'boxsets': 'BoxSet'}
BROWSE_IMAGE_TYPES = 'Primary,Thumb,Backdrop'
GRID_IMAGE_TYPES = ('Banner', 'Logo', 'Disc')
_WORDMARK_CHAIN = {'Banner': ('Banner', 'Logo'), 'Logo': ('Logo', 'Banner'), 'Disc': ('Disc',)}

def browse_image_types(image_type=None):
    """``EnableImageTypes`` for a grid drawn with ``image_type``.

    The base three plus everything that view can *resolve* to -- the whole
    wordmark chain, not just the name asked for, or the Banner fallback to a
    logo would be looking for a tag the query told the server to leave out.
    The base set stays because our fallback chains are wider than web's: a
    view set to Banner still draws the item's Primary where it has neither.
    """
    extra = (image_type or '').strip().capitalize()
    chain = _WORDMARK_CHAIN.get(extra)
    if not chain:
        return BROWSE_IMAGE_TYPES
    return BROWSE_IMAGE_TYPES + ',' + ','.join(chain)
HOME_FANOUT = 8
OFFLINE_ROW_KIND = 'downloaded'
MUSIC_FIELDS = 'PrimaryImageAspectRatio,ItemCounts,CanDelete'
DETAIL_FIELDS = 'Path,Overview,Genres,Studios,People,Taglines,SortName,MediaSources,MediaStreams,Chapters,ProviderIds,PrimaryImageAspectRatio,DateCreated,CanDelete,SpecialFeatureCount,LocalTrailerCount,RemoteTrailers'
EXCLUDED_COLLECTION_TYPES: set = set()
BOOKS_COLLECTION = 'books'
OFFLINE_ROW_SLOTS = ('movies', 'homevideos', 'tvshows', BOOKS_COLLECTION)
LIVE_TV_COLLECTION = 'livetv'
PLAYABLE_TYPES = {'Movie', 'Episode', 'Video', 'MusicVideo', 'Recording'}
PHOTO_TYPE = 'Photo'
SERIES_TYPES = {'Series'}
LIVE_TYPES = {'Program', 'TvChannel'}
FOLDER_TYPES = {'CollectionFolder', 'Folder', 'BoxSet', 'Season', 'UserView', 'PhotoAlbum'}
PLAYLIST_SUPPORTED_TYPES = {'Movie', 'Episode', 'Video', 'Audio', 'AudioBook'}
QUEUEABLE_MEDIA = frozenset({'Video', 'Audio', 'Photo'})
QUEUEABLE_MEDIA_PARAM = ','.join(sorted(QUEUEABLE_MEDIA))
QUEUE_LIMIT = 300
SEARCH_LIMIT = 800
SEARCH_TYPES = 'Movie,Series,Episode,Video,MusicAlbum,Audio,AudioBook,Book'
PEOPLE_SEARCH_LIMIT = 100
ARTIST_SEARCH_LIMIT = 100
PROGRAM_FIELDS = ',ChannelInfo,ChannelImage'
CHANNEL_PAGE = 100
CHANNEL_LISTING = 1000

class ServerConn:
    """A single browse-only connection to one Jellyfin server."""

    def __init__(self, info: dict, device_id: str, player_name: str, verify_ssl: bool):
        self.uuid = info['uuid']
        self.name = info.get('name') or info.get('address')
        self.address = info['address'].rstrip('/')
        self.user_id = info['user_id']
        self.token = info['token']
        client = JellyfinClient(allow_multiple_clients=True)
        client.config.app(USER_APP_NAME, CLIENT_VERSION, player_name, device_id)
        client.config.data['http.user_agent'] = USER_AGENT
        client.config.auth(self.address, self.user_id, self.token, verify_ssl)
        client.logged_in = True
        client.start(websocket=False)
        self.client = client
        self.api = client.jellyfin

    def stop(self):
        try:
            self.client.stop()
        except Exception:
            log.debug('Error stopping browse client', exc_info=True)
STATUS_FILTERS = (('unplayed', 'IsUnplayed'), ('played', 'IsPlayed'), ('resumable', 'IsResumable'))
FEATURE_FILTERS = (('has_subtitles', 'HasSubtitles'), ('has_trailer', 'HasTrailer'), ('has_special_feature', 'HasSpecialFeature'), ('has_theme_song', 'HasThemeSong'), ('has_theme_video', 'HasThemeVideo'))
VIDEO_FLAG_FILTERS = (('is_4k', 'Is4K'), ('is_3d', 'Is3D'))
VIDEO_TYPE_FILTERS = (('vt_dvd', 'Dvd'), ('vt_bluray', 'BluRay'), ('vt_iso', 'Iso'))
LIST_FILTERS = (('official_ratings', 'OfficialRatings'), ('tags', 'Tags'), ('audio_languages', 'AudioLanguages'), ('subtitle_languages', 'SubtitleLanguages'), ('studio_ids', 'StudioIds'))
SUPPORTED_FILTERS = frozenset([key for key, _param in STATUS_FILTERS] + [key for key, _param in FEATURE_FILTERS] + [key for key, _param in VIDEO_FLAG_FILTERS] + [key for key, _param in VIDEO_TYPE_FILTERS] + [key for key, _param in LIST_FILTERS] + ['favorite', 'genre', 'year', 'hd', 'sd', 'letter'])

class LibrarySource:
    """Live, multi-server browse data source.

    The UI is given one of these and addresses servers by ``uuid``. Methods
    raise on network errors; callers run them off the UI thread and surface
    failures in the view.
    """
    supported_filters = SUPPORTED_FILTERS

    def __init__(self, servers, device_id: str, player_name: str, verify_ssl: bool):
        self._conns = {}
        self._order = []
        self._home_prefs: dict[str, Any] = {}
        self._live_tv_prefs: dict[str, Any] = {}
        self._user_prefs: dict[str, Any] = {}
        self._custom_prefs: dict[str, Any] = {}
        self._has_live_tv: dict[str, bool] = {}
        for info in servers:
            try:
                conn = ServerConn(info, device_id, player_name, verify_ssl)
            except Exception:
                log.error('Failed to connect browse client for %s', info.get('name'), exc_info=True)
                continue
            self._conns[conn.uuid] = conn
            self._order.append(conn.uuid)

    def servers(self):
        return [{'uuid': uuid, 'name': self._conns[uuid].name, 'address': self._conns[uuid].address} for uuid in self._order]

    def auth_origins(self):
        """``{(scheme, host, port): authorization-header}`` for every server
        this source is connected to.

        For the thumbnail store, which fetches image URLs on its own session
        and would otherwise have to carry the token in the query string. The
        header is the apiclient's own -- the non-legacy MediaBrowser scheme
        -- so there is one spelling of it in the app.
        """
        out = {}
        for conn in self._conns.values():
            try:
                client = conn.client
                base = client.config.data.get('auth.server') or ''
                header = client.http._get_authenication_header()
            except Exception:
                log.debug('no auth header for a browse connection', exc_info=True)
                continue
            if not header or 'Token=' not in header:
                continue
            parts = urlparse(base)
            if parts.hostname:
                out[parts.scheme, parts.hostname, parts.port] = header
        return out

    def _conn(self, server_uuid) -> ServerConn:
        return self._conns[server_uuid]

    def server_address(self, server_uuid):
        """Base url of one connected server, or None.

        For composing a link *out* of the app — the jellyfin-web page for an
        item (#714). `.get`, like image_url: a rebuilt source can have
        dropped this server while a screen keyed to it is still up, and the
        honest answer then is "no link", not a KeyError on the render thread.
        """
        conn = self._conns.get(server_uuid)
        return conn.address if conn is not None else None

    def stop(self):
        for conn in self._conns.values():
            conn.stop()

    def get_libraries(self, server_uuid):
        api = self._conn(server_uuid).api
        result = api.get_views() or {}
        out = []
        has_live_tv = False
        for item in result.get('Items', []):
            if item.get('CollectionType') in EXCLUDED_COLLECTION_TYPES:
                continue
            has_live_tv = has_live_tv or item.get('CollectionType') == LIVE_TV_COLLECTION
            out.append(item)
        self._has_live_tv[server_uuid] = has_live_tv
        return out

    def has_live_tv(self, server_uuid):
        """Whether this server offers Live TV, per the last get_libraries.

        Defaults to False when views have not been read yet: the cost of
        being wrong in that direction is one missing row until the next home
        load, against a pointless request on every home load for the large
        majority of users who have no tuner.

        getattr rather than a bare attribute read, because a LibrarySource
        built without __init__ (as the home-row tests do) must still answer
        "no Live TV" rather than raise from inside the fan-out.
        """
        return (getattr(self, '_has_live_tv', None) or {}).get(server_uuid, False)

    def syncplay_access(self, server_uuid):
        from ..user_policy import syncplay_access
        try:
            return syncplay_access(self._conn(server_uuid).client)
        except Exception:
            from ..user_policy import CREATE_AND_JOIN
            return CREATE_AND_JOIN

    def can_manage_live_tv(self, server_uuid):
        from ..user_policy import may_manage_live_tv
        try:
            return may_manage_live_tv(self._conn(server_uuid).client)
        except Exception:
            return True

    def can_manage_collections(self, server_uuid):
        from ..user_policy import may_manage_collections
        try:
            return may_manage_collections(self._conn(server_uuid).client)
        except Exception:
            return True

    def can_refresh_metadata(self, server_uuid):
        """`IsAdministrator` -- may this user ask the server to re-scan?

        The **first admin-gated action in this browser**, and the one place
        the fail-open rule can show a button the server refuses. See
        `user_policy.may_refresh_metadata` and
        `docs/PERMISSION_GAPS.md`.
        """
        from ..user_policy import may_refresh_metadata
        try:
            return may_refresh_metadata(self._conn(server_uuid).client)
        except Exception:
            return True

    def can_download(self, server_uuid):
        """`EnableContentDownloading`.

        Read here for books in particular. Every other download is an
        offline convenience -- without the permission you simply watch the
        thing online -- but ``/Items/{id}/Download`` is the *only* path to a
        book's bytes, so a user without it cannot open a book at all. That
        deserves to be said, rather than surfacing as a download that always
        fails. Fails open like its siblings: only an answer the server gave
        closes the gate.
        """
        from ..user_policy import may_download
        try:
            return may_download(self._conn(server_uuid).client)
        except Exception:
            return True
    _PREFS_LOCK_GUARD = threading.Lock()
    _PREFS_LOCKS: dict = {}

    def _display_prefs_lock(self, server_uuid):
        """The lock serialising this server's DisplayPreferences writes.

        There is no partial-update path on this API, so all four writers here
        -- home layout, user prefs, Live TV prefs, per-view settings -- GET
        the whole document, change their own slice of CustomPrefs and POST it
        back. They run on the shared four-worker AsyncRunner and
        ``settings/home.py`` puts two of them on one tab, so two were
        routinely in flight: both read the same document, each posted its own
        copy, and the later POST silently reverted the earlier change while
        both calls reported success. The loss showed up on the next refresh,
        on restart, or in jellyfin-web, which reads the same document.

        Per server, because that is the scope of the document. Held across
        the whole GET/change/POST -- serialising only the POST would leave
        exactly the read-modify-write window that is the bug.

        The in-tree model is ``gateway/editing.py:playlist_move_many``, which
        hit this by fanning per-move tasks onto this pool and fixed it by
        running the sequence in order; ``users.py:save`` is the local-document
        equivalent.

        **Class-level, not per instance.** The document belongs to the
        server, not to whichever ``LibrarySource`` happens to be holding a
        connection to it -- and a reconnect builds a NEW source while
        asynchronous work can still be holding a writer bound to the old one
        (``ui.py`` swaps the source on every reconnect). Two instances with
        two locks is the same lost update this exists to prevent, just harder
        to see.

        Lazily built rather than set in ``__init__`` so every construction
        path shares it, including the subclasses and the tests that build a
        source with ``__new__``.
        """
        with LibrarySource._PREFS_LOCK_GUARD:
            lock = LibrarySource._PREFS_LOCKS.get(server_uuid)
            if lock is None:
                lock = LibrarySource._PREFS_LOCKS[server_uuid] = threading.Lock()
            return lock

    def _display_prefs_dto(self, api):
        """The raw DisplayPreferencesDto. There is no partial-update path on
        this API, so a save has to GET the whole document, mutate CustomPrefs
        and POST it back — dropping fields we do not understand would clobber
        jellyfin-web's other settings (landing screens, tvhome, ...)."""
        return api.get_user_settings(client=home_sections.DISPLAY_PREFS_CLIENT) or {}

    def get_home_prefs(self, server_uuid, refresh=False):
        """(layout, latest_excludes) for a server, cached.

        ``layout`` is the ordered list of section types; ``latest_excludes``
        is the set of library ids the user unchecked under "Display in home
        screen sections", which we must apply ourselves for the Recently Added
        rows (see get_home_rows).

        Never raises: an unreachable or ancient server falls back to the
        default layout with nothing excluded, because a home screen with the
        stock rows beats no home screen at all.
        """
        if not refresh and server_uuid in self._home_prefs:
            return self._home_prefs[server_uuid]
        api = self._conn(server_uuid).api
        try:
            prefs = self._display_prefs_dto(api).get('CustomPrefs') or {}
            layout = home_sections.resolve_layout(prefs)
        except Exception:
            log.warning('Failed to read home layout; using defaults', exc_info=True)
            layout = list(home_sections.DEFAULT_LAYOUT)
        try:
            excludes = self.get_latest_excludes(server_uuid)
        except Exception:
            log.warning('Failed to read library home-screen exclusions', exc_info=True)
            excludes = frozenset()
        self._home_prefs[server_uuid] = (layout, excludes)
        return (layout, excludes)

    def save_home_layout(self, server_uuid, layout):
        """Persist the section layout back to the server. Raises on failure —
        the settings screen reports it rather than pretending it saved."""
        api = self._conn(server_uuid).api
        with self._display_prefs_lock(server_uuid):
            dto = self._display_prefs_dto(api)
            custom = dict(dto.get('CustomPrefs') or {})
            custom.update(home_sections.layout_to_prefs(layout))
            dto['CustomPrefs'] = custom
            api.update_user_settings(dto, client=home_sections.DISPLAY_PREFS_CLIENT)
            excludes = self._home_prefs.get(server_uuid, (None, frozenset()))[1]
            self._home_prefs[server_uuid] = (list(layout), excludes)

    def get_user_prefs(self, server_uuid, refresh=False):
        """Per-user display preferences, cached. See ``user_prefs``.

        Shares the DisplayPreferences document the home layout and the guide
        settings live in, cached separately for the same reason they are:
        each screen wants its own without paying for the others.

        Never raises: an unreachable or ancient server gets jellyfin-web's
        defaults, which is the behaviour we had before the setting existed.
        """
        if not refresh and server_uuid in self._user_prefs:
            return self._user_prefs[server_uuid]
        api = self._conn(server_uuid).api
        try:
            custom = self._display_prefs_dto(api).get('CustomPrefs') or {}
        except Exception:
            log.warning('Failed to read display preferences; using defaults', exc_info=True)
            custom = {}
        prefs = user_prefs.resolve_prefs(custom)
        self._user_prefs[server_uuid] = prefs
        return prefs

    def save_user_prefs(self, server_uuid, prefs):
        """Persist the display preferences. Raises on failure.

        Read-modify-write of the whole DTO, for the same reason
        ``save_home_layout`` and ``save_live_tv_prefs`` do it: there is no
        partial-update path on this API, so posting only our keys would drop
        jellyfin-web's home layout, guide settings and landing screens.

        The cache is adopted before the write and rolled back if it fails --
        the alternative is a cache that disagrees with the server for the
        rest of the session.
        """
        api = self._conn(server_uuid).api
        with self._display_prefs_lock(server_uuid):
            previous = self._user_prefs.get(server_uuid)
            self._user_prefs[server_uuid] = dict(prefs)
            try:
                dto = self._display_prefs_dto(api)
                custom = dict(dto.get('CustomPrefs') or {})
                custom.update(user_prefs.prefs_to_custom(prefs))
                dto['CustomPrefs'] = custom
                api.update_user_settings(dto, client=home_sections.DISPLAY_PREFS_CLIENT)
            except Exception:
                if previous is None:
                    self._user_prefs.pop(server_uuid, None)
                else:
                    self._user_prefs[server_uuid] = previous
                raise

    def get_view_settings(self, server_uuid, parent_id, collection_type):
        """``{setting: (value, key)}`` for a library's saved view settings.

        All of them in one read, because they live in one document -- and the
        key each came from rides along so a save lands where the user's web
        client will look for it (see ``view_prefs``). The sort is in here too:
        same document, same contract, and the grid needs it before it can ask
        for items.
        """
        try:
            custom = self._display_prefs_custom(server_uuid)
        except Exception:
            log.debug('could not read view preferences', exc_info=True)
            custom = {}
        out = {'imageType': view_prefs.resolve_image_type(custom, parent_id, collection_type), 'viewType': view_prefs.resolve_view_type(custom, parent_id, collection_type)}
        for setting in view_prefs.BOOL_SETTINGS:
            out[setting] = view_prefs.resolve_bool(custom, parent_id, collection_type, setting)
        out.update(view_prefs.resolve_sort(custom, parent_id, collection_type))
        out.update(view_prefs.resolve_filters(custom, parent_id, collection_type))
        return out

    def _display_prefs_custom(self, server_uuid, refresh=False):
        """The raw CustomPrefs blob, cached.

        One cache for the whole document rather than one per consumer: the
        home layout, the guide settings, the display prefs and the per-view
        settings are all in it, and re-fetching it per screen would be four
        round trips for one document.
        """
        if not refresh and server_uuid in self._custom_prefs:
            return self._custom_prefs[server_uuid]
        api = self._conn(server_uuid).api
        custom = self._display_prefs_dto(api).get('CustomPrefs') or {}
        self._custom_prefs[server_uuid] = custom
        return custom

    def save_view_setting(self, server_uuid, parent_id, collection_type, setting, value, key=None):
        """Persist one of a library's view settings. Raises on failure.

        ``key`` is the one it was read from, so a change lands where the
        user's web client will look for it; with none stored yet the first
        candidate is used. Read-modify-write of the whole DTO, as every
        other writer here does.

        Booleans go out as ``"true"``/``"false"`` strings for the reason
        every other boolean in this document does: web compares them as
        strings, so a JSON boolean reads there as false.
        """
        api = self._conn(server_uuid).api
        if not key:
            candidates = view_prefs.keys_for(parent_id, collection_type, setting)
            if not candidates:
                return
            key = candidates[0]
        if isinstance(value, bool):
            value = 'true' if value else 'false'
        with self._display_prefs_lock(server_uuid):
            dto = self._display_prefs_dto(api)
            custom = dict(dto.get('CustomPrefs') or {})
            custom[key] = value
            dto['CustomPrefs'] = custom
            api.update_user_settings(dto, client=home_sections.DISPLAY_PREFS_CLIENT)
            self._custom_prefs[server_uuid] = custom

    def get_latest_excludes(self, server_uuid):
        """Library ids excluded from the home screen's generated rows.

        The server applies this itself for Continue Watching and Next Up, but
        only when the query carries no ParentId — and the Recently Added rows
        are deliberately one request *per library*, which bypasses it. So this
        set has to be applied client-side there, exactly as jellyfin-web does
        in recentlyAdded.ts.

        "The server applies it" is a claim about **which endpoint is asked**,
        not about the parameters: only ``ItemsController.GetResumeItems`` and
        ``TVSeriesManager.GetNextUp`` consult ``LatestItemExcludes``, so
        Continue Watching gets it from ``items_api.get_resume`` and not from
        the apiclient's ``get_resume_items``, which sends the generic item
        query (#703).
        """
        api = self._conn(server_uuid).api
        user = api.get_user() or {}
        config = user.get('Configuration') or {}
        return frozenset(config.get('LatestItemsExcludes') or ())
    HOME_SECTIONS = ('primary', 'latest')

    def get_home_rows(self, server_uuid, libraries=None, sections=None, layout=None, latest_excludes=None):
        """Return the ordered rows shown on the home screen.

        Each row is ``{"title", "items", "collection_type", "slot", "kind"}``;
        empty rows are dropped so the home screen only shows what exists.
        ``libraries`` (the get_libraries result) drives the per-library
        "Latest" rows; passing it in avoids a second views fetch when the
        caller already has it.

        ``sections`` limits which groups are fetched (see HOME_SECTIONS), so
        the caller can draw the above-the-fold rows without waiting on the
        Latest fan-out. Defaults to everything.

        ``layout`` is the user's ordered section list (see home_sections);
        defaults to jellyfin-web's stock layout. ``slot`` on each row is its
        index in that layout, which is what lets the caller merge the two
        fetch batches back into the user's order — the batches no longer
        concatenate cleanly now that Latest need not be last.

        ``latest_excludes`` is the set of library ids to skip when building
        Latest rows; see get_latest_excludes for why it is applied here and
        not by the server.
        """
        sections = tuple(self.HOME_SECTIONS if sections is None else sections)
        layout = list(home_sections.DEFAULT_LAYOUT) if layout is None else list(layout)
        latest_excludes = frozenset(latest_excludes or ())
        api = self._conn(server_uuid).api
        if libraries is None:
            try:
                libraries = self.get_libraries(server_uuid)
            except Exception:
                log.warning('Failed to list libraries for latest rows', exc_info=True)
                libraries = []

        def resume_row(title, collection_type=None, **extra):

            def fetch():
                resume = items_api.get_resume(api, limit=20, fields=LIST_FIELDS, enable_image_types='Primary,Thumb,Backdrop', image_type_limit=1, enable_total_record_count=False, **extra) or {}
                return (title, resume.get('Items', []), collection_type, None)
            return fetch

        def video_resume_row():
            return resume_row(_('Continue Watching'), include_item_types='Movie,Episode,Video')()

        def audio_resume_row():
            return resume_row(_('Continue Listening'), collection_type='music', media_types='Audio')()

        def book_resume_row():
            return resume_row(_('Continue Reading'), collection_type=BOOKS_COLLECTION, media_types='Book')()

        def next_up_row():
            nextup = api.shows('/NextUp', {'UserId': '{UserId}', 'Limit': 20, 'Fields': LIST_FIELDS, 'EnableImageTypes': 'Primary,Thumb,Backdrop', 'ImageTypeLimit': 1, 'EnableResumable': False}) or {}
            return (_('Next Up'), nextup.get('Items', []), None, None)

        def live_tv_row():
            onnow = api.get_recommended_programs(is_airing=True, limit=24, fields=LIST_FIELDS + PROGRAM_FIELDS, enable_image_types='Primary,Thumb,Backdrop', image_type_limit=1, enable_total_record_count=False) or {}
            return (_('On Now'), onnow.get('Items', []), 'livetv', None)

        def latest_row(lib):

            def fetch():
                latest = api.get_recently_added(parent_id=lib.get('Id'), limit=16, fields=LIST_FIELDS, enable_image_types='Primary,Thumb,Backdrop', image_type_limit=1, enable_total_record_count=False)
                items = latest.get('Items', []) if isinstance(latest, dict) else latest or []
                return (_('Recently Added in {0}').format(lib.get('Name', '')), items, lib.get('CollectionType'), lib.get('Id'))
            return fetch

        def active_recordings_row():
            return (_('Active Recordings'), self.get_recordings(server_uuid, limit=12, is_in_progress=True), 'livetv', None)
        builders = {home_sections.RESUME: video_resume_row, home_sections.RESUME_AUDIO: audio_resume_row, home_sections.RESUME_BOOK: book_resume_row, home_sections.NEXT_UP: next_up_row, home_sections.LIVE_TV: live_tv_row, home_sections.ACTIVE_RECORDINGS: active_recordings_row}
        live_tv_sections = (home_sections.LIVE_TV, home_sections.ACTIVE_RECORDINGS)
        tasks = []
        for slot, kind in enumerate(layout):
            stage = home_sections.STAGE.get(kind)
            if stage is None or stage == 'local' or stage not in sections:
                continue
            if kind in live_tv_sections and (not self.has_live_tv(server_uuid)):
                continue
            if kind == home_sections.LATEST:
                tasks += [(slot, kind, latest_row(lib)) for lib in libraries if lib.get('CollectionType') not in ('playlists', LIVE_TV_COLLECTION) and lib.get('Id') not in latest_excludes]
            else:
                tasks.append((slot, kind, builders[kind]))
        if not tasks:
            return []
        rows = []
        with ThreadPoolExecutor(max_workers=min(HOME_FANOUT, max(1, len(tasks))), thread_name_prefix='home') as pool:
            futures = [(slot, kind, pool.submit(task)) for slot, kind, task in tasks]
            for slot, kind, future in futures:
                try:
                    rows.append((slot, kind) + future.result())
                except Exception:
                    log.warning('Failed to load a home row', exc_info=True)
        return [{'title': t, 'items': i, 'collection_type': c, 'slot': slot, 'kind': kind, 'parent_id': pid} for slot, kind, t, i, c, pid in rows if i]

    def get_live_tv_prefs(self, server_uuid, refresh=False):
        """The guide preference dict (see ``live_tv.resolve_prefs``), cached.

        Shares the DisplayPreferences document the home layout lives in, so
        one read serves both — but they are cached separately because the
        home screen loads on startup and the guide may never be opened.

        Never raises: an unreachable or ancient server gets jellyfin-web's
        defaults, which is a working guide rather than no guide.
        """
        if not refresh and server_uuid in self._live_tv_prefs:
            return self._live_tv_prefs[server_uuid]
        api = self._conn(server_uuid).api
        try:
            custom = self._display_prefs_dto(api).get('CustomPrefs') or {}
        except Exception:
            log.warning('Failed to read Live TV preferences; using defaults', exc_info=True)
            custom = {}
        prefs = live_tv.resolve_prefs(custom)
        self._live_tv_prefs[server_uuid] = prefs
        return prefs

    def cache_live_tv_prefs(self, server_uuid, prefs):
        """Adopt ``prefs`` as the cached answer without writing anything.

        **Called on the loop thread, before the save is submitted.** Saving
        the guide settings repaints the guide, and repainting it means
        re-fetching it — a pool job whose first act is ``get_live_tv_prefs``.
        That job is submitted *first*, so no amount of care inside the save
        worker wins the race: by the time the save runs, the reload has
        already read the old cache and the guide comes back drawn with the
        settings the user just changed away from.

        So the cache moves on the thread that ordered both, where there is
        no race to lose. A dict assignment, which is why that is safe.
        """
        self._live_tv_prefs[server_uuid] = dict(prefs)

    def save_live_tv_prefs(self, server_uuid, prefs):
        """Persist the guide preferences. Raises on failure.

        Read-modify-write of the whole DTO, for the same reason
        ``save_home_layout`` does it: there is no partial-update path on this
        API, so posting only our keys would drop jellyfin-web's home layout,
        landing screens and everything else the same document holds.

        The cache is adopted before the write and rolled back if it fails —
        the alternative is a cache that disagrees with the server for the
        rest of the session. Callers that also repaint must additionally
        call :meth:`cache_live_tv_prefs` on the loop thread; see there.
        """
        api = self._conn(server_uuid).api
        with self._display_prefs_lock(server_uuid):
            previous = self._live_tv_prefs.get(server_uuid)
            self._live_tv_prefs[server_uuid] = dict(prefs)
            try:
                dto = self._display_prefs_dto(api)
                custom = dict(dto.get('CustomPrefs') or {})
                custom.update(live_tv.prefs_to_custom(prefs))
                dto['CustomPrefs'] = custom
                api.update_user_settings(dto, client=home_sections.DISPLAY_PREFS_CLIENT)
            except Exception:
                if previous is None:
                    self._live_tv_prefs.pop(server_uuid, None)
                else:
                    self._live_tv_prefs[server_uuid] = previous
                raise

    def get_channels(self, server_uuid, start_index=0, limit=CHANNEL_PAGE, prefs=None, categories=(), add_current_program=True, favorites_only=False):
        """Live TV channels as ``(items, total)``.

        Paged, because an IPTV line-up runs to thousands of channels and this
        endpoint has no way to skip the total record count — the unbounded
        call returns every one of them with artwork and user data attached.

        ``add_current_program`` is what puts "what is on now" on each channel
        tile; the guide turns it off because it fetches the programmes for
        the whole window anyway.
        """
        api = self._conn(server_uuid).api
        kwargs = dict(live_tv.channel_sort_kwargs(prefs or {}))
        kwargs.update(live_tv.category_kwargs(categories))
        if favorites_only:
            kwargs['is_favorite'] = True
        result = api.get_channels(start_index=start_index, limit=limit, fields=LIST_FIELDS, image_type_limit=1, enable_image_types='Primary,Thumb,Backdrop', add_current_program=add_current_program, **kwargs) or {}
        return (result.get('Items', []), result.get('TotalRecordCount', 0))

    def get_guide_info(self, server_uuid):
        """The provider's guide date range, for the date picker. ``{}`` when
        the server cannot answer — the picker then imposes no limit."""
        api = self._conn(server_uuid).api
        try:
            return api.get_live_tv_guide_info() or {}
        except Exception:
            log.debug('GuideInfo unavailable', exc_info=True)
            return {}

    def get_guide(self, server_uuid, channel_ids, start, end, want_hd=False):
        """Guide entries for ``channel_ids`` overlapping [start, end).

        ``start``/``end`` are aware datetimes; they go out as UTC ISO 8601
        **with the Z**, because the server binds these with
        ``AdjustToUniversal`` and silently accepts an offset-less string
        without shifting it — i.e. quietly answers for the wrong window.

        The bounds are the pair jellyfin-web uses and they are not symmetric:
        ``MaxStartDate`` is the window end and ``MinEndDate`` its start, which
        is what includes a programme that began before the window opened.

        **No category filter.** The guide's categories are applied by
        drawing (``live_tv.program_displayed``), as jellyfin-web does, and
        not here. Two reasons, one of which is a bug this used to have: the
        server's ``IsMovie`` is a column predicate while the other three are
        a tag filter, so two categories AND together and the guide came back
        empty; and dropping the rows entirely turns a filtered guide into a
        field of dead air rather than a grid with quiet cells.
        """
        api = self._conn(server_uuid).api
        ids = [c for c in channel_ids or [] if c]
        if not ids:
            return []
        result = api.get_programs(channel_ids=ids, max_start_date=_iso_utc(end - datetime.timedelta(seconds=1)), min_end_date=_iso_utc(start + datetime.timedelta(seconds=1)), sort_by='StartDate', fields='ChannelInfo' + (',IsHD' if want_hd else ''), enable_user_data=False, enable_total_record_count=False, enable_image_types='Primary', image_type_limit=1) or {}
        return result.get('Items', [])

    def get_channel_listing(self, server_uuid, channel_id, limit=CHANNEL_LISTING):
        """A channel and everything still to come on it, for the channel page.

        jellyfin-web's ``renderChannelGuide``: ``HasAired=False`` sorted by
        start, which is "has not finished yet" rather than "has not started"
        — so the first entry is what is on right now, not what is on next.

        Returns ``{"channel": dto-or-None, "programs": [...], "capped":
        bool}``. Both halves in one call because they are one screen and the
        page would otherwise draw twice, half-empty.

        One departure from jellyfin-web: ``limit``, a backstop rather than a
        budget now that the page windows its rows (``capped`` says so, so a
        provider with a deeper guide does not look like it has a shallower
        one).

        **No image fields**, which is jellyfin-web's call here too and the
        same one ``get_guide`` makes for the same reason: these rows are
        text. ``ChannelImage`` in particular costs a channel lookup per
        programme -- across a thousand of them -- for a tag nothing on this
        screen draws. A row that is clicked still seeds the program page,
        which re-reads the authoritative DTO anyway and already draws its
        heading as text while artwork is absent.

        Web also sends ``EnableImages: false``, which suppresses the tags
        themselves (~12% of the body here). We cannot: ``get_programs`` has
        no such argument, and passing one raised a TypeError that took the
        whole channel page down. Leaving the tags in is the cheap half of
        the wrong answer; the fields above are the expensive half, and they
        are gone.
        """
        api = self._conn(server_uuid).api
        channel = None
        try:
            channel = api.get_item(channel_id, fields=DETAIL_FIELDS)
        except Exception:
            log.debug('channel %s unavailable', channel_id, exc_info=True)
        result = api.get_programs(channel_ids=[channel_id], has_aired=False, sort_by='StartDate', limit=limit, fields=LIST_FIELDS + ',ChannelInfo', enable_user_data=False, enable_total_record_count=False) or {}
        programs = result.get('Items', [])
        return {'channel': channel, 'programs': programs, 'capped': len(programs) >= limit}

    def programs_page(self, server_uuid, start_index=0, limit=24, with_total=False, **filters):
        """``(items, total)`` from ``LiveTv/Programs``.

        The paged form, for the "see all" list route. ``get_programs``
        below is the same query without the paging — the twelve-item rows
        want a plain list and would pay for a count they never draw.

        ``with_total`` is what buys a real ``TotalRecordCount``: the rows
        turn it off deliberately (it is a second, wider query server-side),
        but a paginated screen cannot page without it — falling back to
        ``len(items)`` tells the paginator the first page is the whole
        list, which is how it came to re-serve page 1 as page 2.
        """
        api = self._conn(server_uuid).api
        result = api.get_programs(start_index=start_index, limit=limit, fields=LIST_FIELDS + PROGRAM_FIELDS, image_type_limit=1, enable_image_types='Primary,Thumb,Backdrop', enable_total_record_count=with_total, **filters) or {}
        items = result.get('Items', [])
        total = result.get('TotalRecordCount', len(items)) if with_total else len(items)
        return (items, total)

    def get_programs(self, server_uuid, limit=24, **filters):
        """Upcoming guide entries for the Programs screen's category rows.

        ``filters`` are the ``is_movie``/``is_sports``/… flags plus
        ``has_aired``; they go straight through to ``LiveTv/Programs``.
        """
        return self.programs_page(server_uuid, limit=limit, **filters)[0]

    def get_recommended_programs(self, server_uuid, limit=24, **filters):
        """The server's own recommendations — the "On Now" strip."""
        api = self._conn(server_uuid).api
        result = api.get_recommended_programs(limit=limit, fields=LIST_FIELDS + PROGRAM_FIELDS, image_type_limit=1, enable_image_types='Primary,Thumb,Backdrop', enable_total_record_count=False, **filters) or {}
        return result.get('Items', [])
    PROGRAM_SECTIONS = (('onnow', lambda: _('On Now'), {'is_airing': True}), ('episodes', lambda: _('Upcoming Episodes'), {'has_aired': False, 'is_series': True, 'is_movie': False, 'is_sports': False, 'is_kids': False, 'is_news': False}), ('movies', lambda: _('Upcoming Movies'), {'has_aired': False, 'is_movie': True}), ('sports', lambda: _('Upcoming Sports'), {'has_aired': False, 'is_sports': True}), ('kids', lambda: _('Upcoming Kids'), {'has_aired': False, 'is_kids': True}), ('news', lambda: _('Upcoming News'), {'has_aired': False, 'is_news': True}))

    def get_program_sections(self, server_uuid, limit=12):
        """The Programs screen's rows as ``[{"key", "title", "items"}]``.

        Fanned out, like the home screen: six independent requests walked
        serially is six round trips before the screen can draw anything, and
        guide queries are not fast. Order is preserved by collecting in
        submit order, and one failed row costs only that row.

        Empty rows are dropped, so a provider with no sports data simply has
        no Sports row rather than an empty heading.
        """

        def fetch(key, title, query):

            def work():
                if key == 'onnow':
                    items = self.get_recommended_programs(server_uuid, limit=limit * 2, **query)
                else:
                    items = self.get_programs(server_uuid, limit=limit, **query)
                return {'key': key, 'title': title(), 'items': items, 'filters': dict(query)}
            return work
        tasks = [fetch(key, title, query) for key, title, query in self.PROGRAM_SECTIONS]
        rows = []
        with ThreadPoolExecutor(max_workers=min(HOME_FANOUT, len(tasks)), thread_name_prefix='livetv') as pool:
            for future in [pool.submit(task) for task in tasks]:
                try:
                    row = future.result()
                except Exception:
                    log.warning('a Live TV programs row failed to load', exc_info=True)
                    continue
                if row['items']:
                    rows.append(row)
        return rows

    def search_live_tv(self, server_uuid, term, limit=24):
        """Channels and guide entries matching ``term``.

        Two requests, not jellyfin-web's seven: it splits programmes into
        movies/episodes/sports/kids/news/other so each row can have its own
        card shape, and then draws them all the same. One Programs row is
        the same information for a fifth of the traffic.

        Never raises — a search that half-worked is better than a search
        screen that reports failure because the tuner was busy.
        """
        api = self._conn(server_uuid).api

        def find(item_type, fields):
            try:
                result = items_api.get_items(api, include_item_types=item_type, search_term=term, recursive=True, limit=limit, fields=fields, image_type_limit=1, enable_image_types='Primary,Thumb') or {}
            except Exception:
                log.debug('Live TV search for %s failed', item_type, exc_info=True)
                return []
            return result.get('Items', [])
        return {'channels': find('TvChannel', LIST_FIELDS), 'programs': find('LiveTvProgram', LIST_FIELDS + PROGRAM_FIELDS)}

    def get_live_program(self, server_uuid, program_id):
        """One guide entry with its live recording state, for the program
        page. ``None`` when the entry has expired out of the guide."""
        api = self._conn(server_uuid).api
        try:
            return api.get_live_tv_program(program_id)
        except Exception:
            log.debug('program %s unavailable', program_id, exc_info=True)
            return None

    def recordings_page(self, server_uuid, start_index=0, limit=60, with_total=False, is_in_progress=None, series_timer_id=None):
        """``(items, total)`` of recordings, newest first. In-progress ones
        with ``is_in_progress=True``; everything else is the recordings
        library.

        An in-progress result is stamped ``_recording`` — a recording DTO
        carries no timer state, so the *query* is the only thing that knows
        it is still being written, and the tile needs to (it draws the red
        dot and a broadcast-progress bar rather than a resume bar). Stamped
        here rather than at each of the three call sites so none of them can
        forget.

        See ``programs_page`` for why paging and ``with_total`` go together.
        """
        api = self._conn(server_uuid).api
        result = api.get_live_tv_recordings(start_index=start_index, limit=limit, is_in_progress=is_in_progress, series_timer_id=series_timer_id, fields=LIST_FIELDS + ',CanDelete', image_type_limit=1, enable_image_types='Primary,Thumb,Backdrop', enable_total_record_count=with_total) or {}
        items = result.get('Items', [])
        if is_in_progress:
            for item in items:
                item['_recording'] = True
        total = result.get('TotalRecordCount', len(items)) if with_total else len(items)
        return (items, total)

    def get_recordings(self, server_uuid, limit=60, is_in_progress=None, series_timer_id=None):
        """Recordings for a row: the list alone, unpaged."""
        return self.recordings_page(server_uuid, limit=limit, is_in_progress=is_in_progress, series_timer_id=series_timer_id)[0]

    def get_recording_folders(self, server_uuid):
        """The virtual folders recordings are filed under. These are ordinary
        folder items, so opening one lands in the normal grid."""
        api = self._conn(server_uuid).api
        try:
            result = api.get_recording_folders() or {}
        except Exception:
            log.debug('recording folders unavailable', exc_info=True)
            return []
        return result.get('Items', [])

    def get_timers(self, server_uuid, is_active=None, is_scheduled=None, series_timer_id=None):
        """Single recording timers, in start order.

        Sorted here rather than by the server: ``LiveTv/Timers`` takes no
        sort arguments, and the screen groups them by day — which only reads
        as a schedule if they arrive in time order.
        """
        api = self._conn(server_uuid).api
        result = api.get_live_tv_timers(is_active=is_active, is_scheduled=is_scheduled, series_timer_id=series_timer_id) or {}
        items = result.get('Items', [])
        return sorted(items, key=lambda t: t.get('StartDate') or '')

    def get_timer(self, server_uuid, timer_id):
        api = self._conn(server_uuid).api
        return api.get_live_tv_timer(timer_id)

    def get_series_timers(self, server_uuid):
        api = self._conn(server_uuid).api
        result = api.get_live_tv_series_timers(sort_by='SortName', sort_order='Ascending') or {}
        return result.get('Items', [])

    def get_series_timer(self, server_uuid, timer_id):
        api = self._conn(server_uuid).api
        return api.get_live_tv_series_timer(timer_id)

    @staticmethod
    def _filter_kwargs(filters):
        """Translate the UI's filter dict into ``get_items`` arguments."""
        kwargs: dict[str, str] = {}
        if not filters:
            return kwargs
        active = [name for key, name in STATUS_FILTERS if filters.get(key)]
        if active:
            kwargs['filters'] = ','.join(active)
        if filters.get('favorite'):
            kwargs['is_favorite'] = 'true'
        if filters.get('genre'):
            kwargs['genres'] = filters['genre']
        if filters.get('year'):
            kwargs['years'] = str(filters['year'])
        extra: dict[str, str] = {}
        for key, param in FEATURE_FILTERS:
            if filters.get(key):
                extra[param] = 'true'
        for key, param in VIDEO_FLAG_FILTERS:
            if filters.get(key):
                extra[param] = 'true'
        if filters.get('hd'):
            extra['IsHd'] = 'true'
        elif filters.get('sd'):
            extra['IsHd'] = 'false'
        chosen = [param for key, param in VIDEO_TYPE_FILTERS if filters.get(key)]
        if chosen:
            extra['VideoTypes'] = ','.join(chosen)
        for key, param in LIST_FILTERS:
            value = filters.get(key)
            if value:
                extra[param] = ','.join((str(v) for v in value)) if isinstance(value, (list, tuple, set)) else str(value)
        if extra:
            kwargs['params'] = extra
        letter = filters.get('letter')
        if letter == '#':
            kwargs['name_less_than'] = 'A'
        elif letter:
            kwargs['name_starts_with'] = letter
        return kwargs
    BY_NAME_SECTIONS = (('movies', lambda: _('Movies'), 'Movie'), ('shows', lambda: _('Shows'), 'Series'), ('episodes', lambda: _('Episodes'), 'Episode'), ('videos', lambda: _('Videos'), 'Video,MusicVideo'), ('trailers', lambda: _('Trailers'), 'Trailer'), ('albums', lambda: _('Albums'), 'MusicAlbum'), ('songs', lambda: _('Songs'), 'Audio'), ('audiobooks', lambda: _('Audiobooks'), AUDIOBOOK_TYPE), ('books', lambda: _('Books'), BOOK_TYPE))

    def get_by_name_sections(self, server_uuid, spec, limit=20):
        """``[{"key", "title", "items", "types", "total"}]`` for a by-name
        screen -- everything a genre, studio or person is attached to,
        grouped by what kind of thing it is.

        ``spec`` is a :meth:`get_list` ``items`` spec minus the item types;
        each row adds its own. Fanned out, empty rows dropped, and ``total``
        rides along so a row only offers "see all" when there is more behind
        it than it is showing -- which is jellyfin-web's rule for its own
        More button (``itemsByName.js:96``).
        """

        def fetch(key, title, types):

            def work():
                row_spec = dict(spec or {})
                row_spec['include_item_types'] = types
                items, total = self._list_items(server_uuid, row_spec, 'SortName', 'Ascending', 0, limit, None)
                return {'key': key, 'title': title(), 'items': items, 'types': types, 'total': total}
            return work
        rows = []
        tasks = [fetch(*section) for section in self.BY_NAME_SECTIONS]
        with ThreadPoolExecutor(max_workers=min(HOME_FANOUT, max(1, len(tasks))), thread_name_prefix='byname') as pool:
            for future in [pool.submit(task) for task in tasks]:
                try:
                    row = future.result()
                except Exception:
                    log.warning('Failed to load a by-name row', exc_info=True)
                    continue
                if row['items']:
                    rows.append(row)
        return rows
    GENRE_ITEM_TYPES = {'movies': 'Movie', 'tvshows': 'Series'}

    def get_genre_sections(self, server_uuid, parent_id, collection_type, limit=10, max_genres=40):
        """``[{"key", "title", "items", "types"}]`` -- one row per genre.

        jellyfin-web's ``moviegenres`` / ``tvgenres``: a heading per genre
        over a random sample of that genre's items, with the heading linking
        to the unbounded listing. ``SortBy: Random`` is theirs too, and it is
        the point -- a fixed sample of ten would make the screen the same
        ten films forever.

        ``max_genres`` bounds the fan-out. A library can have a hundred
        genres and each row is a request; web lazy-loads them on scroll
        (IntersectionObserver), which we have no equivalent for, so a cap is
        the honest version. It is logged when it bites rather than silently
        truncating.
        """
        types = self.GENRE_ITEM_TYPES.get(collection_type)
        if not types:
            return []
        api = self._conn(server_uuid).api
        result = api.get_genres(parent_id, include_item_types=types) or {}
        genres = [g for g in result.get('Items', []) if g.get('Id')]
        if len(genres) > max_genres:
            log.info('Showing %d of %d genres; the rest need the search or the genre filter.', max_genres, len(genres))
            genres = genres[:max_genres]

        def fetch(genre):

            def work():
                items, _total = self._list_items(server_uuid, {'type': 'items', 'genre_ids': genre['Id'], 'include_item_types': types, 'parent_id': parent_id}, 'Random', 'Ascending', 0, limit, None)
                return {'key': genre['Id'], 'title': genre.get('Name') or '', 'items': items, 'types': types}
            return work
        rows = []
        if not genres:
            return rows
        with ThreadPoolExecutor(max_workers=min(HOME_FANOUT, len(genres)), thread_name_prefix='genres') as pool:
            for future in [pool.submit(fetch(g)) for g in genres]:
                try:
                    row = future.result()
                except Exception:
                    log.warning('Failed to load a genre row', exc_info=True)
                    continue
                if row['items']:
                    rows.append(row)
        return rows
    FAVORITE_SECTIONS = (('movies', lambda: _('Movies'), 'Movie'), ('shows', lambda: _('Shows'), 'Series'), ('episodes', lambda: _('Episodes'), 'Episode'), ('videos', lambda: _('Videos'), 'Video,MusicVideo'), ('collections', lambda: _('Collections'), 'BoxSet'), ('artists', lambda: _('Artists'), 'MusicArtist'), ('albums', lambda: _('Albums'), 'MusicAlbum'), ('songs', lambda: _('Songs'), 'Audio'), ('audiobooks', lambda: _('Audiobooks'), AUDIOBOOK_TYPE), ('books', lambda: _('Books'), BOOK_TYPE))

    def get_favorite_sections(self, server_uuid, limit=24):
        """The Favorites screen's rows as ``[{"key", "title", "items",
        "types"}]``.

        Fanned out for the same reason the Programs screen is: seven
        independent queries walked serially is seven round trips before
        anything draws. Empty rows are dropped, so a user with no favourite
        albums has no Albums heading rather than an empty one.

        ``types`` rides along so the row's heading can link to the unbounded
        listing, exactly as the Live TV rows carry their filters.
        """

        def fetch(key, title, types):

            def work():
                items, _total = self._list_items(server_uuid, {'type': 'items', 'include_item_types': types, 'is_favorite': True}, 'SortName', 'Ascending', 0, limit, None)
                return {'key': key, 'title': title(), 'items': items, 'types': types}
            return work
        rows = []
        tasks = [fetch(*section) for section in self.FAVORITE_SECTIONS]
        with ThreadPoolExecutor(max_workers=min(HOME_FANOUT, max(1, len(tasks))), thread_name_prefix='favorites') as pool:
            for future in [pool.submit(task) for task in tasks]:
                try:
                    row = future.result()
                except Exception:
                    log.warning('Failed to load a favourites row', exc_info=True)
                    continue
                if row['items']:
                    rows.append(row)
        return rows

    def get_list(self, server_uuid, spec, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100, filters=None):
        """``(items, total)`` for a generic list route -- jellyfin-web's
        ``#/list?type=…``.

        One entry point for every "see all" destination, because they are the
        same screen with different `where`: Next Up in full, a Live TV
        category beyond the twelve a row shows, everything in a genre, a
        studio's catalogue, the favourites.

        ``spec`` is a plain dict so it can live in a route and survive
        back-navigation. ``spec["type"]`` selects the query; everything else
        in it is that query's arguments. Unknown types raise rather than
        quietly returning an empty list -- a typo in a route should not look
        like an empty library.
        """
        kind = (spec or {}).get('type')
        if kind == 'nextup':
            api = self._conn(server_uuid).api
            result = api.get_next(index=start_index, limit=limit, fields=LIST_FIELDS, enable_image_types='Primary,Thumb,Backdrop', image_type_limit=1) or {}
            return (result.get('Items', []), result.get('TotalRecordCount', 0))
        if kind == 'programs':
            return self.programs_page(server_uuid, start_index=start_index, limit=limit, with_total=True, **spec.get('filters') or {})
        if kind == 'recordings':
            return self.recordings_page(server_uuid, start_index=start_index, limit=limit, with_total=True)
        if kind == 'studios':
            api = self._conn(server_uuid).api
            result = api.get_studios(parent_id=spec.get('parent_id'), include_item_types=spec.get('include_item_types'), start_index=start_index, limit=limit) or {}
            return (result.get('Items', []), result.get('TotalRecordCount', 0))
        if kind == 'items':
            return self._list_items(server_uuid, spec, sort_by, sort_order, start_index, limit, filters)
        raise ValueError('unknown list type %r' % (kind,))

    def _list_items(self, server_uuid, spec, sort_by, sort_order, start_index, limit, filters):
        """The ``Users/{id}/Items`` half of :meth:`get_list`.

        Genre, studio, person and favourites are all one query with a
        different predicate, which is also how jellyfin-web routes them
        (``#/list?genreId=…``, ``&IsFavorite=true``).
        """
        api = self._conn(server_uuid).api
        kwargs = dict(self._filter_kwargs(filters))
        for key in ('genre_ids', 'person_ids', 'artist_ids', 'include_item_types', 'parent_id'):
            if spec.get(key) is not None:
                kwargs[key] = spec[key]
        if spec.get('is_favorite'):
            kwargs['is_favorite'] = 'true'
        if spec.get('studio_ids') is not None:
            kwargs['params'] = {'StudioIds': spec['studio_ids']}
        result = items_api.get_items(api, sort_by=sort_by, sort_order=sort_order, start_index=start_index, limit=limit, recursive=True, fields=LIST_FIELDS, image_type_limit=1, enable_image_types='Primary,Thumb,Backdrop', **kwargs) or {}
        return (result.get('Items', []), result.get('TotalRecordCount', 0))

    def get_library_items(self, server_uuid, parent_id, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100, filters=None, image_type=None, collection_type=None):
        """One page of a library grid.

        ``image_type`` is the artwork the view is set to draw (see
        :func:`browse_image_types`); without it a library set to Banner gets
        no banner tags back and falls through to its thumbnails.

        ``collection_type`` says what kind of library this is, which decides
        whether the query is typed and recursive -- see
        :data:`LIBRARY_ITEM_TYPES`. A folder inside a library has none and is
        listed as it stands.
        """
        api = self._conn(server_uuid).api
        include = LIBRARY_ITEM_TYPES.get(collection_type or '')
        fields = BOOKS_GRID_FIELDS if collection_type == BOOKS_COLLECTION else GRID_FIELDS
        typed = {'include_item_types': include, 'recursive': True} if include else {}
        result = items_api.get_items(api, parent_id=parent_id, **typed, sort_by=sort_by, sort_order=sort_order, start_index=start_index, limit=limit, fields=fields, image_type_limit=1, enable_image_types=browse_image_types(image_type), **self._filter_kwargs(filters)) or {}
        return (result.get('Items', []), result.get('TotalRecordCount', 0))

    def get_movie_collections(self, server_uuid, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100, filters=None, image_type=None):
        """Collections (BoxSets) as a paged grid, for the Movies-library
        Collections toggle. Server-wide and recursive (a BoxSet can gather
        items from several libraries), mirroring jellyfin-web's Collections
        view. Returns ``(items, total)`` like ``get_library_items``."""
        api = self._conn(server_uuid).api
        result = items_api.get_items(api, include_item_types='BoxSet', recursive=True, sort_by=sort_by, sort_order=sort_order, start_index=start_index, limit=limit, fields=GRID_FIELDS, image_type_limit=1, enable_image_types=browse_image_types(image_type), **self._filter_kwargs(filters)) or {}
        return (result.get('Items', []), result.get('TotalRecordCount', 0))

    def get_person_items(self, server_uuid, person_id, start_index=0, limit=100, sort_by='SortName', sort_order='Ascending'):
        """A person's filmography (movies + series they appear in)."""
        api = self._conn(server_uuid).api
        result = api.get_items_by_person(person_id, sort_by=sort_by, sort_order=sort_order, start_index=start_index, limit=limit, fields=GRID_FIELDS, image_type_limit=1, enable_image_types=browse_image_types()) or {}
        return (result.get('Items', []), result.get('TotalRecordCount', 0))

    def _music_items(self, server_uuid, include, parent_id, sort_by, sort_order, start_index, limit, filters=None, extra=None):
        api = self._conn(server_uuid).api
        kwargs = {'parent_id': parent_id, 'include_item_types': include, 'recursive': True, 'sort_by': sort_by, 'sort_order': sort_order, 'start_index': start_index, 'limit': limit, 'fields': MUSIC_FIELDS, 'image_type_limit': 1, 'enable_image_types': 'Primary'}
        if extra:
            kwargs.update(extra)
        got = self._filter_kwargs(filters)
        merged = dict(kwargs.get('params') or {})
        merged.update(got.pop('params', None) or {})
        kwargs.update(got)
        if merged:
            kwargs['params'] = merged
        result = items_api.get_items(api, **kwargs) or {}
        return (result.get('Items', []), result.get('TotalRecordCount', 0))

    def get_music_albums(self, server_uuid, parent_id, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100, filters=None):
        return self._music_items(server_uuid, 'MusicAlbum', parent_id, sort_by, sort_order, start_index, limit, filters)

    def get_songs(self, server_uuid, parent_id, sort_by='Name', sort_order='Ascending', start_index=0, limit=100, filters=None):
        """A music library's tracks, A-Z by title.

        **Name, not SortName** -- the one place in this file where those two
        differ. A track's SortName is not its name: the server builds it from
        the disc and track numbers with the title only as a tie-break, which
        is what makes an album's own listing come out in play order. Ask a
        whole library for it and the ordering is by track number *across
        albums* -- every album's track 1, then every album's track 2 -- with
        the titles scattered through it. jellyfin-web's Songs tab has the same
        two entries in its sort menu and spells its "Track Name" one `Name`
        for this reason; the rest of its options end in SortName because they
        are all album-grouped first.
        """
        return self._music_items(server_uuid, 'Audio', parent_id, sort_by, sort_order, start_index, limit, filters)

    def get_genre_albums(self, server_uuid, parent_id, genre_id, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100, filters=None):
        return self._music_items(server_uuid, 'MusicAlbum', parent_id, sort_by, sort_order, start_index, limit, filters, extra={'genre_ids': genre_id})

    def _artist_list(self, server_uuid, method_name, parent_id, sort_by, sort_order, start_index, limit):
        api = self._conn(server_uuid).api
        result = getattr(api, method_name)(parent_id=parent_id, sort_by=sort_by, sort_order=sort_order, start_index=start_index, limit=limit, fields=MUSIC_FIELDS, image_type_limit=1, enable_image_types='Primary') or {}
        return (result.get('Items', []), result.get('TotalRecordCount', 0))

    def get_album_artists(self, server_uuid, parent_id, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100):
        return self._artist_list(server_uuid, 'get_album_artists', parent_id, sort_by, sort_order, start_index, limit)

    def get_artists(self, server_uuid, parent_id, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100):
        return self._artist_list(server_uuid, 'get_artists', parent_id, sort_by, sort_order, start_index, limit)

    def get_music_genres(self, server_uuid, parent_id):
        api = self._conn(server_uuid).api
        result = api.get_genres(parent_id, include_item_types='MusicAlbum') or {}
        return result.get('Items', [])

    def get_album_tracks(self, server_uuid, album_id):
        """An album's tracks in disc/track order (children of the album)."""
        api = self._conn(server_uuid).api
        result = api.get_album_tracks(album_id, fields=MUSIC_FIELDS) or {}
        return result.get('Items', [])

    def get_artist_albums(self, server_uuid, artist_id):
        api = self._conn(server_uuid).api
        result = api.get_artist_albums(artist_id, fields=MUSIC_FIELDS, image_type_limit=1, enable_image_types='Primary') or {}
        return result.get('Items', [])

    def get_items_by_ids(self, server_uuid, ids):
        """Fetch DTOs for a list of ids, returned in the requested order (the
        server's Ids query does not preserve order). For the queue display.

        Batched: a big queue's ids as one ``Ids=`` param overflows the server's
        request-URI limit (HTTP 414). A partial (failed) batch just leaves those
        rows without metadata rather than losing the whole list.

        No fields: the only caller is the queue table, whose columns (index,
        name, artist, album, runtime) are all unconditional BaseItemDto
        properties — see LIST_FIELDS. It draws no artwork (``art=False``), so
        neither ``PrimaryImageAspectRatio`` nor ``ItemCounts`` reaches a pixel,
        and a field nothing reads is pure server work on a request that is
        already 100 ids wide.
        """
        ids = [i for i in ids if i]
        if not ids:
            return []
        api = self._conn(server_uuid).api
        unique = list(dict.fromkeys(ids))
        by_id = {}
        CHUNK = 100
        for start in range(0, len(unique), CHUNK):
            chunk = unique[start:start + CHUNK]
            try:
                result = api.get_items(chunk, fields='') or {}
            except Exception:
                log.warning('Failed to fetch a metadata batch of %d items', len(chunk), exc_info=True)
                continue
            for i in result.get('Items', []):
                by_id[i.get('Id')] = i
        return [by_id[i] for i in ids if i in by_id]

    def get_artist_songs(self, server_uuid, artist_id, limit=500):
        """All audio tracks by an artist (for Play/Shuffle/Add-to-playlist)."""
        api = self._conn(server_uuid).api
        result = api.get_artist_songs(artist_id, limit=limit, fields=MUSIC_FIELDS) or {}
        return result.get('Items', [])

    def get_genre_songs(self, server_uuid, parent_id, genre_id, limit=500):
        """All audio tracks in a genre. parent_id may be None (server-wide)."""
        api = self._conn(server_uuid).api
        result = api.get_genre_songs(genre_id, parent_id=parent_id or None, limit=limit, fields=MUSIC_FIELDS) or {}
        return result.get('Items', [])

    def get_instant_mix(self, server_uuid, item_id, limit=200):
        """A radio-style queue seeded from an album/artist/genre/song.

        Asks for **no** fields, as jellyfin-web does. The caller only wants the
        ids — it hands them to the player, which fetches the one item it is
        about to start. The apiclient's default here is the ``music_info()``
        set, and ``MediaStreams``/``People``/``ItemCounts`` are per-item
        lookups the server repeats for every one of the 200 results: it turned
        a single query into hundreds and took ~25s on a spinning-disk server.
        """
        api = self._conn(server_uuid).api
        try:
            result = api.get_instant_mix(item_id, limit=limit, fields='') or {}
        except Exception:
            return []
        return result.get('Items', [])

    def get_genres(self, server_uuid, parent_id=None):
        """Genre names available under a library (for the filter picker)."""
        api = self._conn(server_uuid).api
        result = api.get_genres(parent_id) or {}
        return [g.get('Name') for g in result.get('Items', []) if g.get('Name')]

    def get_filter_values(self, server_uuid, parent_id=None, collection_type=None):
        """Filter-picker values: one option list per picker section --
        ``genres``, ``years``, ``official_ratings``, ``tags``.

        All four keys are always present, empty where the server said
        nothing: the panel builds a section per key and a missing one
        would raise where an empty one simply draws no options.

        Only genres and years come from the pre-Items/Filters fallback;
        against those servers the other two pickers are always empty.

        ``collection_type`` scopes the scan to the type the grid lists, which
        is what web's filter menu does. Untyped, the endpoint walks every
        episode of every series to collect the genres of their shows: 3.7s
        against a real 950-series library here, 0.4s typed.
        """
        api = self._conn(server_uuid).api
        try:
            result = api.get_filters(parent_id, include_item_types=LIBRARY_ITEM_TYPES.get(collection_type or '')) or {}
            years = set()
            for y in result.get('Years') or []:
                try:
                    years.add(int(y))
                except (TypeError, ValueError):
                    continue
            out = {'genres': result.get('Genres') or [], 'years': sorted(years, reverse=True), 'official_ratings': result.get('OfficialRatings') or [], 'tags': result.get('Tags') or []}
            out.update(self._filters2(server_uuid, parent_id, collection_type))
            return out
        except Exception:
            log.warning('Items/Filters failed; falling back to genres', exc_info=True)
        return {'genres': self.get_genres(server_uuid, parent_id), 'years': []}

    def _filters2(self, server_uuid, parent_id=None, collection_type=None):
        """The options only ``Items/Filters2`` knows: the two languages.

        **Both endpoints, not one.** They return different, overlapping
        sets: the legacy `Filters` has Years and OfficialRatings and no
        languages; `Filters2` has the languages and no Years. Measured on
        a live server -- switching wholesale to the newer one would have
        silently emptied the Year picker we already ship. jellyfin-web
        calls both for the same reason (`useFetchItems`).

        Absent on Jellyfin 11 and earlier, where the query parameters do
        not exist either -- so an empty answer here is exactly the gate
        the panel needs, and it needs no version check.
        """
        api = self._conn(server_uuid).api
        params = {'userId': '{UserId}'}
        if parent_id:
            params['parentId'] = parent_id
        types = LIBRARY_ITEM_TYPES.get(collection_type or '')
        if types:
            params['includeItemTypes'] = types
        try:
            got = api.items('/Filters2', params=params) or {}
        except Exception:
            log.debug('Items/Filters2 unavailable', exc_info=True)
            return {}

        def pairs(key):
            return [(o.get('Name') or o.get('Value') or '', o.get('Value')) for o in got.get(key) or [] if o.get('Value')]
        return {'audio_languages': pairs('AudioLanguages'), 'subtitle_languages': pairs('SubtitleLanguages')}

    def get_similar(self, server_uuid, item_id, limit=12):
        """"More Like This" items."""
        api = self._conn(server_uuid).api
        result = api.get_similar(item_id, limit=limit, fields=LIST_FIELDS) or {}
        return result.get('Items', [])

    def get_trailers(self, server_uuid, item_id):
        """Local trailer items for an item (playable like any other item).

        ``/Items/{id}/LocalTrailers``: the apiclient's ``get_local_trailers``
        calls the ``[Obsolete]`` ``/Users/{u}/Items/{id}/LocalTrailers``.
        """
        api = self._conn(server_uuid).api
        try:
            result = api.items('/%s/LocalTrailers' % item_id, params={'userId': '{UserId}'}) or []
        except Exception:
            log.debug('Local trailers unavailable for %s', item_id, exc_info=True)
            return []
        return result.get('Items', []) if isinstance(result, dict) else result

    def get_special_features(self, server_uuid, item_id):
        """The item's Special Features, in the server's order (SortName).

        A bare array with no paging and no ``Fields``: the server sends full
        DTOs, ``ExtraType`` and ``UserData`` included. Trailers and theme
        media are never in it. docs/jellyfin-api-notes.md §15.
        """
        api = self._conn(server_uuid).api
        try:
            result = api.items('/%s/SpecialFeatures' % item_id, params={'userId': '{UserId}'}) or []
        except Exception:
            log.debug('Special features unavailable for %s', item_id, exc_info=True)
            return []
        return result.get('Items', []) if isinstance(result, dict) else result

    def search_people(self, server_uuid, term, limit=PEOPLE_SEARCH_LIMIT):
        """People matching a search term.

        Its own query, and its own budget: /Persons is a different endpoint
        from the item search, so people can never be crowded out by episodes
        the way rows sharing SEARCH_LIMIT can.
        """
        api = self._conn(server_uuid).api
        result = api.get_persons(search_term=term, limit=limit) or {}
        return result.get('Items', [])

    def search_artists(self, server_uuid, term, limit=ARTIST_SEARCH_LIMIT):
        """Artists matching a search term.

        /Artists rather than the item query, which is what web does and what
        the item query cannot be relied on for: it answers with fewer
        artists on the server this was written against, and with none at all
        on at least one real one. This endpoint also covers track-level and
        featured artists, who have no MusicArtist item to be found as.
        """
        api = self._conn(server_uuid).api
        result = api.get_artists(search_term=term, limit=limit) or {}
        return result.get('Items', [])

    def get_playlists(self, server_uuid, limit=300):
        """All playlists, for the add-to-playlist picker."""
        api = self._conn(server_uuid).api
        result = api.get_playlists(limit=limit) or {}
        return result.get('Items', [])

    def get_collections(self, server_uuid, limit=300):
        """All user collections (BoxSets), for the add-to-collection picker."""
        api = self._conn(server_uuid).api
        result = api.get_collections(limit=limit, sort_by='SortName') or {}
        return result.get('Items', [])

    def get_shuffle_ids(self, server_uuid, parent_id, limit=QUEUE_LIMIT):
        """Random playable item ids under a library, for shuffle play. The
        server does the shuffling so the sample spans the whole library, not
        just the loaded pages."""
        api = self._conn(server_uuid).api
        result = api.get_random_items(parent_id=parent_id, media_types=QUEUEABLE_MEDIA_PARAM, limit=limit, enable_images=False) or {}
        return [i['Id'] for i in result.get('Items', []) if i.get('Id')]

    def get_play_all_ids(self, server_uuid, parent_id, sort_by='SortName', sort_order='Ascending', filters=None, limit=QUEUE_LIMIT, collection_type=None):
        """Ids Play All should queue, in the grid's own order.

        **Deliberately the grid's own query.** This used to ask a different
        one -- ``Recursive`` plus ``IncludeItemTypes=Movie,Episode,Video`` --
        and on a Home Videos album holding photos and clips it came back
        empty, so a folder whose videos were on screen reported nothing to
        play. Asking the same question the grid asks makes that class of
        disagreement unrepresentable: whatever is drawn is what is queued,
        and the answer cannot depend on a second query's filters agreeing
        with the first's.

        The filter is then :data:`QUEUEABLE_MEDIA`, on **MediaType**, not on
        ``Type``. Type is the concrete entity the scanner chose, which
        depends on which resolver ran -- the same clip is a ``Video`` in a
        Home Videos library and a ``Movie`` in a movies one -- whereas
        MediaType is the question actually being asked: is this a stream or
        a container?

        The recursive query survives only as the fallback for a level that
        is *all folders*, which is the top of a Home Videos library and the
        one case a flat listing genuinely cannot answer.

        Capped at ``limit``; a queue is not a library export.
        """
        items, _total = self.get_library_items(server_uuid, parent_id, sort_by=sort_by, sort_order=sort_order, limit=limit, filters=filters, collection_type=collection_type)
        ids = [i['Id'] for i in items if i.get('Id') and i.get('MediaType') in QUEUEABLE_MEDIA]
        if ids or not any((i.get('IsFolder') for i in items)):
            return ids
        api = self._conn(server_uuid).api
        result = items_api.get_items(api, parent_id=parent_id, recursive=True, media_types=QUEUEABLE_MEDIA_PARAM, sort_by=sort_by, sort_order=sort_order, limit=limit, enable_images=False, **self._filter_kwargs(filters)) or {}
        return [i['Id'] for i in result.get('Items', []) if i.get('Id')]

    def get_playlist_items(self, server_uuid, playlist_id):
        """A playlist's items in playlist order (not sorted).

        Returns the raw contents; the view filters to supported media types so
        it can tell an empty playlist from one that only holds unsupported
        (e.g. music) entries.
        """
        api = self._conn(server_uuid).api
        result = api.get_playlist_items(playlist_id, fields=LIST_FIELDS) or {}
        return result.get('Items', [])

    def get_playlist(self, server_uuid, playlist_id):
        """A playlist's metadata (``OpenAccess`` visibility + shares), for the
        editor's Public/Private control. Returns {} on servers too old to
        expose it."""
        api = self._conn(server_uuid).api
        try:
            return api.get_playlist(playlist_id) or {}
        except Exception:
            return {}

    def get_seasons(self, server_uuid, series_id):
        """A show's seasons, with the fields the season screen's provider
        links and its extras and trailer gates need.

        Issued here rather than through ``api.get_seasons``, which hardcodes
        ``Fields=info()`` and takes no override. The alternative was a whole
        extra ``get_item`` round trip on a screen that already makes two,
        for one row of links and two counts -- this is the same request with
        more fields.

        ``info()`` is kept and added to rather than replaced: the season
        screen reads UserData, artwork tags and item counts off these DTOs,
        and trimming the set to what the links need would quietly take those
        away. ``ExternalUrls`` is absent from list queries unless asked for
        (measured on 10.11 and 12.0 -- the single-item routes fill it in
        unconditionally, list routes do not), which is why this is needed at
        all and why the series and detail screens need nothing. The counts
        and ``RemoteTrailers`` behave the same way (jellyfin-api-notes.md
        §15).

        Falls back to the plain call if this apiclient does not expose the
        helper. An older one costs the links, the extras row and the
        Trailer button, not the screen.
        """
        api = self._conn(server_uuid).api
        try:
            from jellyfin_apiclient_python.api import info
            result = api.shows('/%s/Seasons' % series_id, params={'UserId': '{UserId}', 'EnableImages': True, 'Fields': info() + ',ExternalUrls,SpecialFeatureCount,LocalTrailerCount,RemoteTrailers'}) or {}
        except Exception:
            log.debug('falling back to the stock seasons query', exc_info=True)
            result = api.get_seasons(series_id) or {}
        return result.get('Items', [])

    def get_episodes(self, server_uuid, series_id, season_id):
        api = self._conn(server_uuid).api
        result = api.get_season(series_id, season_id) or {}
        return result.get('Items', [])

    def get_item(self, server_uuid, item_id):
        api = self._conn(server_uuid).api
        return api.get_item(item_id, fields=DETAIL_FIELDS)

    def get_series_queue(self, server_uuid, series_id, start_item_id=None, limit=100):
        """Episodes for a series in aired order, ACROSS seasons, optionally
        starting at ``start_item_id`` — this is how the play queue crosses
        season boundaries (mirrors jellyfin-web's getEpisodes with startItemId
        and no SeasonId).

        **The same two filters :meth:`get_season_queue` sends, for the same
        reason**, and this method went without them: for a user whose
        ``DisplayMissingEpisodes`` is on, the cross-season queue contained
        episodes with no file. The server applies ``isMissing`` *after* the
        include decision and ``startItemId`` and ``limit`` after that
        (``TvShowsController.cs``), so with S01E01 missing the Next Up
        fallback's ``limit=1`` returned exactly that episode -- pressing Play
        on a series nobody had started began a file that does not exist.

        Issued through ``api.shows`` for the reason :meth:`get_season_queue`
        gives one method down: the apiclient helper takes no filter
        arguments, and this is the same request with two more.
        """
        api = self._conn(server_uuid).api
        params = {'UserId': '{UserId}', 'IsVirtualUnaired': False, 'IsMissing': False, 'Fields': LIST_FIELDS}
        if start_item_id:
            params['StartItemId'] = start_item_id
        if limit:
            params['Limit'] = limit
        result = api.shows('/%s/Episodes' % series_id, params=params) or {}
        return result.get('Items', [])

    def get_season_queue(self, server_uuid, series_id, season_id):
        """One season's episodes, in aired order, as a play queue.

        Separate from :meth:`get_episodes`, which the season SCREEN uses,
        because the two want different rows: a listing shows missing and
        unaired episodes so you can see the gaps, and a queue must not try to
        play them. **The listing showing them is web's behaviour too** -- it
        sends no filter either and relies on the server honouring the user's
        ``DisplayMissingEpisodes`` -- so what keeps a gap from being *played*
        from the screen is `components.virtual_episode_label`, asked by the
        tile's play chip and by the detail page's buttons, rather than the
        absence of a filter here. Those are jellyfin-web's own filters for this exact
        gesture (``playbackmanager.js``'s ``getSeriesOrSeasonPlaybackPromise``
        sends ``IsVirtualUnaired: false, IsMissing: false``), and folding
        them into the shared method would quietly hide the gaps from the
        screen.

        Issued through ``api.shows`` rather than ``api.get_episodes`` for the
        same reason :meth:`get_seasons` is: the apiclient helper takes no
        filter arguments, and this is the same request with two more.
        """
        api = self._conn(server_uuid).api
        result = api.shows('/%s/Episodes' % series_id, params={'UserId': '{UserId}', 'SeasonId': season_id, 'IsVirtualUnaired': False, 'IsMissing': False, 'Fields': LIST_FIELDS}) or {}
        return result.get('Items', [])

    def get_next_up(self, server_uuid, series_id):
        """The next episode to watch for a series (resume or next unwatched)."""
        api = self._conn(server_uuid).api
        result = api.get_next(limit=1, series_id=series_id, fields=LIST_FIELDS, enable_image_types='Primary,Thumb,Backdrop', image_type_limit=1) or {}
        items = result.get('Items', [])
        return items[0] if items else None

    def search(self, server_uuid, term, limit=SEARCH_LIMIT):
        """Everything matching ``term``, for the search screen to group.

        **The limit is shared across every type, which is why it is large.**
        The screen splits the answer into a row per type, so a budget of 60
        was not "60 of each" -- it was 60 between them, handed out in
        whatever order the server sorted them. One series with a matching
        name brings its episodes along, and a term that hits an episode
        title spends the lot: the movie the user was looking for never
        arrived, and the row for it simply did not appear (#641).

        jellyfin-web asks the same endpoint for 800 and splits client-side
        exactly as we do, so this is its number. It also turns the total
        count off, which we now do too -- nothing here shows a total, and
        counting matches is work the server can skip.
        """
        api = self._conn(server_uuid).api
        result = items_api.get_items(api, search_term=term, include_item_types=SEARCH_TYPES, recursive=True, limit=limit, fields=GRID_FIELDS, enable_total_record_count=False) or {}
        return result.get('Items', [])

    def image_spec(self, item, image_type='Primary', width=280, inherit=True):
        """Resolve which (item_id, type, tag) actually carries the image.

        Falls back from an item's own image to its series/parent image so
        episodes and seasons still show art. Returns ``None`` when there is no
        usable image (caller shows a placeholder).

        ``image_type`` is a *request*, not a promise: ``"Thumb"`` means "this
        is a landscape tile, find me something landscape-shaped", and is
        jellyfin-web's ``preferThumb: true`` (``cardbuilder/utils/url.ts``).
        ``"Primary"`` means a poster or square one. The type actually returned
        is whatever the chain found.

        ``inherit`` is web's ``inheritThumb``: with it off, a Thumb request
        stops borrowing from the series and the season and resolves against
        the item alone. Default on, matching web's default (its
        ``useEpisodeImagesInNextUpAndResume`` setting defaults to *false*,
        which reads as ``inheritThumb: true``).

        **The Thumb chain used to reach the item's own Primary second**, so a
        recorded episode -- which typically carries only a Primary while its
        series carries Thumb and Backdrop -- put a 2:3 poster in a 16:9 tile.
        Four landscape-shaped candidates come first now, which is web's order.

        **The Primary chain is deliberately not web's.** Ours ends with the
        channel logo (see below), which web has no equivalent for, and unlike
        web it does not fall back to a Backdrop -- that would put 16:9 art in
        a 2:3 tile, the same defect in the other direction.
        """
        tags = item.get('ImageTags') or {}
        backdrops = item.get('BackdropImageTags') or []
        if image_type in tags:
            return (item['Id'], image_type, tags[image_type])
        if image_type in GRID_IMAGE_TYPES:
            for candidate in _WORDMARK_CHAIN.get(image_type, ()):
                if candidate in tags:
                    return (item['Id'], candidate, tags[candidate])
                if candidate == 'Logo' and item.get('ParentLogoItemId') and item.get('ParentLogoImageTag'):
                    return (item['ParentLogoItemId'], 'Logo', item['ParentLogoImageTag'])
            image_type = 'Primary'
            if image_type in tags:
                return (item['Id'], image_type, tags[image_type])
        if item.get('Type') == 'Playlist':
            return (item['Id'], 'Primary', tags.get('Primary') or 'playlist')
        if image_type == 'ParentPrimary':
            owner = item.get('SeriesId') or item.get('ParentBackdropItemId')
            if owner:
                return (owner, 'Primary', item.get('SeriesPrimaryImageTag'))
        if image_type == 'Thumb':
            if inherit and item.get('SeriesId') and item.get('SeriesThumbImageTag'):
                return (item['SeriesId'], 'Thumb', item['SeriesThumbImageTag'])
            if inherit and item.get('ParentThumbItemId') and item.get('ParentThumbImageTag') and (item.get('MediaType') != 'Photo'):
                return (item['ParentThumbItemId'], 'Thumb', item['ParentThumbImageTag'])
            if backdrops:
                return (item['Id'], 'Backdrop', backdrops[0])
            parent_backdrops = item.get('ParentBackdropImageTags') or []
            if inherit and item.get('Type') == 'Episode' and item.get('ParentBackdropItemId') and parent_backdrops:
                return (item['ParentBackdropItemId'], 'Backdrop', parent_backdrops[0])
            if 'Primary' in tags:
                return (item['Id'], 'Primary', tags['Primary'])
        if item.get('PrimaryImageTag'):
            return (item['Id'], 'Primary', item['PrimaryImageTag'])
        if item.get('SeriesId') and item.get('SeriesPrimaryImageTag'):
            return (item['SeriesId'], 'Primary', item['SeriesPrimaryImageTag'])
        if image_type != 'Thumb' and item.get('ParentPrimaryImageItemId') and item.get('ParentPrimaryImageTag'):
            return (item['ParentPrimaryImageItemId'], 'Primary', item['ParentPrimaryImageTag'])
        if 'Thumb' in tags:
            return (item['Id'], 'Thumb', tags['Thumb'])
        if inherit and item.get('ParentThumbItemId') and item.get('ParentThumbImageTag'):
            return (item['ParentThumbItemId'], 'Thumb', item['ParentThumbImageTag'])
        if item.get('ParentPrimaryImageItemId') and item.get('ParentPrimaryImageTag'):
            return (item['ParentPrimaryImageItemId'], 'Primary', item['ParentPrimaryImageTag'])
        if item.get('ChannelId') and item.get('ChannelPrimaryImageTag'):
            return (item['ChannelId'], 'Primary', item['ChannelPrimaryImageTag'])
        if item.get('AlbumId') and item.get('AlbumPrimaryImageTag'):
            return (item['AlbumId'], 'Primary', item['AlbumPrimaryImageTag'])
        if 'Primary' in tags:
            return (item['Id'], 'Primary', tags['Primary'])
        return None

    def image_url(self, server_uuid, item_id, image_type, tag, width, height=None, fill=False, index=None):
        conn = self._conns.get(server_uuid)
        if conn is None:
            return None
        api = conn.api
        if index is None and image_type == 'Backdrop':
            index = 0
        if fill and height:
            return api.image_url(item_id, image_type, index=index, tag=tag, fill_width=int(width), fill_height=int(height), include_apikey=False)
        return api.image_url(item_id, image_type, index=index, tag=tag, max_width=int(width), include_apikey=False)

    def chapter_image_url(self, server_uuid, item_id, chapter_index, chapter, width=320):
        """URL for a chapter thumbnail, or None when the chapter has none."""
        tag = (chapter or {}).get('ImageTag')
        if not tag:
            return None
        return self.image_url(server_uuid, item_id, 'Chapter', tag, width, index=chapter_index)

    @staticmethod
    def backdrop_spec(item):
        """``(owner_item_id, image_type, tag)`` for the header banner, or
        None when this item has nothing wide enough to draw one from.

        The tag is part of the cache key, so it has to be the real one or the
        same item cached from another source — or against an older backdrop —
        is served forever. The *type* is in there too because the chain no
        longer ends at Backdrop.

        **The landscape fallbacks matter for video that is not a film.** A
        home video, a clip, a recording — anything scanned out of a folder
        rather than matched against a metadata provider — has no backdrop and
        never will, and the header was a blank grey box for it. What it does
        have is the still the server extracted, which is a frame of the thing
        itself and exactly what belongs across the top of its page.

        **A poster is not a fallback.** The banner is ~2.67:1; a 2:3 poster
        cropped into it is a horizontal strip through the middle of the key
        art, which is worse than the placeholder because it looks like a
        rendering fault rather than like missing artwork. So the Primary step
        is taken only when the item's own aspect ratio says it is landscape
        (``_LANDSCAPE_ART``), which is what tells a home video's extracted
        frame apart from a film's poster. Where that leaves nothing, the
        caller still draws its heading as text — see ``backdrop_node``.
        """
        tags = item.get('BackdropImageTags') or []
        if tags:
            return (item['Id'], 'Backdrop', tags[0])
        parent_tags = item.get('ParentBackdropImageTags') or []
        if parent_tags and item.get('ParentBackdropItemId'):
            return (item['ParentBackdropItemId'], 'Backdrop', parent_tags[0])
        image_tags = item.get('ImageTags') or {}
        if image_tags.get('Thumb'):
            return (item['Id'], 'Thumb', image_tags['Thumb'])
        if item.get('ParentThumbImageTag') and item.get('ParentThumbItemId'):
            return (item['ParentThumbItemId'], 'Thumb', item['ParentThumbImageTag'])
        ratio = item.get('PrimaryImageAspectRatio')
        if image_tags.get('Primary') and isinstance(ratio, (int, float)) and (ratio >= _LANDSCAPE_ART):
            return (item['Id'], 'Primary', image_tags['Primary'])
        return None

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        spec = self.backdrop_spec(item)
        if spec is None:
            return None
        owner_id, image_type, tag = spec
        return self.image_url(server_uuid, owner_id, image_type, tag, width, height=height, fill=fill, index=0 if image_type == 'Backdrop' else None)

class _OfflineSnapshot:
    """One immutable, internally-consistent view of the offline catalog.

    reload() builds a complete snapshot and publishes it with a single
    attribute assignment, so a reader that grabbed ``self._snap`` never sees
    a torn mix of new and old state (reload runs on an api-pool thread while
    the loop thread reads for artwork). Nothing mutates a snapshot's dicts
    after publish — except ``art_cache``, a memo of resolved artwork paths
    (safe: values are deterministic for the snapshot, so a racing double
    compute is idempotent)."""

    def __init__(self, rows=None, items=None, series_ids=None, season_ids=None, season_series=None, playlists=None, playlist_items=None, playlist_server=None, orphans=None, books=None, book_items=None, userdata=None, actors=None):
        self.rows = rows or {}
        self.userdata = userdata or {}
        self.actors = actors or {}
        self.items = items or []
        self.series_ids = set(series_ids or ())
        self.season_ids = set(season_ids or ())
        self.season_series = season_series or {}
        self.playlists = playlists or []
        self.playlist_items = playlist_items or {}
        self.playlist_server = playlist_server or {}
        self.orphans = orphans or set()
        self.books = books or []
        self.book_items = book_items or {}
        self.art_cache = {}

class OfflineLibrarySource:
    """LibrarySource-compatible browser backed by the offline catalog.

    Mirrors the normal browsing UI (libraries → grids → series → seasons →
    episodes) filtered to downloaded content, with artwork from local files.
    Reads the catalog read-only and caches rows in memory.
    """
    supported_filters = frozenset()
    _wanted = _built = 0

    def __init__(self, catalog_path, actor_on=None, server_name=None):
        """``actor_on(server_id)`` names the person browsing, per Jellyfin
        server -- ``UserManager.actor_on``. ``server_name(server_id)`` names
        the *server*, for telling two same-named playlists apart --
        ``UserManager.server_name_for``.

        Injected rather than imported: which local profile is at the
        keyboard, and what its servers are called, is the credential layer's
        business, and this class is already reached through the gateway.
        Without the first every row overlays the unattributed state, which is
        what a machine that cannot name anybody has; without the second two
        playlists of the same name simply stay ambiguous.
        """
        self.catalog_path = catalog_path
        self.actor_on = actor_on
        self.server_name = server_name
        self.root: Optional[str] = os.path.dirname(catalog_path) if catalog_path else None
        self._reload_lock = threading.Lock()
        self._snap = _OfflineSnapshot()
        self.reload()

    def mark_stale(self):
        """Have the next read re-read the catalog.

        Offline the catalog IS the server, and marks and progress made
        offline are written to it -- so a snapshot kept from the moment the
        browser went offline showed them, then lost them on the next
        re-read, and on every return to the page [iw, 2026-10-04]. Called on
        every route load; cheap here, and the rebuild runs on the reader's
        (pool) thread rather than the loop thread."""
        self._wanted += 1

    @property
    def _snap(self):
        if self._built != self._wanted:
            with self._reload_lock:
                wanted = self._wanted
                if self._built != wanted:
                    self.reload()
                    self._built = wanted
        return self._snapshot

    @_snap.setter
    def _snap(self, snap):
        self._snapshot = snap

    def reload(self):
        rows = []
        playlists = []
        playlist_rows = {}
        userdata = {}
        if self.catalog_path:
            try:
                db = SyncDB(self.catalog_path, read_only=True)
                try:
                    rows = db.list(status=STATUS_COMPLETE)
                    playlists = db.list_playlists()
                    for pl in playlists:
                        key = (pl['playlist_id'], pl.get('server_id'))
                        playlist_rows[key] = db.playlist_item_rows(pl['playlist_id'], server_id=pl.get('server_id'))
                    userdata = db.all_userdata()
                finally:
                    db.close()
            except Exception:
                log.warning('Failed to open offline catalog %s', self.catalog_path, exc_info=True)
                rows, playlists, playlist_rows = ([], [], {})
                userdata = {}
        actors = {}
        for row in rows:
            server_id = row.get('content_server_id') or NO_ACTOR
            if server_id not in actors:
                try:
                    actors[server_id] = (self.actor_on(server_id) if self.actor_on else None) or NO_ACTOR
                except Exception:
                    log.debug('could not resolve the browsing user for %s', server_id, exc_info=True)
                    actors[server_id] = NO_ACTOR
        by_id = {r['item_id']: r for r in rows}
        items = []
        series_ids: set[str] = set()
        season_ids: set[str] = set()
        season_series: dict[str, Any] = {}
        for row in rows:
            item = self._item_from_row(row, userdata, actors)
            if item is not None:
                items.append(item)
            if row.get('type') == 'Episode' and row.get('series_id'):
                series_ids.add(row['series_id'])
                if row.get('season_id'):
                    season_ids.add(row['season_id'])
                    season_series.setdefault(row['season_id'], row['series_id'])
        playlist_dtos, playlist_items, playlist_server = ([], {}, {})
        for pl in playlists:
            pid, scope = (pl['playlist_id'], pl.get('server_id'))
            pl_items = [self._item_from_row(r, userdata, actors) for r in playlist_rows.get((pid, scope), [])]
            pl_items = [i for i in pl_items if i is not None]
            if not pl_items:
                continue
            key = offline_playlist_id(pid, scope)
            playlist_dtos.append({'Id': key, 'Name': pl.get('name') or _('Playlist'), 'Type': 'Playlist', 'ServerId': scope, 'ImageTags': {}})
            playlist_items[key] = pl_items
            playlist_server[key] = scope
        orphans = {row['item_id'] for row in rows if row.get('item_id') and (not row.get('content_server_id'))}
        self._qualify_duplicate_playlists(playlist_dtos)
        books, book_items = self._book_shelf(self._without_orphans(items, orphans))
        self._snap = _OfflineSnapshot(rows=by_id, items=items, series_ids=series_ids, season_ids=season_ids, season_series=season_series, playlists=playlist_dtos, playlist_items=playlist_items, playlist_server=playlist_server, orphans=orphans, books=books, book_items=book_items, userdata=userdata, actors=actors)

    def stop(self):
        pass

    def servers(self):
        return [{'uuid': OFFLINE_SERVER_UUID, 'name': _('Downloaded')}]

    def server_address(self, server_uuid):
        """No server to link to: this source *is* the answer to the server
        being unreachable. The web-link button is simply not offered."""
        return None

    @staticmethod
    def _aggregate_userdata(episodes):
        """UserData for a synthesized Series/Season DTO, derived from its
        downloaded episodes. Without it the watched badge/label lies offline:
        ``is_watched`` falls back to UnplayedItemCount for these types, and a
        missing UserData reads as never-watched. Counts only what's downloaded
        — offline that IS the visible library."""
        unplayed = sum((1 for e in episodes if not (e.get('UserData') or {}).get('Played')))
        return {'Played': unplayed == 0, 'UnplayedItemCount': unplayed}

    def get_libraries(self, server_uuid):
        snap = self._snap
        libs = []
        if self._typed(snap, 'Movie'):
            libs.append({'Id': 'offline:movies', 'Name': _('Movies'), 'Type': 'CollectionFolder', 'CollectionType': 'movies', 'ImageTags': {}})
        if self._typed(snap, 'Video'):
            libs.append({'Id': 'offline:videos', 'Name': _('Videos'), 'Type': 'CollectionFolder', 'CollectionType': 'homevideos', 'ImageTags': {}})
        if self._typed(snap, 'Episode'):
            libs.append({'Id': 'offline:tv', 'Name': _('TV Shows'), 'Type': 'CollectionFolder', 'CollectionType': 'tvshows', 'ImageTags': {}})
        if snap.books:
            libs.append({'Id': 'offline:books', 'Name': _('Books'), 'Type': 'CollectionFolder', 'CollectionType': BOOKS_COLLECTION, 'ImageTags': {}})
        if snap.orphans:
            libs.append({'Id': 'offline:orphans', 'Name': _('Orphaned Items'), 'Type': 'CollectionFolder', 'CollectionType': 'folders', 'ImageTags': {}})
        if snap.playlists:
            libs.append({'Id': 'offline:playlists', 'Name': _('Playlists'), 'Type': 'CollectionFolder', 'CollectionType': 'playlists', 'ImageTags': {}})
        return libs

    def _qualify_duplicate_playlists(self, dtos):
        """Name two servers' same-named playlists apart, in place.

        Jellyfin hashes a playlist id from its name, so the collision that
        makes two tiles is *also* a collision of what they are called: both
        are "Music", and the scoped id that keeps them apart underneath is
        not something a user can see.

        **Only where there is a duplicate.** One server and a bare name is
        the normal case, and qualifying every playlist would be noise on
        every machine to serve the few with two.

        Left alone when nothing names the server: a raw ServerId is worse than
        an ambiguous tile, and the pair is still told apart by its contents
        and its art.
        """
        counts = {}
        for dto in dtos:
            counts[dto.get('Name')] = counts.get(dto.get('Name'), 0) + 1
        for dto in dtos:
            if counts.get(dto.get('Name'), 0) < 2:
                continue
            server = None
            if self.server_name:
                try:
                    server = self.server_name(dto.get('ServerId'))
                except Exception:
                    log.debug('could not name the server for playlist %s', dto.get('Id'), exc_info=True)
            if server:
                dto['Name'] = _('%(name)s (%(server)s)') % {'name': dto['Name'], 'server': server}

    @staticmethod
    def _typed(snap, *types):
        """Items of these types, **without the orphans**.

        Every place that partitions the library by type goes through here, and
        that is the whole point: an orphan listed under Movies *and* under
        Orphaned Items is in two places at once, and a rule applied at all but
        one site is this repository's recurring defect. The test that walks
        every category and counts an orphan exactly once is what guards it.

        The books shelf is the one site that cannot call this: it is built
        while the snapshot still is, so there is no `snap` to pass. It calls
        `_without_orphans` instead, which is the rule itself -- one
        implementation, two entrances, rather than the second copy that let
        this happen once already.
        """
        return OfflineLibrarySource._without_orphans([i for i in snap.items if i.get('Type') in types], snap.orphans)

    @staticmethod
    def _without_orphans(items, orphans):
        """The rule `_typed` applies, for a caller that has no snapshot yet.

        Separate from `_typed` only because of *when* the books shelf is
        built, never because the rule differs. R25: an unattributable download
        is listed under Orphaned Items and **nowhere else** -- an item in two
        categories at once is worse than an odd one.
        """
        return [i for i in items if i.get('Id') not in orphans]

    def _series_list(self, snap=None):
        snap = snap or self._snap
        episodes_by_series: dict[str, list[Any]]
        episodes_by_series, names, order = ({}, {}, [])
        for item in self._typed(snap, 'Episode'):
            sid = item.get('SeriesId')
            if not sid:
                continue
            if sid not in episodes_by_series:
                episodes_by_series[sid] = []
                names[sid] = item.get('SeriesName') or _('Series')
                order.append(sid)
            episodes_by_series[sid].append(item)
        return [{'Id': sid, 'Name': names[sid], 'Type': 'Series', 'ImageTags': {}, 'PrimaryImageAspectRatio': 2 / 3, 'UserData': self._aggregate_userdata(episodes_by_series[sid])} for sid in order]

    @staticmethod
    def _book_shelf(items):
        """The top level of the offline books library, and its containers.

        **A books library browses by folder, and offline there are no
        folders** — ``sync.manager._expand`` lists one and downloads its
        children, so the catalog holds leaves. Nothing else in the DTO puts
        them back together either, and the two halves of a books library
        disagree about which field would: measured against a real server, a
        ``Book`` carries ``SeriesName`` (the folder, or a real series when
        the file is tagged) and no ``Album``; an ``AudioBook`` carries
        ``Album`` and ``AlbumArtist`` and no ``SeriesName``; and neither
        carries ``ParentId`` under the ``Fields`` the downloader asks for.

        So the shelf is rebuilt from what is actually there, and only where
        it changes what can be done:

        * A **``Book``** is one file and one thing to read. It stands on its
          own, exactly as it does in a folder online.
        * **``AudioBook``s that share an album** are the chapters of one
          book, which is the case that needs a container: without one, a
          twelve-part recording is twelve tiles and playing "from chapter
          four" is the only way to start it. They get a synthesized
          ``Folder``, which is what ``BooksPage`` already draws as an album.
        * A **lone ``AudioBook``** is left alone, because a container around
          one chapter is a click that leads to the same thing — the same
          reason the online screens give a loose audiobook a page of its own.

        Grouping on ``AlbumId`` where the server gave one and on the album
        name otherwise: the id is stable across a retag, and the name is all
        an untagged rip has. A recording with neither is treated as loose,
        which is the honest answer — nothing joins those files.
        """
        loose, groups, order = ([], {}, [])
        for item in items:
            kind = item.get('Type')
            if kind == BOOK_TYPE:
                loose.append(item)
                continue
            if kind != AUDIOBOOK_TYPE:
                continue
            key = item.get('AlbumId') or (item.get('Album') or '').strip()
            if not key:
                loose.append(item)
                continue
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)
        shelf, contents = (list(loose), {})
        for key in order:
            members = groups[key]
            if len(members) == 1:
                shelf.append(members[0])
                continue
            first = members[0]
            members = sorted(members, key=lambda i: (i.get('IndexNumber') or 0, (i.get('Name') or '').lower()))
            cid = 'offline:book:%s' % key
            contents[cid] = members
            shelf.append({'Id': cid, 'Name': first.get('Album') or first.get('Name') or '', 'Type': 'Folder', 'ImageTags': {}, 'PrimaryImageAspectRatio': 1.0, 'AlbumArtist': first.get('AlbumArtist'), 'ChildCount': len(members), 'UserData': OfflineLibrarySource._aggregate_userdata(members)})
        return (shelf, contents)

    def get_home_prefs(self, server_uuid, refresh=False):
        """Signature parity with LibrarySource — see get_home_rows below for
        why parity matters here. There is no server to ask, and the offline
        rows are not the configurable ones, so this is always the default
        layout with nothing excluded."""
        return (list(home_sections.DEFAULT_LAYOUT), frozenset())

    def get_home_rows(self, server_uuid, libraries=None, sections=None, layout=None, latest_excludes=None):
        """Offline home rows.

        Every keyword is accepted for signature parity with LibrarySource, and
        must stay that way: _load_home fetches in two batches, and the offline
        source is what the failure path falls back TO. A TypeError here would
        fail that fallback, which re-triggers the fallback, which fails
        again — an unbounded retry loop rather than a degraded screen.

        ``layout`` and ``latest_excludes`` are ignored rather than honoured:
        these rows are "what you downloaded", not the server's configurable
        sections, so there is nothing for a section layout to reorder. They
        still carry ascending ``slot`` values so the caller's merge-by-slot
        works the same for both sources.

        Everything is local, so there is nothing to stagger: the whole set is
        returned for the primary batch and the latest batch adds nothing.
        """
        sections = tuple(LibrarySource.HOME_SECTIONS if sections is None else sections)
        if 'primary' not in sections:
            return []
        snap = self._snap
        rows: list[dict[str, Any]] = []
        movies = self._typed(snap, 'Movie')
        if movies:
            rows.append({'title': _('Downloaded Movies'), 'items': movies, 'collection_type': 'movies'})
        videos = self._typed(snap, 'Video')
        if videos:
            rows.append({'title': _('Downloaded Videos'), 'items': videos, 'collection_type': 'homevideos'})
        series = self._series_list(snap)
        if series:
            rows.append({'title': _('Downloaded Shows'), 'items': series, 'collection_type': 'tvshows'})
        if snap.books:
            rows.append({'title': _('Downloaded Books'), 'items': snap.books, 'collection_type': BOOKS_COLLECTION})
        for row in rows:
            row['slot'] = OFFLINE_ROW_SLOTS.index(row['collection_type'])
            row['kind'] = OFFLINE_ROW_KIND
        return rows

    @staticmethod
    def _apply_filters(items, filters):
        """Offline mirror of the server-side filter params. Genres live in the
        item_json snapshot (DETAIL_FIELDS includes them); synthesized series
        DTOs have none and simply drop out of a genre-filtered view."""
        if not filters:
            return items
        out = []
        for i in items:
            data = i.get('UserData') or {}
            if filters.get('unplayed') and data.get('Played'):
                continue
            if filters.get('favorite') and (not data.get('IsFavorite')):
                continue
            if filters.get('genre') and filters['genre'] not in (i.get('Genres') or []):
                continue
            if filters.get('year') and i.get('ProductionYear') != filters['year']:
                continue
            letter = filters.get('letter')
            if letter:
                first = (i.get('Name') or '?')[:1].upper()
                if letter == '#':
                    if first.isalpha():
                        continue
                elif first != letter:
                    continue
            out.append(i)
        return out

    def get_library_items(self, server_uuid, parent_id, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100, filters=None, image_type=None, collection_type=None):
        snap = self._snap
        if parent_id == 'offline:movies':
            items = self._typed(snap, 'Movie')
        elif parent_id == 'offline:videos':
            items = self._typed(snap, 'Video')
        elif parent_id == 'offline:orphans':
            items = [i for i in snap.items if i.get('Id') in snap.orphans]
        elif parent_id == 'offline:tv':
            items = self._series_list(snap)
        elif parent_id == 'offline:books':
            items = list(snap.books)
        elif parent_id in snap.book_items:
            items = snap.book_items[parent_id]
            return (items[start_index:start_index + limit], len(items))
        elif parent_id == 'offline:playlists':
            items = sorted(snap.playlists, key=lambda i: (i.get('Name') or '').lower())
            return (items[start_index:start_index + limit], len(items))
        else:
            items = []
        items = self._apply_filters(items, filters)
        items = sorted(items, key=lambda i: (i.get('Name') or '').lower())
        return (items[start_index:start_index + limit], len(items))

    def get_person_items(self, server_uuid, person_id, start_index=0, limit=100, sort_by='SortName', sort_order='Ascending'):
        return ([], 0)

    def has_live_tv(self, server_uuid):
        """There is no tuner in a folder of downloaded files.

        Declared rather than left to fail: the offline source is what the
        online one falls back TO, so a missing method here turns a degraded
        screen into an AttributeError on the fallback path.
        """
        return False

    def syncplay_access(self, server_uuid):
        """Nobody to sync with, and no server to ask. Declared for the same
        reason has_live_tv is."""
        from ..user_policy import NO_SYNCPLAY
        return NO_SYNCPLAY

    def can_manage_live_tv(self, server_uuid):
        return False

    def can_manage_collections(self, server_uuid):
        """No server to write to. Declared for the same reason the others
        are: the offline source is what the online one falls back TO."""
        return False

    def can_download(self, server_uuid):
        """Nothing to download FROM. The book screens read this to decide
        whether to offer a fetch, and offline the answer is no -- what is
        already on disk is still readable, and that path does not ask."""
        return False

    def can_refresh_metadata(self, server_uuid):
        """No server to re-scan. Declared for signature parity, like its
        siblings above: the offline source is what the online one falls back
        to, and a missing method there reads as an AttributeError on the
        loop thread rather than as an absent menu entry."""
        return False

    def get_genres(self, server_uuid, parent_id=None):
        genres: set[str] = set()
        for i in self._snap.items:
            genres.update(i.get('Genres') or [])
        return sorted(genres)

    def get_filter_values(self, server_uuid, parent_id=None, collection_type=None):
        years, ratings, tags = (set(), set(), set())
        for i in self._snap.items:
            if i.get('ProductionYear'):
                years.add(i['ProductionYear'])
            if i.get('OfficialRating'):
                ratings.add(i['OfficialRating'])
            tags.update(i.get('Tags') or ())
        return {'genres': self.get_genres(server_uuid, parent_id), 'years': sorted(years, reverse=True), 'official_ratings': sorted(ratings), 'tags': sorted(tags)}

    def get_similar(self, server_uuid, item_id, limit=12):
        return []

    def get_trailers(self, server_uuid, item_id):
        return []

    def get_special_features(self, server_uuid, item_id):
        return []

    def search_people(self, server_uuid, term, limit=PEOPLE_SEARCH_LIMIT):
        return []

    def search_artists(self, server_uuid, term, limit=ARTIST_SEARCH_LIMIT):
        return []

    def get_playlists(self, server_uuid, limit=300):
        return list(self._snap.playlists)

    def get_collections(self, server_uuid, limit=300):
        return []

    def get_shuffle_ids(self, server_uuid, parent_id, limit=QUEUE_LIMIT):
        snap = self._snap
        if parent_id == 'offline:tv':
            pool = self._typed(snap, 'Episode')
        elif parent_id == 'offline:movies':
            pool = self._typed(snap, 'Movie')
        elif parent_id == 'offline:videos':
            pool = self._typed(snap, 'Video')
        elif parent_id == 'offline:orphans':
            pool = [i for i in snap.items if i.get('Id') in snap.orphans]
        else:
            pool = []
        ids = [i['Id'] for i in pool if i.get('Id')]
        random.shuffle(ids)
        return ids[:limit]

    def get_play_all_ids(self, server_uuid, parent_id, sort_by='SortName', sort_order='Ascending', filters=None, limit=QUEUE_LIMIT, collection_type=None):
        """Downloaded items of a library, in the grid's order.

        Reuses the grid loader rather than re-deriving the pools: it already
        knows how each offline parent id maps onto the snapshot, and the two
        drifting is how Play All would come to queue something the grid does
        not show.
        """
        items, _total = self.get_library_items(server_uuid, parent_id, sort_by=sort_by, sort_order=sort_order, limit=limit, filters=filters)
        return [i['Id'] for i in items if i.get('Id') and i.get('MediaType') in QUEUEABLE_MEDIA]

    def chapter_image_url(self, server_uuid, item_id, chapter_index, chapter, width=320):
        return None

    def get_playlist_items(self, server_uuid, playlist_id):
        """Downloaded items of a playlist, in playlist order."""
        return list(self._snap.playlist_items.get(playlist_id, []))

    def get_seasons(self, server_uuid, series_id):
        snap = self._snap
        episodes_by_key: dict[str, list[Any]]
        seen, episodes_by_key, order = ({}, {}, [])
        for item in snap.items:
            if item.get('Type') != 'Episode' or item.get('SeriesId') != series_id:
                continue
            key = item.get('SeasonId') or 'p%s' % item.get('ParentIndexNumber')
            if key not in seen:
                pidx = item.get('ParentIndexNumber')
                if item.get('SeasonName'):
                    name = item['SeasonName']
                elif pidx == 0:
                    name = _('Specials')
                elif pidx is not None:
                    name = _('Season %d') % pidx
                else:
                    name = _('Episodes')
                seen[key] = {'Id': item.get('SeasonId') or key, 'Name': name, 'Type': 'Season', 'ImageTags': {}, 'SeriesId': series_id, 'SeriesName': item.get('SeriesName'), 'IndexNumber': pidx}
                episodes_by_key[key] = []
                order.append(key)
            episodes_by_key[key].append(item)
        for key in order:
            seen[key]['UserData'] = self._aggregate_userdata(episodes_by_key[key])
        return sorted((seen[k] for k in order), key=lambda s: (s.get('IndexNumber') is None, s.get('IndexNumber') or 0))

    def get_episodes(self, server_uuid, series_id, season_id):
        synthetic = isinstance(season_id, str) and season_id.startswith('p')
        eps = []
        for i in self._snap.items:
            if i.get('Type') != 'Episode' or i.get('SeriesId') != series_id:
                continue
            if synthetic:
                if 'p%s' % i.get('ParentIndexNumber') != season_id:
                    continue
            elif i.get('SeasonId') != season_id:
                continue
            eps.append(i)
        eps.sort(key=lambda i: (i.get('ParentIndexNumber') or 0, i.get('IndexNumber') or 0))
        return eps
    _NO_VIEWING = {'Played': False, 'PlaybackPositionTicks': 0, 'PlayCount': 0, 'IsFavorite': False, 'LastPlayedDate': None}

    def _userdata_for(self, row, userdata, actors):
        """This person's state for one row, in Jellyfin's DTO spelling.

        Empty when they have nothing recorded. **That is not the same as what
        the snapshot says**, which is what this docstring used to claim and
        what `_item_from_row` acted on: `item_json` is the DTO the server
        sent, stored verbatim, so its `UserData` is the watched flag and
        resume position of the account that asked for the download. A caller
        must overlay an empty answer rather than fall back to the snapshot --
        state is per actor and "there is no second copy"
        (`docs/offline-sync.md` section 1).
        """
        row_server = row.get('content_server_id')
        _state, key = filing_state(row_server, (row_server, actors.get(row_server or NO_ACTOR)))
        got = userdata.get((row['item_id'],) + key) if key else None
        if not got:
            return {}
        return {'Played': got['played'], 'PlaybackPositionTicks': got['position_ticks'], 'PlayCount': got['play_count'], 'IsFavorite': got['is_favorite'], 'LastPlayedDate': got['last_played_date']}

    def _item_from_row(self, row, userdata=None, actors=None):
        """Build an item DTO from a catalog row, overlaying **the browsing
        person's** live watched state and resume position onto the item_json
        snapshot frozen at download time.

        Without the overlay, offline resume positions and watched state were
        written but never read back -- playback always restarted from the
        beginning after a relaunch.

        Per actor since the state moved off the download row's own blob (a
        column CX8 has since dropped): overlaying a machine-global blob meant
        the second account to open a film resumed where the first stopped, and
        then reported that position to its own server as its own viewing.
        """
        try:
            item = json.loads(row['item_json'])
        except (TypeError, ValueError):
            return None
        userdata = self._userdata_for(row, self._snap.userdata if userdata is None else userdata, self._snap.actors if actors is None else actors)
        merged = dict(item.get('UserData') or {})
        merged.update(userdata or self._NO_VIEWING)
        pos = merged.get('PlaybackPositionTicks')
        runtime = row.get('runtime_ticks') or item.get('RunTimeTicks')
        if pos and runtime:
            merged['PlayedPercentage'] = min(pos / runtime * 100, 100.0)
        else:
            merged.pop('PlayedPercentage', None)
        item['UserData'] = merged
        return item

    def get_item(self, server_uuid, item_id):
        snap = self._snap
        row = snap.rows.get(item_id)
        if row and row.get('item_json'):
            item = self._item_from_row(row)
            if item is not None:
                return item
        if item_id in snap.series_ids:
            eps = [i for i in snap.items if i.get('SeriesId') == item_id and i.get('Type') == 'Episode']
            name = next((i.get('SeriesName') for i in eps), _('Series'))
            return {'Id': item_id, 'Name': name, 'Type': 'Series', 'ImageTags': {}, 'UserData': self._aggregate_userdata(eps)}
        if item_id in snap.book_items:
            return next((b for b in snap.books if b.get('Id') == item_id), None)
        return None

    def get_series_queue(self, server_uuid, series_id, start_item_id=None, limit=100):
        eps = [i for i in self._snap.items if i.get('Type') == 'Episode' and i.get('SeriesId') == series_id]
        eps.sort(key=lambda i: (i.get('ParentIndexNumber') or 0, i.get('IndexNumber') or 0))
        if start_item_id:
            ids = [e.get('Id') for e in eps]
            if start_item_id in ids:
                eps = eps[ids.index(start_item_id):]
        return eps[:limit]

    def get_season_queue(self, server_uuid, series_id, season_id):
        """Signature parity with the live source. The catalog holds only what
        was actually downloaded, so there is nothing missing or unaired in it
        to filter out -- `get_episodes` already returns the season in order.
        """
        return self.get_episodes(server_uuid, series_id, season_id)

    def get_by_name_sections(self, server_uuid, spec, limit=20):
        """Empty offline: every row is a server-side predicate over the
        whole library. Signature parity with the live source."""
        return []

    def get_genre_sections(self, server_uuid, parent_id, collection_type, limit=10, max_genres=40):
        """Empty offline: a genre row is a server-side GenreIds query over
        the whole library. Signature parity, as with every other source
        method -- the offline catalog is the failure path's fallback."""
        return []

    def get_view_settings(self, server_uuid, parent_id, collection_type):
        """No server, no saved view settings. Signature parity."""
        return {}

    def get_favorite_sections(self, server_uuid, limit=24):
        """Empty offline, and hidden rather than shown empty -- see the
        Favorites nav button. Present for signature parity: the offline
        source is what a failed load falls back TO."""
        return []

    def get_list(self, server_uuid, spec, sort_by='SortName', sort_order='Ascending', start_index=0, limit=100, filters=None):
        """Signature parity with the live source, which is load-bearing: the
        offline catalog is what a failed load falls back TO, so a call it
        cannot accept makes the fallback itself raise.

        Answers empty rather than guessing. Every list type is a server-side
        predicate -- still-to-air programmes, a genre across a library, the
        favourites -- and a downloaded subset cannot stand in for any of
        them; a partial answer here would read as "you have nothing in this
        genre" rather than "this needs the server".
        """
        return ([], 0)

    def get_next_up(self, server_uuid, series_id):
        eps = self.get_series_queue(server_uuid, series_id)
        for ep in eps:
            if not (ep.get('UserData') or {}).get('Played'):
                return ep
        return eps[0] if eps else None

    def search(self, server_uuid, term, limit=SEARCH_LIMIT):
        needle = term.lower()
        return [i for i in self._snap.items if needle in (i.get('Name') or '').lower()][:limit]

    @staticmethod
    def _name_for(image_type):
        kind = (image_type or '').lower()
        if kind.startswith('backdrop'):
            return 'backdrop.jpg'
        if kind.startswith('thumb'):
            return 'thumb.jpg'
        return 'poster.jpg'

    def _in_dir(self, item_dir, name):
        path = os.path.join(item_dir, name)
        if os.path.exists(path):
            return path
        fallback = os.path.join(item_dir, 'poster.jpg')
        return fallback if os.path.exists(fallback) else None

    def _art_path(self, item_id, image_type, snap=None):
        """Resolve an item's local artwork file. Memoized per snapshot: this
        runs on the loop thread from tile compositing, and each uncached call
        costs several os.path.exists probes (a real stutter source when the
        download folder lives on a network share). The memo dies with its
        snapshot, so a reload invalidates it automatically."""
        snap = snap or self._snap
        cache_key = (item_id, image_type)
        try:
            return snap.art_cache[cache_key]
        except KeyError:
            pass
        path = self._art_path_uncached(item_id, image_type, snap)
        snap.art_cache[cache_key] = path
        return path

    def _art_path_uncached(self, item_id, image_type, snap):
        if not self.root or not item_id:
            return None
        name = self._name_for(image_type)
        row = snap.rows.get(item_id)
        if row and row.get('file_path'):
            return self._in_dir(os.path.join(self.root, os.path.dirname(row['file_path'])), name)
        if item_id in snap.series_ids:
            series_dir = series_art_dir(self.root, item_id)
            return self._in_dir(series_dir, name)
        if item_id in snap.season_ids:
            season_dir = season_art_dir(self.root, item_id)
            found = self._in_dir(season_dir, name)
            if found:
                return found
            series_id = snap.season_series.get(item_id)
            if series_id:
                return self._art_path(series_id, image_type, snap)
            return None
        if item_id in snap.playlist_server:
            real_id, _scope = split_offline_playlist_id(item_id)
            return self._in_dir(playlist_art_dir(self.root, snap.playlist_server.get(item_id), real_id), name)
        if item_id == 'offline:movies':
            return self._representative(('Movie',), snap, self.root)
        if item_id == 'offline:videos':
            return self._representative(('Video',), snap, self.root)
        if item_id == 'offline:tv':
            for series_id in snap.series_ids:
                path = self._art_path(series_id, 'Primary', snap)
                if path:
                    return path
            return self._representative(('Episode',), snap, self.root)
        if item_id == 'offline:playlists':
            for pid in snap.playlist_server:
                path = self._art_path(pid, image_type, snap)
                if path:
                    return path
            return None
        members = snap.book_items.get(item_id)
        if members:
            for member in members:
                path = self._art_path(member.get('Id'), image_type, snap)
                if path:
                    return path
            return None
        if item_id == 'offline:books':
            return self._representative((BOOK_TYPE, AUDIOBOOK_TYPE), snap, self.root)
        return None

    def _representative(self, types, snap, root: str):
        """Artwork from any downloaded item of ``types``, as a stand-in for a
        synthetic library tile.

        ``root`` is passed in rather than read from ``self``: every caller is
        downstream of ``_art_path_uncached``'s "no root" early return, so it
        is known non-None there. Taking it as an argument puts that in the
        signature instead of in a comment — a fourth call site from somewhere
        that had not checked would previously have produced
        ``os.path.join(None, ...)``, and no amount of casting would have
        caught it.
        """
        for row in snap.rows.values():
            if row.get('type') in types and row.get('file_path'):
                path = self._in_dir(os.path.join(root, os.path.dirname(row['file_path'])), 'poster.jpg')
                if path:
                    return path
        return None

    def image_spec(self, item, image_type='Primary', width=280, inherit=True):
        if self._art_path(item.get('Id'), image_type):
            return (item['Id'], image_type, 'offline')
        return None

    def image_url(self, server_uuid, item_id, image_type, tag, width, height=None, fill=False, index=None):
        return self._art_path(item_id, image_type)

    @staticmethod
    def backdrop_spec(item):
        """Cache-key spec matching LibrarySource.backdrop_spec. The "offline"
        sentinel keeps offline header art keyed apart from the online tags, so
        source switches can't serve each other's cached bitmaps.

        Three-tuple like the online one, and the middle element is the
        sentinel rather than a type: a downloaded item has exactly one header
        image on disk whatever it was online, so there is no chain here to
        name a step of."""
        return (item.get('Id'), 'offline', 'offline')

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        return self._art_path(item.get('Id'), 'Backdrop')

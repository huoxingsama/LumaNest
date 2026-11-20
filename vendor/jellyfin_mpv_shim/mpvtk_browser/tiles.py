"""Tiles: rendering, art, and the tile context menu.

Poster fetch/decode plumbing, the tile / row / grid builders, the detail
banner compositor, the track list, and the right-click menu on a tile.

State on ``self``: ``_requested`` and ``_img_retry`` (the
image cache — ``_image_done`` runs on a pool thread and writes then
``invalidate()``s), the ``_downloaded*`` sets behind the tile badge, and
``_menu`` (the open context menu, loop thread only).
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from ..i18n import _, _p
from ..extras import is_extra
from ..utils import launches_as_audio
from ..mpvtk.widgets import Menu
from . import components
from .repository import PLAYABLE_TYPES, PLAYLIST_SUPPORTED_TYPES
log = logging.getLogger('mpvtk_browser.tiles')

class TilesMixin:
    """The tile context menu, plus forwarders to the renderer.

    Tile/row/grid CONSTRUCTION moved to tile_renderer.TileRenderer (step 6b).
    What is left needs route, navigate, run_async and the gateway -- it is
    page work, not rendering. The forwarders below stay until every caller is
    a Page reaching the renderer through its context.
    """

    def _tile_row(self, *a, **k):
        return self.tiles.tile_row(*a, **k)

    def _track_list(self, *a, **k):
        return self.tiles.track_list(*a, **k)

    def _banner_box(self, *a, **k):
        return self.tiles.banner_box(*a, **k)

    def _is_downloaded(self, item):
        return self.tiles.is_downloaded(item)

    def _cols(self, *a, **k):
        return self.tiles.cols(*a, **k)

    def _tile(self, *a, **k):
        return self.tiles._tile(*a, **k)

    def _open_tile_menu(self, item, x, y):
        if not self._tile_menu_entries(item):
            return
        self._menu = {'item': item, 'server': self.route.get('server') or self.server, 'x': x, 'y': y}
        self.invalidate()

    def _close_menu(self):
        self._menu = None
        self.invalidate()
    MENU_PLAYABLE = PLAYABLE_TYPES | {'Audio', 'AudioBook', 'MusicAlbum', 'MusicArtist', 'MusicGenre', 'Series', 'Season', 'Playlist'}
    MENU_WATCHED = PLAYABLE_TYPES | {'Series', 'Season', 'Folder', 'AudioBook'}
    MENU_FAVORITE = MENU_PLAYABLE - {'MusicGenre'}
    MENU_ADD_TO = PLAYABLE_TYPES | {'Audio', 'AudioBook', 'MusicAlbum', 'MusicArtist', 'MusicGenre', 'Series', 'Season'}
    MENU_DOWNLOAD = PLAYABLE_TYPES | {'Audio', 'AudioBook', 'Book', 'Series', 'Season', 'Playlist'}
    MENU_READ = {'Book'}
    MENU_LIVE = {'TvChannel', 'Program'}
    MENU_MEDIA_INFO = PLAYABLE_TYPES | {'Audio', 'AudioBook'}
    MENU_RESUMABLE = PLAYABLE_TYPES | {'AudioBook'}
    MENU_REFRESH = (MENU_PLAYABLE | MENU_WATCHED | {'Book', 'Photo', 'PhotoAlbum', 'BoxSet'}) - MENU_LIVE
    CHIP_CONTAINERS = {'BoxSet', 'Folder', 'PhotoAlbum'}

    def _tile_playable(self, item):
        """Whether a hovered tile gets a play chip. Cheap and pure: this runs
        for every tile of every strip that is built."""
        if components.virtual_episode_label(item):
            return False
        t = item.get('Type')
        if t in self.MENU_LIVE or t in self.MENU_PLAYABLE:
            return True
        if t in self.CHIP_CONTAINERS and self._in_books_library():
            return False
        return t in self.CHIP_CONTAINERS and (not item.get('CollectionType'))

    def _in_books_library(self):
        """Whether the screen being drawn is inside a books library.

        Read off the route rather than the item because it is not on the
        item: a folder's own DTO has no CollectionType, which is exactly
        why the browse route carries one down the tree (see
        MpvtkBrowser._open_item).
        """
        from .repository import BOOKS_COLLECTION
        return self.route.get('collection_type') == BOOKS_COLLECTION

    def _play_tile(self, item):
        """What the hover chip does. The tile's own click still opens the
        page -- these are different questions for anything with contents."""
        server = self.route.get('server') or self.server
        t = item.get('Type')
        if t == 'Series':
            self._actions.play_next_up(item.get('Id'), server)
            return
        if t in self.CHIP_CONTAINERS:
            self._play_container(item, server)
            return
        self._menu_play(item, server)

    def _play_container(self, item, server):
        """A collection, a folder or a library: its contents, in the order
        the grid shows them."""
        source = self.source
        parent = item.get('Id')
        ctype = item.get('CollectionType')
        ep = self._epoch

        def work():
            return source.get_play_all_ids(server, parent, collection_type=ctype)

        def done(ids):
            if ids:
                self._actions.play_list(ids, server, 0, pause_stills=False, audio=launches_as_audio(item, ctype))
            else:
                self.set_status(_('There is nothing here to play.'))
        self.run_async(work, done, ep)

    def _live_menu_entries(self, item):
        """Menu for a channel or a guide entry.

        The guide's context menu in jellyfin-web, which is where recording a
        programme from a listing actually happens — without it the only way
        to set a timer is to open each programme in turn.
        """
        from . import live_tv
        out = []
        if item.get('Type') == 'TvChannel':
            out.append((_('Watch'), 'play_arrow', 'play'))
            return out
        if item.get('ChannelId'):
            out.append((_('Watch Channel'), 'play_arrow', 'play'))
        if not self._actions.can_record(self.route.get('server') or self.server):
            return out
        single = live_tv.single_timer_state(item)
        if single:
            out.append((_('Stop Recording') if single == 'recording' else _('Do not record'), 'cancel', 'unrecord'))
        else:
            out.append((_('Record'), 'fiber_manual_record', 'record'))
        if item.get('IsSeries'):
            if item.get('SeriesTimerId'):
                out.append((_('Cancel Series'), 'cancel', 'unrecordseries'))
            else:
                out.append((_('Record series'), 'fiber_smart_record', 'recordseries'))
        return out

    def _tile_menu_entries(self, item):
        """``[(label, icon, action-key)]`` for this item's type."""
        t = item.get('Type')
        ud = item.get('UserData') or {}
        watched = components.is_watched(item)
        fav = bool(ud.get('IsFavorite'))
        out = []
        if t in self.MENU_LIVE:
            out = self._live_menu_entries(item)
            if t == 'TvChannel':
                out.append((_('Remove from favorites') if fav else _('Add to favorites'), 'favorite', 'favorite'))
            return out
        if t in self.MENU_PLAYABLE and (not components.virtual_episode_label(item)):
            pos = ud.get('PlaybackPositionTicks') or 0 if t in self.MENU_RESUMABLE else 0
            if pos > 0:
                out.append((_('Resume'), 'play_arrow', 'play'))
                out.append((_('Play from beginning'), 'first_page', 'restart'))
            else:
                out.append((_('Play'), 'play_arrow', 'play'))
            out.append((_('Add to play queue'), 'playlist_add', 'queue'))
            if self._is_playing():
                out.append((_('Play next'), 'queue_play_next', 'queuenext'))
        if item.get('SeriesId') and (not (self.route.get('kind') == 'series' and self.route.get('item_id') == item['SeriesId'])):
            out.append((_('Go to Series'), 'movie', 'goseries'))
        if t in self.MENU_WATCHED:
            out.append((_('Mark unplayed') if watched else _('Mark played'), 'check', 'watched'))
        if t in self.MENU_FAVORITE:
            out.append((_('Remove from favorites') if fav else _('Add to favorites'), 'favorite', 'favorite'))
        if t in self.MENU_READ:
            out.append((_p('open a book', 'Read'), 'menu_book', 'read'))
        if t in self.MENU_ADD_TO and (not self._offline) and self._edit_apis():
            out.append((_('Add to playlist'), 'queue_music', 'addto'))
        if t in self.MENU_DOWNLOAD and (not self._offline) and (not is_extra(item)):
            out.append((_('Download'), 'file_download', 'download'))
        if t in self.MENU_MEDIA_INFO:
            out.append((_('Media Info'), 'info', 'mediainfo'))
        if t in self.MENU_REFRESH and (not self._offline):
            from . import live_tv
            if not live_tv.is_recording_now(item) and self._actions.can_refresh_metadata(self.route.get('server') or self.server):
                out.append((_('Refresh metadata'), 'refresh', 'refresh'))
        if not self._offline and self._actions.can_delete(item):
            out.append((_('Delete from Disk'), 'delete', 'deleteitem'))
        if self.route.get('kind') == 'playlist' and item.get('PlaylistItemId') and (not self._offline) and self._edit_apis():
            out.append((_('Remove from playlist'), 'delete', 'unplaylist'))
        if self.route.get('parent_type') == 'BoxSet' and item.get('Id') and (not self._offline) and self._actions.can_manage_collections(self.route.get('server') or self.server):
            out.append((_('Remove from collection'), 'delete', 'uncollect'))
        return out

    def _is_playing(self):
        """Whether there is a queue to insert into.

        Read off the now-playing snapshot the shell already keeps rather
        than asking the player: this runs while a menu is being built, on
        the loop thread, and that snapshot is what the rest of the chrome
        trusts. It is set while a queue is still starting, which is the
        right answer -- there will be something to queue behind.
        """
        return bool(getattr(self, '_now_playing', None))

    def _edit_apis(self):
        return self._actions.can_edit()

    def _tile_menu_node(self):
        m = self._menu
        entries = self._tile_menu_entries(m['item'])
        if not entries:
            return None
        return Menu('tilemenu', [e[0] for e in entries], m['x'], m['y'], icons=[e[1] for e in entries], on_select=self._menu_action, on_dismiss=self._close_menu)

    def _menu_action(self, index, value):
        m = self._menu
        if m is None:
            return
        item, server = (m['item'], m['server'])
        entries = self._tile_menu_entries(item)
        if not 0 <= index < len(entries):
            return self._close_menu()
        action = entries[index][2]
        if action == 'play':
            self._menu_play(item, server)
        elif action == 'restart':
            self._menu_play(item, server, resume=False)
        elif action == 'queue':
            self._menu_queue(item, server)
        elif action == 'queuenext':
            self._menu_queue(item, server, next_up=True)
        elif action == 'watched':
            self._act_watched(item, server)
        elif action == 'favorite':
            self._act_favorite(item, server)
        elif action == 'addto':
            self._close_menu()
            self._open_add_to(item)
            return
        elif action == 'goseries':
            self._close_menu()
            self._go_to_series(item, server)
            return
        elif action == 'read':
            self._close_menu()
            self._menu_read(item, server)
            return
        elif action == 'download':
            self._close_menu()
            self._open_download(item)
            return
        elif action == 'mediainfo':
            self._close_menu()
            self._open_media_info(item, server)
            return
        elif action == 'refresh':
            self._close_menu()
            self._actions.refresh_metadata(item, server)
            return
        elif action == 'deleteitem':
            self._close_menu()
            self._actions.confirm_delete_item(item, server, on_done=self._reload_after_delete)
            return
        elif action == 'unplaylist':
            self._close_menu()
            self._remove_from_playlist(item)
            return
        elif action == 'uncollect':
            self._close_menu()
            self._remove_from_collection(item)
            return
        elif action in ('record', 'recordseries', 'unrecord', 'unrecordseries'):
            self._close_menu()
            self._live_menu_action(action, item, server)
            return
        self._close_menu()

    def _go_to_series(self, item, server):
        """Open the show an episode or season belongs to (#719).

        The same route `pages/detail.py`'s "Go to Series" button builds, so
        the two doors land on the same page -- and `SeriesName` for the
        title, because the route's title is what the top bar shows before
        the series DTO has loaded.
        """
        self.navigate({'kind': 'series', 'server': server, 'item_id': item['SeriesId'], 'title': item.get('SeriesName', '')})

    def _menu_read(self, item, server):
        """Open a book from its tile.

        The split is `books.book_format`'s, not this menu's, and it is the
        same call the book's own page makes: an epub opens in the window,
        anything else is downloaded and handed to the desktop. Deciding it
        here would be a second place to keep in step with what the reader
        can actually draw.
        """
        from ..books import reader_route
        kind = reader_route(item)
        if kind:
            self.navigate({'kind': kind, 'server': server, 'item_id': item.get('Id'), 'title': item.get('Name', '')})
            return
        self._actions.read_book(item, server)

    def _live_menu_action(self, action, item, server):
        """Record / cancel from a tile or guide cell.

        Followed by a reload of the screen rather than an optimistic flip:
        the DTO on the tile has no timer id until the server issues one, so
        there is nothing to flip to (see ItemActions).
        """
        route = self.route

        def done():
            self._reload_route(route)
        if action == 'record':
            self._actions.schedule_recording(item, server, on_done=done)
        elif action == 'recordseries':
            self._actions.schedule_recording(item, server, series=True, on_done=done)
        elif action == 'unrecord':
            self._actions.cancel_timer(item.get('TimerId'), server, on_done=done)
        else:
            self._actions.cancel_series_timer(item.get('SeriesTimerId'), server, on_done=done)

    def _reload_after_delete(self):
        """Re-read the list the deleted tile came from.

        Same shape as ``_do_remove_from_collection``'s re-read, and for the
        same reason: the grid still lists what is no longer there, and a
        tile pointing at nothing invites a second press. Dropping ``_items``
        as well as ``_loading`` matters -- a load that finds the route
        already holding items returns without fetching.
        """
        route = self.route
        route.pop('_items', None)
        route.pop('_data', None)
        route.pop('_loading', None)
        self._load_route(route)

    def _remove_from_playlist(self, item):
        entry = item.get('PlaylistItemId')
        pid = self.route.get('item_id')
        server = self.route.get('server') or self.server
        if not (entry and pid):
            return
        self._confirm(_('Remove %s from this playlist?') % item.get('Name', ''), lambda: self._do_remove_from_playlist(server, pid, entry), title=_('Remove from playlist'), yes=_('Remove'))

    def _remove_from_collection(self, item):
        cid = self.route.get('parent_id')
        server = self.route.get('server') or self.server
        iid = item.get('Id')
        if not (cid and iid):
            return
        self._confirm(_('Remove %s from this collection?') % item.get('Name', ''), lambda: self._do_remove_from_collection(server, cid, iid), title=_('Remove from collection'), yes=_('Remove'))

    def _do_remove_from_collection(self, server, collection_id, item_id):
        route = self.route
        ep = self._epoch

        def work():
            self.controller.collection_remove(server, collection_id, [item_id])

        def done(_ok):
            route.pop('_items', None)
            route.pop('_loading', None)
            self._load_route(route)

        def failed(_exc):
            self.set_status(_('The change could not be applied.'))
        self.run_async(work, done, ep, on_error=failed)

    def _do_remove_from_playlist(self, server, playlist_id, entry_id):
        ep = self._epoch

        def work():
            self.controller.playlist_remove(server, playlist_id, [entry_id])
            return self.source.get_playlist_items(server, playlist_id)

        def done(items):
            self.route['_data'] = items
            self.invalidate()

        def failed(_exc):
            self.set_status(_('The change could not be applied.'))
        self.run_async(work, done, ep, on_error=failed)

    def _menu_queue(self, item, server, next_up=False):
        """Add to the playing queue -- at the end, or (``next_up``) straight
        after the current item. A music container is resolved to its tracks
        first — queueing the container id itself is meaningless."""
        ep = self._epoch
        parent = self.route.get('parent_id')

        def work():
            return self._resolve_play_ids(item, server, parent)

        def done(ids):
            if ids:
                self._queue_items(ids, server, next_up=next_up)
            else:
                self.set_status(_('There is nothing here to queue.'))
                self.invalidate()

        def failed(_exc):
            self.set_status(_('Those items could not be added to the queue.'))
            self.invalidate()
        self.run_async(work, done, ep, on_error=failed)

    def _resolve_play_ids(self, item, server, parent_id=None):
        """Ids only, for callers that queue without deciding a mode."""
        return [i.get('Id') for i in self._resolve_play_items(item, server, parent_id) if i.get('Id')]

    def _resolve_play_items(self, item, server, parent_id=None):
        """The item DTOs "Play"/"Add to play queue" should act on.

        **DTOs, not ids, because the container lies.** A playlist's own
        `MediaType` is computed by the server from its contents, and a
        playlist can arrive looking like video while holding music [iw] --
        so the only reliable answer to "is this launch audio" is the first
        thing actually being queued. It is also the right answer for a
        MIXED playlist, where what starts is what decides.

        A music container (album/artist/playlist/series) is not itself a
        playable item — queueing or playing its own id does nothing, which
        is why Play on an album tile used to just navigate. Runs off the
        loop thread: these hit the server.

        ``parent_id`` (a genre's library) must be captured by the CALLER on
        the loop thread. Reading self.route here raced navigation: a genre
        could resolve against whatever page the user had moved on to."""
        t, iid = (item.get('Type'), item.get('Id'))
        if not iid:
            return []
        try:
            if t == 'MusicAlbum':
                return list(self.source.get_album_tracks(server, iid))
            if t == 'MusicArtist':
                return list(self.source.get_artist_songs(server, iid))
            if t == 'Playlist':
                return [i for i in self.source.get_playlist_items(server, iid) if i.get('Type') in PLAYLIST_SUPPORTED_TYPES]
            if t == 'MusicGenre':
                return list(self.source.get_genre_songs(server, parent_id, iid))
            if t == 'Season':
                return list(self._season_queue(item, server))
            if t == 'Series':
                return list(self.source.get_series_queue(server, iid))
        except Exception:
            log.warning('could not resolve %s for playback', t, exc_info=True)
            return []
        return [item]

    def _season_queue(self, item, server):
        """A season's episodes, in order. Runs off the loop thread.

        **A Season is not a Series id.** This used to go through
        `get_series_queue`, which asks `Shows/{id}/Episodes` -- and a season
        id there is not a series, so the server answered with nothing, and
        `_menu_play`'s empty-result branch fell through to `_open_item`. The
        play chip on a season opened the season, which is #720.
        """
        series_id = item.get('SeriesId')
        if not series_id:
            log.warning('season %s carries no SeriesId', item.get('Id'))
            return []
        return self.source.get_season_queue(server, series_id, item.get('Id'))

    @staticmethod
    def _first_unplayed(items):
        """Index of the first unwatched entry, or 0 if they all are.

        jellyfin-web's rule for a season's Play button
        (`playbackmanager.js`, `getSeriesOrSeasonPlaybackPromise`): a season
        queues WHOLE and starts at the first unplayed episode. Deliberately
        not Next Up -- web skips that lookup when a season is named, because
        "play this season" and "carry on with this show" are different
        questions and only the second one may leave the season.

        Web spells the fallback `StartIndex || seasonStartIndex || 0`, which
        works only because index 0 is falsy; this returns the real index.
        """
        for i, e in enumerate(items or ()):
            if not (e.get('UserData') or {}).get('Played'):
                return i
        return 0

    def _menu_play(self, item, server, resume=True):
        t = item.get('Type')
        if t in self.MENU_LIVE:
            self._play_list([item.get('ChannelId') or item.get('Id')], server)
            return
        if t in ('Audio', 'AudioBook'):
            items = [item] if resume and t == 'AudioBook' else None
            self._play_list([item.get('Id')], server, audio=True, items=items)
            return
        if t in PLAYABLE_TYPES:
            offset = None
            if resume:
                offset = (item.get('UserData') or {}).get('PlaybackPositionTicks') or None
            self._play(item, server, offset_ticks=offset)
            return
        if t == 'Season':
            self._play_season(item, server)
            return
        ep = self._epoch
        parent = self.route.get('parent_id')

        def work():
            return self._resolve_play_items(item, server, parent)

        def done(items):
            if items:
                ids = [i.get('Id') for i in items if i.get('Id')]
                self._play_list(ids, server, 0, audio=launches_as_audio(item, first=items[0]))
            else:
                self._open_item(item)
        self.run_async(work, done, ep)

    def _play_season(self, item, server):
        """Play a whole season from its first unplayed episode (#720)."""
        ep = self._epoch

        def work():
            try:
                return self._season_queue(item, server)
            except Exception:
                log.warning('could not resolve the season for playback', exc_info=True)
                return []

        def done(eps):
            eps = [e for e in eps or () if e.get('Id')]
            if not eps:
                self._open_item(item)
                return
            self._play_list([e['Id'] for e in eps], server, self._first_unplayed(eps), items=eps)
        self.run_async(work, done, ep)

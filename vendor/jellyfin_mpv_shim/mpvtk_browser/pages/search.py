"""The search results screen — first route converted to a :class:`Page`.

Chosen to go first because it is representative without being large: it has
both a loader and a renderer, it fans results into typed groups (so it
exercises the art context), and it is one of the seven screens pinned by
``tests/test_scene_snapshots.py`` — which is what makes "the move changed
nothing" checkable rather than asserted.

Body is moved verbatim from ``ViewsMixin._load_search`` / ``_render_search``.
The only edits are ``self.X`` -> ``self.ctx.X``. It originally spent nine
``ctx.shell`` uses -- the entire budget -- on the tile and chrome helpers;
step 6c's two prep commits gave every one of them a real home, and it now
reaches the shell for nothing.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from ...books import AUDIOBOOK_TYPE, BOOK_TYPE
from ...i18n import _
from ...mpvtk.widgets import Column, Text, VScroll
from .. import theme
from ..components import chrome
from .base import Page
log = logging.getLogger('mpvtk_browser.pages.search')
ROW_MAX = 60
AUTO_SHAPE = object()
PEOPLE_ROW = object()
ARTISTS_ROW = object()
SONGS_ROW = object()

class SearchPage(Page):
    kind = 'search'

    def load(self, epoch):
        srv = self.route.get('server') or self.ctx.server
        term = self.route.get('term', '')
        source = self.ctx.source
        route = self.route

        def work():
            if not term:
                return {'items': [], 'people': [], 'artists': [], 'live': {}}
            items = source.search(srv, term)
            people = []
            try:
                people = source.search_people(srv, term)
            except Exception:
                pass
            artists = []
            search_artists = getattr(source, 'search_artists', None)
            if search_artists is not None:
                try:
                    artists = search_artists(srv, term)
                except Exception:
                    log.debug('artist search failed', exc_info=True)
            live = {}
            if getattr(source, 'has_live_tv', lambda _s: False)(srv):
                search_live = getattr(source, 'search_live_tv', None)
                if search_live is not None:
                    live = search_live(srv, term) or {}
            return {'items': items, 'people': people, 'artists': artists, 'live': live}
        self.route_async(work, lambda d: route.__setitem__('_data', d), epoch)

    def render(self, size):
        art = self.ctx.art
        tiles = art.tiles
        route = self.route
        term = route.get('term', '')
        if not term:
            return chrome.error(_('Type in the search box above.'))
        data = route.get('_data')
        if data is None:
            return chrome.busy()
        items = data.get('items') or []
        people = data.get('people') or []
        artists = data.get('artists') or []
        rows = [Text(_('Results for "%s"') % term, size='page', bold=True)]
        first = [True]

        def claim():
            got, first[0] = (first[0], False)
            return got
        groups = [(_('Movies'), ('Movie',), art.geom, 'Primary', True), (_('Shows'), ('Series',), art.geom, 'Primary', True), (_('Episodes'), ('Episode',), art.geom_wide, 'Thumb', False), (PEOPLE_ROW, (), art.geom, 'Primary', True), (ARTISTS_ROW, (), art.geom_square, 'Primary', True), (_('Albums'), ('MusicAlbum',), art.geom_square, 'Primary', True), (SONGS_ROW, (), None, None, None), (_('Videos'), ('Video', 'MusicVideo'), art.geom_wide, 'Primary', True), (_('Audiobooks'), (AUDIOBOOK_TYPE,), AUTO_SHAPE, None, True), (_('Books'), (BOOK_TYPE,), AUTO_SHAPE, None, True)]
        used = set()
        for label, types_, geom, itype, inherit in groups:
            if label is PEOPLE_ROW:
                if people:
                    rows.append(tiles.tile_row(_('People'), people[:ROW_MAX], 'search-people', geom=art.geom, autofocus_first=claim(), lazy=True))
                continue
            if label is ARTISTS_ROW:
                used.add('MusicArtist')
                row = artists or [it for it in items if it.get('Type') == 'MusicArtist']
                if row:
                    rows.append(tiles.tile_row(_('Artists'), row[:ROW_MAX], 'search-Artists', geom=art.geom_square, autofocus_first=claim(), lazy=True))
                continue
            if label is SONGS_ROW:
                rows.extend(self._songs_row(items, route, tiles))
                continue
            group = [it for it in items if it.get('Type') in types_][:ROW_MAX]
            if group:
                used.update(types_)
                row_geom, row_itype = (geom, itype)
                if geom is AUTO_SHAPE:
                    row_geom, row_itype = tiles.auto_geom(group, default=art.geom, default_type='Primary')
                rows.append(tiles.tile_row(label, group, 'search-' + label, geom=row_geom, image_type=row_itype, inherit=inherit, autofocus_first=claim(), lazy=True))
        live = data.get('live') or {}
        if live.get('programs'):
            rows.append(tiles.tile_row(_('On TV'), live['programs'], 'search-programs', geom=tiles.caption_geom(live['programs'], art.geom_wide), image_type='Thumb', lazy=True))
        if live.get('channels'):
            rows.append(tiles.tile_row(_('Channels'), live['channels'], 'search-channels', geom=art.geom_square, lazy=True))
        other = [it for it in items if it.get('Type') not in used and it.get('Type') != 'Audio'][:ROW_MAX]
        if other:
            rows.append(tiles.tile_row(_('Other'), other, 'search-other', lazy=True))
        if not items and (not people) and (not artists) and (not any(live.values())):
            rows.append(Text(_('No results found.'), size='large', color=theme.SUBTLE_FG))
        blocks = tiles.window_rows(chrome.split_bleed(rows, chrome.CONTENT_PAD, 12, 'stretch'), 'search', size, 12, pad=chrome.CONTENT_PAD)
        return VScroll(Column(blocks, pad=(0, chrome.CONTENT_PAD), gap=12, align='stretch'), id='search', flex=1, offset=self.parked_scroll('search'), on_scroll=lambda off, mx: art.scroll.on_scroll('search', off, mx))

    def _songs_row(self, items, route, tiles):
        """The Songs heading and its table, or an empty list.

        A method only because the section order is a list the render loop
        walks now, and songs are the one entry in it that is a table rather
        than a carousel. It never claims focus: a table row is reachable by
        arrow keys from the carousel above it, and a search that matched a
        song and a film should land on the film.
        """
        rows = []
        songs = [it for it in items if it.get('Type') == 'Audio'][:ROW_MAX]
        if songs:
            server = route.get('server') or self.ctx.server
            ids = [s.get('Id') for s in songs]
            rows.append(Text(_('Songs'), size='heading', bold=True))
            rows.append(tiles.track_list(songs, 'search-song', lambda i: self.ctx.actions.play_list(ids, server, i, audio=True), menu=True))
        return rows

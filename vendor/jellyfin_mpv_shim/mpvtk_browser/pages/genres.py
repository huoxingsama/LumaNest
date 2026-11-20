"""The Genres screen for a video library.

A heading per genre over a random sample of that genre's items, each
heading linking to the unbounded listing -- jellyfin-web's ``moviegenres``
and ``tvgenres``, which are the same controller with a different item type.

Music genres are deliberately *not* here. They are drawn as tiles in the
music library's own Genres tab and open a page of that genre's albums
(``music_detail.MusicGenrePage``), which is what web does too: its
``MusicGenre`` is a card and its video ``Genre`` is a heading over a row.
The two look like one feature and are two, because a music genre has
albums to show as tiles and a video genre has nothing of its own to draw.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from ...i18n import _
from ...mpvtk.widgets import Column, VScroll
from ..components import chrome
from ..tile_renderer import GRID_GAP
from .base import Page

class GenresPage(Page):
    kind = 'genres'

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get('server') or self.ctx.server
        parent = route.get('parent_id')
        collection_type = route.get('collection_type')

        def work():
            return source.get_genre_sections(srv, parent, collection_type)
        self.route_async(work, lambda rows: route.__setitem__('_data', rows), epoch)

    def render(self, size):
        rows = self.route.get('_data')
        if rows is None:
            return chrome.busy()
        if not rows:
            return chrome.error(_('No genres to show for this library.'))
        art = self.ctx.art
        built = []
        for row in rows:
            geom, image_type = art.tiles.auto_geom(row['items'], default=art.geom, default_type='Primary')
            built.append(art.tiles.tile_row(row['title'], row['items'], 'genre-' + str(row['key']), geom=geom, image_type=image_type, see_all=self._see_all(row), lazy=True))
        built = art.tiles.window_rows(built, 'genres', size, GRID_GAP, pad=chrome.CONTENT_PAD)
        return VScroll(Column(built, pad=(0, chrome.CONTENT_PAD), gap=GRID_GAP, align='stretch'), id='genres', flex=1, offset=self.parked_scroll('genres'), on_scroll=lambda off, mx: art.scroll.on_scroll('genres', off, mx))

    def _see_all(self, row):
        """The genre's full listing. The row is a random sample of ten, so
        this is the only way to see the eleventh -- and the only reason the
        sample being random is tolerable."""
        route = self.route
        server = route.get('server') or self.ctx.server
        title = row['title']
        return lambda: self.ctx.nav.navigate({'kind': 'list', 'server': server, 'title': title, 'list': {'type': 'items', 'genre_ids': row['key'], 'include_item_types': row.get('types'), 'parent_id': route.get('parent_id')}})

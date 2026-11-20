"""The home screen.

Body moved verbatim from ``ViewsMixin._load_home`` / ``_render_home``, plus
the two helpers only this screen used (``_row_shape``, ``_order_rows``) —
which is the point of the conversion: they were methods on a 348-method
object because there was nowhere else to put them, and they belong to one
screen.

Reaches the shell for nothing. The first page to manage that.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import time
from ...i18n import _
from ...mpvtk.widgets import Box, Busy, Button, Column, HScroll, Row, Spacer, Text, VScroll
from .. import components, home_sections, theme, user_prefs, cinema_featured
from ..repository import OFFLINE_ROW_KIND
from ..components import chrome
from .base import Page
from .grid import sorts_for
log = logging.getLogger('mpvtk_browser.pages.home')

class HomePage(Page):
    kind = 'home'

    def close(self):
        current = self.route.get('_cinema_layers')
        if current:
            background = current.get('background', {})
            background.pop('_motion_assets', None)
        self.route['_cinema_prepared'] = {current['key']: current} if current else {}
        self.route.pop('_cinema_layer_jobs', None)
        self.route.pop('_cinema_prepare_pending', None)
        self.route.pop('_cinema_prepare_attempted', None)
    EPISODE_IMAGE_ROWS = frozenset({home_sections.RESUME, home_sections.RESUME_AUDIO, home_sections.NEXT_UP})

    def load(self, epoch):
        """Two batches: draw the top of the page, then fill in Latest.

        The Latest rows are one request per library and sit below the fold,
        so waiting for them gated first paint on content nobody has scrolled
        to yet. Libraries + Continue Watching + Next Up now publish as soon as
        they land, and the Latest rows replace that partial data when they
        arrive.

        The batches are merged by slot rather than concatenated: the user can
        put Recently Added above Continue Watching, so "primary then latest"
        is no longer the display order.

        **The partial batch is published only on a first paint.** This loader
        also runs for #560's refreshes, which fire on a UserDataChanged burst
        while the user is looking at the screen -- and publishing
        primary-only there takes the Latest rows *away* for the length of one
        request per library before putting them back. "Load, not reload"
        promised the screen keeps what it has; a partial publish breaks that
        promise more visibly than a spinner would.
        """
        route = self.route
        shuffle = route.pop('_cinema_shuffle_due', not bool(route.get('_cinema_featured')))
        if shuffle:
            route['_cinema_featured_index'] = 0
        source = self.ctx.source
        run = self.ctx.run
        invalidate = self.ctx.invalidate
        refresh = getattr(self.ctx.player, 'refresh_downloaded_userdata', None)
        if refresh is not None:
            try:
                refresh()
            except Exception:
                log.debug('could not request a userdata refresh', exc_info=True)

        def work():
            server = route.get('server') or self.ctx.server
            get_prefs = getattr(source, 'get_home_prefs', None)
            layout, excludes = get_prefs(server) if get_prefs else (list(home_sections.DEFAULT_LAYOUT), frozenset())
            get_user = getattr(source, 'get_user_prefs', None)
            inherit = user_prefs.inherit_artwork(get_user(server) if get_user else None)
            libs = source.get_libraries(server)

            def rows(stage):
                return source.get_home_rows(server, libs, sections=(stage,), layout=layout, latest_excludes=excludes)
            primary = rows('primary')
            if run.epoch == epoch and route.get('_data') is None:
                route['_data'] = {'libraries': libs, 'layout': layout, 'inherit': inherit, 'featured': route.get('_cinema_featured', []), 'rows': self._order_rows(primary)}
                invalidate()
            latest = rows('latest')
            scope = tuple(sorted((lib.get('Id', '') for lib in libs))) + tuple(sorted(excludes))
            cached_at = route.get('_cinema_featured_at', 0)
            cached = route.get('_cinema_featured_scope') == scope and (not shuffle) and bool(route.get('_cinema_featured'))
            if cached:
                featured = route.get('_cinema_featured', [])
            else:
                featured = cinema_featured.load_featured(source, server, libs, primary + latest, excludes)
                cached_at = time.monotonic()
            current = {i.get('Id'): i for row in primary for i in row.get('items', [])}
            for item in featured:
                if cached and item.get('Id') not in current and (item.get('UserData') or {}).get('PlaybackPositionTicks'):
                    try:
                        fresh = source.get_item(server, item['Id'])
                        if fresh and fresh.get('UserData') is not None:
                            current[item['Id']] = fresh
                    except Exception:
                        log.debug('Featured resume refresh deferred')
            featured = [{**i, **({'UserData': current[i['Id']]['UserData']} if current.get(i.get('Id'), {}).get('UserData') is not None else {})} for i in featured]
            return {'libraries': libs, 'layout': layout, 'inherit': inherit, 'featured': featured, 'featured_at': cached_at, 'featured_scope': scope, 'rows': self._order_rows(primary + latest)}
        self.route_async(work, self._publish, epoch)

    def _publish(self, data):
        """Adopt a loaded home screen, unless it is what is already there.

        A UserDataChanged burst re-reads this screen every few seconds and
        most of those change nothing it draws -- someone else's play position
        on an item in no row of yours, or the same item in the same place.
        Rebuilding the tree for that costs a full home layout, and the rows
        are on screen while it happens.
        """
        if self.route.get('_data') == data:
            return
        if 'featured_at' in data:
            self.route['_cinema_featured'] = data.get('featured', [])
            self.route['_cinema_featured_at'] = data['featured_at']
            self.route['_cinema_featured_scope'] = data['featured_scope']
        self.route['_data'] = data
    SEE_ALL_ROWS = {home_sections.NEXT_UP: {'type': 'nextup'}, home_sections.LIVE_TV: {'type': 'programs', 'filters': {'is_airing': True}}}
    _LATEST_SORT_BY = {'tvshows': 'DateLastContentAdded'}
    _DEFAULT_LATEST_SORT_BY = 'DateCreated'

    @classmethod
    def _latest_sort(cls, collection_type):
        """Index into ``sorts_for(collection_type)`` for a Latest heading.

        An *index*, because that is what a grid route stores -- and it is an
        index into whichever list that library's own screen offers, which is
        why this cannot be a constant: ``EXTRA_SORTS`` appends to ``SORTS``,
        so "Date Episode Added" exists only for the libraries that have it
        and sits at a position no other library shares.

        Resolved by looking the sort *name* up rather than by arithmetic on
        ``len(SORTS)``: the tables are edited by hand and a new base sort
        would silently re-point this at whatever landed on that index --
        which is the same trap ``EXTRA_SORTS`` documents for stored routes.
        Falling back to ``DateCreated`` rather than raising keeps a library
        whose sort is missing on the old behaviour instead of on nothing.
        """
        want = cls._LATEST_SORT_BY.get(collection_type or '', cls._DEFAULT_LATEST_SORT_BY)
        sorts = sorts_for(collection_type)
        for index, (_label, sort_by, _order) in enumerate(sorts):
            if sort_by == want:
                return index
        for index, (_label, sort_by, _order) in enumerate(sorts):
            if sort_by == cls._DEFAULT_LATEST_SORT_BY:
                return index
        return 0

    def _see_all(self, kind, title, row=None):
        """``on_click`` for a row's heading, or None if it has no listing.

        Takes the kind rather than re-deriving it from the row id: the id now
        carries a library id for the rows there can be several of (see
        ``_row_id``), and pulling a section name back out of a string that
        holds an arbitrary server GUID is a parse waiting to be wrong. The
        caller has the kind in hand either way.
        """
        server = self.route.get('server') or self.ctx.server
        if kind == home_sections.LATEST and (row or {}).get('parent_id'):
            return lambda: self.ctx.nav.navigate({'kind': 'grid', 'server': server, 'parent_id': row['parent_id'], 'collection_type': row.get('collection_type'), 'title': title, '_sort': self._latest_sort(row.get('collection_type'))})
        spec = self.SEE_ALL_ROWS.get(kind)
        if spec is None:
            return None
        return lambda: self.ctx.nav.navigate({'kind': 'list', 'server': server, 'title': title, 'list': dict(spec)})

    @staticmethod
    def _order_rows(rows):
        """Restore the user's section order across the two fetch batches.

        Stable, so the per-library Latest rows keep the library order they
        were submitted in rather than shuffling within their slot.
        """
        return sorted(rows, key=lambda r: r.get('slot', 0))

    def render(self, size):
        art = self.ctx.art
        route = self.route
        if self.ctx.server is None:
            return Box([Spacer(), Row([Spacer(), Busy(), Spacer()]), Row([Spacer(), Text(_('Connecting to your server…'), size='large', color=theme.SUBTLE_FG), Spacer()]), Spacer()], flex=1, direction='column', align='stretch', gap=16)
        data = route.get('_data')
        if data is None:
            return chrome.busy()
        layout = data.get('layout') or list(home_sections.DEFAULT_LAYOUT)
        inherit = data.get('inherit', True)
        entries = []
        used: dict = {}
        for hr in data['rows']:
            if not hr.get('items'):
                continue
            kind = hr.get('kind') or 'row'
            geom, itype = self._row_shape(hr)
            row_id = self._unique(self._row_id(kind, hr), used)
            entries.append((hr.get('slot', 0), hr['title'], hr['items'], geom, itype, row_id, self._latest_tv(hr), inherit if kind in self.EPISODE_IMAGE_ROWS else True, self._see_all(kind, hr['title'], hr), kind))
        entries.sort(key=lambda e: (e[-1] != home_sections.RESUME, e[0]))
        rows = []
        from ..components.cinema_home import render_featured, clear_background, page_surface, preload_featured
        featured = data.get('featured', [])
        if featured:
            index = self.route.get('_cinema_featured_index', 0) % len(featured)
            preload_featured(self, size, featured, index)
            rows.append(render_featured(self, size, featured, index, self._select_featured))
        else:
            clear_background(self)
            self.route.pop('_cinema_layers', None)
        libraries_drawn = False
        live_tv_buttons_drawn = False
        for _slot, title, items, geom, itype, row_id, pitem, inh, see_all, kind in entries:
            if kind != home_sections.RESUME and (not libraries_drawn):
                links = self._library_links(data['libraries'])
                if links is not None:
                    rows.append(links)
                libraries_drawn = True
            if kind == home_sections.LIVE_TV and (not live_tv_buttons_drawn):
                live_tv_buttons_drawn = True
                rows.append(self._live_tv_buttons())
            rows.append(art.tiles.tile_row(title, items, row_id, geom=geom, image_type=itype, parent_item=pitem, inherit=inh, see_all=see_all, lazy=True))
        if not libraries_drawn:
            links = self._library_links(data['libraries'])
            if links is not None:
                rows.append(links)
        if not rows:
            rows.append(Row([Spacer(w=chrome.CONTENT_PAD), Text(_('Nothing to show yet.'), size='large', color=theme.SUBTLE_FG)]))
        rows = art.tiles.window_rows(rows, 'home', size, 20, pad=0)
        scroll = VScroll(Column(rows, pad=0, gap=20), id='home', flex=1, cinema_pull=bool(self.route.get('_cinema_immersive')), offset=self.parked_scroll('home'), snaps=components.section_offsets(rows, 20, pad=0), on_scroll=self._home_scrolled)
        return page_surface(self, [scroll], 'home')

    def _home_scrolled(self, offset, maximum):
        from ..components.cinema_home import scrolled
        scrolled(self, 'home', offset, maximum)

    def _select_featured(self, index):
        items = (self.route.get('_data') or {}).get('featured', [])
        if items:
            self.route['_cinema_featured_index'] = index % len(items)
            self.ctx.invalidate()

    def _library_links(self, libraries):
        if not libraries:
            return None
        from ..components.controls import action_btn
        buttons = [Text('浏览媒体库', size='normal', color=theme.SUBTLE_FG)]
        for lib in libraries:
            buttons.append(action_btn('movie', lib.get('Name', ''), 'cinema-library-' + lib['Id'], lambda lib=lib: self.ctx.art.tiles.on_open(lib)))
        return HScroll(Row(buttons, gap=12, pad=(chrome.CONTENT_PAD, 4), align='center'), id='cinema-library-links', h=56)

    def _live_tv_buttons(self):
        """The Live TV section's nav row: one button per Live TV tab.

        They land on the same page the library tile opens, with the tab
        pre-selected — which is why the tab key travels on the route rather
        than being a separate screen each.
        """
        from .livetv import LiveTvPage
        return Row([self.ctx.art.tiles.section_heading(_('Live TV')), Spacer(w=8)] + [Button(label, id='home-lt-' + key, on_click=lambda k=key: self.ctx.nav.navigate({'kind': 'livetv', 'server': self.ctx.server, 'title': _('Live TV'), '_tab': k})) for key, label in LiveTvPage.TABS], gap=8, align='center')
    MULTI_ROW_KEYS = {home_sections.LATEST: 'parent_id', OFFLINE_ROW_KIND: 'collection_type'}

    @classmethod
    def _row_id(cls, kind, hr):
        """The scroll-container id for a home row.

        ``row-<kind>-<slot>``, plus whatever identifies the row for the two
        kinds that build several rows from one slot (``row-<kind>-<key>#<n>``,
        one per library or collection type).

        **Nothing here counts rows.** The id is a function of this row alone,
        because any number taken from the rows around it moves when one of
        *them* goes away -- and the repository drops a row whose own request
        came back empty or failed, independently of its siblings. The row that
        inherits the number inherits the scroll offset parked under it.

        The slot is the user's own layout position, so it is stable across
        renders and only moves when they reorder their home screen, which is
        the one time a row losing its offset is the right answer.
        """
        field = cls.MULTI_ROW_KEYS.get(kind)
        key = hr.get(field) if field else None
        slot = hr.get('slot', 0)
        return 'row-%s-%s#%s' % (kind, key, slot) if key else 'row-%s-%s' % (kind, slot)

    @staticmethod
    def _unique(row_id, used):
        """``row_id``, suffixed only if this render already used it.

        A layout may hold one section twice — jellyfin-web offers every type
        in every slot — and the repository then builds the rows for it once
        per slot. The ``MULTI_ROW_KEYS`` kinds are keyed by what identifies
        the row rather than by position, so those repeats collide, and
        ``layout()`` targets only the last occurrence: the earlier row's
        tiles are unreachable and its clicks land on the later one's.

        This is ``tile_renderer.tile_row``'s rule (#20) one level up: there
        an *item* repeats within a row, here a *row* repeats within a screen.
        It diverges twice, so neither reads as an oversight:

        It suffixes with ``-<i>``; this uses ``#``. A row id's ``-`` namespace
        already holds children built from item ids (``<row>-<itemId>``, plus
        ``-pl``/``-pr``/``-more``), so a numeric ``-`` tail is a shape a tile
        can also produce. ``#`` cannot be: it is already the row-level
        synthetic separator (``<row>#strip``).

        **A backstop, not the mechanism.** ``_row_id`` puts the slot in every
        id, so two rows collide here only if the repository emits two with the
        same identity *in the same slot*. Renumbering is not a safe way to
        keep ids apart on its own: this counter runs over the rows that
        survived, so which occurrence gets renamed changes when one of them
        goes away, and the survivor lands on the other's parked scroll offset.
        """
        count = used.get(row_id, 0)
        used[row_id] = count + 1
        return row_id if not count else '%s#%d' % (row_id, count + 1)

    @staticmethod
    def _latest_tv(hr):
        """A "Latest" row for a TV library.

        The server answers these with a *mix* of Series (a show that got
        several new episodes) and bare Episodes (a show that got one), so the
        row reads as a list of shows with an episode dropped into the middle
        of it. The Episodes are therefore drawn as their series — see
        ``TileRenderer._tile``. The row's shape is not affected: these stay
        poster rows like every other TV row.
        """
        return hr.get('kind') == home_sections.LATEST and hr.get('collection_type') == 'tvshows'

    def _row_shape(self, hr):
        """(geom, image_type) for a home row, classified like the Tk browser:
        movies/tv/boxsets and Live TV -> poster; music/playlists -> square;
        home-video/misc or episode-bearing rows -> landscape Thumb."""
        art = self.ctx.art
        ctype = hr.get('collection_type')
        items = hr.get('items', [])
        has_episode = any((it.get('Type') == 'Episode' for it in items))
        if hr.get('kind') == home_sections.RESUME:
            return (art.geom_wide, 'Thumb')
        if ctype == 'livetv':
            return art.tiles.auto_geom(items, default=art.geom_wide, default_type='Thumb')
        if ctype in ('movies', 'tvshows', 'boxsets'):
            return (art.geom, 'Primary')
        if ctype == 'books':
            return (art.geom, 'Primary')
        if ctype in ('music', 'playlists'):
            return (art.geom_square, 'Primary')
        if art.tiles.square_geom(items):
            return (art.geom_square, 'Primary')
        if ctype:
            return (art.geom_wide, 'Thumb' if has_episode else 'Primary')
        if has_episode:
            return (art.geom_wide, 'Thumb')
        return (art.geom, 'Primary')

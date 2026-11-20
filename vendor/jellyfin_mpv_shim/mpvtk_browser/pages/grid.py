"""A library grid, and a person's filmography.

Two route kinds, one page class. They share a renderer and differ only in
where their items come from and which header controls make sense, so
``PersonPage`` is a three-line subclass rather than a copy — which is what
the ``ROUTES`` table could not express (``"person": ("_load_person",
"_render_grid")`` pointed two kinds at one method and left the differences
as ``route["kind"] ==`` checks inside it).

Everything here was private to these two routes: the sort and filter bars,
the paged-grid layout, the infinite-scroll fetcher, the reload-on-change
handlers.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import dataclasses
import json
import logging
import threading
from ...i18n import _
from ...mpvtk.widgets import Box, Column, Dropdown, Row, Spacer, Text, VScroll
from .. import dialogs, pagination, theme, view_prefs
from ..components import chrome, controls
from ..tile_renderer import GRID_GAP
from .base import Page

def _remembering():
    """`remember_filters`, read at the moment of each save and load so the
    setting applies without a restart."""
    from ...conf import settings
    return settings.remember_filters
_FILTER_SAVE_LOCK = threading.Lock()
SORTS = [(_('Name'), 'SortName', 'Ascending'), (_('Date Added'), 'DateCreated', 'Descending'), (_('Release Date'), 'PremiereDate', 'Descending'), (_('Community Rating'), 'CommunityRating', 'Descending'), (_('Date Played'), 'DatePlayed', 'Descending'), (_('Play Count'), 'PlayCount', 'Descending'), (_('Runtime'), 'Runtime', 'Ascending'), (_('Critics Rating'), 'CriticRating', 'Descending'), (_('Parental Rating'), 'OfficialRating', 'Ascending'), (_('Random'), 'Random', 'Ascending')]
EXTRA_SORTS = {'tvshows': [(_('Date Episode Added'), 'DateLastContentAdded', 'Descending')]}
_LETTERS = '#ABCDEFGHIJKLMNOPQRSTUVWXYZ'

def sorts_for(collection_type):
    """The sort menu for a library of this kind."""
    return SORTS + EXTRA_SORTS.get(collection_type or '', [])
_DEFAULT_VIEW = {'imageType': (view_prefs.DEFAULT_IMAGE_TYPE, None), 'viewType': (view_prefs.GRID_VIEW, None), 'showTitle': (True, None), 'showYear': (True, None), 'sortby': (None, None), 'sortorder': (None, None), 'filters': (None, None)}
GENRE_LIBRARIES = frozenset({'movies', 'tvshows'})
STUDIO_LIBRARIES = frozenset({'tvshows'})
NO_PLAY_ALL = frozenset({'tvshows', 'books'})
NO_LIBRARY_PLAY = frozenset({'books'})
GENRE_ITEM_TYPES = {'movies': 'Movie', 'tvshows': 'Series'}
log = logging.getLogger('mpvtk_browser.pages.grid')

def _image_type_of(view):
    """The artwork name to ask the server for, given a view-settings dict.

    ``None`` for "Auto", which asks for nothing beyond the browse defaults --
    the grid is shaped by whatever artwork comes back.
    """
    stored = (view or {}).get('imageType')
    named = view_prefs.shape_for(stored[0] if stored else None)
    return named[1] if named else None

def _fmt_runtime(ticks):
    """``1h 42m`` for a list row, or "" with no runtime."""
    mins = int((ticks or 0) // 600000000)
    if not mins:
        return ''
    return '%dh %dm' % (mins // 60, mins % 60) if mins >= 60 else '%dm' % mins

class GridPage(Page):
    kind = 'grid'
    HEAD_H = 40 + 110

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        run = self.ctx.run
        invalidate = self.ctx.invalidate
        srv = route.get('server') or self.ctx.server
        parent = route['parent_id']
        _n, sort_by, sort_order = self._sorts()[route.get('_sort', 0)]
        filters = route.get('_filters') or {}
        collections = bool(route.get('_collections'))
        ctype = route.get('collection_type')

        def work():
            view = route.get('_view')
            if view is None:
                get_view = getattr(source, 'get_view_settings', None)
                view = get_view(srv, parent, ctype) if get_view else dict(_DEFAULT_VIEW)
            image_type = _image_type_of(view)
            q_sort_by, q_sort_order = (sort_by, sort_order)
            if route.get('_sort') is None:
                index = self._sort_index(view)
                if index is not None:
                    route['_sort'] = index
                    _n, q_sort_by, q_sort_order = self._sorts()[index]
            q_filters = filters
            if route.get('_filters') is None:
                seeded = self._stored_filters(view)
                route['_filters'] = seeded
                q_filters = seeded
            if collections:
                items, total = source.get_movie_collections(srv, sort_by=q_sort_by, sort_order=q_sort_order, filters=q_filters, image_type=image_type)
            else:
                items, total = source.get_library_items(srv, parent, sort_by=q_sort_by, sort_order=q_sort_order, filters=q_filters, image_type=image_type, collection_type=ctype)
            if run.epoch == epoch:
                self._install(items, total, view, q_sort_by)
                invalidate()
            vals = route.get('_filtervals')
            if vals is None:
                try:
                    vals = source.get_filter_values(srv, parent, collection_type=ctype)
                except Exception:
                    log.debug('filter values unavailable', exc_info=True)
                    vals = {'genres': [], 'years': []}
            return (items, total, vals, view, q_sort_by)

        def done(res):
            items, total, vals, view, used_sort = res
            route['_filtervals'] = vals
            self._install(items, total, view, used_sort)
        self.route_async(work, done, epoch)

    def _install(self, items, total, view, sort_by):
        """Publish a loaded page onto the route. Called twice -- once when
        the items land and once when the whole job does -- so it must stay
        idempotent."""
        route = self.route
        if route.get('_view') is None:
            route['_view'] = view
        total = len(items) if sort_by == 'Random' else total
        route['_total'] = total
        route['_items'] = pagination.spread(route.get('_items') or [], total, items, 0)
        route['_collection_capable'] = route.get('collection_type') == 'movies' and hasattr(self.ctx.source, 'get_movie_collections')

    def render(self, size):
        art = self.ctx.art
        tiles = art.tiles
        route = self.route
        items = route.get('_items')
        if items is None:
            return Column(self._header(None, size[0]) + [chrome.busy()], pad=chrome.CONTENT_PAD, gap=GRID_GAP, flex=1, align='stretch')
        if not items and self._active_filters():
            return Column(self._header(items, size[0]) + [Text(_('No items match these filters.'), size='large', color=theme.SUBTLE_FG), Row([controls.action_btn('close', _('Clear Filters'), 'grid-empty-clear', self._clear_filters), Spacer(flex=1)])], pad=chrome.CONTENT_PAD, gap=GRID_GAP, flex=1, align='stretch')
        if view_prefs.is_list(self._view('imageType'), self._view('viewType')):
            return self._list_view(items, self._header(items, size[0]))
        geom, image_type = self._grid_shape(items)
        labels = (bool(self._view('showTitle')), bool(self._view('showYear')))
        if not labels[0]:
            geom = dataclasses.replace(geom, caption_h=max(0, geom.caption_h - geom.title_size - 7) if labels[1] else 0)
        gpad, geom = tiles.grid_layout(size[0], geom)
        header = self._header(items, size[0] - 2 * (gpad - chrome.CONTENT_PAD))
        if self._pages.enabled():
            return self._paged_grid(size, header, geom, image_type, labels, gpad)
        cols = tiles.cols(size[0], geom)
        first, last = tiles.row_window(size, geom, 'grid', self.HEAD_H)
        self._window(first * cols, (last + 1) * cols)
        rows = header + tiles.grid_of(items, 'grid', size, geom=geom, image_type=image_type, scroll_id='grid', head_h=self.HEAD_H, labels=labels)
        return VScroll(Column(rows, pad=(gpad, chrome.CONTENT_PAD), gap=GRID_GAP, align='stretch'), id='grid', flex=1, offset=self.parked_scroll('grid'), snap=geom.strip_h + GRID_GAP, snap_off=tiles.header_offset(header), on_scroll=lambda off, mx: art.scroll.on_scroll('grid', off, mx, lambda o, m: self._pages.rewindow(route)))

    @property
    def _pages(self):
        """The shell's Paginator. Reached through ``ctx.art`` for the same
        reason ``tiles`` and ``scroll`` are: it is a render-time service."""
        return self.ctx.art.pages

    def _header(self, items, width=0):
        route = self.route
        header: list = [Row([Text(route.get('title', ''), size='page', bold=True), Spacer(flex=1), controls.action_btn('settings', _('View'), 'grid-viewcfg', self._open_view_settings)], align='center', w=max(0, (width or 0) - 2 * chrome.CONTENT_PAD))]
        header.append(self._filter_bar(width))
        if not self._pages.enabled():
            header.append(Text(_('%d items') % (route.get('_total') or 0), size='caption', color=theme.SUBTLE_FG))
        return header

    def _view_controls(self):
        """Buttons that LEAVE this library -- Genres, Networks, Collections.

        Collections is one of these and **not a filter**: it sets
        `route["_collections"]`, tears down `_items`, `_total`, the grid shape
        and the paginator, and comes back with a different item type wearing a
        different tile shape. The test is whether it composes -- everything in
        `_filters` intersects, and Collections cannot intersect with anything.

        They lead the filter row rather than sitting among the filters, because
        a filter changes what this grid shows and these navigate somewhere
        else. See docs/browser-shell.md section 12.
        """
        route = self.route
        out = []
        if route.get('collection_type') in STUDIO_LIBRARIES:
            out.append(controls.action_btn('apartment', _('Networks'), 'grid-studios', self._open_studios))
        if route.get('collection_type') in GENRE_LIBRARIES:
            out.append(controls.action_btn('label', _('Genres'), 'grid-genres', self._open_genres))
        if route.get('_collection_capable'):
            out.append(controls.action_btn('video_library', _('Collections'), 'grid-collections', self._toggle_collections, on=bool(route.get('_collections'))))
        return out

    def _filter_bar(self, width=0):
        route = self.route
        vals = route.get('_filtervals') or {}
        filters = route.get('_filters') or {}
        genres = vals.get('genres') or []
        gi = 0
        if filters.get('genre') in genres:
            gi = genres.index(filters['genre']) + 1
        years = list(vals.get('years') or [])
        yi = 0
        if filters.get('year') in years:
            yi = years.index(filters['year']) + 1
        active = self._active_filters()
        bar = Row([Dropdown('grid-sort', [s[0] for s in self._sorts()], selected=route.get('_sort', 0), w=180, force=True, on_select=lambda i, v: self._set_sort(i))] + ([controls.action_btn('filter_alt', _('Filter (%d)') % active if active else _('Filter'), 'grid-filter', self._open_filters, primary=bool(active))] if self._filters_offered() else []) + ([controls.action_btn('play_arrow', _('Play All'), 'grid-playall', self._play_all)] if self._play_all_capable() else []) + ([controls.action_btn('shuffle', _('Shuffle'), 'grid-shuffle', self._shuffle)] if self._shuffle_capable() else []) + [Spacer(flex=1)], gap=10, align='center')
        bar = self._fit_bar(bar, self._view_controls(), width)
        cur_letter = filters.get('letter')
        cells = [Box([Text(ch, size='small', align='center', flex=1, color=theme.ACCENT_FG if cur_letter == ch else theme.SUBTLE_FG)], id='grid-l-' + ch, w=26, h=26, align='center', direction='row', radius=4, bg=theme.ACCENT if cur_letter == ch else None, hover={'fill': theme.ACCENT_HOVER if cur_letter == ch else theme.BUTTON_ACTIVE}, on_click=lambda c=ch: self._set_filter('letter', None if cur_letter == c else c)) for ch in _LETTERS]
        letters = chrome.wrap_row(cells, width - 2 * chrome.CONTENT_PAD, gap=2, row_gap=4)
        return Column([bar, letters], gap=8)

    def _paged_grid(self, size, header, geom, image_type='Primary', labels=None, gpad=None):
        """Paginated grid/person: one screenful of tiles, no scroll — the
        bottom pagination bar (drawn by the shell) moves between pages."""
        tiles = self.ctx.art.tiles
        head_h = tiles.header_offset(header)
        ps = self._pages.page_size(self.route, size, head_h, geom, chrome.CONTENT_PAD)
        page_items = self._pages.ensure(self.route, ps, self._page_fetcher(), seed=self.route.get('_items'))
        if page_items is None:
            body = [Text(_('Loading…'), size='large', color=theme.SUBTLE_FG)]
        else:
            body = tiles.grid_of(page_items, 'grid', size, geom=geom, image_type=image_type, labels=labels)
        return Column(header + body, pad=(chrome.CONTENT_PAD if gpad is None else gpad, chrome.CONTENT_PAD), gap=GRID_GAP, align='stretch', flex=1)

    def _sorts(self):
        """This route's sort menu. A TV library has one the others do not --
        see EXTRA_SORTS -- and a route's stored ``_sort`` is an index into
        whichever list its own screen offers."""
        return sorts_for(self.route.get('collection_type'))

    def _sort_index(self, view):
        """This screen's menu index for the stored sort, or None for "leave
        the screen's own default alone".

        None rather than 0 in two cases, and both are the point:

        * the stored sort is not in THIS library's menu -- a sort set in
          jellyfin-web on a TV library ("Date Episode Added") read on a screen
          that does not offer it. Falling back is what keeps that from
          selecting something arbitrary;
        * nothing is stored at all.

        **Our menu couples a direction to each field and web does not**, so a
        stored `SortName`/`Descending` has no entry here. The field wins: an
        entry with the right field in our direction is much closer to what was
        asked for than ignoring the setting, and the alternative is a menu
        entry per direction for a screen that has never had one.
        """
        stored_by = ((view or {}).get('sortby') or (None, None))[0]
        stored_order = ((view or {}).get('sortorder') or (None, None))[0]
        if not stored_by:
            return None
        loose = None
        for index, (_label, by, order) in enumerate(self._sorts()):
            if by != stored_by:
                continue
            if order == stored_order:
                return index
            if loose is None:
                loose = index
        return loose

    def _set_sort(self, index):
        """Change the sort, reload, and remember it where web looks.

        The names, never the index -- see `view_prefs.resolve_sort` and
        `EXTRA_SORTS`. Said plainly because it is a cross-client write and
        somebody will report it as one: **changing the sort here changes it in
        the user's jellyfin-web client**, which is what parity with it means.
        """
        _label, sort_by, sort_order = self._sorts()[index]
        self._set('_sort', index)
        self._persist_sort(sort_by, sort_order)

    def _persist_sort(self, sort_by, sort_order):
        """Write the sort to the server without touching the screen.

        `_set_view` is the other spelling and does more, because the settings
        it writes change how the grid DRAWS: it repaints optimistically and
        rolls back on failure. A sort has already been re-fetched by the time
        this runs, and rolling THAT back would re-sort the grid under somebody
        who is looking at it -- so a failure reports and leaves the screen as
        it is.
        """
        route = self.route
        source = self.ctx.source
        save = getattr(source, 'save_view_setting', None)
        if save is None:
            return
        server = route.get('server') or self.ctx.server
        parent = route.get('parent_id')
        ctype = route.get('collection_type')
        view = dict(route.get('_view') or _DEFAULT_VIEW)
        writes = []
        for setting, value in (('sortby', sort_by), ('sortorder', sort_order)):
            key = (view.get(setting) or (None, None))[1]
            writes.append((setting, value, key))
            view[setting] = (value, key)
        route['_view'] = view

        def work():
            for setting, value, key in writes:
                save(server, parent, ctype, setting, value, key=key)

        def failed(_exc):
            self.ctx.status(_('That sort could not be saved.'))
        self.ctx.run.run(work, lambda _r: None, self.ctx.run.epoch, on_error=failed)

    def _stored_filters(self, view):
        """The stored filter dict as THIS source can apply it, or {} for
        "nothing stored".

        Only what the source can apply survives the read -- the same
        fallback judgement `_sort_index` makes about a sort this library's
        menu does not offer. A downloaded library seeds nothing it cannot
        honour rather than drawing a Filter badge for filtering that is
        not happening.

        The values are not validated against the library -- a genre it
        stopped offering, a year nothing has. They are the user's own
        choices, an unmatched one answers with an empty grid rather than
        a wrongly filtered one, and the Filter button's count says what
        is on.
        """
        stored = ((view or {}).get('filters') or (None, None))[0]
        if not stored or not _remembering():
            return {}
        keys = self._panel_keys()
        return {k: v for k, v in self._persistable(stored).items() if k in keys}

    def _persistable(self, filters):
        """`filters` without the A-Z letter, which is never saved or
        restored: it is navigation, the Filter badge does not count it, and
        a library reopened on "S" looks like it lost most of its items."""
        return {k: v for k, v in (filters or {}).items() if k not in self._NOT_IN_PANEL}

    def _persist_filters(self):
        """Write the filters to the server without touching the screen.

        `_persist_sort`'s contract exactly: the reload has already been
        asked for by the caller, a failure reports and leaves the screen
        as it is, and the route's `_view` copy is kept in step so the next
        change writes to the key this one did.

        No `_view` yet means the first load is still in flight, and
        publishing a default here would stop `_install` from ever
        publishing the real one (it only writes where the route has
        none) -- stranding the screen on the defaults for the rest of the
        session. Nothing is published in that case: `key=None` already
        means "the first candidate" to `save_view_setting`, so the next
        change writes to the same key this one did either way.

        The whole dict bar the letter (`_persistable`), JSON-encoded. Ours,
        not a cross-client write: no jellyfin-web client reads this key (see
        `view_prefs.FILTERS_SETTING`), so unlike the sort this changes
        nothing anywhere else.
        """
        route = self.route
        source = self.ctx.source
        save = getattr(source, 'save_view_setting', None)
        if save is None or not _remembering():
            return
        server = route.get('server') or self.ctx.server
        parent = route.get('parent_id')
        ctype = route.get('collection_type')
        filters = self._persistable(route.get('_filters'))
        value = json.dumps(filters, sort_keys=True)
        route_view = route.get('_view')
        key = ((route_view or {}).get('filters') or (None, None))[1]
        if route_view is not None:
            view = dict(route_view)
            view['filters'] = (filters, key)
            route['_view'] = view
        with _FILTER_SAVE_LOCK:
            state = route.setdefault('_filter_save', {'pending': None, 'running': False})
            state['pending'] = (value, key)
            if state['running']:
                return
            state['running'] = True

        def work():
            error = None
            while True:
                with _FILTER_SAVE_LOCK:
                    item = state['pending']
                    state['pending'] = None
                    if item is None:
                        state['running'] = False
                        break
                try:
                    save(server, parent, ctype, view_prefs.FILTERS_SETTING, item[0], key=item[1])
                except Exception as exc:
                    error = exc
            if error is not None:
                raise error

        def failed(_exc):
            self.ctx.status(_('Those filters could not be saved.'))
        self.ctx.run.run(work, lambda _r: None, self.ctx.run.epoch, on_error=failed)

    def _bound_query(self):
        """``(sort_by, sort_order, filters, person, srv, image_type,
        collections, ctype)`` read NOW, on the loop thread. The sort/filters
        a page is fetched with must be the ones it was asked for, not
        whatever they are when it lands -- and the artwork it asks the server
        for must be the one the grid is being drawn with, or page two of a
        Banner view arrives with no banners.

        ``collections`` for the same reason as the rest, and it was the one
        thing missing: `_fetch_at` read it live off the route, so a page-in
        submitted before the Collections toggle and landing after it would
        answer from whichever endpoint the flag named by then -- the movies
        query spliced into a list of collections or the reverse. Not
        reachable today, because `_toggle_collections` drops `_items` and
        the loading shell is drawn before any window is computed; bound
        here so that stays a property of this tuple rather than of what
        render happens to do.
        """
        _n, sort_by, sort_order = self._sorts()[self.route.get('_sort', 0)]
        return (sort_by, sort_order, self.route.get('_filters') or {}, self.route.get('person_id'), self.route.get('server') or self.ctx.server, _image_type_of(self.route.get('_view')), bool(self.route.get('_collections')), self.route.get('collection_type'))

    def _fetch_at(self, start, limit=None, bound=None):
        """One page of results. ``bound`` is a _bound_query() tuple captured
        on the loop thread; omitted only where the caller is already on it."""
        sort_by, sort_order, filters, person, srv, image_type, collections, ctype = bound or self._bound_query()
        source = self.ctx.source
        kw = {} if limit is None else {'limit': limit}
        if person:
            return source.get_person_items(srv, person, start_index=start, sort_by=sort_by, sort_order=sort_order, **kw)
        if collections:
            return source.get_movie_collections(srv, start_index=start, sort_by=sort_by, sort_order=sort_order, filters=filters, image_type=image_type, **kw)
        return source.get_library_items(srv, self.route['parent_id'], start_index=start, sort_by=sort_by, sort_order=sort_order, filters=filters, image_type=image_type, collection_type=ctype, **kw)

    def _window(self, first, last):
        """Fetch the items in ``[first, last)`` that are not loaded yet.

        Called from render, because which items are visible is a question
        about geometry. A Random sort is exempt: it reshuffles per request,
        so ``_install`` reports what it loaded as the whole list and there
        are no holes to fill.

        The query is bound HERE, on the loop thread, not inside the worker:
        the sort and filters a page is fetched with must be the ones it was
        asked for, not whatever they are when it lands.
        """
        bound = self._bound_query()
        if bound[0] == 'Random':
            return

        def put(r, items, total):
            r['_items'], r['_total'] = (items, total)
        self._pages.window(self.route, first, last, lambda r: (r.get('_items') or [], r.get('_total') or 0), put, lambda start, limit: self._fetch_at(start, limit, bound=bound))

    def _page_fetcher(self):
        """``fetch(start, limit) -> (items, total)`` for a paginated grid or
        person route. A Random sort reshuffles server-side per request, so it
        can't be paged — page the already-loaded items in memory instead."""
        sort_by = self._bound_query()[0]
        if sort_by == 'Random':
            items = self.route.get('_items') or []
            return lambda start, limit: (items[start:start + limit], len(items))
        return lambda start, limit: self._fetch_at(start, limit)

    def _list_view(self, items, header):
        """The library as a table rather than a grid.

        jellyfin-web's List view type. Deliberately plain -- name, year,
        runtime -- because the point of choosing it is that the artwork was
        not helping, so re-introducing art in a leading column would defeat
        it. It is also what makes the view cheap: no strips, no overlays, so
        a library of thousands costs nothing to draw.
        """
        rows = [{'cells': [it.get('Name') or '', str(it.get('ProductionYear') or ''), _fmt_runtime(it.get('RunTimeTicks'))], 'item': it} if it else {'cells': ['', '', ''], 'item': None} for it in items]
        head_h = self.ctx.art.tiles.header_offset(header)
        table = self.ctx.art.tiles.item_list(rows, 'grid', on_click=lambda i: self.ctx.art.tiles.on_open(items[i]), scroll_id='grid', head_h=head_h)
        self._window(*self.ctx.art.tiles.list_window(len(items), 'grid', head_h))
        return VScroll(Column(header + [table], pad=chrome.CONTENT_PAD, gap=GRID_GAP, align='stretch'), id='grid', flex=1, offset=self.parked_scroll('grid'), on_scroll=lambda off, mx: self.ctx.art.scroll.on_scroll('grid', off, mx, lambda o, m: self._pages.rewindow(self.route)))

    def _sort_bar(self):
        """Just the sort dropdown, for routes with no filterable axes.

        On ``GridPage`` rather than a subclass because two of them want it
        now. The node id carries the route kind: the renderer targets by id
        and keeps per-node state against it, so two screens sharing one id
        would share a dropdown -- and the id was literally "person-sort"
        while the list route was being built on top of it.
        """
        return Row([Text(_('Sort'), size='small', color=theme.SUBTLE_FG), Dropdown('%s-sort' % self.kind, [s[0] for s in self._sorts()], selected=self.route.get('_sort', 0), w=180, on_select=lambda i, v: self._set_sort(i))], gap=10, align='center')

    def _open_studios(self):
        route = self.route
        self.ctx.nav.navigate({'kind': 'list', 'server': route.get('server') or self.ctx.server, 'title': _('Networks'), 'list': {'type': 'studios', 'parent_id': route.get('parent_id'), 'include_item_types': GENRE_ITEM_TYPES.get(route.get('collection_type')), 'shape': 'landscape'}})

    def _open_genres(self):
        route = self.route
        self.ctx.nav.navigate({'kind': 'genres', 'server': route.get('server') or self.ctx.server, 'parent_id': route.get('parent_id'), 'collection_type': route.get('collection_type'), 'title': _('Genres')})

    def _open_view_settings(self):
        self.ctx.dialogs.view_settings(self._view, self._set_view, paginated=(self._pages.enabled, lambda: self._pages.toggle(self.route, 'grid')))

    def _view(self, setting):
        """The stored value of one view setting, or its default.

        Per setting, not per dict: a source may answer with only the
        settings it knows about -- the offline catalog answers ``{}`` -- and
        falling to None for the rest would read as "off", blanking every
        caption rather than leaving them alone.
        """
        view = self.route.get('_view') or {}
        stored = view.get(setting)
        if stored is None:
            stored = _DEFAULT_VIEW.get(setting) or (None, None)
        return stored[0]

    def _set_view(self, setting, value):
        """Persist one view setting and redraw with it.

        Optimistic, like every other edit here: the grid changes on the next
        frame and rolls back if the server refuses.
        """
        route = self.route
        if setting == 'imageType' and value != view_prefs.LIST_IMAGE_TYPE:
            self._retire_legacy_list_view()
        view = dict(route.get('_view') or _DEFAULT_VIEW)
        previous, key = view.get(setting) or (None, None)
        if value == previous:
            return
        was = _image_type_of(route.get('_view'))
        view[setting] = (value, key)
        route['_view'] = view
        route.pop('_grid_shape', None)
        self._redraw_or_refetch(was)
        server = route.get('server') or self.ctx.server
        parent = route.get('parent_id')
        ctype = route.get('collection_type')
        source = self.ctx.source
        save = getattr(source, 'save_view_setting', None)
        if save is None:
            return

        def work():
            save(server, parent, ctype, setting, value, key=key)

        def failed(_exc):
            was_rolled = _image_type_of(route.get('_view'))
            rolled = dict(route.get('_view') or {})
            rolled[setting] = (previous, key)
            route['_view'] = rolled
            route.pop('_grid_shape', None)
            self.ctx.status(_('That view setting could not be saved.'))
            self._redraw_or_refetch(was_rolled)
        self.ctx.run.run(work, lambda _r: None, self.ctx.run.epoch, on_error=failed)

    def _retire_legacy_list_view(self):
        """Clear a ``viewType`` an earlier build of this client wrote.

        ``view_prefs.is_list`` still honours that key, which means nothing
        writing only ``imageType`` can take such a library back OUT of list
        view -- the checkbox saved the grid value, the legacy key outvoted it,
        and it came back ticked on the next frame.

        Called from the top of :meth:`_set_view`, ahead of **both** the no-op
        check and the snapshot it takes of ``_view``: the box unticks by
        writing the imageType the library already had, so the write it rides on
        is very often no write at all, and a caller holding an older ``_view``
        puts the legacy value straight back.

        One-way, deliberately: nothing writes ``viewType`` any more, so this is
        a migration rather than a setting.
        See docs/browser-shell.md section 12.
        """
        if view_prefs.is_list_view(self._view('viewType')):
            self._set_view('viewType', view_prefs.GRID_VIEW)

    def _redraw_or_refetch(self, was):
        """Repaint after a view change -- and re-ask the server when the change
        was one it needs to hear about.

        Which artwork the tiles are drawn with is part of the QUERY: the server
        only sends the image tags a request names, so a grid fetched as Auto
        has no Banner in its ImageTags. **The test is what the query would be,
        not which setting was picked** -- Auto and Poster ask for the same
        artwork and differ only in the shape, so that pair is a repaint.

        In place rather than through ``_reload``: the items are not stale, only
        the tags on them.

        ``nav.load`` rather than ``nav.reload``, because the rollback path
        reaches here from ``on_error``, which is deliberately not epoch-gated
        -- and ``reload`` would bump the epoch and strand the in-flight load of
        whatever IS on screen. See docs/browser-shell.md section 12.
        """
        from ..repository import browse_image_types
        now = _image_type_of(self.route.get('_view'))
        if browse_image_types(was) == browse_image_types(now):
            self.ctx.invalidate()
            return
        for k in ('_items', '_total', '_win_tried', '_win_load'):
            self.route.pop(k, None)
        self._pages.reset(self.route)
        if self.ctx.nav.is_current(self.route):
            self.ctx.nav.reload(self.route)
        else:
            self.ctx.nav.load(self.route)
    _NOT_IN_PANEL = ('letter',)

    def _panel_keys(self):
        """Filter keys this source can actually apply.

        Asked of the SOURCE rather than tested as "are we offline": a
        page is not supposed to care which source it has, and the honest
        question is what the source can do. Empty on a downloaded
        library, which is what takes the Filter button off the bar.

        `getattr` with a default because the shell's stand-ins and any
        third source predate the capability; the default is the online
        set, so a source that says nothing keeps today's behaviour rather
        than silently losing its filters.
        """
        from ..repository import SUPPORTED_FILTERS
        return getattr(self.ctx.source, 'supported_filters', SUPPORTED_FILTERS)

    def _filters_offered(self):
        """Whether to draw the Filter button at all."""
        return bool(set(self._panel_keys()) - set(self._NOT_IN_PANEL))

    def _active_filters(self):
        """How many filters are on. Drives the button's dot.

        Counts only what this source can APPLY -- a key it ignores is not
        a filter that is on, and counting it made the badge report
        filtering that was not happening.
        """
        f = self.route.get('_filters') or {}
        keys = self._panel_keys()
        return sum((1 for k, v in f.items() if v and k not in self._NOT_IN_PANEL and (k in keys)))

    def _open_filters(self):
        """The filter panel: a modal, because it is a page of controls."""
        self.ctx.dialogs.filter_panel(lambda: self.route.get('_filtervals') or {}, lambda: self.route.get('_filters') or {}, self._match_count, self._set_filter, self._panel_toggle, self._clear_filters, collection_type=self.route.get('collection_type'))

    def _match_count(self):
        """``(matches, pending)`` for the filter panel's count.

        The same pair the header draws with: ``_total`` (which ``_reload``
        keeps across a re-query on purpose) and whether the query behind
        it has answered yet. A missing ``_items`` is exactly that test --
        it is what ``render`` blanks the tiles on.

        Only the panel asks; the header shows the count *conditionally*
        (a paginated grid says "of N" on the pagination bar instead) and
        the panel covers both of those, so it shows it unconditionally.

        A failed load is not pending. Its ``_items`` stays None for good
        -- the page swaps itself for the retry screen -- so testing that
        alone leaves a spinner turning in here over a query that is never
        coming back.
        """
        return (self.route.get('_total') or 0, self.route.get('_items') is None and (not self.route.get('_error')))

    def _panel_toggle(self, key):
        """A panel checkbox. Repaints the dialog as well as reloading.

        The renderer flips a Dropdown's own selection optimistically, but
        **a Checkbox is drawn from its `checked` argument** and only a
        redraw can move the tick -- the standing footgun in this codebase
        (CLAUDE.md), and the reason the Add to Playlist dialog once had a
        Private box that flipped invisibly.
        """
        self._toggle_filter(key)
        self.ctx.invalidate()

    def _clear_filters(self):
        """Everything off, except the A-Z rail, which is not in here."""
        keep = {k: v for k, v in (self.route.get('_filters') or {}).items() if k in self._NOT_IN_PANEL}
        self.route['_filters'] = keep
        self._persist_filters()
        self._reload()
        self.ctx.invalidate()

    def _fit_bar(self, bar, extras, width):
        """``bar`` with ``extras`` appended, or stacked under it if that
        would not fit.

        Measured rather than switched on a width constant, for the same
        reason the top bar is: what fits depends on how many controls this
        particular library has -- a movies library carries a Collections
        toggle and a Genres button that a music one does not, and the sort
        and genre dropdowns are sized to their contents.
        """
        avail0 = (width or 0) - 2 * chrome.CONTENT_PAD
        if not extras:
            return Row(bar.children, gap=10, align='center', w=avail0) if avail0 > 0 else bar
        wide = Row(extras + bar.children, gap=10, align='center')
        avail = (width or 0) - 2 * chrome.CONTENT_PAD
        if avail <= 0:
            return wide
        try:
            from ...mpvtk.layout import measure
            fits = measure(wide)[0] <= avail
        except Exception:
            log.debug('could not measure the filter bar', exc_info=True)
            fits = True
        if fits:
            return Row(extras + bar.children, gap=10, align='center', w=avail)
        return Column([Row(extras, gap=10, align='center'), Row(bar.children, gap=10, align='center', w=avail)], gap=8, align='stretch')

    def _grid_shape(self, items):
        """``(geom, image_type)`` for this grid, shaped by its own artwork.

        jellyfin-web asks for ``CardShape.Auto`` and lets the median
        ``PrimaryImageAspectRatio`` decide (``ItemsView.tsx:87``); movies come
        out as posters because movie posters *are* 2:3, not because anything
        says so. Ours said it for every collection type at once, which is why a
        Home Videos library came out portrait.

        **Computed once per route and parked**, because a grid is paged and a
        per-page median would change the grid's shape as you scroll. Cleared
        wherever ``_items`` is. An explicit choice beats the median outright;
        the fallback when nothing carries a ratio is square.
        See docs/browser-shell.md section 12.
        """
        route = self.route
        named = view_prefs.shape_for(self._view('imageType'))
        if named is not None:
            attr, image_type = named
            return (self.ctx.art.tiles.caption_geom(items, getattr(self.ctx.art, attr)), image_type)
        parked = route.get('_grid_shape')
        if parked is None:
            art = self.ctx.art
            parked = art.tiles.auto_geom(items, default=art.geom_square, default_type='Primary')
            route['_grid_shape'] = parked
            if log.isEnabledFor(logging.DEBUG):
                loaded = [i for i in items or () if i]
                ratios = sorted((r for r in (i.get('PrimaryImageAspectRatio') for i in loaded) if isinstance(r, (int, float)) and r > 0))
                log.debug('grid %s (%s): %d/%d items carry a ratio %s -> %dx%d %s', route.get('title'), route.get('collection_type'), len(ratios), len(loaded), '%.3f..%.3f' % (ratios[0], ratios[-1]) if ratios else '(none)', parked[0].tile_w, parked[0].tile_h, parked[1])
        return parked

    def _reload(self):
        """Re-run this route's query, **keeping what is on screen until the
        new results land**.

        This used to pop ``_items``, and ``render`` answers a missing
        ``_items`` with ``chrome.busy()`` for the whole page -- so every filter
        tick, sort change and letter press blanked the library behind the
        filter panel.

        Stale-while-revalidate, the rule refresh_live_tv already argues for.
        Unlike that one this DOES bump the epoch, since the query has changed
        and anything in flight is answering the wrong question.

        ``_total`` is deliberately NOT dropped: it is the header's item count,
        and zeroing it puts "0 items" over the spinner, which is worse than a
        count one second out of date. ``_install`` overwrites it.
        See docs/browser-shell.md section 11.
        """
        route = self.route
        for k in ('_items', '_grid_shape', '_win_tried', '_win_load'):
            route.pop(k, None)
        route['_loading'] = False
        self._pages.reset(route)
        self.ctx.nav.reload(route)

    def _set(self, key, value):
        self.route[key] = value
        self._reload()

    def _set_filter(self, key, value):
        self.route.setdefault('_filters', {})[key] = value
        if key not in self._NOT_IN_PANEL:
            self._persist_filters()
        self._reload()

    def _toggle_filter(self, key):
        f = self.route.setdefault('_filters', {})
        on = not f.get(key)
        f[key] = on
        if on:
            other = dialogs.MUTUALLY_EXCLUSIVE.get(key)
            if other:
                f[other] = False
        self._persist_filters()
        self._reload()

    def _toggle_collections(self):
        """Movies library <-> its collections, like jellyfin-web's toggle.
        Collections are server-wide and recursive, so this is a different
        query rather than a filter."""
        self.route['_collections'] = not self.route.get('_collections')
        for k in ('_items', '_total', '_loading', '_grid_shape', '_win_tried', '_win_load'):
            self.route.pop(k, None)
        self._pages.reset(self.route)
        self.ctx.nav.reload(self.route)

    def _play_all_capable(self):
        """Whether this library gets a Play All button.

        Everywhere but a TV library. A TV grid is a grid of *shows*, so
        "play all" over it means every episode of everything in name order,
        which is not a thing anyone wants and is a large queue to build by
        accident. Shuffle over the same set is fine -- a random episode is a
        perfectly good ask, and it is why Shuffle stays.

        (jellyfin-web does offer it there, on a tab that lists episodes
        rather than series. We have no such tab, so the button would mean
        something different from the one it is copying.)
        """
        return self.route.get('collection_type') not in NO_PLAY_ALL | NO_LIBRARY_PLAY

    def _shuffle_capable(self):
        """Whether this library gets a Shuffle button. See NO_LIBRARY_PLAY."""
        return self.route.get('collection_type') not in NO_LIBRARY_PLAY

    def _shuffle(self):
        self._queue_library(lambda source, srv, parent, _bound: source.get_shuffle_ids(srv, parent))

    def _play_all(self):
        """Queue the library in the order the grid is showing it.

        The sort and the filters go to the server, not the loaded page: what
        is on screen is one screenful of an infinite scroll, and "Play All"
        that plays the first hundred is a worse answer than no button. Same
        as web, which hands its query options straight to playbackManager.
        """
        ctype = self.route.get('collection_type')

        def work(source, srv, parent, bound):
            sort_by, sort_order, filters = (bound[0], bound[1], bound[2])
            return source.get_play_all_ids(srv, parent, sort_by=sort_by, sort_order=sort_order, filters=filters, collection_type=ctype)
        self._queue_library(work)

    def _queue_library(self, fetch_ids):
        """Shared body of Play All and Shuffle: resolve ids off the loop
        thread, then start the queue."""
        srv = self.route.get('server') or self.ctx.server
        source = self.ctx.source
        parent = self.route['parent_id']
        actions = self.ctx.actions
        bound = self._bound_query()

        def work():
            return fetch_ids(source, srv, parent, bound)

        def done(ids):
            if ids:
                actions.play_list(ids, srv, 0, pause_stills=False)
            else:
                self.ctx.status(_('There is nothing here to play.'))
        self.ctx.run.run(work, done, self.ctx.run.epoch)

class ListPage(GridPage):
    """A generic "everything of this kind" listing -- jellyfin-web's
    ``#/list?type=…``.

    The destination behind a section heading. Every row on the home and
    Live TV screens is a top-N of something (twelve upcoming films, twenty
    Next Up) with no way to see the rest, and this is the rest. One page
    rather than one per section because they differ only in the query:
    the grid, the paging, the sort and the shape are the same screen.

    A ``GridPage`` subclass for that reason, exactly as ``PersonPage`` is.
    What it replaces is ``parent_id`` -- a list is defined by a *predicate*
    (this genre, this studio, favourites, still-to-air films) rather than by
    a folder, which is the one thing the grid could not express.
    """
    kind = 'list'
    HEAD_H = 40 + 46
    UNSORTABLE = frozenset({'nextup', 'programs', 'recordings'})

    def _spec(self):
        return self.route.get('list') or {}

    def _sortable(self):
        return self._spec().get('type') not in self.UNSORTABLE

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get('server') or self.ctx.server
        spec = self._spec()
        sort_by, sort_order = self._sort_args()

        def work():
            return source.get_list(srv, spec, sort_by=sort_by, sort_order=sort_order)

        def done(res):
            items, total = res
            total = len(items) if sort_by == 'Random' else total
            route['_total'] = total
            route['_items'] = pagination.spread([], total, items, 0)
        self.route_async(work, done, epoch)

    def _sort_args(self):
        if not self._sortable():
            return (None, None)
        _label, sort_by, sort_order = self._sorts()[self.route.get('_sort', 0)]
        return (sort_by, sort_order)

    def _bound_query(self):
        sort_by, sort_order = self._sort_args()
        return (sort_by, sort_order, self.route.get('_filters') or {}, None, self.route.get('server') or self.ctx.server, None, False, None)

    def _fetch_at(self, start, limit=None, bound=None):
        sort_by, sort_order, filters, _person, srv, _itype, _coll, _ct = bound or self._bound_query()
        kw = {} if limit is None else {'limit': limit}
        return self.ctx.source.get_list(srv, self._spec(), sort_by=sort_by, sort_order=sort_order, start_index=start, filters=filters, **kw)
    SHAPES = {'poster': ('geom', 'Primary'), 'landscape': ('geom_wide', 'Thumb'), 'square': ('geom_square', 'Primary')}

    def _grid_shape(self, items):
        named = self.SHAPES.get(self._spec().get('shape'))
        if named is None:
            return super()._grid_shape(items)
        attr, image_type = named
        return (getattr(self.ctx.art, attr), image_type)

    def _header(self, items, width=0):
        header = [Text(self.route.get('title', ''), size='page', bold=True)]
        if self._sortable():
            header.append(self._sort_bar())
        if not self._pages.enabled():
            header.append(Text(_('%d items') % (self.route.get('_total') or 0), size='caption', color=theme.SUBTLE_FG))
        return header

class PersonPage(GridPage):
    """A person's filmography. Same grid, different source and header.

    Sort only: genre/year/letter filters make no sense over one person's
    credits, but "newest first" very much does. That distinction used to be
    a ``route["kind"] == "person"`` branch inside the shared renderer.
    """
    kind = 'person'
    HEAD_H = 40 + 46

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get('server') or self.ctx.server
        _label, sort_by, sort_order = self._sorts()[route.get('_sort', 0)]

        def work():
            return source.get_person_items(srv, route['person_id'], sort_by=sort_by, sort_order=sort_order)

        def done(res):
            items, total = res
            total = len(items) if sort_by == 'Random' else total
            route['_total'] = total
            route['_items'] = pagination.spread([], total, items, 0)
        self.route_async(work, done, epoch)

    def _header(self, items, width=0):
        return [Text(self.route.get('title', ''), size='page', bold=True), self._sort_bar()]

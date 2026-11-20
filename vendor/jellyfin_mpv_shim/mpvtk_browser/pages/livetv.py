"""The Live TV screen: Programs, Guide, Channels, Recordings, Schedule,
Series.

jellyfin-web's six Live TV tabs, in one page rather than six controllers,
because the shim's tabs share a route dict and a per-tab cache exactly as
the music library's do (``pages/music.py``). Each tab is a ``_load_*`` /
``_render_*`` pair; the dispatch tables below are the whole routing.

This page owns *which* guide window is shown (date, time, channel page) and
``guide_view`` owns drawing it; the preferences are jellyfin-web's, which is
why the settings dialog saves through the repository and not into
``conf.settings``. ``docs/live-tv.md`` is the reference for the subsystem.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import datetime
from ...i18n import _, _p
from ...mpvtk.widgets import Button, Column, Icon, Row, Spacer, Text, VScroll
from .. import components, guide_view, live_tv, theme
from ..components import chrome, controls
from ..repository import CHANNEL_PAGE
from ..tile_renderer import GRID_GAP
from .base import Page
DAY = datetime.timedelta(days=1)

class LiveTvPage(Page):
    """The tabbed Live TV library."""
    kind = 'livetv'
    TABS = (('programs', _('Programs')), ('guide', _('Guide')), ('channels', _('Channels')), ('recordings', _('Recordings')), ('schedule', _('Schedule')), ('series', _('Series')))
    DEFAULT_TAB = 'programs'

    def _tabs(self):
        """Every tab, to every user who can see Live TV at all.

        `EnableLiveTvManagement` gates *changing* the DVR, not reading it, so
        no tab is hidden by policy and the *actions* are gated instead. See
        `docs/live-tv.md` section 10 and `docs/jellyfin-api-notes.md`
        section 9.5.
        """
        return self.TABS

    def _current_tab(self):
        """The tab to draw. An unknown tab carried in on a route falls back
        to the default rather than rendering a screen with no way out of
        it."""
        tab = self.route.get('_tab') or self.DEFAULT_TAB
        if tab not in [key for key, _label in self._tabs()]:
            return self.DEFAULT_TAB
        return tab

    def load(self, epoch):
        tab = self._current_tab()
        getattr(self, '_load_' + tab, self._load_programs)(epoch)

    def _put(self, key='_data'):
        """The usual "stash it on the route and repaint" completion."""
        route = self.route

        def done(value):
            route[key] = value
            route['_loading'] = False
        return done

    def _srv(self):
        return self.route.get('server') or self.ctx.server

    def _load_programs(self, epoch):
        source, srv = (self.ctx.source, self._srv())
        self.route_async(lambda: source.get_program_sections(srv), self._put(), epoch)

    def _load_channels(self, epoch):
        source, srv = (self.ctx.source, self._srv())
        cats = self._categories()
        favorites = bool(self.route.get('_fav_only'))
        have = len(self.route.get('_data') or ())
        limit = max(CHANNEL_PAGE, have)

        def work():
            prefs = source.get_live_tv_prefs(srv)
            items, total = source.get_channels(srv, prefs=prefs, limit=limit, categories=cats, favorites_only=favorites)
            return {'prefs': prefs, 'items': items, 'total': total}

        def done(data):
            route = self.route
            route['_prefs'] = data['prefs']
            route['_data'] = data['items']
            route['_total'] = data['total']
            route['_loading'] = False
        self.route_async(work, done, epoch)

    def _load_guide(self, epoch):
        source, srv = (self.ctx.source, self._srv())
        cats = self._categories()
        page = int(self.route.get('_chan_page') or 0)
        self._reseed_window()
        start = self._window_start()
        end = start + datetime.timedelta(minutes=live_tv.CELL_MINUTES * live_tv.MAX_CELLS)
        known_info = self.route.get('_guide_info')

        def work():
            prefs = source.get_live_tv_prefs(srv)
            info = known_info if known_info is not None else source.get_guide_info(srv)
            channels, total = source.get_channels(srv, start_index=page * CHANNEL_PAGE, limit=CHANNEL_PAGE, prefs=prefs, categories=cats, add_current_program=False)
            programs = source.get_guide(srv, [c.get('Id') for c in channels], start, end, want_hd=bool((prefs.get('indicators') or {}).get('hd')))
            by_channel: dict = {}
            for program in programs:
                by_channel.setdefault(program.get('ChannelId'), []).append(program)
            return {'prefs': prefs, 'info': info, 'channels': channels, 'total': total, 'programs': by_channel, 'start': start, 'end': end}

        def done(data):
            route = self.route
            route['_guide_info'] = data['info']
            route['_data'] = data
            route['_loading'] = False
        self.route_async(work, done, epoch)

    def _load_recordings(self, epoch):
        source, srv = (self.ctx.source, self._srv())

        def work():
            latest = source.get_recordings(srv, limit=24)
            active = {r.get('Id') for r in source.get_recordings(srv, is_in_progress=True)}
            for item in latest:
                if item.get('Id') in active:
                    item['_recording'] = True
            return {'latest': latest, 'folders': source.get_recording_folders(srv)}
        self.route_async(work, self._put(), epoch)

    def _load_schedule(self, epoch):
        source, srv = (self.ctx.source, self._srv())

        def work():
            return {'active': source.get_recordings(srv, is_in_progress=True), 'timers': source.get_timers(srv, is_active=False, is_scheduled=True)}
        self.route_async(work, self._put(), epoch)

    def _load_series(self, epoch):
        source, srv = (self.ctx.source, self._srv())
        self.route_async(lambda: source.get_series_timers(srv), self._put(), epoch)

    def render(self, size):
        tab = self._current_tab()
        body = getattr(self, '_render_' + tab, self._render_programs)
        return Column([self._tab_bar(tab), body(size)], flex=1, align='stretch')

    def _tab_bar(self, current):
        tabs = [controls.tab_btn(label, 'lttab-' + key, key == current, lambda k=key: self._set_tab(k)) for key, label in self._tabs()]
        return Row(tabs, gap=8, pad=chrome.CONTENT_PAD, align='center')

    def _scroll(self, children, scroll_id, gap=16):
        """A stack of carousels declaring where their section tops are.

        The same treatment the home screen gets. Explicit content-y
        breakpoints rather than a uniform pitch, because auto-shaped rows
        differ in height (see ``_auto_row``) and a fixed step drifts out of
        alignment within two sections. ``docs/live-tv.md`` section 5.
        """
        blocks = chrome.split_bleed(children, chrome.CONTENT_PAD, gap, align='stretch')
        return VScroll(Column(blocks, pad=(0, chrome.CONTENT_PAD), gap=gap, align='stretch'), id=scroll_id, flex=1, offset=self.parked_scroll(scroll_id), snaps=components.section_offsets(blocks, gap, pad=chrome.CONTENT_PAD))

    def _render_programs(self, size):
        rows = self.route.get('_data')
        if rows is None:
            return chrome.busy()
        if not rows:
            return chrome.error(_('No programs are listed right now.'))
        return self._scroll([self._program_row(row) for row in rows], 'livetv-programs')

    def _program_row(self, row):
        """One Programs-tab row.

        Upcoming Movies is the exception jellyfin-web also makes: it forces
        portrait with preferThumb off (``livetvsuggested.js:87-91``) rather
        than letting the artwork decide. Films are the one guide category
        that reliably carries poster art, and a median over a handful of
        them lands on landscape often enough to make the row inconsistent
        between refreshes.
        """
        art = self.ctx.art
        see_all = self._see_all_programs(row)
        if row['key'] == 'movies':
            return art.tiles.tile_row(row['title'], row['items'], 'lt-' + row['key'], geom=art.tiles.caption_geom(row['items'], art.geom), image_type='Primary', see_all=see_all)
        return self._auto_row(row['title'], row['items'], 'lt-' + row['key'], see_all=see_all)

    def _see_all_programs(self, row):
        """``on_click`` for a Programs row heading.

        The row's own query is the listing's query -- PROGRAM_SECTIONS holds
        the flags, so the destination is the same predicate without the
        limit. Rows the source did not describe (a stand-in in a test, a
        future section) simply get no chevron rather than a broken one.
        """
        filters = row.get('filters')
        if not filters:
            return None
        server = self._srv()
        title = row['title']
        return lambda: self.ctx.nav.navigate({'kind': 'list', 'server': server, 'title': title, 'list': {'type': 'programs', 'filters': dict(filters)}})

    def _auto_row(self, title, items, row_id, on_click=None, see_all=None):
        """A row shaped by its own artwork, like jellyfin-web's.

        Its card builder resolves one shape per row from the items' median
        aspect ratio (see ``TileRenderer.auto_geom``), which is why the
        Programs screen shows posters for the film rows and landscape stills
        for the rest. Landscape is the fallback for a row where nothing
        carries a ratio — most guide entries have no art of their own.
        """
        art = self.ctx.art
        geom, image_type = art.tiles.auto_geom(items, default=art.geom_wide, default_type='Thumb')
        return art.tiles.tile_row(title, items, row_id, geom=geom, image_type=image_type, on_click=on_click, see_all=see_all)

    def _window_start(self):
        """Where the visible window begins.

        Seeded from the clock (rounded down to the half hour, so what is on
        *now* is in the first column) and thereafter whatever the arrows and
        _reseed_window left. Never re-seeded from *here*: this is read while
        building the header too, and moving the window during a render would
        label the grid with a range its data does not cover.
        """
        start = self.route.get('_start')
        if start is None:
            start = live_tv.floor_to_cell(live_tv.now())
            self.route['_start'] = start
        return start

    def _reseed_window(self):
        """Follow the clock, unless the user has paged away from it.

        Without this, every one of the Guide's three re-read triggers
        re-fetched the *same* window and "on now" stopped being now. A
        **flag** rather than a staleness test, because once the arrows have
        moved the window it is the user's and a background refresh must not
        yank them back. ``docs/live-tv.md`` section 2.
        """
        if self.route.get('_start_pinned'):
            return
        self.route['_start'] = live_tv.floor_to_cell(live_tv.now())

    def _categories(self):
        """The guide's category filter. Session state, like jellyfin-web's
        ``categoryOptions`` — it is a way to look through a big line-up, not
        a preference worth persisting."""
        return tuple(self.route.get('_categories') or ())
    GUIDE_CHROME_H = 90

    def _render_guide(self, size):
        data = self.route.get('_data')
        art = self.ctx.art
        cells = live_tv.cells_for_width(guide_view.grid_width(size))
        head = self._guide_controls(data, cells)
        if data is None:
            return Column([head, chrome.busy()], flex=1, align='stretch')
        start = data['start']
        end = min(data['end'], start + datetime.timedelta(minutes=live_tv.CELL_MINUTES * cells))
        grid = guide_view.guide_grid(data['channels'], data['programs'], start, end, (size[0], max(120, size[1] - self.GUIDE_CHROME_H)), data['prefs'], art.tiles, art.scroll, 'livetv-guide', on_program=self._open_program, offset=self.parked_scroll('livetv-guide'), on_scroll=lambda off, mx: art.scroll.on_scroll('livetv-guide', off, mx), categories=self._categories(), on_context=art.tiles.on_context, on_channel=self._open_channel)
        return Column([head, grid], flex=1, align='stretch', gap=6)

    def _guide_controls(self, data, cells):
        """Date/time/channel-page navigation above the grid.

        The time arrows step by one visible window, so "later" always means
        "the next screenful" however wide the window happens to be.
        """
        start = (data or {}).get('start') or self._window_start()
        step = datetime.timedelta(minutes=live_tv.CELL_MINUTES * cells)
        info = (data or {}).get('info') or {}
        total = (data or {}).get('total') or 0
        page = int(self.route.get('_chan_page') or 0)

        def nav(icon, node_id, delta, tip):
            return Button('', id=node_id, icon=icon, flat=True, icon_size=20, tip=tip, on_click=lambda: self._move_window(delta, info, cells))
        controls_row = [nav('keyboard_double_arrow_left', 'lt-prevday', -DAY, _('Previous Day')), nav('chevron_left', 'lt-prevwin', -step, _('Earlier')), Text('%s   %s' % (live_tv.fmt_day(start), live_tv.fmt_time(start)), size='normal', bold=True), nav('chevron_right', 'lt-nextwin', step, _p('guide navigation', 'Later')), nav('keyboard_double_arrow_right', 'lt-nextday', DAY, _('Next Day')), controls.action_btn('schedule', _('Now'), 'lt-now', self._jump_to_now), Spacer(flex=1)]
        if total > CHANNEL_PAGE:
            last = (total - 1) // CHANNEL_PAGE
            controls_row += [Text(_('Channels %(from)d-%(to)d of %(total)d') % {'from': page * CHANNEL_PAGE + 1, 'to': min((page + 1) * CHANNEL_PAGE, total), 'total': total}, size='caption', color=theme.SUBTLE_FG), Button('', id='lt-chanprev', icon='keyboard_arrow_up', flat=True, icon_size=20, tip=_('Previous Channels'), on_click=lambda: self._channel_page(page - 1, last)), Button('', id='lt-channext', icon='keyboard_arrow_down', flat=True, icon_size=20, tip=_('More Channels'), on_click=lambda: self._channel_page(page + 1, last))]
        controls_row.append(controls.action_btn('settings', _('Guide Settings'), 'lt-guidecfg', self._open_guide_settings))
        return Row(controls_row, gap=6, align='center', pad=(chrome.CONTENT_PAD, 0))

    def _move_window(self, delta, info, cells=live_tv.MAX_CELLS):
        target = live_tv.clamp_window(self._window_start() + delta, info, cells)
        if target == self.route.get('_start'):
            return
        self.route['_start'] = target
        self.route['_start_pinned'] = True
        self._reload_tab()

    def _jump_to_now(self):
        self.route['_start'] = live_tv.floor_to_cell(live_tv.now())
        self.route['_start_pinned'] = False
        self._reload_tab()

    def _channel_page(self, page, last):
        page = max(0, min(page, last))
        if page == int(self.route.get('_chan_page') or 0):
            return
        self.route['_chan_page'] = page
        self.ctx.art.scroll.forget('livetv-guide')
        self._reload_tab()

    def _open_guide_settings(self):
        data = self.route.get('_data')
        prefs = (data.get('prefs') if isinstance(data, dict) else None) or self.route.get('_prefs')
        if prefs is None:
            return
        self.ctx.dialogs.guide_settings(self._srv(), prefs, self._categories(), self._guide_settings_saved)

    def _guide_settings_saved(self, prefs, categories):
        self.route['_prefs'] = prefs
        self.route['_categories'] = tuple(categories)
        self.route.pop('_tab_cache', None)
        self._reload_tab()
    CHANNEL_CHROME_H = 44

    def _render_channels(self, size):
        items = self.route.get('_data')
        head = self._channel_controls()
        if items is None:
            return Column([head, chrome.busy()], flex=1, align='stretch')
        if not items:
            return Column([head, chrome.error(_('No favorite channels yet.') if self.route.get('_fav_only') else _('No channels available.'))], flex=1, align='stretch')
        art = self.ctx.art
        grid_size = (size[0], max(120, size[1] - self.CHANNEL_CHROME_H))
        gpad, cgeom = art.tiles.grid_layout(size[0], art.geom_square)
        return Column([head, VScroll(Column(art.tiles.grid_of(items, 'ltchan', grid_size, geom=cgeom, scroll_id='livetv-channels'), pad=(gpad, chrome.CONTENT_PAD), gap=GRID_GAP), id='livetv-channels', flex=1, offset=self.parked_scroll('livetv-channels'), snap=art.geom_square.strip_h + GRID_GAP, snap_off=chrome.CONTENT_PAD, on_scroll=lambda off, mx: art.scroll.on_scroll('livetv-channels', off, mx, self._channels_scrolled))], flex=1, align='stretch', gap=6)

    def _channel_controls(self):
        """Filter and settings above the channel grid.

        jellyfin-web's Channels tab has a filter button whose dialog, in
        ``livetvchannels`` mode, collapses to exactly one control: Favorites.
        The settings button is ours, and it earns its place — the categories
        in that dialog filter this list too (see ``LibrarySource``), and the
        only door to them used to be on the Guide tab, so filtering the
        channel grid meant leaving it.
        """
        favorites = bool(self.route.get('_fav_only'))
        row = [controls.action_btn('favorite', _('Favorites'), 'lt-chanfav', self._toggle_channel_favorites, on=favorites), controls.action_btn('settings', _('Guide Settings'), 'lt-chancfg', self._open_guide_settings)]
        total = self.route.get('_total') or 0
        if total:
            row.append(Text(_('%d channels') % total, size='caption', color=theme.SUBTLE_FG))
        return Row(row, gap=6, align='center', pad=(chrome.CONTENT_PAD, 0))

    def _toggle_channel_favorites(self):
        """Show only favourites, or everything again.

        Session state on the route, not a saved preference: jellyfin-web's
        filter is the same -- it lives in that tab's query object and is gone
        when you leave. ``favorites_first`` in the guide settings is the
        durable half of this and is a different question.
        """
        self.route['_fav_only'] = not self.route.get('_fav_only')
        self.ctx.art.scroll.forget('livetv-channels')
        self._reload_tab()

    def _channels_scrolled(self, offset, maximum):
        """Page the next block of channels in near the bottom. A tuner
        line-up routinely runs past the first page, and without this the
        list simply stopped there."""
        source, srv = (self.ctx.source, self._srv())
        prefs = self.route.get('_prefs') or {}
        cats = self._categories()

        def put(route, items, total):
            route['_data'], route['_total'] = (items, total)
        self.ctx.art.pages.more(self.route, offset, maximum, lambda r: (r.get('_data') or [], r.get('_total') or 0), put, lambda start: source.get_channels(srv, start_index=start, prefs=prefs, categories=cats, favorites_only=bool(self.route.get('_fav_only'))))

    def _render_recordings(self, size):
        data = self.route.get('_data')
        if data is None:
            return chrome.busy()
        rows = []
        if data.get('latest'):
            title = _('Latest Recordings')
            server = self._srv()
            rows.append(self._auto_row(title, data['latest'], 'lt-recent', see_all=lambda: self.ctx.nav.navigate({'kind': 'list', 'server': server, 'title': title, 'list': {'type': 'recordings'}})))
        if data.get('folders'):
            rows.append(self._auto_row(_('Recording Folders'), data['folders'], 'lt-recfolders'))
        if not rows:
            return chrome.error(_('Nothing has been recorded yet.'))
        return self._scroll(rows, 'livetv-recordings')

    def _render_schedule(self, size):
        data = self.route.get('_data')
        if data is None:
            return chrome.busy()
        blocks = []
        if data.get('active'):
            blocks.append(self._auto_row(_('Active Recordings'), data['active'], 'lt-active'))
        for day, timers in self._group_by_day(data.get('timers') or []):
            blocks.append(self._auto_row(day, timers, 'lt-timers-' + day.replace(' ', ''), on_click=self._open_timer))
        if not blocks:
            return chrome.error(_('Nothing is scheduled to record.'))
        return self._scroll(blocks, 'livetv-schedule')

    @staticmethod
    def _group_by_day(timers):
        """Day grouping — see ``live_tv.group_by_day``, which the channel
        page shares."""
        return live_tv.group_by_day(timers)

    def _render_series(self, size):
        items = self.route.get('_data')
        if items is None:
            return chrome.busy()
        if not items:
            return chrome.error(_('No series are set to record.'))
        art = self.ctx.art
        gpad, sgeom = art.tiles.grid_layout(size[0], art.tiles.caption_geom(items, art.geom))
        return VScroll(Column(art.tiles.grid_of(items, 'ltseries', size, geom=sgeom, scroll_id='livetv-series', on_click=self._open_series_timer), pad=(gpad, chrome.CONTENT_PAD), gap=GRID_GAP), id='livetv-series', flex=1, offset=self.parked_scroll('livetv-series'), on_scroll=lambda off, mx: art.scroll.on_scroll('livetv-series', off, mx))

    def _open_program(self, program):
        """Open a guide cell.

        Always the program page, even for something airing right now: it is
        one click from there to Watch, and it is the only place Record lives.
        jellyfin-web makes the same split (a dialog on desktop, immediate
        playback only in its TV layout, where there is nowhere to put the
        recording controls).
        """
        self.ctx.nav.navigate({'kind': 'program', 'server': self._srv(), 'item_id': program.get('Id'), 'channel_id': program.get('ChannelId'), 'title': program.get('Name', ''), '_seed': program})

    def _open_channel(self, channel):
        """Open the guide's channel column, which is a link in jellyfin-web
        too (``guide-channelHeaderCell``, ``data-action="link"``) — not a
        tune-in button."""
        self.ctx.nav.navigate({'kind': 'channel', 'server': self._srv(), 'item_id': channel.get('Id'), 'title': channel.get('Name', ''), '_seed': channel})

    def _open_timer(self, timer):
        self.ctx.dialogs.timer_editor(self._srv(), timer, series=False, on_change=self._reload_tab)

    def _open_series_timer(self, timer):
        self.ctx.dialogs.timer_editor(self._srv(), timer, series=True, on_change=self._reload_tab)

    def _reload_tab(self):
        """Re-fetch the current tab, dropping its cached copy.

        Used after anything that changes what the tab should show — a timer
        cancelled, the guide window moved, the settings saved.
        """
        route = self.route
        cache = route.get('_tab_cache')
        if cache:
            cache.pop(route.get('_tab') or self.DEFAULT_TAB, None)
        for key in ('_data', '_total'):
            route.pop(key, None)
        route['_loading'] = False
        self.ctx.nav.reload(route)

    def _set_tab(self, tab):
        """Switch tab, keeping what each tab has already loaded.

        Per route dict, so it dies with the page and cannot go stale across
        a navigation — the same cache ``MusicLibraryPage`` keeps, and for the
        same reason: the guide fetch in particular is expensive enough that
        flipping to Channels and back must not pay for it twice.
        """
        route = self.route
        old = route.get('_tab') or self.DEFAULT_TAB
        if tab == old:
            return
        cache = route.setdefault('_tab_cache', {})
        if route.get('_data') is not None:
            cache[old] = (route['_data'], route.get('_total'))
        route['_tab'] = tab
        for key in ('_data', '_total'):
            route.pop(key, None)
        route['_loading'] = False
        self.ctx.art.pages.reset(route)
        self.ctx.art.scroll.forget('livetv-programs', 'livetv-channels', 'livetv-guide', 'livetv-recordings', 'livetv-schedule', 'livetv-series')
        hit = cache.get(tab)
        if hit is not None:
            route['_data'], route['_total'] = hit
            self.ctx.run.bump()
            self.ctx.invalidate()
            self.ctx.nav.load(route)
        else:
            self.ctx.nav.reload(route)

class ProgramPage(Page):
    """One guide entry: what it is, when it is on, and what to do about it.

    jellyfin-web's ``recordingcreator`` dialog as a page. A page rather than
    a dialog because this is also where "Watch" lives, and a modal that
    starts playback has to tear itself down over a window it no longer owns.
    """
    kind = 'program'

    def load(self, epoch):
        source, srv = (self.ctx.source, self._srv())
        program_id = self.route['item_id']
        if self.route.get('_data') is None and self.route.get('_seed'):
            self.route['_data'] = self.route['_seed']
        self.route_async(lambda: source.get_live_program(srv, program_id), self._done, epoch)

    def _done(self, item):
        if item:
            self.route['_data'] = item
        self.route['_loading'] = False

    def _srv(self):
        return self.route.get('server') or self.ctx.server

    def render(self, size):
        item = self.route.get('_data')
        if item is None:
            return chrome.busy()
        tiles = self.ctx.art.tiles
        w = size[0]
        full_bleed = tiles.full_bleed_header(item)
        bw, bh = tiles.banner_box(w, full_bleed, size[1])
        title = live_tv.program_title(item)
        meta = self._meta(item)
        banner = tiles.backdrop_node(item, (bw, bh), 'program-bd', title=title, meta=meta, context=item.get('ChannelName') or '')
        blocks = [banner]
        if not tiles.header_bakes_heading(item):
            if item.get('ChannelName'):
                blocks.append(Text(item['ChannelName'], size='normal', color=theme.SUBTLE_FG))
            blocks.append(Text(title, size='page', bold=True, wrap=True, w=tiles.body_w(w)))
            if meta:
                blocks.append(Text(meta, size='large', color=theme.SUBTLE_FG))
        sub = item.get('EpisodeTitle')
        if sub:
            blocks.append(Text(sub, size='large'))
        blocks.append(self._buttons(item))
        state = live_tv.timer_state(item)
        if state:
            blocks.append(Row([Icon(live_tv.STATE_ICONS[state], 18, color=theme.SUBTLE_FG if state == 'series_inactive' else theme.FAV_RED), Text(self.STATE_LABELS[state](), size='small', color=theme.SUBTLE_FG)], gap=6, align='center'))
        text = chrome.overview(item)
        if text:
            blocks.append(chrome.paragraph(text, 18, tiles.body_w(w)))
        return VScroll(chrome.header_body(blocks[0], blocks[1:], gap=16, full_bleed=full_bleed), id='program', flex=1, offset=self.parked_scroll('program'))
    STATE_LABELS = {'timer': lambda: _('Scheduled to record'), 'recording': lambda: _('Recording now'), 'series': lambda: _('Recording this series'), 'series_inactive': lambda: _('This series is set to record, but this showing is not scheduled')}

    @staticmethod
    def _meta(item):
        parts = [live_tv.air_time_label(item)]
        start = live_tv.parse_time(item.get('StartDate'))
        if start is not None:
            parts.insert(0, live_tv.fmt_day(start))
        for field, label in (('IsLive', _('Live')), ('IsPremiere', _('Premiere')), ('IsRepeat', _p('guide badge', 'Repeat'))):
            if item.get(field):
                parts.append(label)
        if item.get('OfficialRating'):
            parts.append(str(item['OfficialRating']))
        if item.get('Genres'):
            parts.append(', '.join(item['Genres'][:3]))
        return '   ·   '.join((p for p in parts if p))

    def _buttons(self, item):
        actions = self.ctx.actions
        server = self._srv()
        channel = item.get('ChannelId') or self.route.get('channel_id')
        single = live_tv.single_timer_state(item)
        btns = []
        if channel:
            btns.append(controls.action_btn('play_arrow', _('Watch') if live_tv.is_airing(item) else _('Watch Channel'), 'pg-watch', lambda: actions.play_list([channel], server, 0), primary=live_tv.is_airing(item), size=controls.PRIMARY_ROW))
        if not actions.can_record(server):
            return Row(btns, gap=10)
        if single:
            btns.append(controls.action_btn('cancel', _('Stop Recording') if single == 'recording' else _('Do not record'), 'pg-cancel', lambda: actions.cancel_timer(item.get('TimerId'), server, on_done=self._refresh), size=controls.PRIMARY_ROW))
        else:
            btns.append(controls.action_btn('fiber_manual_record', _('Record'), 'pg-record', lambda: actions.schedule_recording(item, server, on_done=self._refresh), size=controls.PRIMARY_ROW))
        if item.get('IsSeries'):
            if item.get('SeriesTimerId'):
                btns.append(controls.action_btn('cancel', _('Cancel Series'), 'pg-cancelseries', lambda: actions.cancel_series_timer(item.get('SeriesTimerId'), server, on_done=self._refresh), size=controls.PRIMARY_ROW))
                btns.append(controls.action_btn('settings', _('Series Options'), 'pg-seriesopts', lambda: self.ctx.dialogs.timer_editor(server, {'Id': item.get('SeriesTimerId')}, series=True, on_change=self._refresh), size=controls.PRIMARY_ROW))
            else:
                btns.append(controls.action_btn('fiber_smart_record', _('Record series'), 'pg-recseries', lambda: actions.schedule_recording(item, server, series=True, on_done=self._refresh), size=controls.PRIMARY_ROW))
        return Row(btns, gap=10)

    def _refresh(self):
        """Re-read the program after a recording change.

        A re-read rather than an optimistic flip: the server decides whether
        a "record this" became a series rule, what the timer's id is, and
        whether the request was honoured at all — and every one of those
        changes which buttons this page should show.
        """
        self.route.pop('_data', None)
        self.route['_loading'] = False
        self.ctx.nav.reload(self.route)

class ChannelPage(Page):
    """One channel: its logo and number, and everything still to come on it.

    jellyfin-web's ``renderChannelGuide``, and the destination a channel tile
    or a guide channel cell **links** to rather than tuning in. A page rather
    than a dialog because Watch lives on it and a modal that starts playback
    has to tear itself down over a window it no longer owns.
    ``docs/live-tv.md`` section 3.
    """
    kind = 'channel'
    ROW_H = 34
    LOGO = 84
    GAP = 14
    ROW_GAP = 2

    def load(self, epoch):
        source, srv = (self.ctx.source, self._srv())
        channel_id = self.route['item_id']
        if self.route.get('_channel') is None and self.route.get('_seed'):
            self.route['_channel'] = self.route['_seed']
        self.route_async(lambda: source.get_channel_listing(srv, channel_id), self._done, epoch)

    def _done(self, data):
        data = data or {}
        if data.get('channel'):
            self.route['_channel'] = data['channel']
        self.route['_data'] = data.get('programs') or []
        self.route['_capped'] = bool(data.get('capped'))
        self.route['_loading'] = False

    def _groups(self):
        """The listing grouped by day, computed once per fetch.

        ``render`` needs this every frame and ``live_tv.parse_time`` is not
        cheap, so it is keyed on the programme list's *identity* -- a refresh
        that replaces the list regroups and nothing else does.
        ``docs/live-tv.md`` section 12.
        """
        programs = self.route.get('_data') or []
        cached = self.route.get('_groups')
        if cached is None or cached[0] is not programs:
            cached = (programs, live_tv.group_by_day(programs))
            self.route['_groups'] = cached
        return cached[1]

    def _srv(self):
        return self.route.get('server') or self.ctx.server

    def _channel(self):
        return self.route.get('_channel') or {}

    def render(self, size):
        channel = self.route.get('_channel')
        programs = self.route.get('_data')
        if channel is None and programs is None:
            return chrome.busy()
        blocks = [self._header(size), self._buttons()]
        if programs is None:
            blocks.append(chrome.busy())
        elif not programs:
            blocks.append(chrome.error(_('No guide data is available for this channel.')))
        else:
            blocks += self._listing(blocks, size)
            if self.route.get('_capped'):
                blocks.append(Text(_('Showing the next %d programs.') % len(programs), size='small', color=theme.SUBTLE_FG))
        return VScroll(Column(blocks, pad=chrome.CONTENT_PAD, gap=self.GAP, align='stretch'), id='channel', flex=1, offset=self.parked_scroll('channel'), on_scroll=lambda off, mx: self.ctx.art.scroll.on_scroll('channel', off, mx))

    def _listing(self, head_blocks, size):
        """The day sections, with the off-screen ones as exact-height gaps.

        Same windowing ``grid_of`` and the guide do -- a screen of slack
        either side of the viewport, so a scroll lands on rows that are
        already drawn -- and it is what lets the fetch be a thousand
        programmes rather than two hundred: the scene stays the size of the
        window instead of the size of the guide.

        **Day granularity, and the headings always drawn.** A heading is one
        node and there are at most a couple of dozen; drawing them all is
        cheaper than the arithmetic to place a heading-shaped hole, and it
        means only the row blocks -- whose height is exactly
        ``n * ROW_H + (n - 1) * ROW_GAP`` -- need a measured stand-in. A day
        materializes whole, which for half-hour listings is ~48 rows, about
        two screens. A provider slicing five-minute segments would want this
        windowed per row as well.
        """
        from ...mpvtk.layout import measure
        y = float(chrome.CONTENT_PAD)
        for block in head_blocks:
            y += measure(block)[1] + self.GAP
        offset = max(0.0, self.ctx.art.scroll.offset('channel'))
        view = max(240.0, float(size[1]))
        out = []
        for day, items in self._groups():
            heading = Text(day, size='large', bold=True)
            out.append(heading)
            y += measure(heading)[1] + self.GAP
            h = len(items) * self.ROW_H + (len(items) - 1) * self.ROW_GAP
            if y + h >= offset - view and y <= offset + 2 * view:
                out.append(Column([self._program_row(p) for p in items], align='stretch', gap=self.ROW_GAP))
            else:
                out.append(Spacer(h=h))
            y += h + self.GAP
        return out

    def _header(self, size):
        """Logo, name and number, plus what is on right now.

        The logo comes through ``art_cell`` — the same path the guide's
        channel column uses, so a channel that draws there draws here. A
        channel has no backdrop to bake a heading into (``backdrop_node``
        would return its placeholder for every one of them), so the heading
        is ordinary text.
        """
        channel = self._channel()
        tiles = self.ctx.art.tiles
        number = live_tv.channel_number(channel)
        lines = []
        if number:
            lines.append(Text(number, size='normal', color=theme.SUBTLE_FG))
        lines.append(Text(channel.get('Name') or _('Channel'), size='hero', bold=True, wrap=True, w=tiles.body_w(size[0]) - self.LOGO - 16))
        now = self._now_playing_program()
        if now is not None:
            lines.append(Text('%s   ·   %s' % (live_tv.program_title(now), live_tv.air_time_label(now)), size='normal', color=theme.ACCENT))
        return Row([tiles.art_cell(channel, size=self.LOGO), Column(lines, gap=4)], gap=16, align='center')

    def _now_playing_program(self):
        """What is on right now — but only while the listing is still loading.

        Once it lands, its first row *is* what is on air (the fetch asks for
        ``HasAired=False``, which keeps whatever is mid-broadcast) and that
        row is drawn in the accent colour, so a header line saying the same
        thing is noise. This covers the moment before that: the tile that
        seeded the page carries ``CurrentProgram``, which is the only place
        it exists — the ordinary item endpoint the channel is re-fetched
        from does not populate it.
        """
        if self.route.get('_data') is not None:
            return None
        current = self._channel().get('CurrentProgram') or {}
        return current if current and live_tv.is_airing(current) else None

    def _buttons(self):
        actions = self.ctx.actions
        channel = self._channel()
        server = self._srv()
        channel_id = channel.get('Id') or self.route.get('item_id')
        btns = [controls.action_btn('play_arrow', _('Watch'), 'ch-watch', lambda: actions.play_list([channel_id], server, 0), primary=True, size=controls.PRIMARY_ROW)]
        if channel.get('Id'):
            fav = bool((channel.get('UserData') or {}).get('IsFavorite'))
            btns.append(controls.action_btn('favorite', _('Remove from favorites') if fav else _('Add to favorites'), 'ch-fav', lambda: actions.toggle_favorite(channel, server), on=fav, size=controls.PRIMARY_ROW))
        return Row(btns, gap=10)

    def _program_row(self, program):
        """One listing row: when it is on, whether it is being recorded, and
        what it is. Clicking opens the program page, which is where Record
        lives — the same destination a guide cell has."""
        airing = live_tv.is_airing(program)
        state = live_tv.timer_state(program)
        cells = [Text(live_tv.air_time_label(program), size='normal', color=theme.ACCENT if airing else theme.SUBTLE_FG, w=130), Icon(live_tv.STATE_ICONS[state], 16, color=theme.SUBTLE_FG if state == 'series_inactive' else theme.FAV_RED) if state else Spacer(w=16, h=1), Text(live_tv.program_title(program), size='normal', flex=1)]
        subtitle = program.get('EpisodeTitle')
        if subtitle:
            cells.append(Text(subtitle, size='small', color=theme.SUBTLE_FG, flex=1))
        return Row(cells, id='ch-pg-' + str(program.get('Id') or ''), h=self.ROW_H, gap=12, pad=(8, 0), align='center', radius=4, hover={'fill': theme.BUTTON_BG}, on_click=lambda p=program: self._open_program(p), on_context=lambda x, y, p=program: self.ctx.art.tiles.on_context(p, x, y))

    def _open_program(self, program):
        self.ctx.nav.navigate({'kind': 'program', 'server': self._srv(), 'item_id': program.get('Id'), 'channel_id': program.get('ChannelId') or self.route.get('item_id'), 'title': program.get('Name', ''), '_seed': program})

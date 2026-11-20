"""Reading an epub in the player's window.

The screen is one bitmap and two bars. The bitmap is the page, rasterized
by :mod:`jellyfin_mpv_shim.epub` at the exact size it will be drawn (mpv
never resamples an overlay — mpvtk GUIDE §5) and handed to the scene as a
single `Image`; the bars are ordinary widgets. Nothing about the book's
typography is expressed as toolkit nodes (``docs/readers.md`` §4.6).

**The book cannot be read from the server**, so this page is reachable only
once the file is on disk and it fetches it when it is not (§1). **Position
is written back on every turn**, as the same number jellyfin-web writes,
which is what changed the old "epub progress is not settable" rule (§4.3).

Everything expensive happens on the pool: opening the archive, building the
locations index, paginating a chapter, rasterizing a page. The loop thread
only ever reads what those left behind.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import logging
from ...books import fraction_of, ticks_for_fraction
from ...i18n import _
from ...mpvtk.widgets import Box, Button, Column, Dropdown, ImageMap, Menu, Row, Spacer, Text
from .. import theme
from ..components import chrome
from .base import Page
log = logging.getLogger('mpvtk_browser.pages.reader')
TOP_BAR_H = 44
BOTTOM_BAR_H = 48
PAGE_PAD = 8
FONT_STEPS = (15, 17, 19, 21, 24, 27, 31, 36)
MIN_FONT, MAX_FONT = (10, 72)
PALETTE_ORDER = ('dark', 'sepia', 'light')

class ReaderPage(Page):
    """An open book, one page at a time."""
    kind = 'reader'
    claimed_keys = ('LEFT', 'RIGHT', 'PGUP', 'PGDWN', 'SPACE', 'HOME', 'END', 'WHEEL_UP', 'WHEEL_DOWN')

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get('server') or self.ctx.server
        item_id = route['item_id']

        def work():
            item = source.get_item(srv, item_id)
            state = (None, None)
            try:
                state = self.ctx.player.book_download_state(item_id, srv)
            except Exception:
                log.debug('book download state unavailable', exc_info=True)
            return {'item': item, 'state': state}
        self.route_async(work, self._opened, epoch)

    def _opened(self, data):
        self.route['_data'] = data
        status, path = data.get('state') or (None, None)
        if path:
            self._open_book(path)
        elif status not in ('pending', 'downloading'):
            self._fetch()

    def refresh_download_state(self):
        """The catalog changed — called by the shell's downloads hook.

        This page is the one screen that is *waiting* on a download rather
        than merely reporting one, so it opens the book the moment the file
        appears instead of leaving the reader on "Downloading…" until the
        user goes back and in again.
        """
        data = self.route.get('_data')
        if not data or self.route.get('_doc') is not None:
            return
        item = data.get('item') or {}
        if not item.get('Id'):
            return
        try:
            data['state'] = self.ctx.player.book_download_state(item['Id'], self.route.get('server') or self.ctx.server)
        except Exception:
            log.debug('book download state unavailable', exc_info=True)
            return
        _status, path = data['state']
        if path:
            self._open_book(path)

    def _fetch(self):
        data = self.route.get('_data') or {}
        item = data.get('item') or {}
        server = self.route.get('server') or self.ctx.server
        self.ctx.actions.download_book(item, server)

    def _open_book(self, path):
        """Open the archive and build the index, off the loop thread."""
        route = self.route
        if route.get('_opening'):
            return
        route['_opening'] = True

        def work():
            from ...epub import EpubDocument
            from ...mpvtk import pilfont
            item = (route.get('_data') or {}).get('item') or {}
            script = pilfont.script_of(item.get('Name') or '')
            doc = EpubDocument(path, self._reader_style(), script)
            doc.build_index()
            doc.goto_fraction(fraction_of(item))
            return doc

        def done(doc):
            route['_doc'] = doc
            route['_opening'] = False
            self.ctx.actions.clear_downloading_toast((route.get('_data') or {}).get('item') or {})
            self.ctx.invalidate()

        def failed(exc):
            route['_opening'] = False
            route['_error'] = str(exc) or _('This book could not be opened.')
            self.ctx.invalidate()
        self.ctx.run.run(work, done, self.ctx.run.epoch, on_error=failed, always=lambda: route.update(_opening=False))

    def _reader_style(self):
        from ...epub.layout import ReaderStyle
        from ...mpvtk.scaling import px
        return ReaderStyle(font_px=px(self.font_size()), margin_x=px(28), margin_y=px(20), justify=self._setting('reader_justify', True))

    @staticmethod
    def _setting(key, fallback):
        """One config read, tolerant of a hand-edited value.

        Read at the moment it is used rather than captured when the page
        was built, because Settings is reachable from the tray while a book
        is open (``docs/readers.md`` §4.7).
        """
        from ...conf import settings
        value = getattr(settings, key, None)
        return fallback if value is None else value

    def font_size(self):
        """Type size in logical pixels, clamped to something readable."""
        try:
            size = int(self._setting('reader_font_size', FONT_STEPS[3]))
        except (TypeError, ValueError):
            size = FONT_STEPS[3]
        return max(MIN_FONT, min(size, MAX_FONT))

    def palette_name(self):
        """The page colour, falling back rather than trusting the string.

        A value that is not a palette must not reach ``paint.palette`` —
        the setting is a plain string in a JSON file somebody can type into.
        """
        name = self._setting('reader_theme', PALETTE_ORDER[0])
        return name if name in PALETTE_ORDER else PALETTE_ORDER[0]

    def _save_position(self, doc=None):
        """Write where we are back to wherever it can be kept.

        Fire and forget: a failed write costs the position on other
        clients, and blocking a page turn on a round trip would cost the
        reading. ``record_reading_position`` rather than ``set_position``,
        so the local catalog and the offline replay queue get it too — see
        ``docs/readers.md`` §2.2.
        """
        doc = doc or self.route.get('_doc')
        if doc is None:
            return
        fraction = doc.fraction()
        if fraction is None:
            return
        data = self.route.get('_data') or {}
        item = data.get('item') or {}
        server = self.route.get('server') or self.ctx.server
        if not item.get('Id') or server is None:
            return
        if self.route.get('_saved') == round(fraction, 6):
            return
        self.route['_saved'] = round(fraction, 6)
        ticks = ticks_for_fraction(fraction)
        seq = self.route.get('_pos_seq', 0) + 1
        self.route['_pos_seq'] = seq
        item.setdefault('UserData', {})['PlaybackPositionTicks'] = ticks
        setter = getattr(self.ctx.player, 'record_reading_position', None)
        if setter is None:
            return

        def work():
            if self.route.get('_pos_seq') != seq:
                return
            setter(server, item['Id'], ticks)
        self.ctx.run.submit(work)

    def on_key(self, key):
        """A claimed key. Loop thread; see ``claimed_keys``."""
        if key in ('RIGHT', 'PGDWN', 'SPACE', 'WHEEL_DOWN'):
            self._turn(1)
        elif key in ('LEFT', 'PGUP', 'WHEEL_UP'):
            self._turn(-1)
        elif key == 'HOME':
            self._jump_section(-1)
        elif key == 'END':
            self._jump_section(1)

    def _turn(self, direction):
        doc = self.route.get('_doc')
        if doc is None:
            return
        moved = doc.next_page() if direction > 0 else doc.prev_page()
        if moved:
            self._save_position(doc)
        self.ctx.invalidate()

    def _jump_section(self, direction):
        doc = self.route.get('_doc')
        if doc is None:
            return
        if doc.next_section() if direction > 0 else doc.prev_section():
            self._save_position(doc)
        self.ctx.invalidate()

    def _goto_chapter(self, spine_index):
        doc = self.route.get('_doc')
        if doc is None:
            return
        doc.goto(spine_index, 0)
        self._save_position(doc)
        self.ctx.invalidate()

    def _step_font(self, delta):
        """Move to the next size in ``FONT_STEPS`` past the current one.

        Past, not "index + delta": the stored value is a number and may be
        one nobody stepped to, so stepping up from a typed 22 has to land on
        24 and down on 21. An index cannot do that without first rewriting
        the user's number.
        """
        current = self.font_size()
        if delta > 0:
            larger = [s for s in FONT_STEPS if s > current]
            size = larger[0] if larger else min(current + 2, MAX_FONT)
        else:
            smaller = [s for s in FONT_STEPS if s < current]
            size = smaller[-1] if smaller else max(current - 2, MIN_FONT)
        self._write('reader_font_size', max(MIN_FONT, min(size, MAX_FONT)))
        doc = self.route.get('_doc')
        if doc is not None:
            doc.set_style(self._reader_style())
        self.ctx.invalidate()

    def _cycle_palette(self):
        current = self.palette_name()
        index = (PALETTE_ORDER.index(current) + 1) % len(PALETTE_ORDER)
        self._write('reader_theme', PALETTE_ORDER[index])
        self.ctx.invalidate()

    def _write(self, key, value):
        """Persist a reader preference.

        Through ``config.set_setting`` rather than by assigning to
        ``settings``, so the control on screen and the row in Settings are
        one value seen twice (``docs/readers.md`` §4.7).
        """
        from .. import config
        try:
            config.set_setting(key, value)
        except Exception:
            log.warning('could not save %s', key, exc_info=True)

    def _open_menu(self, x, y):
        """Right-click on the page: offer to copy what is under it.

        **This is what stands in for selecting text** — the page is a
        single bitmap, so a pointer can name the paragraph it landed in and
        nothing finer. See ``docs/readers.md`` §4.9.

        The paragraph is resolved **here, at the click**, not when the menu
        is drawn: the point is the only thing that knows which paragraph is
        meant, and by the time an entry is chosen the pointer has moved to
        the menu.
        """
        doc = self.route.get('_doc')
        if doc is None:
            return
        text = None
        try:
            top = self._column_y(y)
            if top is not None:
                text = doc.paragraph_at(top)
        except Exception:
            log.debug('could not locate the paragraph', exc_info=True)
        self.route['_menu'] = {'x': x, 'y': y, 'para': text}
        self.ctx.invalidate()

    def _column_y(self, y):
        """Window y -> physical pixels from the top of the text column.

        Three coordinate spaces meet here and getting any of them wrong
        picks a paragraph a few lines off. The event is in **logical**
        window pixels; the bitmap's rect (from the last drawn frame) is in
        the same space; layout happened in **physical** pixels, inside a
        column inset by the style's margins. So: subtract the bitmap's
        origin, scale, then subtract the top margin.
        """
        from ...mpvtk.scaling import px
        rect = self.ctx.art.node_rect(self.PAGE_ID)
        if not rect:
            return None
        doc = self.route.get('_doc')
        style = doc.style if doc is not None else None
        if style is None:
            return None
        return px(y - rect['y']) - style.margin_y

    def _menu_node(self):
        menu = self.route.get('_menu')
        if not menu:
            return None
        labels, actions = ([], [])
        if menu.get('para'):
            labels.append(_('Copy Paragraph'))
            actions.append('para')
        labels.append(_('Copy Page'))
        actions.append('page')
        return Menu('rd-menu', labels, menu['x'], menu['y'], size='normal', icons=['content_copy'] * len(labels), on_select=lambda i, _v=None: self._menu_pick(actions, i), on_dismiss=self._close_menu)

    def _close_menu(self):
        self.route.pop('_menu', None)
        self.ctx.invalidate()

    def _menu_pick(self, actions, index):
        menu = self.route.get('_menu') or {}
        action = actions[index] if 0 <= index < len(actions) else None
        self._close_menu()
        doc = self.route.get('_doc')
        if doc is None or action is None:
            return
        if action == 'para':
            self._copy(menu.get('para'), _('Copied the paragraph.'))
        else:
            self._copy(doc.page_text(), _('Copied the page.'))

    def _copy(self, text, done_message):
        """Put ``text`` on the clipboard, off the loop thread.

        A clipboard helper is a subprocess (``jellyfin_mpv_shim.clipboard``)
        and on a wedged one the timeout would freeze the window mid-page.
        The file fallback is reported rather than hidden, because a box with
        no clipboard at all should still hand the user something they can
        send on — the same bargain the log copier makes.
        """
        if not text:
            self.ctx.status(_('There is nothing to copy.'))
            return
        copier = getattr(self.ctx.player, 'copy_text', None)
        if copier is None:
            self.ctx.status(_('Copying is not available.'))
            return

        def work():
            return copier(text)

        def done(result):
            ok, _method, path = result
            if not ok:
                self.ctx.status(_('Could not copy'))
            elif path:
                self.ctx.status(_('No clipboard available — saved to %s') % path)
            else:
                self.ctx.status(done_message)

        def failed(_exc):
            self.ctx.status(_('Could not copy'))
        self.ctx.run.run(work, done, self.ctx.run.epoch, on_error=failed)

    def _chapter_picker(self, doc):
        """The table of contents, as the bar's one wide control.

        A Dropdown rather than a dialog: the toolkit's popup already
        scrolls, clamps to the screen and walks with the arrow keys, and a
        book's TOC is exactly the list that control is for. Books with no
        TOC at all are common enough (a plain-text conversion has none), so
        the control is left out rather than shown empty.
        """
        chapters = doc.chapters()
        if not chapters:
            return None
        labels = ['   ' * min(c.level, 3) + c.title for c in chapters]
        targets = [c.spine_index for c in chapters]
        selected = 0
        for i, target in enumerate(targets):
            if target <= doc.spine_index:
                selected = i
        return Dropdown('rd-toc', labels, selected=selected, size='caption', force=True, trigger_icon='menu_book', tip=_('Contents'), trigger_chip=(theme.BUTTON_BG, theme.BUTTON_ACTIVE, theme.TEXT_FG), w=34, h=34, popup_w=420, on_select=lambda i, _v=None: self._goto_chapter(targets[i]))

    def render(self, size):
        route = self.route
        width, height = size
        if route.get('_error'):
            return chrome.error(route['_error'])
        data = route.get('_data')
        if data is None:
            return chrome.busy()
        if route.get('_doc') is None and (not route.get('_opening')) and (not route.get('_error')):
            path = (data.get('state') or (None, None))[1]
            if path:
                self._open_book(path)
        item = data.get('item') or {}
        doc = route.get('_doc')
        body = self._page_node(doc, width, self._area_height(height)) if doc is not None else self._waiting(data)
        children = [self._top_bar(item, doc), body, self._bottom_bar(doc, width)]
        menu = self._menu_node()
        if menu is not None:
            children.append(menu)
        return Column(children, flex=1, align='stretch')
    AREA_ID = 'rd-area'
    PAGE_ID = 'rd-page'

    def _area_height(self, window_height):
        """How tall to rasterize, from the last frame's measured hole.

        An `Image` cannot flex — the renderer never resamples one — so the
        size has to be known before the node is built, and the only thing
        that knows it is the layout that has already run. First frame falls
        back to the whole window less the bars, which is right whenever
        nothing else is on screen and one frame stale when something is.
        """
        rect = None
        try:
            rect = self.ctx.art.node_rect(self.AREA_ID)
        except Exception:
            log.debug('node_rect unavailable', exc_info=True)
        if rect and rect.get('h'):
            return int(rect['h'])
        return max(80, window_height - TOP_BAR_H - BOTTOM_BAR_H)

    def _waiting(self, data):
        status, _path = data.get('state') or (None, None)
        if status in ('pending', 'downloading') or self.route.get('_opening'):
            message = _('Getting the book…')
        elif status == 'error':
            message = _('The download failed.')
        else:
            message = _('Getting the book…')
        return Box([Spacer(), Text(message, size='large', color=theme.SUBTLE_FG, align='center'), Spacer()], id=self.AREA_ID, flex=1, align='center', bg='000000', alpha=0)

    def _sync_style(self, doc):
        """Adopt the settings if they have moved since the last frame.

        The A−/A+ buttons already re-style the document themselves, so this
        is for the other way in: Settings from the tray, and a route sat in
        the history with a document built at the old size. Asked every frame
        and answered by comparing keys, which is a tuple compare;
        ``set_style`` is what costs, and it only runs when that changed.
        """
        style = self._reader_style()
        if doc.style.key() != style.key():
            doc.set_style(style)
            self.route.pop('_entry', None)

    def _page_node(self, doc, width, height):
        """The page bitmap, with the two halves that turn it."""
        from ...mpvtk.scaling import raster
        self._sync_style(doc)
        page_h = max(80, height)
        page_w = max(80, width - 2 * PAGE_PAD)
        entry = self._bitmap(doc, raster(page_w, page_h), (page_w, page_h), self.palette_name())
        if entry is None:
            return Box([Spacer(), chrome.busy(), Spacer()], id=self.AREA_ID, flex=1, bg='000000', alpha=0)
        lw, lh = (entry['lw'], entry['lh'])
        half = lw // 2
        regions = [{'id': 'rd-back-half', 'x': 0, 'y': 0, 'w': half, 'h': lh, 'zone': True, 'on_click': lambda: self._turn(-1), 'on_context': self._open_menu}, {'id': 'rd-fwd-half', 'x': half, 'y': 0, 'w': lw - half, 'h': lh, 'zone': True, 'on_click': lambda: self._turn(1), 'on_context': self._open_menu}]
        return Box([ImageMap(entry['src'], entry['iw'], entry['ih'], id=self.PAGE_ID, regions=regions, w=lw, h=lh, v=entry.get('v', 0))], direction='row', id=self.AREA_ID, flex=1, justify='center', align='center', bg='000000', alpha=0)

    def _bitmap(self, doc, physical, logical, palette_name):
        """The current page's bitmap entry, composited on the pool.

        Keyed by everything that changes the pixels: which page, at which
        layout, in which palette. A key that is already in the strip store
        costs nothing, which is what makes this safe to ask for every frame.
        """
        from ...epub import paint
        route = self.route
        store = self.ctx.art.strips
        if store is None:
            return None
        style = doc.style
        col_w, col_x = style.column(physical[0])
        if doc.set_viewport(col_w, physical[1] - 2 * style.margin_y):
            route.pop('_entry', None)
        key = self._page_key(doc.page_key(), physical, palette_name)
        entry = self._held_entry(route, store)
        if entry is not None and route.get('_entry_key') == key:
            return entry
        if route.get('_busy_key') == key:
            return entry
        route['_busy_key'] = key
        colors = paint.palette(palette_name)

        def work():
            drawn, image = doc.render_keyed(physical, colors, (col_x, style.margin_y))
            drawn_key = self._page_key(drawn, physical, palette_name)
            return (drawn_key, store.bitmap(drawn_key, image, lsize=logical))

        def done(result):
            drawn_key, entry = result
            route['_entry'] = entry
            route['_entry_key'] = drawn_key
            self.ctx.invalidate()

        def failed(exc):
            log.warning('could not draw the page', exc_info=exc)
            route['_error'] = _('This book could not be drawn.')
            self.ctx.invalidate()

        def settle():
            if route.get('_busy_key') == key:
                route['_busy_key'] = None
        self.ctx.run.run(work, done, self.ctx.run.epoch, on_error=failed, always=settle)
        return route.get('_entry')

    @staticmethod
    def _held_entry(route, store):
        """The parked page bitmap, or None once the store has freed it.

        The route dict outlives the store's hold on its bitmap -- the back and
        forward stacks keep it, and ``clear()`` on an mpv restart frees every
        buffer -- so a parked entry is only drawable while ``keep`` says so.
        """
        entry = route.get('_entry')
        if entry is not None and (not store.keep(entry)):
            route.pop('_entry', None)
            route.pop('_entry_key', None)
            return None
        return entry

    @staticmethod
    def _page_key(page_key, physical, palette_name):
        """The strip-store key for a page identity.

        Takes the document's ``page_key()`` VALUE rather than the document,
        so the caller decides when it was read — the worker reads it under
        the document's lock together with the pixels (``render_keyed``),
        and the loop thread reads it to ask whether the cache already has
        the frame it wants to draw.
        """
        raw = '%r|%dx%d|%s' % (page_key, physical[0], physical[1], palette_name)
        return 'epub-' + hashlib.sha1(raw.encode('utf-8')).hexdigest()

    def _top_bar(self, item, doc):
        title = (doc.chapter_title() if doc is not None else '') or (item.get('Name') or self.route.get('title', ''))
        fraction = doc.fraction() if doc is not None else None
        right = []
        if fraction is not None:
            right.append(Text('%.0f%%' % (fraction * 100), size='caption', color=theme.SUBTLE_FG))
        return Row([Button('', id='rd-back', icon='arrow_back', w=34, h=34, pad=0, justify='center', tip=_('Back'), on_click=self.ctx.nav.go_back), Text(title, size='small', color=theme.TEXT_FG, flex=1)] + right, h=TOP_BAR_H, pad=(10, 4), gap=10, align='center', bg=theme.PANEL_BG)

    def _bottom_bar(self, doc, width):
        pages = ''
        if doc is not None:
            pages = _('Page %(page)d of %(total)d') % {'page': doc.page_number + 1, 'total': doc.page_count()}
        narrow = width < 760

        def icon_btn(icon, node_id, cb, tip):
            return Button('', id=node_id, icon=icon, w=34, h=34, pad=0, justify='center', tip=tip, on_click=cb)

        def text_btn(label, node_id, cb, tip, size=17):
            return Button(label, id=node_id, w=36, h=34, pad=0, size=size, justify='center', tip=tip, on_click=cb)
        children = [icon_btn('chevron_left', 'rd-prev', lambda: self._turn(-1), _('Previous page')), icon_btn('chevron_right', 'rd-next', lambda: self._turn(1), _('Next page'))]
        if not narrow and pages:
            children.append(Text(pages, size='caption', color=theme.SUBTLE_FG))
        children.append(Spacer())
        if doc is not None:
            picker = self._chapter_picker(doc)
            if picker is not None:
                children.append(picker)
        children += [text_btn('A−', 'rd-smaller', lambda: self._step_font(-1), _('Smaller text'), size=15), text_btn('A+', 'rd-bigger', lambda: self._step_font(1), _('Larger text'), size=19), text_btn(self._palette_label(), 'rd-theme', self._cycle_palette, _('Page colour'), size=14)]
        return Row(children, h=BOTTOM_BAR_H, pad=(10, 6), gap=6, align='center', bg=theme.PANEL_BG)

    def _palette_label(self):
        name = self.palette_name()
        return {'light': _('Light'), 'sepia': _('Sepia'), 'dark': _('Dark')}.get(name, name)

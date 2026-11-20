"""``Paginator`` — paging a result set too large to hold.

Four routes (grid, person, music, music_genre) page their contents, in three
modes that share this machinery: **windowed** (``window``), a full-length
list of holes filled as they scroll into view; **infinite scroll** (``more``),
appending near the bottom; and **paginated** (``ensure``/``go``/``jump``), one
screenful at a time with a bar instead of a scrollbar.

Page state lives on the *route dict* rather than here, and ``content_h`` is a
callback rather than a method. Why each mode exists and why those two are
shaped that way: see docs/browser-shell.md section 13.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from ..i18n import _
log = logging.getLogger('mpvtk_browser.pagination')
PAGINATION_BAR_H = 48
PAGE_MAX = 60
PAGE_SLOP = 800
WINDOW_PAGE = 100

def spread(items, total, new, start):
    """``items`` widened to ``total`` slots, with ``new`` placed at ``start``.

    An item's index **is** its position in the library, which is what lets the
    grid be the right height and the scrollbar the right length before
    anything has been fetched; everything downstream reads a hole as "not here
    yet". A falsy ``total`` makes this a plain splice instead. See
    docs/browser-shell.md section 13.
    """
    out = list(items or ())
    new = list(new or ())
    if total:
        if start >= total:
            new = []
        elif start + len(new) > total:
            del new[total - start:]
    if start + len(new) > len(out):
        out.extend([None] * (start + len(new) - len(out)))
    for i, it in enumerate(new):
        out[start + i] = it
    if total:
        if len(out) < total:
            out.extend([None] * (total - len(out)))
        elif len(out) > total:
            del out[total:]
    return out

def enabled_from_settings():
    """The global paginate-tile-grids toggle (``settings.paginated``).

    Read live so the Settings toggle takes effect on the next frame; a
    missing key (test stand-ins) reads as off.
    """
    try:
        from ..conf import settings
        return bool(getattr(settings, 'paginated', False))
    except Exception:
        return False

class Paginator:
    """Infinite-scroll and fixed-page paging over a route's result set."""

    def __init__(self, run, content_h, is_current, status, invalidate, enabled, cols, set_enabled=None, forget=None):
        self.run = run
        self._content_h = content_h
        self._is_current = is_current
        self._status = status
        self._invalidate = invalidate
        self.enabled = enabled
        self._cols = cols
        self._set_enabled = set_enabled or (lambda _v: None)
        self._forget = forget or (lambda *_ids: None)

    def toggle(self, route, *scroll_ids):
        """The inline Paginated checkbox: flip and persist the GLOBAL setting.

        No reload -- the data is unchanged, only how it is presented -- but
        reset the page state so turning it on lands on page 1.

        ``scroll_ids`` are the scroll containers this view has when it is
        NOT paginated; they are torn down and rebuilt across the flip and
        must not carry an offset over. ``ScrollState`` gets this right on its
        own on mpv >= 0.36 -- this is what covers the older builds with no
        ``user-data`` to answer with, where a stale remembered offset
        virtualizes the returning grid into blank spacers.
        """
        self._set_enabled(not self.enabled())
        self.reset(route)
        if scroll_ids:
            self._forget(*scroll_ids)
        self._invalidate()

    def more(self, route, offset, maximum, get, put, fetch, error=None):
        """One page of an infinite-scroll list.

        ``get(route)`` returns ``(items, total)``, ``put(route, items, total)``
        writes them back, and ``fetch(start_index)`` asks the source for the
        next page as ``(new_items, total)``.

        Five invariants ride on the guards below: only page the route that is
        on screen, never from an empty list, never against a list being
        replaced (``_refreshing``), an in-range empty page ends the list, and
        ``_loading`` must not survive a failure. Each, and the bug behind it:
        see docs/browser-shell.md section 13.
        """
        if not self._is_current(route) or route.get('_loading') or route.get('_refreshing'):
            return
        items, total = get(route)
        if not items or len(items) >= total:
            return
        if maximum - offset >= PAGE_SLOP:
            return
        route['_loading'] = True
        ep = self.run.epoch
        start = len(items)

        def done(res):
            new, total2 = res
            cur, _t = get(route)
            merged = list(cur) + list(new)
            put(route, merged, total2 if new else len(merged))

        def failed(_exc):
            if self._is_current(route):
                self._status(error or _('Could not load more items.'))

        def clear_guard():
            route['_loading'] = False
        self.run.run(lambda: fetch(start), done, ep, on_error=failed, always=clear_guard)

    def window(self, route, first, last, get, put, fetch, error=None):
        """Fetch whatever of items ``[first, last)`` is not loaded yet.

        ``get``/``put``/``fetch`` are ``more``'s, except that ``fetch`` takes
        ``(start, limit)`` -- an arbitrary offset, not just the end of the
        list -- and what comes back is spliced in at ``start`` rather than
        appended.

        Called from **render**, like ``ensure``, so the range passed must be
        the range the view actually composites. That is also why a failed
        page must not retry itself: ``_win_tried`` remembers the attempt and
        ``rewindow`` clears it on a scroll, while ``_win_load`` is a separate
        in-flight set. See docs/browser-shell.md section 13.
        """
        if not self._is_current(route):
            return
        items, total = get(route)
        if not total:
            return
        first = max(0, min(int(first), total - 1))
        last = max(first + 1, min(int(last), total))
        tried = route.setdefault('_win_tried', set())
        load = route.setdefault('_win_load', set())
        for page in range(first // WINDOW_PAGE, (last - 1) // WINDOW_PAGE + 1):
            if page in tried or page in load:
                continue
            start = page * WINDOW_PAGE
            stop = min(total, start + WINDOW_PAGE)
            if all((i < len(items) and items[i] is not None for i in range(start, stop))):
                continue
            tried.add(page)
            load.add(page)
            self._window_fetch(route, page, get, put, fetch, error)

    def _window_fetch(self, route, page, get, put, fetch, error):
        ep = self.run.epoch
        start = page * WINDOW_PAGE

        def done(res):
            new, total2 = res
            cur, cur_total = get(route)
            total3 = total2 or cur_total
            put(route, spread(cur, total3, new, start), total3)

        def failed(_exc):
            if self._is_current(route):
                self._status(error or _('Could not load more items.'))

        def clear():
            route.setdefault('_win_load', set()).discard(page)
        self.run.run(lambda: fetch(start, WINDOW_PAGE), done, ep, on_error=failed, always=clear)

    @staticmethod
    def rewindow(route):
        """Forget which windows have been *asked for* (not which are in
        flight).

        Called on a scroll, so a window that failed is retried when the user
        moves; and wherever the result set is replaced (a sort, a filter, a
        reload), because page 3's items are not page 3's items any more.
        """
        route.pop('_win_tried', None)

    def page_size(self, route, size, head_h, geom, pad):
        """Tiles per page = columns × rows that fit under the header. Rounds
        the row count DOWN so a page never overflows its slot (which would
        clip the last row or force a scroll); capped at PAGE_MAX for the
        overlay budget."""
        from .tile_renderer import GRID_GAP
        avail = self._content_h(route, size) - head_h - pad
        pitch = geom.strip_h + GRID_GAP
        rows = max(1, int((avail + GRID_GAP) // pitch)) if pitch > 0 else 1
        cols = self._cols(size[0], geom)
        return max(1, min(cols * rows, PAGE_MAX))

    @staticmethod
    def page_count(route, ps):
        total = route.get('_total')
        if not total or ps <= 0:
            return None
        return max(1, -(-total // ps))

    def ensure(self, route, ps, fetch, seed=None):
        """Make the current page's items available at page size ``ps`` and
        return them (or None while a fetch is in flight).

        ``fetch(start, limit) -> (items, total)`` gets a page from the source;
        ``seed`` is an already-loaded head of the list (the initial-load chunk)
        used to fill page 0 without a second request."""
        if route.get('_page_size') != ps:
            route['_page_size'] = ps
            route['_pages'] = {}
            route['_page_loading'] = set()
            route['_page_tried'] = set()
        pages = route['_pages']
        npages = self.page_count(route, ps)
        cur = route.get('_page') or 0
        if npages is not None:
            cur = max(0, min(cur, npages - 1))
        route['_page'] = cur
        route['_npages'] = npages
        if seed and cur == 0 and (0 not in pages) and (len(seed) >= ps):
            pages[0] = list(seed[:ps])
        self._fetch(route, cur, ps, fetch)
        for nb in (cur + 1, cur - 1):
            if nb >= 0 and (npages is None or nb < npages):
                self._fetch(route, nb, ps, fetch, prefetch=True)
        keep = {cur - 1, cur, cur + 1}
        for p in [p for p in pages if p not in keep]:
            pages.pop(p, None)
        return pages.get(cur)

    def _fetch(self, route, page, ps, fetch, prefetch=False):
        pages = route['_pages']
        loading = route['_page_loading']
        if page in pages or page in loading or page in route.get('_page_tried', ()):
            return
        loading.add(page)
        ep = self.run.epoch
        start = page * ps

        def done(res):
            if route.get('_page_size') != ps:
                return
            items, total = res
            route.setdefault('_pages', {})[page] = list(items)
            if total:
                route['_total'] = total

        def failed(_exc):
            route.setdefault('_page_tried', set()).add(page)
            if not prefetch and self._is_current(route):
                self._status(_('Could not load this page.'))

        def clear():
            route.setdefault('_page_loading', set()).discard(page)
        self.run.run(lambda: fetch(start, ps), done, ep, on_error=failed, always=clear)

    @staticmethod
    def reset(route):
        """Drop the page cache and return to page 1. Called whenever the
        underlying result set changes (sort, filter, collections toggle, music
        tab) — page 3 of one ordering is nothing like page 3 of another."""
        for k in ('_pages', '_page_size', '_page_loading', '_page_tried', '_npages', '_win_tried', '_win_load'):
            route.pop(k, None)
        route['_page'] = 0

    def go(self, route, page):
        """Jump to a page (0-based); ``ensure`` clamps into range next frame,
        so an out-of-range target from Last/typing is harmless.

        Forgets failed attempts, exactly as ``rewindow`` does on a scroll: a
        deliberate move is the user asking again, and without this a page that
        failed once is dead for the rest of the session.
        """
        route['_page'] = page
        route.pop('_page_tried', None)
        self._invalidate()

    def jump(self, route, text):
        """The page-number box: a 1-based page to go to."""
        try:
            n = int(str(text).strip())
        except (TypeError, ValueError):
            return
        self.go(route, max(0, n - 1))

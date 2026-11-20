"""``Page`` and ``PageContext`` — one class per screen.

A page owns its own state and is handed what it needs: ``load(epoch)`` on
navigation, ``render(size)`` on the loop thread, both against a
``PageContext`` of ten names rather than the 413 distinct ``self.*`` the
mixins can reach.

**``ctx.shell`` is an escape hatch, and what makes it one rather than a
loophole is that it is counted** — ``tests/test_page_contract.py`` pins the
number of references and it can only go down. The page budget is zero; the
one use left is ``route_async`` below, which is pinned by ``BASE_SHELL_USES``
rather than budgeted because it is not transitional.

Adding a view means adding a page, and converting an unconverted route is a
four-step recipe with the app shippable throughout. Both, and why the hatch
and the budget are shaped this way: see docs/browser-shell.md section 1.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional
log = logging.getLogger('mpvtk_browser.pages.base')

@dataclass(frozen=True)
class PageContext:
    """Everything a page is allowed to depend on.

    Ten names instead of the 413 distinct ``self.*`` the mixins can reach.
    Small enough to fake, which is the point: a page test constructs the page
    and nothing else. Frozen because a page that rewired its own dependencies
    would be invisible to everything that reasons about navigation.
    """
    source: Any
    server: Optional[str]
    nav: Any
    run: Any
    art: Any
    player: Any
    actions: Any
    dialogs: Any
    status: Callable[[str], None]
    invalidate: Callable[[], None]
    shell: Any = None

class Page:
    """One screen.

    ``load()`` runs on navigation and may go off-thread via ``ctx.run``;
    ``render(size)`` runs on the loop thread and returns a widget tree for the
    **content area only** — the shell owns chrome, dialogs and the
    now-playing bar.
    """
    kind: str = ''

    def __init__(self, ctx: PageContext, route: dict):
        self.ctx = ctx
        self.route = route

    def load(self, epoch: int) -> None:
        """Fetch what the screen needs. Called once per navigation, on the
        loop thread; do the actual work through ``self.ctx.run``.

        ``epoch`` is read by the shell on the loop thread and handed down —
        a page that read it later would be racing the navigation it guards.
        """

    def render(self, size) -> Any:
        """Build the content-area widget tree. Loop thread."""
        raise NotImplementedError

    def close(self) -> None:
        """This page has stopped being the screen. Loop thread.

        Called once, when the shell renders a *different* page -- not on
        navigation, which happens on threads the render loop does not own and
        this may touch the player. Almost no page needs it, and it may be
        followed by another ``load()``, so it must leave the page usable
        rather than spent. Which pages need it: see docs/browser-shell.md
        section 1.
        """

    def parked_scroll(self, scroll_id):
        """Where ``scroll_id`` was when this route was last navigated away
        from, for passing to ``VScroll(offset=...)``. None on a first visit.

        The shell parks the offsets on the route dict, which is what makes
        them survive the ``ScrollState.reset()`` that stops one view's
        offsets bleeding into the next; see docs/browser-shell.md section 7.

        **A restore is a one-shot**, which is why this goes through
        ``ScrollState.pending`` rather than reading the route directly: the
        parked value is not a standing order. ``offset=`` only bites the
        first frame an id is seen (widgets.Scroll), and an in-place reload
        makes an id un-seen — a sort or filter change drops ``_items``, the
        page draws a spinner where the scroller was, and the container that
        comes back with the results is new to the renderer. Reading the
        route re-commanded it to wherever the user had been on the *previous*
        visit, so every filter tick jumped the library back down.
        """
        return self.ctx.art.scroll.pending(scroll_id)

    def open_link(self, url):
        """Hand an external link to the desktop's browser.

        On ``Page`` rather than on the one screen that first wanted it,
        because three screens do (detail, series, season) and a copy per
        screen is how the error handling ends up different on each.

        Reports a failure rather than swallowing it: pressing a button and
        having nothing at all happen is indistinguishable from a dead UI,
        and that is the outcome on a box with no desktop opener -- or for a
        scheme ``system_open`` refuses, which these links can carry because
        the *server* composed them. The gateway answers ``(ok, method)`` for
        exactly this.
        """
        from ...i18n import _
        try:
            ok, _method = self.ctx.player.open_url(url)
        except Exception:
            log.warning('could not open an external link', exc_info=True)
            ok = False
        if not ok:
            self.ctx.status(_('Could not open that link.'))

    def web_url(self, item):
        """This item's page in jellyfin-web, or None (#714).

        Beside :meth:`open_link` and for the same reason: the three screens
        that draw the provider row all want it, and the composition —
        which server, and what web calls the route — is one answer, not
        three. ``None`` offline, where there is no server to send anyone to.
        """
        from ..components import detail
        source = self.ctx.source
        server = self.route.get('server') or self.ctx.server
        return detail.jellyfin_web_url(source.server_address(server), item)

    def route_async(self, work, on_done, epoch) -> None:
        """``ctx.run`` for this page's data, recording a failure on the route
        so the view can offer a retry instead of spinning forever."""
        self.ctx.shell._route_async(self.route, work, on_done, epoch)

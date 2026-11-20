"""The season screen: a season picker, the shared actions, an episode grid."""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
from ...i18n import _
from ...mpvtk.widgets import Column, Dropdown, Text, VScroll
from ..components import chrome, cinema_detail, controls, detail as detail_components
from ..tile_renderer import GRID_GAP
from .base import Page
log = logging.getLogger('mpvtk_browser.pages.season')

def _grouped(buttons, avail):
    """The header's buttons as ONE item for the title row, spaced at 8.
    ``[]`` for nothing, so a caller can add it unconditionally.

    Through ``wrap_row`` rather than a bare ``Row``: this group can be To
    Series plus a button per metadata database, which at a narrow window is
    wider than the page. ``wrap_row`` hands back a plain Row when it all
    fits -- the common case, and the same tree a bare Row would have built
    -- and a Column of rows when it does not, so the gap inside the group
    stays 8 at every width instead of degrading to the outer row's 12.

    Passed the FULL content width rather than what is left beside the
    season picker: the outer ``wrap_row`` will drop this whole item onto
    its own line if it does not fit next to the picker, and sizing the
    inner break to the remainder would wrap it early on the line it ends
    up having to itself.

    Module-level and tiny because it is a layout decision with one input and
    one output; on the page it would read as state.
    """
    from ..components import chrome
    if not buttons:
        return []
    return [chrome.wrap_row(buttons, avail, gap=8, align='center')]

class SeasonPage(Page):
    kind = 'season'

    def close(self):
        cinema_detail.release(self)

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        actions = self.ctx.actions
        srv = route.get('server') or self.ctx.server

        def work():
            series_id = route.get('series_id')
            next_up = None
            if series_id:
                try:
                    next_up = source.get_next_up(srv, series_id)
                except Exception:
                    log.debug('next up lookup failed', exc_info=True)
            seasons = source.get_seasons(srv, series_id)
            item = next((s for s in seasons if s.get('Id') == route['item_id']), None)
            return {'episodes': source.get_episodes(srv, series_id, route['item_id']), 'seasons': seasons, 'next_up': next_up, 'may_refresh': actions.can_refresh_metadata(srv), 'trailers': detail_components.local_trailers(source, srv, item), 'extras': detail_components.special_features(source, srv, item)}
        self.route_async(work, lambda d: route.__setitem__('_data', d), epoch)

    def render(self, size):
        art = self.ctx.art
        tiles = art.tiles
        actions = self.ctx.actions
        route = self.route
        data = route.get('_data')
        if data is None:
            return chrome.busy()
        episodes = data.get('episodes') or []
        seasons = data.get('seasons') or []
        server = route.get('server') or self.ctx.server
        geom = art.geom_wide
        season_item: dict = next((s for s in seasons if s.get('Id') == route['item_id']), {})
        title = season_item.get('Name') or route.get('title', '')
        full_bleed = True
        gpad, geom = tiles.grid_layout(size[0], geom)
        banner = cinema_detail.render_header(self, size, season_item, 'season', title=title, context=season_item.get('SeriesName') or route.get('bar_title') or None)
        title_row: list = []
        if banner is None:
            title_row.append(Text(title, size='page', bold=True))
        if len(seasons) > 1:
            names = [s.get('Name', '') for s in seasons]
            cur = next((i for i, s in enumerate(seasons) if s.get('Id') == route['item_id']), 0)
            title_row.append(Dropdown('season-switch', names, selected=cur, w=220, on_select=lambda i, v: self._switch_season(seasons[i])))
        row_buttons: list = []
        if route.get('series_id'):
            row_buttons.append(controls.action_btn('movie', _('To Series'), 'season-to-series', lambda: self.ctx.nav.navigate({'kind': 'series', 'server': server, 'item_id': route['series_id'], 'title': season_item.get('SeriesName') or route.get('bar_title') or ''})))
        acts = []
        if route.get('series_id'):
            next_up = data.get('next_up') or {}
            if next_up.get('Id') and any((e.get('Id') == next_up['Id'] for e in episodes)):
                acts.append(controls.action_btn('play_arrow', _('Next Up'), 'se-nextup', lambda: actions.play_next_up(route['series_id'], server), primary=True))
            else:
                acts.append(controls.action_btn('play_arrow', _('Play'), 'se-play', lambda: actions.play_season(route['series_id'], route['item_id'], server), primary=True))
            acts.append(controls.action_btn('shuffle', _('Shuffle'), 'se-shuffle', lambda: actions.shuffle_season(route['series_id'], route['item_id'], server)))
        trailer = detail_components.trailer_button(actions, season_item, data.get('trailers'), server, 'se-trailer', self.open_link)
        if trailer is not None:
            acts.append(trailer)
        acts += detail_components.common_actions(actions, tiles, season_item or {'Id': route['item_id'], 'Type': 'Season'}, server, 'se')
        more = detail_components.more_entries(actions, season_item, server, data.get('may_refresh'))
        more_btn = detail_components.more_button(route, self.ctx.invalidate, more, 'se-more')
        if more_btn is not None:
            acts.append(more_btn)
        row_buttons += detail_components.provider_link_buttons(season_item, self.open_link, web_url=self.web_url(season_item or {'Id': route['item_id']}))
        title_row += _grouped(row_buttons, size[0] - 2 * gpad)
        header = []
        if title_row:
            header.append(chrome.wrap_row(title_row, size[0] - 2 * gpad, gap=12, align='center'))
        header.append(cinema_detail.action_surface(chrome.wrap_row(acts, size[0] - 2 * gpad, gap=8, align='center'), size[0] - 2 * gpad))
        text = chrome.overview(season_item)
        if text:
            header.append(chrome.paragraph(text, 18, chrome.body_width(size[0], gpad)))
        head_h = tiles.header_offset(header if banner is None else [banner] + header)
        if full_bleed:
            head_h -= chrome.CONTENT_PAD
        rows = header + tiles.grid_of(episodes, 'ep', size, geom=geom, image_type='Thumb', inherit=False, scroll_id='season', head_h=head_h)
        extras = detail_components.special_features_row(tiles, data.get('extras'), 'season-extras')
        if extras is not None:
            rows.append(extras)
        if banner is not None:
            body = chrome.header_body(banner, rows, gap=GRID_GAP, pad=(gpad, chrome.CONTENT_PAD), full_bleed=full_bleed)
        elif extras is not None:
            body = chrome.padded_body(rows, pad=(gpad, chrome.CONTENT_PAD), gap=GRID_GAP)
        else:
            body = Column(rows, gap=GRID_GAP, pad=(gpad, chrome.CONTENT_PAD))
        children = [VScroll(body, id='season', flex=1, cinema_pull=bool(route.get('_cinema_immersive')), offset=self.parked_scroll('season'), on_scroll=lambda off, mx: cinema_detail.scrolled(self, 'season', off, mx))]
        menu = detail_components.more_menu(route, self.ctx.invalidate, art, more, 'se-more', 'season')
        if menu is not None:
            children.append(menu)
        return cinema_detail.page_surface(self, children)

    def _switch_season(self, season):
        self.ctx.nav.navigate({'kind': 'season', 'server': self.route.get('server') or self.ctx.server, 'item_id': season.get('Id'), 'series_id': self.route.get('series_id'), 'title': season.get('Name', ''), 'bar_title': season.get('SeriesName') or self.route.get('bar_title')})

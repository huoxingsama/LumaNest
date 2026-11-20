"""The series screen: banner, actions, season carousel, cast.

``_series_actions`` was private to this route and comes with it. The Watched
/ Favorite / Download trio does not — it is shared with detail, season and
playlist, so it lives in ``components/detail.py``.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from ...i18n import _
from ...mpvtk.widgets import Column, Text, VScroll
from .. import theme
from ..components import chrome, cinema_detail, controls, detail as detail_components
from .base import Page

class SeriesPage(Page):
    kind = 'series'

    def close(self):
        cinema_detail.release(self)

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        actions = self.ctx.actions
        srv = route.get('server') or self.ctx.server
        iid = route['item_id']

        def work():
            similar: list = []
            try:
                similar = source.get_similar(srv, iid) or []
            except Exception:
                pass
            item = source.get_item(srv, iid)
            return {'item': item, 'seasons': source.get_seasons(srv, iid), 'similar': similar, 'may_refresh': actions.can_refresh_metadata(srv), 'trailers': detail_components.local_trailers(source, srv, item), 'extras': detail_components.special_features(source, srv, item)}
        self.route_async(work, lambda d: route.__setitem__('_data', d), epoch)

    def render(self, size):
        tiles = self.ctx.art.tiles
        route = self.route
        data = route.get('_data')
        if data is None:
            return chrome.busy()
        item = data.get('item') or {}
        w = size[0]
        full_bleed = True
        server = route.get('server') or self.ctx.server
        banner = cinema_detail.render_header(self, size, item, 'series')
        blocks = [banner]
        more = detail_components.more_entries(self.ctx.actions, item, server, data.get('may_refresh'))
        blocks.append(cinema_detail.action_surface(self._series_actions(item, server, route['item_id'], trailers=data.get('trailers'), more=more, avail=tiles.body_w(w)), tiles.body_w(w)))
        text = chrome.overview(item)
        if text:
            blocks.append(chrome.paragraph(text, 18, tiles.body_w(w)))
        links = detail_components.provider_links(item, tiles.body_w(w), self.open_link, web_url=self.web_url(item))
        if links is not None:
            blocks.append(links)
        seasons = data.get('seasons') or []
        if seasons:
            blocks.append(tiles.tile_row(_('Seasons'), seasons, 'series-seasons'))
        people = detail_components.people_row(tiles, item.get('People') or [])
        if people is not None:
            blocks.append(people)
        extras = detail_components.special_features_row(tiles, data.get('extras'), 'series-extras')
        if extras is not None:
            blocks.append(extras)
        if data.get('similar'):
            sim_geom, sim_type = tiles.auto_geom(data['similar'], default=tiles.art.geom, default_type='Primary')
            blocks.append(tiles.tile_row(_('More Like This'), data['similar'], 'series-similar', geom=sim_geom, image_type=sim_type))
        children = [VScroll(chrome.header_body(blocks[0], blocks[1:], gap=16, full_bleed=full_bleed), id='series', flex=1, cinema_pull=bool(route.get('_cinema_immersive')), offset=self.parked_scroll('series'), on_scroll=lambda off, mx: cinema_detail.scrolled(self, 'series', off, mx))]
        menu = detail_components.more_menu(route, self.ctx.invalidate, self.ctx.art, more, 'sa-more', 'series')
        if menu is not None:
            children.append(menu)
        return cinema_detail.page_surface(self, children)

    def _series_actions(self, item, server, series_id, trailers=None, more=(), avail=0):
        actions = self.ctx.actions
        btns = [controls.action_btn('play_arrow', _('Next Up'), 'sa-nextup', lambda: actions.play_next_up(series_id, server), primary=True), controls.action_btn('shuffle', _('Shuffle'), 'sa-shuffle', lambda: actions.shuffle_series(series_id, server))]
        trailer = detail_components.trailer_button(actions, item, trailers, server, 'sa-trailer', self.open_link)
        if trailer is not None:
            btns.append(trailer)
        btns += detail_components.common_actions(actions, self.ctx.art.tiles, item, server, 'sa')
        more_btn = detail_components.more_button(self.route, self.ctx.invalidate, more, 'sa-more')
        if more_btn is not None:
            btns.append(more_btn)
        return chrome.wrap_row(btns, avail, gap=8, align='center')

"""A playlist: a track list when it holds music, a tile grid otherwise."""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from ...i18n import _
from ...mpvtk.widgets import Column, Row, Spacer, Text, VScroll
from .. import theme
from ..components import chrome, controls, detail as detail_components
from ..repository import PLAYLIST_SUPPORTED_TYPES
from ..tile_renderer import GRID_GAP
from .base import Page

class PlaylistPage(Page):
    kind = 'playlist'

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get('server') or self.ctx.server
        iid = route['item_id']
        self.route_async(lambda: source.get_playlist_items(srv, iid), lambda d: route.__setitem__('_data', d), epoch)

    def render(self, size):
        art = self.ctx.art
        actions = self.ctx.actions
        route = self.route
        data = route.get('_data')
        if data is None:
            return chrome.busy()
        server = route.get('server') or self.ctx.server
        pid = route['item_id']
        raw = list(data)
        items = [i for i in raw if i.get('Type') in PLAYLIST_SUPPORTED_TYPES]
        ids = [i.get('Id') for i in items]
        audio = any((i.get('Type') == 'Audio' for i in items))
        pl_item = {'Id': pid, 'Type': 'Playlist', 'Name': route.get('title', ''), 'ServerId': next((i.get('ServerId') for i in raw if i.get('ServerId')), None)}
        header = Row([Text(route.get('title', ''), size='hero', bold=True), Spacer(), controls.action_btn('play_arrow', _('Play All'), 'pl-play', lambda: actions.play_list(ids, server, 0, audio=audio, items=items), primary=True) if ids else None, controls.action_btn('shuffle', _('Shuffle'), 'pl-shuffle', lambda: actions.play_shuffle(ids, server, audio=audio)) if ids else None, detail_components.download_button(actions, art.tiles, pl_item, server, 'pl'), controls.action_btn('edit', _('Edit'), 'pl-edit', lambda: self.ctx.nav.navigate({'kind': 'playlist_edit', 'server': server, 'item_id': pid, 'title': route.get('title', '')})) if not actions.offline and actions.can_edit() else None], align='center', gap=10)
        geom = None
        pad = chrome.CONTENT_PAD
        if not items:
            body = [Text(_('This playlist is empty.') if not raw else _('This playlist has no supported media types.'), size='large', color=theme.SUBTLE_FG)]
        elif audio:
            body = [art.tiles.track_list(items, 'pl', lambda i: actions.play_list(ids, server, i, audio=True, items=items), art=True, scroll_id='playlist', head_h=70, menu=True)]
        else:
            geom, image_type = art.tiles.auto_geom(items)
            pad, geom = art.tiles.grid_layout(size[0], geom)
            body = art.tiles.grid_of(items, 'pl', size, geom=geom, image_type=image_type, inherit=False, scroll_id='playlist', head_h=70, on_click=lambda it: actions.play_list(ids, server, items.index(it), audio=False, items=items))
        return VScroll(Column([header, Spacer(h=2)] + body, pad=(pad, chrome.CONTENT_PAD), gap=GRID_GAP, align='stretch'), id='playlist', flex=1, offset=self.parked_scroll('playlist'), on_scroll=lambda off, mx: art.scroll.on_scroll('playlist', off, mx))

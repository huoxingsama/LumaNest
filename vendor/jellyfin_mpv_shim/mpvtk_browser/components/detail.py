"""Pieces of an item's detail presentation, shared by more than one family.

``meta_line`` is wanted by detail, series *and* the music header; ``people_row``
by detail and series. That cross-family reach is what makes them components
rather than methods on a page.

See ``docs/archive/ARCHITECTURE_TARGET.md`` §1.4 for the line being drawn.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from personal_cinema.runtime_paths import option
import logging
from ...extras import is_extra
from ...i18n import _
from ...mpvtk.widgets import Menu
from . import controls
from .labels import is_watched
log = logging.getLogger('mpvtk_browser.components.detail')

def fmt_ticks(ticks):
    """h:mm:ss / m:ss — a bare minutes:seconds rendered a 1h20m resume
    offset as "80:00".

    Was ``ViewsMixin._fmt_ticks``.
    """
    secs = int((ticks or 0) // 10000000)
    h, m, sec = (secs // 3600, secs % 3600 // 60, secs % 60)
    return '%d:%02d:%02d' % (h, m, sec) if h else '%d:%02d' % (m, sec)

def meta_line(item):
    """The year · runtime · rating · genres line under a title.

    Was ``ViewsMixin._meta_line``.
    """
    parts = []
    if item.get('ProductionYear'):
        parts.append(str(item['ProductionYear']))
    rt = item.get('RunTimeTicks')
    if rt:
        parts.append(fmt_ticks(rt))
    if item.get('OfficialRating'):
        parts.append(str(item['OfficialRating']))
    if item.get('CommunityRating'):
        parts.append('★ %.1f' % item['CommunityRating'])
    genres = ', '.join((item.get('Genres') or [])[:3])
    if genres:
        parts.append(genres)
    return '   ·   '.join(parts)

def people_row(tiles, people):
    """The Cast & Crew carousel, or None when there is no one credited.

    Takes the ``TileRenderer`` rather than reaching for a shell: the row is
    built from tiles and tile geometry, which is all it needs.

    Was ``ViewsMixin._people_row``. Its ``server`` parameter was never used
    and is gone.
    """
    policy = option('artwork.missing_portrait', 'placeholder')
    cast = []
    for person in people:
        if not isinstance(person, dict):
            continue
        has_photo = bool(person.get('PrimaryImageTag') or (person.get('ImageTags') or {}).get('Primary'))
        if not has_photo and policy == 'hide':
            continue
        subtitle = person.get('Role') or person.get('Type') or ''
        if not has_photo:
            subtitle = ' · '.join(filter(None, (subtitle, '暂无照片')))
        cast.append(dict(person, Type='Person', _subtitle=subtitle))
        if len(cast) == 24:
            break
    if not cast:
        return None
    return tiles.tile_row(_('Cast & Crew'), cast, 'detail-people', geom=tiles.art.geom)

def download_button(actions, tiles, item, server, prefix, size=controls.ROW):
    """Download, or Remove when it's already downloaded.

    ``size`` because this is the one button in the set that a caller
    appends to a row it built itself, rather than getting from
    ``common_actions``. The AudioBook page builds a row of 18s and got a
    16 on the end -- 42.5px against 40.0, the same defect the grid bar was
    levelled for, twice the size.

    The button used to always say Download, so pressing it on a complete
    item did nothing visible and there was no way to reclaim the space
    outside Settings -> Downloads.

    Takes the two services it needs rather than a shell: ``actions`` to run
    the effect, ``tiles`` because the downloaded-id sets live with the badge
    that draws from them.

    Was ``ViewsMixin._download_btn``.
    """
    if not tiles.is_downloaded(item):
        if actions.offline:
            return None
        if is_extra(item):
            return None
        return controls.action_btn('file_download', _('Download'), prefix + '-download', lambda: actions.open_download(item), size=size)
    return controls.action_btn('delete', _('Remove Download'), prefix + '-undownload', lambda: actions.confirm_remove_download(item), size=size)

def common_actions(actions, tiles, item, server, prefix):
    """Watched / Favorite / Download — the buttons detail, series, season
    and playlist all carry.

    Was ``ViewsMixin._common_actions``.
    """
    ud = item.get('UserData') or {}
    return [controls.action_btn('check', _('Watched'), prefix + '-watched', lambda: actions.toggle_watched(item, server), on=is_watched(item)), controls.action_btn('favorite', _('Favorite'), prefix + '-fav', lambda: actions.toggle_favorite(item, server), on=bool(ud.get('IsFavorite'))), download_button(actions, tiles, item, server, prefix)]

def more_entries(actions, item, server, may_refresh, on_deleted=None):
    """The More menu's ``(label, icon, callback)`` rows; ``[]`` for none.

    ``may_refresh`` is ``actions.can_refresh_metadata`` asked in the page's
    ``work()``: it can fetch the user policy, and a failed fetch is not
    cached, so asked here it would go to the server on every repaint.

    Delete is offered only to a page that passes ``on_deleted``, because a
    page that *is* the deleted item has to leave itself afterwards.
    """
    from .. import live_tv
    if actions.offline or not item or (not item.get('Id')):
        return []
    out = []
    if may_refresh and (not live_tv.is_recording_now(item)):
        out.append((_('Refresh metadata'), 'refresh', lambda: actions.refresh_metadata(item, server)))
    if on_deleted is not None and actions.can_delete(item):
        out.append((_('Delete from Disk'), 'delete', lambda: actions.confirm_delete_item(item, server, on_done=on_deleted)))
    return out
MORE_KEY = '_more'

def more_button(route, invalidate, entries, node_id):
    """The More button, or None when there is nothing behind it."""
    if not entries:
        return None

    def open_menu():
        route[MORE_KEY] = True
        invalidate()
    return controls.action_btn('more_horiz', _('More'), node_id, open_menu)

def more_menu(route, invalidate, art, entries, anchor_id, scroll_id):
    """The open More menu, dropped below its button; None while closed.

    ``node_rect`` is in content space inside a scroll (mpvtk/layout.py), so
    the page's live offset comes off before it is a window position.
    """
    if not route.get(MORE_KEY) or not entries:
        return None
    x = y = 0
    rect = art.node_rect(anchor_id)
    if rect is not None:
        x = rect['x']
        y = rect['y'] + rect['h'] + 4 - art.scroll.offset(scroll_id)

    def close():
        route.pop(MORE_KEY, None)
        invalidate()

    def pick(index, _value):
        close()
        if 0 <= index < len(entries):
            entries[index][2]()
    return Menu(anchor_id + '-menu', [e[0] for e in entries], x, y, icons=[e[1] for e in entries], on_select=pick, on_dismiss=close)

def special_features(source, server, item):
    """The item's Special Features, for a page's ``work()``.

    Fetched only when the server counts some, which is jellyfin-web's gate
    (itemDetails/index.js:862) and is type-agnostic: any item that can own
    extras says so here. A failure costs the row, never the page.
    """
    if not (item or {}).get('SpecialFeatureCount'):
        return []
    try:
        return source.get_special_features(server, item['Id']) or []
    except Exception:
        log.debug('special features failed', exc_info=True)
        return []

def special_features_row(tiles, items, row_id):
    """The Special Features carousel, or None for an empty list.

    Hidden when empty where web draws a bare heading: on 12.x the count is
    not parental-filtered and the list is, so a count of 3 can fetch none.
    The default click opens the extra's own page, like every other tile.

    Three caption lines in every shape, for the runtime: web's row asks for
    it (``showRuntime``, itemDetails/index.js:1843), so it belongs to this
    row and not to an extra wherever it appears.
    """
    if not items:
        return None
    geom, image_type = tiles.auto_geom(items, default=tiles.art.geom_wide, default_type='Primary')
    return tiles.tile_row(_('Special Features'), items, row_id, geom=geom.with_caption_lines(3), image_type=image_type)

def local_trailers(source, server, item):
    """The item's local trailers for ``work()``, gated on the server's count
    like :func:`special_features`."""
    if not (item or {}).get('LocalTrailerCount'):
        return []
    try:
        return source.get_trailers(server, item['Id']) or []
    except Exception:
        log.debug('local trailers failed', exc_info=True)
        return []

def trailer_target(item, trailers, offline):
    """What the Trailer button does: ``("local", ids)``, ``("remote", url)``
    or None for no button.

    Local first, as web (index.js:508-514); otherwise the first remote URL
    in the desktop's browser -- never offline, where it would leave for a
    page the user may not be able to reach either.
    """
    ids = [t.get('Id') for t in trailers or () if t.get('Id')]
    if ids:
        return ('local', ids)
    if offline:
        return None
    for entry in (item or {}).get('RemoteTrailers') or ():
        url = entry.get('Url') if isinstance(entry, dict) else None
        if url:
            return ('remote', url)
    return None

def trailer_button(actions, item, trailers, server, node_id, open_link, size=controls.ROW):
    """The Trailer button per :func:`trailer_target`, or None.

    A remote trailer wears ``open_in_new`` like the provider links: the one
    thing to know before pressing is that it leaves the application.
    """
    target = trailer_target(item, trailers, actions.offline)
    if target is None:
        return None
    kind, what = target
    if kind == 'local':
        return controls.action_btn('movie', _('Trailer'), node_id, lambda: actions.play_list(what, server, 0), size=size)
    return controls.action_btn('open_in_new', _('Trailer'), node_id, lambda: open_link(what), size=size)
MAX_PROVIDER_LINKS = 8
DEAD_PROVIDER_HOSTS = ('zap2it.com',)

def _is_dead(url):
    """True for a link we will not offer. Host-only, so a query string
    naming one of these does not take an unrelated provider down with it."""
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or '').lower()
    except ValueError:
        return True
    return any((host == dead or host.endswith('.' + dead) for dead in DEAD_PROVIDER_HOSTS))

def jellyfin_web_url(address, item):
    """The item's page in jellyfin-web, or None when it cannot be composed.

    ``#/details?id=…&serverId=…`` — read out of jellyfin-web's own
    ``appRouter.getRouteUrl`` and checked on both supported servers (12.0 in
    the bundle it serves, 10.11 in ``release-10.11.z``); the ``#!`` spelling
    is 10.8 and older. ``serverId`` comes from the DTO rather than from the
    uuid the browser keys servers by: those are the *server's* id only for a
    login that asked for it, and a random uuid4 otherwise (``clients.py``).
    It is left off when the DTO has none, which web reads as "the server you
    are signed in to" — the right answer for the only source that omits it.
    """
    if not address or not (item or {}).get('Id'):
        return None
    url = '%s/web/#/details?id=%s' % (address.rstrip('/'), item['Id'])
    server_id = item.get('ServerId')
    if server_id:
        url += '&serverId=%s' % server_id
    return url

def provider_link_buttons(item, on_open, web_url=None):
    """The provider links as a plain list of buttons, or ``[]``.

    Split from :func:`provider_links` because the season screen does not
    want a row of its own: it has a header row already (the title, the
    season picker, To Series) and these belong on the end of it, which
    means it needs the buttons rather than something already packed.

    ``ExternalUrls`` needs no ``Fields`` and costs no request: the server
    fills it in unconditionally on the single-item routes, which is what the
    detail page already fetches through (measured on 10.11 and 12.0, both
    ``/Users/{uid}/Items/{id}`` and ``/Items/{id}``). It is absent from list
    queries unless asked for, which is why this belongs to detail screens
    and not to a tile.

    Buttons rather than jellyfin-web's bare coloured text: the one thing the
    user has to know before pressing is that it *leaves the application*,
    and on a ten-foot UI a differently-coloured word does not say that. The
    ``open_in_new`` glyph is the same one web puts on its own external links.

    ``on_open`` takes the url. It is passed in rather than reached for
    because opening one is the shell stepping outside the process, which is
    the gateway's job and not a component's.

    ``web_url`` is the item's own page on the server it came from
    (:func:`jellyfin_web_url`), and leads rather than joining the tail: it is
    the one link here that is about *this* library rather than about the
    title in general, and it is the one somebody presses to carry on with the
    item somewhere else. Its node id is deliberately outside the
    ``detail-link-N`` series — those are indexed by provider, and shifting
    them by one would make every existing id name a different database.
    """
    seen, links = (set(), [])
    for entry in item.get('ExternalUrls') or ():
        if not isinstance(entry, dict):
            continue
        url, name = (entry.get('Url'), entry.get('Name'))
        if not url or not name or url in seen or _is_dead(url):
            continue
        seen.add(url)
        links.append((str(name), str(url)))
        if len(links) >= MAX_PROVIDER_LINKS:
            break
    buttons = []
    if web_url:
        buttons.append(controls.action_btn('open_in_new', _('Web'), 'detail-web-link', lambda u=web_url: on_open(u), size=controls.ROW))
    buttons += [controls.action_btn('open_in_new', name, 'detail-link-%d' % i, lambda u=url: on_open(u), size=controls.ROW) for i, (name, url) in enumerate(links)]
    return buttons

def provider_links(item, avail, on_open, web_url=None):
    """The provider links as a row of their own, or None when there are no
    links to draw. What the detail and series screens want, both of which
    place this under the synopsis with nothing else beside it."""
    from . import chrome
    buttons = provider_link_buttons(item, on_open, web_url=web_url)
    if not buttons:
        return None
    return chrome.wrap_row(buttons, avail, gap=8, align='center')

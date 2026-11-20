"""Item- and server-derived strings, and small layout maths.

Moved verbatim from ``TilesMixin`` — these never used ``self``, so being
methods only made them look coupled. Each carries the comment explaining the
bug it encodes; those are the reason to keep the logic rather than "simplify"
it later.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import datetime
import ipaddress
import urllib.parse
from ...extras import is_extra
from ...i18n import _, _p
LISTING_TYPES = ('Program', 'Timer', 'Recording', 'SeriesTimer')

def air_time_line(item):
    """When a listing is on, as its own caption line — or "" for anything
    that is not a listing, or has no air time to report.

    Split out of :func:`episode_subtitle` because "BBC Two   ·   20:00 -
    20:30" is about 200px of text and a poster tile is 150 wide, so the
    channel survived and the time -- the half a listing exists to tell you
    -- was ellipsized away. Two lines fit; one never did.

    ``episode_subtitle(air_time=False)`` is the other half. Keeping them as
    a pair rather than having the caller slice a joined string means the
    separator, the SeriesTimer "any time" case and the recording-with-no-
    start case are decided once.
    """
    kind = item.get('Type')
    if kind not in LISTING_TYPES or item.get('_subtitle') is not None:
        return ''
    from .. import live_tv
    if kind == 'SeriesTimer':
        return '' if item.get('RecordAnyTime') else _a_time(item, live_tv)
    return live_tv.air_time_label(item)

def episode_subtitle(item, show_year=True, air_time=True):
    """The line under a tile's title.

    ``air_time=False`` leaves the air time out of a listing's line, for a
    caller that is drawing it as a third line instead — see
    :func:`air_time_line`. It changes nothing for any other item type.

    Was ``TilesMixin._subtitle``.
    """
    if item.get('_subtitle') is not None:
        return item['_subtitle']
    if is_extra(item):
        return extra_type_label(item)
    if item.get('Type') == 'Episode':
        series = item.get('SeriesName') or ''
        s, e = (item.get('ParentIndexNumber'), item.get('IndexNumber'))
        if s is not None and e is not None:
            se = 'S%dE%d' % (s, e)
            return '%s · %s' % (series, se) if series else se
        return series
    kind = item.get('Type')
    if kind in ('Program', 'Timer', 'Recording'):
        from .. import live_tv
        when = live_tv.air_time_label(item) if air_time else ''
        return '   ·   '.join((p for p in (item.get('ChannelName') or '', when) if p))
    if kind == 'SeriesTimer':
        from .. import live_tv
        when = '' if not air_time or item.get('RecordAnyTime') else _a_time(item, live_tv)
        channel = '' if item.get('RecordAnyChannel') else item.get('ChannelName') or ''
        return '   ·   '.join((p for p in (channel, when) if p))
    if kind == 'TvChannel':
        current = (item.get('CurrentProgram') or {}).get('Name') or ''
        number = str(item.get('Number') or '').strip()
        return '   ·   '.join((p for p in (number, current) if p))
    return str(item.get('ProductionYear') or '') if show_year else ''

def extra_type_label(item):
    """An extra's translated ``ExtraType``, or "" -- for ``Unknown`` (an
    ``extras/`` folder says nothing about what is in it), and for the types
    a Special Features list never holds. Wording is jellyfin-web's
    (en-us.json:1858-1869)."""
    return {'Featurette': _p('kind of special feature', 'Featurette'), 'BehindTheScenes': _p('kind of special feature', 'Behind the Scenes'), 'DeletedScene': _p('kind of special feature', 'Deleted Scene'), 'Interview': _p('kind of special feature', 'Interview'), 'Scene': _p('kind of special feature', 'Scene'), 'Sample': _p('kind of special feature', 'Sample'), 'Clip': _p('kind of special feature', 'Clip'), 'Short': _p('kind of special feature', 'Short')}.get((item or {}).get('ExtraType'), '')
NO_SUBTITLE_TYPES = frozenset({'UserView', 'CollectionFolder', 'Genre', 'MusicGenre', 'Person', 'Studio'})

def has_no_subtitles(items):
    """Whether a row of ``items`` can be drawn with a one-line caption.

    Conservative on both sides: empty is False (nothing to shrink for, and a
    row that fills in later must not have been pitched for one line), and one
    item of any other type is enough to keep the second line for the row --
    a strip is composited at one caption height, so this cannot be per-tile.
    """
    items = [i for i in items or () if i]
    if not items:
        return False
    return all((i.get('_subtitle') is None and i.get('Type') in NO_SUBTITLE_TYPES for i in items))

def _a_time(item, live_tv):
    start = live_tv.parse_time(item.get('StartDate'))
    return live_tv.fmt_time(start) if start else ''

def tile_lines(item, parent_item=False, show_year=True, air_time=True):
    """``(title, subtitle)`` for a tile.

    ``air_time`` is passed through to :func:`episode_subtitle`: False when
    the caller is going to draw a listing's air time on its own line.

    ``parent_item`` flips an episode around: the series becomes the title
    and the episode name the subtitle. That is what a "Latest TV" row wants
    — the server hands back a Series when a show got several new episodes
    and a bare Episode when it got one, so without this the same row reads
    as a list of shows with an episode title dropped in the middle of it.
    Anything that is not an Episode is unaffected, series rows included.
    """
    if parent_item and item.get('Type') == 'Episode':
        series = item.get('SeriesName')
        if series:
            return (series, item.get('Name', ''))
    return (item.get('Name', ''), episode_subtitle(item, show_year, air_time))

def is_watched(item):
    """Whether a tile shows the watched check.

    Was ``TilesMixin._is_watched``.
    """
    ud = item.get('UserData') or {}
    if ud.get('Played'):
        return True
    if item.get('Type') in ('Series', 'Season', 'Folder'):
        return ud.get('UnplayedItemCount') == 0
    return False

def placeholder_glyph(item):
    """What to draw on a tile with no artwork.

    A **Material icon name** where one fits, else the title's first
    initial. The compositor tells them apart by looking the name up
    (`strips._paint_poster`), so a one-character answer can never be
    mistaken for an icon and vice versa.

    Library tiles are asked by collection type first: a UserView's `Type`
    is "CollectionFolder" for every library there is, so answering from
    the type map alone drew a folder on all of them.
    """
    ctype = (item.get('CollectionType') or '').lower()
    glyph = _LIBRARY_GLYPHS.get(ctype) if ctype else None
    if glyph is None:
        glyph = _TYPE_GLYPHS.get(item.get('Type'))
    if glyph:
        return glyph
    name = (item.get('Name') or '').strip()
    return name[0].upper() if name else '?'
TYPE_INDICATOR_ICONS = {'Video': 'videocam', 'Folder': 'folder', 'PhotoAlbum': 'photo_album', 'Photo': 'photo'}

def virtual_episode_label(item):
    """"Missing", "Unaired", or None for an episode that has a file.

    **jellyfin-web's rule, in one place.** `indicators.js` marks an `Episode`
    whose `LocationType` is `Virtual` -- one the server listed out of the
    series metadata with nothing behind it -- and which of the two words it
    gets depends only on whether the air date is still in the future.

    Neither is hidden, because seeing the gaps is the point: the server has
    already applied the user's own `DisplayMissingEpisodes`
    (`TvShowsController.cs`, both supported majors), so an item only reaches
    here because somebody asked to see it. What web's card does NOT give one
    is a play button, and that is the other half of this predicate's job --
    `tiles._tile_playable` and the detail page's own buttons ask it too, and
    a rule applied at one of its sites is the recurring defect shape here.

    An undated virtual episode is "Missing": a future date is the only
    evidence that it is coming rather than absent.
    """
    if item.get('Type') != 'Episode':
        return None
    if item.get('LocationType') != 'Virtual':
        return None
    from .. import live_tv
    premiere = live_tv.parse_time(item.get('PremiereDate'))
    if premiere is not None and premiere > datetime.datetime.now(datetime.timezone.utc):
        return _('Unaired')
    return _('Missing')

def type_indicator_icon(item):
    """The corner type marker for a tile, or "" for types that get none."""
    return TYPE_INDICATOR_ICONS.get(item.get('Type'), '')
_TYPE_GLYPHS = {'MusicAlbum': 'album', 'MusicArtist': 'person', 'Person': 'person', 'Audio': 'audiotrack', 'Movie': 'movie', 'Series': 'tv', 'Episode': 'tv', 'Season': 'tv', 'Program': 'live_tv', 'TvChannel': 'live_tv', 'Book': 'book', 'Folder': 'folder', 'CollectionFolder': 'folder', 'BoxSet': 'video_library', 'Playlist': 'queue', 'Photo': 'photo', 'PhotoAlbum': 'photo_album', 'Trailer': 'theaters', 'MusicVideo': 'music_video', 'AudioBook': 'audiotrack'}
_LIBRARY_GLYPHS = {'movies': 'movie', 'music': 'music_note', 'homevideos': 'photo', 'photos': 'photo', 'livetv': 'live_tv', 'tvshows': 'tv', 'trailers': 'theaters', 'musicvideos': 'music_video', 'books': 'book', 'boxsets': 'video_library', 'playlists': 'queue', 'channels': 'videocam'}

def heading_for(item):
    """``(title, context)`` for a detail heading.

    An episode's series and SxEy go on their own line above the episode
    title rather than being joined into one string — joined, a name of
    any length ran off the end of the banner and was cut mid-word
    ("Clannad · S1E1 · On the Hillside Pa").

    Was ``TilesMixin._heading_for``.
    """
    title = item.get('Name', '')
    if item.get('Type') != 'Episode':
        return (title, '')
    s, e = (item.get('ParentIndexNumber'), item.get('IndexNumber'))
    se = 'S%sE%s' % (s, e) if s is not None and e is not None else ''
    context = '   ·   '.join((p for p in (item.get('SeriesName'), se) if p))
    return (title, context)

def section_offsets(elements, gap, pad=0):
    """Content-y of each element's top in a ``Column(pad=pad, gap=gap)`` —
    the snap breakpoints for a variable-height list (home sections, which
    are heading + carousel of differing heights). Uses the layout engine's
    own measurement so a stop lands the section heading flush, not a few px
    off. None entries are skipped to match Column.

    Was ``TilesMixin._section_offsets``.
    """
    from ...mpvtk.layout import measure
    offs, y = ([], float(pad))
    for el in elements:
        if el is None:
            continue
        offs.append(y)
        y += measure(el)[1] + gap
    return offs

def track_duration(item):
    """``m:ss`` for a track, or "" when the server gave no runtime.

    Was ``MusicMixin._duration``; moved so the track table can be rendered
    without a MusicMixin (see tile_renderer.TileRenderer.track_list).
    """
    secs = (item.get('RunTimeTicks') or 0) // 10000000
    return '%d:%02d' % (secs // 60, secs % 60) if secs else ''

def track_artists(item):
    """Comma-joined artist names, falling back to album artists.

    Was ``MusicMixin._artists``.
    """
    return ', '.join(item.get('Artists') or item.get('AlbumArtists') or [])

def human_size(n):
    """Byte count as a short human string.

    Was ``DialogsMixin._human_size``; the detail page's media-info line
    wants it too, and a second copy is how the two drift.
    """
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return '%d %s' % (n, unit) if unit == 'B' else '%.1f %s' % (n, unit)
        n /= 1024
_LOCAL_SUFFIXES = ('.local', '.lan', '.home', '.internal', '.localdomain')

def is_local_server(address):
    """Is this address on the user's own network rather than out on the
    internet -- **going only by how it is spelled**?

    The fallback, not the answer. `clients.server_is_local` resolves the
    name when it connects and that verdict reaches here as the entry's
    `local` key; this runs when nothing has connected, so there is nothing
    to resolve from and no connection to describe.

    Syntactic because it runs on the render path, where a name lookup is a
    blocking call to decide an icon. It cannot see split-horizon DNS at all
    -- a self-hoster's own domain resolves to a LAN address at home and a
    public one away, and the spelling is identical -- which is exactly why
    the resolved verdict outranks it.

    A bare hostname with no dot counts as local: `http://mediaserver:8096`
    is a name only a LAN resolver (mDNS, NetBIOS, the router's own DNS) can
    answer, which is what makes it one.
    """
    if not address:
        return False
    try:
        host = urllib.parse.urlsplit(str(address)).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.rstrip('.').lower()
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        pass
    return host == 'localhost' or '.' not in host or host.endswith(_LOCAL_SUFFIXES)

def server_icon(server):
    """The glyph for one server entry: where it is, or that it is not there.

    Every server gets one, which is the point as much as the meaning is. A
    dropdown indents every row as soon as any item carries an icon, so a
    list that marked only the broken entries paid that width on all of them
    and gave the names back an ellipsis in exchange for nothing.

    ``connected`` absent means connected -- the login screen's list of
    previously added servers is addresses, with no connection state to
    report.

    ``local`` is the resolved verdict from the connection itself, and it
    wins whenever there is one: reading the URL cannot tell a domain that
    points at a LAN address from one that does not, and for anybody running
    their own domain at home that is every server they have.
    """
    if not server.get('connected', True):
        return 'cloud_off'
    on_lan = server.get('local')
    if on_lan is None:
        on_lan = is_local_server(server.get('address'))
    return 'lan' if on_lan else 'cloud'

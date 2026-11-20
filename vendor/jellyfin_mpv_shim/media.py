from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import logging
import ipaddress
import urllib.parse
import os.path
import re
import pathlib
import stat
from io import BytesIO
from sys import platform
from . import conf
from .conf import settings
from .language_config import apply as apply_language_config
from .utils import IMAGE_SUBTITLE_CODECS, answering_server, is_local_domain, get_profile, get_seq
from .user_policy import may_download
from .i18n import _
log = logging.getLogger('media')
_LOCAL_SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup', '.smi'}
from typing import TYPE_CHECKING, Optional, List
if TYPE_CHECKING:
    from jellyfin_apiclient_python import JellyfinClient as JellyfinClient_type
_video_factory = None

def set_video_factory(factory):
    global _video_factory
    _video_factory = factory
_SERVER_CONVERTED_IMAGES = {'heic', 'heif', 'avif', 'cr2', 'nef', 'arw', 'dng', 'orf', 'raf', 'rw2'}
_PHOTO_MAX_WIDTH = 3840
PLAY_DIRECT = 'DirectPlay'
PLAY_DIRECT_STREAM = 'DirectStream'
PLAY_REMUX = 'Remux'
PLAY_TRANSCODE = 'Transcode'

def _target_codecs(transcoding_url):
    """``(video, audio, reasons)`` out of a ``TranscodingUrl`` query string.

    **All three are query parameters, and that is measured, not assumed** --
    the MediaSource DTO carries no ``TranscodeReasons`` on 10.11, so reading
    them off the DTO yields None every time (docs/jellyfin-api-notes.md
    section 11). Each codec parameter may name *several* codecs, which is
    why the caller tests membership rather than equality.
    """
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(transcoding_url or '').query)
    except Exception:
        log.debug('could not parse the transcoding url', exc_info=True)
        return ('', '', [])

    def one(key):
        return (query.get(key) or [''])[0]
    reasons = [r.strip() for r in one('TranscodeReasons').split(',') if r.strip()]
    return (one('VideoCodec'), one('AudioCodec'), reasons)
_VIDEO_REASONS = frozenset({'ContainerBitrateExceedsLimit', 'VideoBitrateNotSupported', 'VideoResolutionNotSupported', 'VideoFramerateNotSupported', 'VideoLevelNotSupported', 'VideoProfileNotSupported', 'VideoBitDepthNotSupported', 'VideoRangeTypeNotSupported', 'AnamorphicVideoNotSupported', 'InterlacedVideoNotSupported', 'RefFramesNotSupported'})
_AUDIO_REASONS = frozenset({'AudioBitrateNotSupported', 'AudioChannelsNotSupported', 'AudioSampleRateNotSupported', 'AudioBitDepthNotSupported', 'AudioProfileNotSupported', 'SecondaryAudioNotSupported'})

def transcode_play_method(media_source, transcoding_url, aid=None):
    """``(play_method, reasons)`` for a source the server is serving us.

    Only for the transcoding branch -- a direct path or a static stream is
    :data:`PLAY_DIRECT` without asking anyone.

    The rule is jellyfin-web's, reached by a different route: web reads its
    own session back off the server (``TranscodingInfo.IsVideoDirect`` /
    ``IsAudioDirect``) because the *server* made the decision and web only
    finds out, while here the same facts are in the URL we were handed -- so
    this compares the codec the server says it will emit against the codec
    the file already has. The resulting table, measured against a live 10.11
    server with three device profiles built to force each row, is in
    docs/jellyfin-api-notes.md section 11.
    """
    target_v, target_a, reasons = _target_codecs(transcoding_url)
    streams = (media_source or {}).get('MediaStreams') or []
    video = next((s for s in streams if s.get('Type') == 'Video'), None)
    audio = None
    if aid is not None:
        audio = next((s for s in streams if s.get('Index') == aid and s.get('Type') == 'Audio'), None)
    if audio is None:
        audio = next((s for s in streams if s.get('Type') == 'Audio'), None)
    named = set(reasons)

    def direct(stream, target, touching):
        if stream is None:
            return True
        if named & touching:
            return False
        codec = (stream.get('Codec') or '').lower()
        names = [t.strip().lower() for t in (target or '').split(',')]
        return not target or (codec and codec in names)
    if not direct(video, target_v, _VIDEO_REASONS):
        return (PLAY_TRANSCODE, reasons)
    if not direct(audio, target_a, _AUDIO_REASONS):
        return (PLAY_DIRECT_STREAM, reasons)
    return (PLAY_REMUX, reasons)

def build_video(item_id, parent, aid=None, sid=None, srcid=None, explicit_tracks=False):
    if _video_factory is not None:
        try:
            video = _video_factory(item_id, parent, aid, sid, srcid, explicit_tracks=explicit_tracks)
            if video is not None:
                return video
        except Exception:
            log.warning('Offline video factory failed for %s', item_id, exc_info=True)
    if parent.client is None:
        log.warning('Item %s is not downloaded and there is no server connection.', item_id)
        return None
    return Video(item_id, parent, aid, sid, srcid, explicit_tracks=explicit_tracks)

class Intro(object):

    def __init__(self, type, start, end):
        self.type: str = type
        self.start: float = start
        self.end: float = end
        self.has_triggered: bool = False

def segment_labels(segment_type):
    """``(button, done, prompt)`` for a segment type.

    Whole phrases per type rather than "Skip {0}" with a noun: gettext keys
    on the English, and a language that inflects the verb with the object
    cannot fix a sentence assembled after translation. This is also what
    jellyfin-web does.

    Built per call, not at import: ``_()`` resolves against whatever locale
    is loaded, and at module scope that is whatever was loaded first.
    """
    table = {'Outro': (_('Skip Credits'), _('Skipped Credits'), _('Seek to Skip Credits')), 'Commercial': (_('Skip Commercial'), _('Skipped Commercial'), _('Seek to Skip Commercial')), 'Preview': (_('Skip Preview'), _('Skipped Preview'), _('Seek to Skip Preview')), 'Recap': (_('Skip Recap'), _('Skipped Recap'), _('Seek to Skip Recap'))}
    return table.get(segment_type, (_('Skip Intro'), _('Skipped Intro'), _('Seek to Skip Intro')))

class Video(object):
    _tracks_resolved = False
    explicit_tracks = False
    audio_external = frozenset()
    subtitle_extracted = frozenset()

    def __init__(self, item_id: str, parent: 'Media', aid: Optional[int]=None, sid: Optional[int]=None, srcid: Optional[str]=None, explicit_tracks: bool=False):
        self.item_id = item_id
        self.parent = parent
        self.client = parent.client
        self.aid = aid
        self.sid = sid
        self.secondary_sid: Optional[int] = None
        self.explicit_tracks = explicit_tracks
        self.item = self.client.jellyfin.get_item(item_id)
        self.is_tv = self.item.get('Type') == 'Episode'
        self.is_photo = self.item.get('Type') == 'Photo'
        self.auth_via_header = False
        self.subtitle_seq = {}
        self.subtitle_uid = {}
        self.subtitle_url = {}
        self.subtitle_enc = set()
        self.audio_seq = {}
        self.audio_uid = {}
        self.is_transcode = False
        self.play_method = None
        self.transcode_reasons: List[str] = []
        self.direct_path = False
        self.trs_ovr = None
        self.playback_info = None
        self.media_source = None
        self.srcid = srcid
        self.intros: List[Intro] = []
        self.intro_tried = False
        self._tracks_resolved = False

    def foreign_subtitle_hosts(self):
        """Hosts, other than our server, that mpv would fetch a subtitle from.

        ``--http-header-fields`` is a **global** mpv option: it applies to
        every HTTP request mpv makes, not just the stream. An external
        subtitle whose Path is an absolute URI is handed to ``sub_add``
        unchanged (see map_streams), so setting the header while one of
        those is in play would send our access token to whoever hosts it.

        Read off the item rather than the media source because this has to
        be answerable *before* PlaybackInfo -- the header has to be decided
        before the stream URL is built. IsExternalUrl is set by the server
        exactly when a subtitle's Path is an absolute http(s) URI
        (StreamInfo.cs:1264-1274), so the same test on Path is the honest
        pre-check.
        """
        from .utils import same_origin
        try:
            server = self.client.config.data.get('auth.server') or ''
        except Exception:
            return {'unknown'}
        foreign = set()
        for source in self.item.get('MediaSources') or []:
            for stream in source.get('MediaStreams') or []:
                if stream.get('Type') != 'Subtitle':
                    continue
                path = stream.get('Path') or ''
                if not path.lower().startswith(('http://', 'https://')):
                    continue
                if not same_origin(path, server):
                    try:
                        host = urllib.parse.urlparse(path).hostname
                    except Exception:
                        host = None
                    foreign.add(host or 'unknown')
        return foreign

    def source_for_track_rules(self):
        """The media source to resolve track rules against, before there is a
        negotiated one.

        The item's own MediaSources, which is what the details page already
        uses to default its pickers (`pages/detail.py`), so the screen and the
        stream agree. Once PlaybackInfo has run, the negotiated source is the
        better answer and is preferred.
        """
        if self.media_source:
            return self.media_source
        sources = (self.item or {}).get('MediaSources') or []
        if not sources:
            return None
        if self.srcid:
            for src in sources:
                if src.get('Id') == self.srcid:
                    return src
        return sources[0]

    def resolve_tracks_for_negotiation(self):
        """Settle aid/sid from language_config BEFORE PlaybackInfo.

        `get_play_info` is sent `self.aid`/`self.sid`, and for a transcode the
        server bakes the audio index it is given into `TranscodingUrl` -- so
        this must precede the negotiation it exists to influence, and
        `configure_streams` cannot repair it afterwards (it skips audio on a
        transcode, correctly). Step 1 of docs/track-selection.md section 2.

        Idempotent, because `get_playback_url` runs again on a quality change
        or a forced-transcode retry, and because `play()` calls this first so
        it can apply a remembered track *over* the rule.

        **Only the rule.** A source default is deliberately NOT resolved here:
        posting nothing is what makes Jellyfin fall back to
        DefaultAudioStreamIndex itself, so client and server agree. Resolving
        one client-side and posting it would be a behaviour change, and
        `map_streams` fills it in afterwards for the UI regardless.
        """
        if self.explicit_tracks or self._tracks_resolved:
            return
        source = self.source_for_track_rules()
        if not source:
            return
        self._tracks_resolved = True
        rule_aid, rule_sid = apply_language_config(settings.language_config, source, self.item)
        if rule_aid is not None:
            self.aid = rule_aid
        if rule_sid is not None:
            self.sid = rule_sid

    def reauthorize_sidecars(self):
        """Put a token back into our own subtitle urls after the mpv header
        has been revoked.

        `map_streams` builds an external subtitle's url from its DeliveryUrl
        and does NOT add a token: with the Authorization header installed it
        does not need one. But the header is revoked when the *media* turns
        out to live on somebody else's host -- and the subtitle is still ours,
        so it is left with no credential at all and comes back 401, i.e. no
        captions on exactly the items that take the direct-path route.

        Only same-origin urls: a sidecar hosted elsewhere (IsExternalUrl) must
        not be handed our token, which is the rule the revoke exists to
        enforce in the first place.
        """
        from .utils import same_origin
        if not self.subtitle_url:
            return
        try:
            server = self.client.config.data['auth.server']
            token = self.client.config.data['auth.token']
        except Exception:
            return
        if not token:
            return
        for index, url in list(self.subtitle_url.items()):
            if not url or not same_origin(url, server):
                continue
            if 'ApiKey=' in url or 'api_key=' in url:
                continue
            sep = '&' if '?' in url else '?'
            self.subtitle_url[index] = '%s%sApiKey=%s' % (url, sep, token)

    def _local_subtitle_path(self, stream):
        """Use a readable local sidecar only for a server on this machine."""
        if not stream.get('IsExternal') or stream.get('IsExternalUrl'):
            return None
        try:
            server = urllib.parse.urlparse(self.client.config.data.get('auth.server', ''))
            host = server.hostname or ''
            if server.scheme not in ('http', 'https'):
                return None
            if host != 'localhost' and (not ipaddress.ip_address(host).is_loopback):
                return None
        except (AttributeError, TypeError, ValueError):
            return None
        path = stream.get('Path')
        if not isinstance(path, str) or path.startswith(('\\\\', '//')):
            return None
        candidate = pathlib.Path(path)
        if not candidate.is_absolute() or candidate.suffix.lower() not in _LOCAL_SUBTITLE_EXTENSIONS:
            return None
        try:
            if os.name == 'nt':
                import ctypes
                if ctypes.windll.kernel32.GetDriveTypeW(str(candidate.anchor)) not in (2, 3, 6):
                    return None
            for part in (*reversed(candidate.parents), candidate):
                info = part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
                    return None
            if not stat.S_ISREG(info.st_mode):
                return None
            with candidate.open('rb') as subtitle:
                subtitle.read(1)
        except (OSError, ValueError):
            return None
        return str(candidate)

    def map_streams(self):
        self.subtitle_seq = {}
        self.subtitle_uid = {}
        self.subtitle_url = {}
        self.subtitle_enc = set()
        self.audio_seq = {}
        self.audio_uid = {}
        if self.media_source is None:
            return
        streams = self.media_source.get('MediaStreams') or []
        self.audio_external = frozenset((s['Index'] for s in streams if s.get('Type') == 'Audio' and s.get('IsExternal')))
        index = 1
        for stream in streams:
            if stream.get('Type') != 'Audio' or stream.get('IsExternal'):
                continue
            self.audio_uid[index] = stream['Index']
            self.audio_seq[stream['Index']] = index
            index += 1
        self.subtitle_extracted = frozenset((s['Index'] for s in streams if s.get('Type') == 'Subtitle' and (not s.get('IsExternal')) and (s.get('DeliveryMethod') == 'External') and ((s.get('Codec') or '').lower() in IMAGE_SUBTITLE_CODECS)))
        index = 1
        for sub in streams:
            if sub.get('Type') != 'Subtitle':
                continue
            if sub.get('DeliveryMethod') == 'Embed':
                self.subtitle_uid[index] = sub['Index']
                self.subtitle_seq[sub['Index']] = index
            elif sub.get('DeliveryMethod') == 'External':
                url = self._local_subtitle_path(sub)
                if url is None:
                    url = sub.get('DeliveryUrl')
                    if not sub.get('IsExternalUrl'):
                        url = self.client.config.data['auth.server'] + url
                self.subtitle_url[sub['Index']] = url
            elif sub.get('DeliveryMethod') == 'Encode':
                self.subtitle_enc.add(sub['Index'])
            if not sub.get('IsExternal'):
                index += 1
        if self.explicit_tracks:
            return
        if not self._tracks_resolved:
            rule_aid, rule_sid = apply_language_config(settings.language_config, self.media_source, self.item)
            if rule_aid is not None:
                self.aid = rule_aid
            if rule_sid is not None:
                self.sid = rule_sid
        user_aid = self.media_source.get('DefaultAudioStreamIndex')
        user_sid = self.media_source.get('DefaultSubtitleStreamIndex')
        if user_aid is not None and self.aid is None:
            self.aid = user_aid
        if user_sid is not None and self.sid is None:
            self.sid = user_sid

    def get_current_streams(self):
        return (self.aid, self.sid)

    def get_proper_title(self):
        if not hasattr(self, '_title'):
            title = self.item.get('Name')
            if self.is_tv and self.item.get('IndexNumber') is not None and (self.item.get('ParentIndexNumber') is not None):
                episode_number = int(self.item.get('IndexNumber'))
                season_number = int(self.item.get('ParentIndexNumber'))
                series_name = self.item.get('SeriesName')
                title = '%s - s%de%.2d - %s' % (series_name, season_number, episode_number, title)
            elif self.item.get('Type') == 'Movie':
                year = self.item.get('ProductionYear')
                if year is not None:
                    title = '%s (%s)' % (title, year)
            setattr(self, '_title', title)
        return getattr(self, '_title') + (_(' (Transcode)') if self.is_transcode else '')

    def set_trs_override(self, video_bitrate: Optional[int], force_transcode: bool):
        if force_transcode:
            self.trs_ovr = (video_bitrate, force_transcode)
        else:
            self.trs_ovr = None

    def get_transcode_bitrate(self):
        if not self.is_transcode:
            return 'none'
        elif self.trs_ovr is not None:
            if self.trs_ovr[0] is not None:
                return self.trs_ovr[0]
            elif self.trs_ovr[1]:
                return 'max'
        elif self.parent.is_local:
            return 'max'
        else:
            return settings.remote_kbps

    def terminate_transcode(self):
        live_stream_id = (self.media_source or {}).get('LiveStreamId')
        if live_stream_id:
            try:
                self.client.jellyfin.close_live_stream(live_stream_id)
                return
            except Exception:
                log.warning('Closing live stream failed.', exc_info=True)
        if not self.is_transcode:
            return
        play_session_id = (self.playback_info or {}).get('PlaySessionId')
        if play_session_id is None:
            return
        try:
            self.client.jellyfin.close_transcode(self.client.config.data['app.device_id'], play_session_id)
        except Exception:
            log.warning('Terminating transcode failed.', exc_info=True)

    @staticmethod
    def _join_substituted_path(target: str, suffix: str):
        if not suffix:
            return target
        is_windows_path = target.startswith('\\\\') or re.match('^[a-zA-Z]:[\\\\/]', target) is not None or '\\' in target
        separator = '\\' if is_windows_path else '/'
        clean_target = target.rstrip('\\/')
        clean_suffix = suffix.strip('\\/')
        if not clean_suffix:
            return clean_target
        if is_windows_path:
            clean_suffix = clean_suffix.replace('/', '\\')
        else:
            clean_suffix = clean_suffix.replace('\\', '/')
        return clean_target + separator + clean_suffix

    @classmethod
    def _apply_path_substitutions(cls, path: str):
        substitutions = settings.path_substitutions or []
        normalized_path = path.replace('\\', '/')
        for substitution in substitutions:
            if not isinstance(substitution, (list, tuple)) or len(substitution) != 2:
                log.warning('Ignoring invalid path substitution: %r', substitution)
                continue
            source, target = substitution
            if not isinstance(source, str) or not isinstance(target, str):
                log.warning('Ignoring invalid path substitution: %r', substitution)
                continue
            expanded_source = os.path.expanduser(os.path.expandvars(source))
            expanded_target = os.path.expanduser(os.path.expandvars(target))
            normalized_source = expanded_source.replace('\\', '/').rstrip('/')
            if not normalized_source:
                continue
            if normalized_path == normalized_source:
                log.debug('Rewriting media path %s to %s', path, expanded_target)
                return expanded_target
            source_prefix = normalized_source + '/'
            if normalized_path.startswith(source_prefix):
                suffix = normalized_path[len(source_prefix):]
                rewritten_path = cls._join_substituted_path(expanded_target, suffix)
                log.debug('Rewriting media path %s to %s', path, rewritten_path)
                return rewritten_path
        return path

    def _get_url_from_source(self):
        self.play_method = None
        self.transcode_reasons = []
        self.direct_path = False
        if (self.media_source.get('Protocol') == 'Http' or self.media_source['SupportsDirectPlay']) and settings.direct_paths and (settings.remote_direct_paths or self.parent.is_local):
            source_path = self.media_source['Path']
            if platform.startswith('win32') or platform.startswith('cygwin'):
                match = re.search('(?:\\\\).+:.*@(.+)', source_path)
                if match:
                    log.debug('cleaned up credentials from path')
                    source_path = str(pathlib.Path('\\\\' + match.groups()[0]))
            source_path = self._apply_path_substitutions(source_path)
            parsed_source_path = urllib.parse.urlparse(source_path)
            has_scheme = len(parsed_source_path.scheme) > 1
            if not has_scheme:
                expanded_source_path = os.path.expanduser(os.path.expandvars(source_path))
                if expanded_source_path != source_path:
                    log.debug('Expanded direct path %s to %s', source_path, expanded_source_path)
                source_path = expanded_source_path
                parsed_source_path = urllib.parse.urlparse(source_path)
                has_scheme = len(parsed_source_path.scheme) > 1
            self.media_source['Path'] = source_path
            if has_scheme:
                self.is_transcode = False
                self.play_method = PLAY_DIRECT
                self.direct_path = True
                log.debug('Using remote direct path.')
                if parsed_source_path.scheme not in ('http', 'https'):
                    return str(pathlib.Path(source_path))
                return source_path
            elif os.path.isfile(source_path):
                log.debug('Using local direct path.')
                self.is_transcode = False
                self.play_method = PLAY_DIRECT
                self.direct_path = True
                return source_path
        if self.media_source['SupportsDirectStream']:
            self.is_transcode = False
            self.play_method = PLAY_DIRECT
            log.info('Using direct url.')
            query_params = {'static': 'true', 'MediaSourceId': self.media_source['Id']}
            if not self.auth_via_header:
                query_params['ApiKey'] = self.client.config.data['auth.token']
            if 'LiveStreamId' in self.media_source:
                query_params['LiveStreamId'] = self.media_source['LiveStreamId']
            query = urllib.parse.urlencode(query_params)
            return '%s/Videos/%s/stream?%s' % (self.client.config.data['auth.server'], self.item_id, query)
        elif self.media_source['SupportsTranscoding']:
            log.info('Using transcode url.')
            self.is_transcode = True
            transcoding_url = self.media_source.get('TranscodingUrl')
            self.play_method, self.transcode_reasons = transcode_play_method(self.media_source, transcoding_url, self.aid)
            return self.client.config.data['auth.server'] + transcoding_url

    def get_best_media_source(self, preferred: Optional[str]=None):
        weight_selected = 0
        preferred_selected = None
        selected = None
        for media_source in self.playback_info['MediaSources']:
            if media_source.get('Id') == preferred:
                preferred_selected = media_source
            weight = (media_source.get('SupportsDirectPlay') or 0) * 50000 + (media_source.get('Bitrate') or 0) / 1000
            if selected is None or weight > weight_selected:
                weight_selected = weight
                selected = media_source
        if preferred_selected:
            return preferred_selected
        else:
            if preferred is not None:
                log.warning('Preferred media source is unplayable.')
            return selected

    def get_intro(self, media_source_id):
        if self.intro_tried:
            return
        self.intro_tried = True
        try:
            skip_intro_data = self.client.jellyfin.get_media_segments(media_source_id, include_segment_types=conf.wanted_segment_types())
            for intro in skip_intro_data['Items']:
                self.intros.append(Intro(intro['Type'], intro['StartTicks'] / 10000000, intro['EndTicks'] / 10000000))
        except:
            log.warning('Fetching intro data failed.', exc_info=1)

    def get_current_intro(self, time):
        for intro in self.intros:
            if intro.start <= time and time <= intro.end:
                return (intro.start <= time, intro)
        return (False, None)

    def _imaged_chapters(self):
        """``(server index, chapter)`` for chapters that have an image. A
        chapter whose extraction failed has none, and the previews' times and
        frames must both skip it -- one list feeds both -- or every frame
        after it is drawn under the next chapter's time. The index stays the
        server's: it is the chapter's position, not the image's."""
        return [(i, c) for i, c in enumerate(self.item.get('Chapters') or []) if c.get('ImageTag')]

    def get_chapters(self):
        return [{'start': item['StartPositionTicks'] / 10000000, 'name': item['Name']} for _i, item in self._imaged_chapters()]

    def get_chapter_images(self, max_width=400, quality=90):
        for i, item in self._imaged_chapters():
            data = BytesIO()
            self.client.jellyfin.get_chapter_image(data, self.item_id, i, tag=item['ImageTag'], max_width=max_width, quality=quality)
            yield data.getvalue()

    def get_hls_tile_images(self, width, count, start=0):
        """``count`` tile mosaics from tile index ``start``.

        A tile carries ``TileWidth * TileHeight`` thumbnails, so a caller
        after a window of the video asks for the tiles that window falls in
        rather than for all of them. See ``trickplay.TrickPlay._window_for``.
        """
        for i in range(start, start + count):
            data = BytesIO()
            self.client.jellyfin.get_trickplay_tile(data, self.item['Id'], width, i, media_source_id=self.media_source['Id'])
            yield data.getvalue()

    def get_bif(self, prefer_width=320):
        manifest = self.item.get('Trickplay')
        if manifest is None:
            item = self.client.jellyfin.get_item(self.item_id, fields='Trickplay')
            manifest = item.get('Trickplay')
        if manifest is not None and manifest.get(self.media_source['Id']) is not None and (len(manifest[self.media_source['Id']]) > 0):
            available_widths = [int(x) for x in manifest[self.media_source['Id']].keys()]
            if prefer_width is not None:
                width = min(available_widths, key=lambda x: abs(x - prefer_width))
            else:
                width = max(available_widths)
            return manifest[self.media_source['Id']][str(width)]
        else:
            return None

    def get_playback_url(self, video_bitrate: Optional[int]=None, force_transcode: Optional[int]=False):
        """
        Returns the URL to use for the transcoded file.
        """
        self.terminate_transcode()
        if self.is_photo:
            self.play_method = PLAY_DIRECT
            self.direct_path = True
            keep_token = not self.auth_via_header
            container = (self.item.get('Container') or '').lower()
            path = (self.item.get('Path') or '').lower()
            if container in _SERVER_CONVERTED_IMAGES or any((path.endswith('.' + ext) for ext in _SERVER_CONVERTED_IMAGES)) or (not may_download(self.client)):
                return self.client.jellyfin.artwork(self.item_id, 'Primary', _PHOTO_MAX_WIDTH, include_apikey=keep_token)
            return self.client.jellyfin.download_url(self.item_id, include_apikey=keep_token)
        if self.trs_ovr:
            video_bitrate, force_transcode = self.trs_ovr
        log.info('Bandwidth: local={0}, bitrate={1}, force={2}'.format(self.parent.is_local, video_bitrate, force_transcode))
        self.resolve_tracks_for_negotiation()
        profile = get_profile(not self.parent.is_local, video_bitrate, force_transcode, answering_server(self.client).get('Version'), image_sidecars=not self._declines_extraction())
        srcid = self.srcid
        if srcid is None and (self.aid is not None and self.aid >= 0 or (self.sid is not None and self.sid >= 0)):
            srcid = (self.source_for_track_rules() or {}).get('Id')
        self.playback_info = self.client.jellyfin.get_play_info(self.item_id, profile, self.aid, self.sid, media_source_id=srcid)
        self.media_source = self.get_best_media_source(self.srcid)
        if self.media_source is None:
            log.warning('This item has no media source; there is nothing to play.')
            return None
        if conf.any_segment_wanted():
            self.get_intro(self.media_source['Id'])
        self.map_streams()
        url = self._get_url_from_source()
        if url is None and len(self.playback_info['MediaSources']) > 1:
            log.warning('Selected media source is unplayable.')
            for media_source in self.playback_info['MediaSources']:
                if media_source['Id'] != self.srcid:
                    self.media_source = media_source
                    self.map_streams()
                    url = self._get_url_from_source()
                    if url is not None:
                        break
        if settings.log_decisions:
            if len(self.playback_info['MediaSources']) > 1:
                log.info('Full Playback Info: {0}'.format(self.playback_info))
            log.info('Media Decision: {0}'.format(self.media_source))
        return url

    def _declines_extraction(self):
        """The user opted out of extraction and the subtitle being negotiated
        -- the chosen one, else the server's default -- is an embedded image
        track. An external .sup is not affected: that is a file, not an
        extraction (#811)."""
        if settings.extract_image_subtitles:
            return False
        source = self.source_for_track_rules() or {}
        sid = self.sid
        if sid is None:
            sid = source.get('DefaultSubtitleStreamIndex')
        stream = next((s for s in source.get('MediaStreams') or [] if s.get('Type') == 'Subtitle' and s.get('Index') == sid), None)
        return bool(stream and (not stream.get('IsExternal')) and ((stream.get('Codec') or '').lower() in IMAGE_SUBTITLE_CODECS))

    def get_duration(self):
        source = self.media_source or {}
        ticks = source.get('RunTimeTicks')
        if ticks:
            return ticks / 10000000
        source_id = source.get('Id')
        if source_id is None or source_id == self.item_id:
            ticks = self.item.get('RunTimeTicks')
            if ticks:
                return ticks / 10000000

    def set_played(self, watched: bool=True):
        """Mark this item played/unplayed on the server, and locally.

        The local half matters for the same reason `mirror_playstate` does:
        an item can be *streamed* while a downloaded copy of it sits on
        disk, and that copy -- the one kept precisely for when the server is
        gone -- was the only thing that never heard about "Quit and Mark
        Unwatched". Deliberate marks are authoritative in both directions,
        so this goes through `mirror_watched` rather than the advance-only
        playback path; it is a no-op for an item nothing was downloaded for.
        `OfflineVideo` overrides this and keeps its own replay queue.
        """
        self.client.jellyfin.item_played(self.item_id, watched)
        try:
            from .sync.manager import syncManager
            from .clients import clientManager
            syncManager.mirror_watched(self.item_id, watched, server_uuid=clientManager.uuid_for_client(self.client))
        except Exception:
            log.debug('Could not mirror the watched mark for %s', self.item_id, exc_info=True)

    def set_streams(self, aid: Optional[int], sid: Optional[int]):
        """A deliberate pick from the menu or the HUD.

        Marked explicit, which is what stops language_config re-deciding it.
        A transcode restarts through `play()` and negotiates again, and the
        rule used to win that second pass -- so the stream carried the track
        the user asked for while `video.aid`, the HUD and the progress report
        all named the rule's. The user consciously overrode the rule; that is
        exactly what `explicit_tracks` means.
        """
        need_restart = False
        if aid is not None or sid is not None:
            self.explicit_tracks = True
        if aid is not None and self.aid != aid:
            self.aid = aid
            if self.is_transcode or aid in self.audio_external:
                need_restart = True
        if sid is not None and self.sid != sid:
            was_burned_in = self.sid in self.subtitle_enc
            self.sid = sid
            if sid in self.subtitle_enc or was_burned_in:
                need_restart = True
            if sid in self.subtitle_extracted and (not settings.extract_image_subtitles):
                need_restart = True
        return need_restart

    def get_playlist_id(self):
        return self.parent.queue[self.parent.seq]['PlaylistItemId']

class Media(object):

    def __init__(self, client: 'JellyfinClient_type', queue: list, seq: int=0, user_id: Optional[str]=None, aid: Optional[int]=None, sid: Optional[int]=None, srcid: Optional[str]=None, queue_override: bool=True, explicit_tracks: bool=False, queue_rev: Optional[int]=None):
        if queue_override:
            self.queue = [{'PlaylistItemId': 'playlistItem{0}'.format(get_seq()), 'Id': id_num} for id_num in queue]
        else:
            self.queue = queue
        self.client = client
        self.seq = seq
        self.queue_rev = get_seq() if queue_rev is None else queue_rev
        self.user_id = user_id
        self.video = build_video(self.queue[seq]['Id'], self, aid, sid, srcid, explicit_tracks=explicit_tracks)
        self.is_tv = self.video.is_tv if self.video is not None else False
        try:
            self.is_local = is_local_domain(client) if client is not None else True
        except Exception:
            self.is_local = True
        self.has_next = seq < len(queue) - 1
        self.has_prev = seq > 0

    def get_next(self):
        if self.has_next:
            return Media(self.client, self.queue, self.seq + 1, self.user_id, queue_override=False, queue_rev=self.queue_rev)

    def get_prev(self):
        if self.has_prev:
            return Media(self.client, self.queue, self.seq - 1, self.user_id, queue_override=False, queue_rev=self.queue_rev)

    def get_from_key(self, key: str):
        """Locate a queue entry by PlaylistItemId, else by item Id.

        PlaylistItemId first because it is the precise identifier: a queue
        may hold the same item twice, and matching on Id alone always jumps
        to the first copy. Id is still accepted — the websocket remote and
        the Tk browser both address entries that way."""
        if key is None:
            return None
        for i, video in enumerate(self.queue):
            if video.get('PlaylistItemId') == key:
                return Media(self.client, self.queue, i, self.user_id, queue_override=False, queue_rev=self.queue_rev)
        for i, video in enumerate(self.queue):
            if video['Id'] == key:
                return Media(self.client, self.queue, i, self.user_id, queue_override=False, queue_rev=self.queue_rev)
        return None

    def get_video(self, index: int):
        if index == 0 and self.video:
            return self.video
        if index < len(self.queue):
            return build_video(self.queue[index]['Id'], self)
        log.error("Media::get_video couldn't find video at index %s" % index)

    def publish_queue(self, queue, seq=None):
        """The one write of the queue after construction: the list, then
        ``seq`` where given, then a new ``queue_rev`` -- last, so a reader
        that sees the new revision also sees the queue it stands for.
        has_next/has_prev stay each caller's, whose orderings differ."""
        self.queue = queue
        if seq is not None:
            self.seq = seq
        self.queue_rev = get_seq()

    def insert_items(self, items, append: bool=False):
        items = [{'PlaylistItemId': 'playlistItem{0}'.format(get_seq()), 'Id': id_num} for id_num in items]
        if append:
            new_queue = self.queue + items
        else:
            new_queue = self.queue[0:self.seq + 1] + items + self.queue[self.seq + 1:]
        self.publish_queue(new_queue)
        self.has_next = self.seq < len(new_queue) - 1

    def replace_queue(self, sp_items, seq):
        """Swap in a different queue, keeping this Media if it can.

        Returns None when the entry at ``seq`` is the item already playing
        (updated in place), or a new Media when it is not.

        Written for SyncPlay, which is still its main caller, but not
        specific to it: PlayerManager._widen_queue_backwards uses it to
        prepend the episodes before a Next Up start (#650). What makes it
        the right tool for both is the ordering discipline below -- any
        publisher of a whole new queue needs it.
        """
        if self.queue[self.seq]['Id'] == sp_items[seq]['Id']:
            new_next = seq < len(sp_items) - 1
            new_prev = seq > 0
            self.has_next = self.has_next and new_next
            self.has_prev = self.has_prev and new_prev
            self.publish_queue(sp_items, seq)
            self.has_next = new_next
            self.has_prev = new_prev
            return None
        else:
            return Media(self.client, sp_items, seq, self.user_id, queue_override=False)

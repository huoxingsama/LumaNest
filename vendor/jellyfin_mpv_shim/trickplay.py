from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import threading
import os
import logging
from .conf import settings
from . import conffile
from .constants import APP_NAME
try:
    from . import bifdecode
    BIFDECODE_AVAILABLE = bifdecode.PIL_AVAILABLE
except ImportError:
    BIFDECODE_AVAILABLE = False
    bifdecode = None
log = logging.getLogger('trickplay')
MAX_OFFSET = 2 ** 31 - 1
IMG_PREFIX = 'raw_images'
IMG_SUFFIX = '.bin'
_seq_lock = threading.Lock()
_seq = 0

def _next_seq():
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq

def _img_path(seq):
    return conffile.get(APP_NAME, '%s.%d%s' % (IMG_PREFIX, seq, IMG_SUFFIX))

def _scale_arg():
    """`thumbnail_scale` as the last argument of both publish messages.

    "auto" rather than a resolved number: each consumer draws at its own
    scale, and only it knows which (docs/artwork-pipeline.md section 11.2).
    """
    value = settings.thumbnail_scale
    if value and value > 0:
        return '%g' % value
    return 'auto'
WINDOW_BUDGET_BYTES = 25 * 1024 * 1024
_pending_unlink = []
_pending_lock = threading.Lock()

def _unlink(path):
    """Best-effort removal. On Windows this fails while mpv holds the file
    mapped; the name is never reused, so the file is remembered and retried
    rather than left for the next run's cleanup_stale_files()."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        log.debug('Could not remove %s', path, exc_info=True)
        with _pending_lock:
            if path not in _pending_unlink:
                _pending_unlink.append(path)

def _sweep_unlinks():
    """Retry the removals that were refused earlier."""
    with _pending_lock:
        if not _pending_unlink:
            return
        paths, _pending_unlink[:] = (list(_pending_unlink), [])
    for path in paths:
        _unlink(path)

def cleanup_stale_files():
    """Remove frame files left behind by a previous run.

    They are unlinked on the way out normally, but a crash or a kill leaves
    them, and nothing else ever will — the names are unique per generation.
    """
    try:
        directory = os.path.dirname(_img_path(0))
        for name in os.listdir(directory):
            if not (name.startswith(IMG_PREFIX + '.') and name.endswith(IMG_SUFFIX)):
                continue
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                log.debug('Could not remove stale %s', name, exc_info=True)
        legacy = conffile.get(APP_NAME, IMG_PREFIX + IMG_SUFFIX)
        if os.path.isfile(legacy):
            os.remove(legacy)
    except Exception:
        log.debug('Could not clean up stale trickplay files.', exc_info=True)

class TrickPlay(threading.Thread):

    def __init__(self, player):
        self.trigger = threading.Event()
        self.halt = False
        self.player = player
        self._current = None
        self._file_lock = threading.Lock()
        self._want = 0.0
        self._bif = None
        self._window = None
        self._bad = []
        threading.Thread.__init__(self)
        self.daemon = True

    def stop(self, join=True):
        self.halt = True
        self.trigger.set()
        self._retire_current()
        if join:
            self.join()

    def fetch_thumbnails(self, position=0.0):
        """Load the window around ``position`` (seconds).

        The player passes where playback actually starts, which for a resumed
        item is not zero -- the first place the user is going to scrub is
        where they already are.
        """
        self._want = max(0.0, position or 0.0)
        self.trigger.set()

    def request_at(self, seconds):
        """A preview was asked for at ``seconds`` and the window does not
        cover it. Sent by the renderer (and by thumbfast.lua for the lua
        OSCs), which is the only side that knows where the pointer is.

        Runs on mpv's event thread -- see ``player._on_client_message`` --
        so it must not do anything but record and wake.
        """
        self._want = max(0.0, seconds or 0.0)
        self.trigger.set()

    def clear(self):
        self.player.script_message('shim-trickplay-clear')
        self._retire_current()
        self._bif = None
        self._window = None
        self._bad = []

    def _next_file(self):
        """A fresh path for the next set of frames. Never an existing one,
        for the life of the PROCESS -- see _next_seq."""
        return _img_path(_next_seq())

    def _publish(self, path):
        """Adopt `path` as the live frame file and retire the previous one.

        The previous file is dropped **one publish later**, not now:
        `script_message` returns when the message is queued, not when lua has
        run it, so a render timer firing in between still issues `overlay-add`
        against the OLD path (docs/artwork-pipeline.md section 11.4). One
        cycle of grace costs at most a second window on disk.
        """
        with self._file_lock:
            old, self._current = (self._current, path)
        _sweep_unlinks()
        if old:
            with _pending_lock:
                if old not in _pending_unlink:
                    _pending_unlink.append(old)

    def _retire_current(self):
        with self._file_lock:
            old, self._current = (self._current, None)
        _unlink(old)
        _sweep_unlinks()

    def _manifest(self, video):
        """This video's trickplay manifest, fetched once.

        ``get_bif`` re-requests the whole ITEM when ``Trickplay`` was not in
        the fields it was loaded with, so asking per window would put a round
        trip in front of every scrub.
        """
        cached = self._bif
        if cached is not None and cached[0] is video:
            return cached[1]
        data = video.get_bif(settings.thumbnail_preferred_size)
        self._bif = (video, data)
        return data

    @staticmethod
    def _frame_index(data, seconds):
        """Which frame of the WHOLE video ``seconds`` falls on.

        The cadence is the video's, not the window's -- ``Interval`` is
        milliseconds between thumbnails from the start of the file -- so
        this is the number every consumer computes and then rebases onto
        whatever part of it is loaded.
        """
        total = int(data['ThumbnailCount'])
        interval = max(1, int(data['Interval']))
        return max(0, min(int(seconds * 1000 // interval), total - 1))

    @staticmethod
    def _window_for(data, seconds):
        """``(first, count)`` -- the frames to put on disk for a preview at
        ``seconds``.

        The whole video when the user asked for it (fast mode) or when it
        fits under WINDOW_BUDGET_BYTES anyway, which is most short content
        and is why an ordinary episode never pays for windowing. Otherwise a
        budget's worth centred on the position, clamped to the video so the
        first and last few minutes are reachable without a window that runs
        off either end.
        """
        total = int(data['ThumbnailCount'])
        frame = int(data['Width']) * int(data['Height']) * 4
        if total <= 0 or frame <= 0:
            return (0, total)
        budget = max(1, WINDOW_BUDGET_BYTES // frame)
        if budget >= total:
            return (0, total)
        if settings.trickplay_fast_mode and (total - 1) * frame <= MAX_OFFSET:
            return (0, total)
        centre = TrickPlay._frame_index(data, seconds)
        first = max(0, min(centre - budget // 2, total - budget))
        return (first, budget)

    def _covers(self, video, frame):
        """Whether the last fetch already answered for ``frame`` of this video.

        Asked about the POSITION, not about the range a fresh window would
        have: the window is recentred on wherever it was asked for, so
        comparing ranges answers "no" for every request that is not dead
        centre and re-fetches the neighbouring frames of a file already on
        disk. This is what makes the window hysteretic -- one fetch buys
        half a window of scrubbing in either direction.

        The span recorded is the one that was ASKED for, not the number of
        frames that arrived. Those differ when the tile source ends early --
        in practice a partial download, since an online tile failure aborts
        the window rather than truncating it -- and the difference is a
        loop: the frames in the shortfall are inside the
        window every request for them produces, so answering "not covered"
        re-fetches, re-publishes, clears the consumer's one-ask-per-window
        marker, and is asked again on the very next frame -- at render
        cadence, for as long as the pointer sits there. Recording the
        attempt is what makes a second fetch pointless *and* silent. Nothing
        is lost by it: a re-fetch would come back equally short, and a new
        item or a clear() drops this outright.
        """
        for bad_video, first, asked in self._bad:
            if bad_video is video and first <= frame < first + asked:
                return True
        cur = self._window
        if cur is None or cur[0] is not video:
            return False
        _v, first, asked, _total = cur
        return first <= frame < first + asked

    def run(self):
        if not BIFDECODE_AVAILABLE:
            log.warning('Trickplay thumbnails disabled: Pillow (PIL) not available. Install with: pip install pillow')
            return
        while not self.halt:
            self.trigger.wait()
            self.trigger.clear()
            if self.halt:
                break
            try:
                log.info('Collecting trickplay images...')
                if not self.player.has_video():
                    continue
                video = self.player.get_video()
                want = self._want
                windowing = False
                path = None
                try:
                    data = self._manifest(video)
                    if not self.player.has_video() or video != self.player.get_video():
                        continue
                    if data and int(data.get('ThumbnailCount') or 0) > 0:
                        windowing = True
                        if self._covers(video, self._frame_index(data, want)):
                            log.debug('Trickplay window already loaded.')
                            continue
                        first, count = self._window_for(data, want)
                        per_tile = int(data['TileWidth']) * int(data['TileHeight'])
                        tile_first = first // per_tile
                        tile_count = (first + count - 1) // per_tile - tile_first + 1
                        path = self._next_file()
                        with open(path, 'wb') as fh:
                            written = bifdecode.decompress_tiles(data['Width'], data['Height'], data['TileWidth'], data['TileHeight'], count, video.get_hls_tile_images(data['Width'], tile_count, start=tile_first), fh, skip=first - tile_first * per_tile)
                        if not written:
                            log.warning('Trickplay produced no frames.')
                            _unlink(path)
                            continue
                        if written < count:
                            log.warning('Trickplay short: %d of %d frames.', written, count)
                        bif_meta = {'count': written, 'multiplier': data['Interval'], 'width': data['Width'], 'height': data['Height']}
                        if not self.player.has_video() or video != self.player.get_video():
                            _unlink(path)
                            continue
                        total = int(data['ThumbnailCount'])
                        self.player.script_message('shim-trickplay-bif', str(bif_meta['count']), str(bif_meta['multiplier']), str(bif_meta['width']), str(bif_meta['height']), path, str(first), str(total), _scale_arg())
                        self._window = (video, first, count, total)
                        self._publish(path)
                        log.info('Collected %d trickplay preview images (frames %d-%d of %d)', bif_meta['count'], first, first + written - 1, total)
                        continue
                    else:
                        log.warning('No trickplay data available')
                except BaseException as exc:
                    if path and path != self._current:
                        _unlink(path)
                    if windowing and isinstance(exc, bifdecode.BadTile):
                        self._bad = [b for b in self._bad if b[0] is video]
                        self._bad.append((video, first, count))
                        log.warning('Unusual trickplay tile %d for item %s at width %s: %s%s. Previews in frames %d-%d are skipped.', tile_first + exc.index, video.item_id, data['Width'], exc, '; it still divides into the %sx%s grid' % (data['TileWidth'], data['TileHeight']) if exc.on_grid else '', first, first + count - 1)
                        continue
                    if windowing:
                        log.error('Could not load a trickplay window.', exc_info=True)
                        continue
                    log.error('Could not get trickplay data.', exc_info=True)
                chapter_data = video.get_chapters()
                if chapter_data is None or len(chapter_data) == 0:
                    log.info('No chapters available')
                    continue
                path = self._next_file()
                with open(path, 'wb') as fh:
                    bif_meta = bifdecode.decompress_bif(video.get_chapter_images(settings.thumbnail_preferred_size), fh)
                if not self.player.has_video() or video != self.player.get_video():
                    _unlink(path)
                    continue
                self.player.script_message('shim-trickplay-chapters', str(bif_meta['width']), str(bif_meta['height']), str(path), ','.join((str(x['start']) for x in chapter_data)), _scale_arg())
                self._window = None
                self._publish(path)
                log.info(f'Collected {len(chapter_data)} chapter preview images')
            except:
                log.error('Could not get trickplay images', exc_info=True)

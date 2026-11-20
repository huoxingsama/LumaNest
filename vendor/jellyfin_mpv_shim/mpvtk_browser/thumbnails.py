"""Thumbnail fetching/caching for the mpvtk browser.

Pipeline (network + decode off the UI/loop thread):

  request() ── worker pool ──> download bytes / read disk ──> PIL decode+resize
            └─ result queue ─> pump() on the loop thread ─> callbacks(PIL.Image)

Unlike the Tk browser's store, this yields **PIL images** — the strip
compositor pastes them into row bitmaps — so nothing here is thread-affine
and there is no final ``ImageTk`` step. ``notify`` (thread-safe) is called
when a result lands so the owner can wake its loop (``MpvtkApp.invalidate``)
and drain via ``pump()`` on the next render.

The on-disk cache is a persistent artwork store keyed by ``(item, type, tag,
width)`` — the same store the offline-sync feature reuses, which is why it is
not just an in-memory LRU.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from personal_cinema.runtime_paths import option
import hashlib
import logging
import os
import shutil
from urllib.parse import urlparse
import queue
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import requests
from PIL import Image
from .. import imageutil
try:
    import pillow_jxl
except ImportError:
    pass
log = logging.getLogger('mpvtk_browser.thumbnails')
DEFAULT_MEM_MB = 96
DEFAULT_DISK_MB = 1024
SCRATCH_DISK_MB = 64

def disk_cache(app=None):
    """Where the artwork cache goes, and how much it may hold there.

    Persistent config dir by preference, RAM-backed scratch as a fallback --
    and **the budget follows the medium**, because a gigabyte of somebody's
    disk and a gigabyte of their RAM are not the same offer. Measures
    compressed bytes on disk, not decoded pixels.
    See docs/artwork-pipeline.md section 10.1.
    """
    from ..conffile import get_cache_dir
    from ..constants import APP_NAME
    path = get_cache_dir(app or APP_NAME, 'artwork')
    budget_mb = max(0, int(option('cache.image_gib', 2))) * 1024
    if path is not None:
        return (path, budget_mb)
    from ..mpvtk.rawimage import cache_dir
    log.info('No persistent cache directory; artwork will not be kept between launches.')
    return (cache_dir('mpvtk-thumbs-'), min(budget_mb, SCRATCH_DISK_MB))
THUMB_POOL_HOSTS = 4

def make_key(item_id, image_type, tag, width, height=None, fit=''):
    """Identity of one cached bitmap.

    ``fit`` distinguishes two crops of the same source at the same size --
    the server can be asked to fill the box or to fit inside it, and the two
    are different pictures. Empty by default so every existing key (and every
    file already on disk under one) is unchanged.
    """
    raw = '%s:%s:%s:%s:%s%s' % (item_id, image_type, tag, width, height, ':' + fit if fit else '')
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()

class MemoryCache:
    """Byte-bounded LRU of decoded images.

    Sizing is approximate (``sizer(value)``); least-recently-used entries are
    evicted until the total is back under budget. No UI dependency, so the
    eviction policy is unit-testable without a display.

    **An image the screen is using is never evicted to make room**, once
    :meth:`on_scene_pushed` has been heard from: "using" is read by this
    build or the one on screen, stamped by generation as StripStore does it.
    Without that, a screen whose art outgrew the budget evicted what the
    previous build drew with, and every repaint put a row back to
    placeholders while its images were fetched again -- the home screen
    flicker with many libraries. The budget is exceeded instead, by what is
    on screen; ``TileRenderer.window_rows`` is what keeps that bounded.
    """
    PROTECT_GENERATIONS = 1

    def __init__(self, max_bytes, sizer):
        self._max_bytes = max_bytes
        self._sizer = sizer
        self._items = OrderedDict()
        self._bytes = 0
        self._gen = 0
        self._marked = False

    def get(self, key):
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        item[2] = self._gen
        return item[0]

    def put(self, key, value):
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        nbytes = self._sizer(value)
        self._items[key] = [value, nbytes, self._gen]
        self._bytes += nbytes
        while self._bytes > self._max_bytes and len(self._items) > 1:
            head = next(iter(self._items.values()))
            if self._marked and head[2] >= self._gen - self.PROTECT_GENERATIONS:
                break
            _k, (_v, nb, _g) = self._items.popitem(last=False)
            self._bytes -= nb

    def on_scene_pushed(self):
        """A scene reached the renderer; advances what counts as in use."""
        self._gen += 1
        self._marked = True

    def trim(self, max_bytes):
        """Drop least-recently-used entries until the total is within
        ``max_bytes``. Unlike ``put``'s eviction this may empty the cache
        entirely -- it is called when the caller knows the entries have
        stopped being interesting, not when one more has to fit."""
        while self._bytes > max_bytes and self._items:
            _k, (_v, nb, _g) = self._items.popitem(last=False)
            self._bytes -= nb

    def __len__(self):
        return len(self._items)

    @property
    def nbytes(self):
        return self._bytes

def _fit_into(image, box):
    """Crop ``image`` to ``box``'s SHAPE, at the largest size it can manage.

    Cover-crop rather than ``thumbnail()``, and **it never upscales** -- a
    contain would blow a 16:9 still up 2.65x to fill a 200x300 poster tile.
    Pinned by tests/test_grid_artwork.py:271.
    See docs/artwork-pipeline.md section 10.4.
    """
    from ..imageutil import scale_to_cover
    bw, bh = (max(1, box[0]), max(1, box[1]))
    iw, ih = image.size
    if not iw or not ih:
        return image
    shrink = min(iw / bw, ih / bh)
    if shrink < 1.0:
        bw, bh = (max(1, int(bw * shrink)), max(1, int(bh * shrink)))
    if (iw, ih) == (bw, bh):
        return image
    return scale_to_cover(image, bw, bh)

def _image_bytes(image):
    """Approximate resident size of a decoded PIL image."""
    try:
        return image.width * image.height * len(image.getbands())
    except Exception:
        return 0

class ThumbnailStore:
    PRUNE_EVERY = 16 * 1024 * 1024
    LOW_DISK_SHARE = 0.05
    MIN_DISK_BYTES = 24 * 1024 * 1024
    MAX_AGE_SECS = 30 * 24 * 60 * 60

    def __init__(self, cache_dir, verify_ssl=True, max_mem_mb=DEFAULT_MEM_MB, max_disk_mb=DEFAULT_DISK_MB, workers=6, notify=None):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.verify_ssl = verify_ssl
        self.max_disk_bytes = max_disk_mb * 1024 * 1024
        self._notify = notify
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=THUMB_POOL_HOSTS, pool_maxsize=workers, pool_block=True)
        self._session.mount('http://', adapter)
        self._session.mount('https://', adapter)
        self._auth: dict = {}
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='thumb')
        self._results: 'queue.Queue[tuple[str, Optional[Image.Image]]]' = queue.Queue()
        self._mem = MemoryCache(max_mem_mb * 1024 * 1024, _image_bytes)
        self._pending = {}
        self._gone = set()
        self._lock = threading.Lock()
        self._closed = False
        self._unpruned = 0
        self._prune_lock = threading.Lock()
        self._startup_prune = self._pool.submit(self._prune_disk)

    def set_notify(self, notify):
        """Attach the wake-up callback after construction.

        The store outlives any one owner and is usually built before the
        thing that wants waking — the browser is handed a ready-made store —
        so the constructor argument alone isn't enough, and the browser used
        to reach in and assign ``_notify`` directly.

        A plain assignment: worker threads only ever read it, and swapping
        one callable for another is atomic.
        """
        self._notify = notify
    ROUTE_KEEP_BYTES = 16 * 1024 * 1024

    def trim_memory(self, max_bytes=None):
        """Shrink the decoded-image cache to ``max_bytes``.

        Decoded artwork is the cheapest thing this app can shed: it exists only
        to composite strips, and a screenful is ~7 MiB against ~400 KB on the
        wire. See docs/artwork-pipeline.md section 10.
        """
        self._mem.trim(self.ROUTE_KEEP_BYTES if max_bytes is None else max_bytes)

    def get_cached(self, key):
        return self._mem.get(key)

    def on_scene_pushed(self):
        """See MemoryCache.on_scene_pushed. Loop thread, like every other
        access to the decoded cache."""
        self._mem.on_scene_pushed()

    def is_gone(self, key):
        """True once the server has said this image doesn't exist, so the
        caller can stop re-requesting it (see ``request``)."""
        with self._lock:
            return key in self._gone

    def request(self, key, url, box, callback, cover=False):
        """Fetch image for `key` from `url`, resized to fit `box` (w, h).

        ``cover`` asks for the box FILLED rather than fitted -- see
        :func:`_fit_into`. The caller has to say, because whether artwork is
        cover-cropped is a property of the tile it is going into and not of
        the picture (``TileRenderer._contains``), and the key it hands over
        already distinguishes the two.

        `callback(image)` runs on the loop thread (via pump) when ready, with a
        decoded ``PIL.Image``. Multiple callers requesting the same key share
        one fetch but each get the image. A synchronous mem-cache hit calls back
        immediately.

        On failure the callback still runs, with ``image=None``, so the caller
        can release whatever dedup marker it holds and retry later — a fetch
        that fails silently used to leave the tile blank permanently. Use
        ``is_gone`` to tell a permanent miss from a retryable one.
        """
        cached = self.get_cached(key)
        if cached is not None:
            callback(cached)
            return
        with self._lock:
            if self._closed:
                return
            waiters = self._pending.get(key)
            if waiters is not None:
                waiters.append(callback)
                return
            self._pending[key] = [callback]
        self._pool.submit(self._work, key, url, box, cover)

    def cancel(self, key, callback=None):
        """Drop a pending fetch (or a single waiter of one).

        A queued ``_work`` whose key is no longer pending short-circuits before
        downloading, so a fast-scrolled backlog can't delay the next view's
        artwork. If other callers still await the same key, only the given
        callback is removed and the fetch continues.
        """
        with self._lock:
            waiters = self._pending.get(key)
            if waiters is None:
                return
            if callback is not None:
                try:
                    waiters.remove(callback)
                except ValueError:
                    pass
                if waiters:
                    return
            self._pending.pop(key, None)

    def pump(self):
        """Drain finished work and deliver decoded images. Call from the loop.

        Returns True if it delivered at least one image (so the caller can
        decide whether a re-render is warranted)."""
        delivered = False
        while True:
            try:
                key, image = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                callbacks = self._pending.pop(key, [])
            if image is not None:
                self._mem.put(key, image)
            for cb in callbacks:
                try:
                    cb(image)
                    delivered = delivered or image is not None
                except Exception:
                    log.debug('Thumbnail callback failed', exc_info=True)
        return delivered

    def shutdown(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self._session.close()
        except Exception:
            pass

    def _work(self, key, url, box, cover=False):
        with self._lock:
            if key not in self._pending:
                return
        try:
            image = self._load_image(key, url, box, cover)
        except Exception as e:
            log.debug('Thumbnail load failed: %s', url, exc_info=True)
            image = None
            resp = getattr(e, 'response', None)
            status = getattr(resp, 'status_code', None)
            if isinstance(e, imageutil.TooLarge) or (status is not None and 400 <= status < 500):
                with self._lock:
                    self._gone.add(key)
        self._results.put((key, image))
        if self._notify is not None:
            try:
                self._notify()
            except Exception:
                log.debug('Thumbnail notify failed', exc_info=True)

    def _load_image(self, key, url, box, cover=False):
        cached = fetched = path = None
        if url.startswith('http://') or url.startswith('https://'):
            path = os.path.join(self.cache_dir, key + '.img')
            data = cached = self._read_cached(path)
            if data is None:
                data = fetched = self._fetch(url)
        else:
            with open(url, 'rb') as fh:
                data = fh.read()
        try:
            image: Image.Image = imageutil.decode(data, box=box)
        except Exception:
            if cached is not None:
                self._drop_cached(path)
            raise
        if cached is not None:
            self._touch_cached(path)
        elif fetched is not None:
            self._persist(path, fetched)
        if image.mode in ('RGBA', 'LA', 'PA') or 'transparency' in image.info:
            image = image.convert('RGBA')
        else:
            image = image.convert('RGB')
        if cover and image.mode != 'RGBA':
            image = _fit_into(image, box)
        else:
            image.thumbnail(box, Image.LANCZOS)
        if image.mode == 'RGBA':
            from ..imageutil import measure_transparency
            measure_transparency(image)
        return image

    def set_auth(self, origins):
        """``{origin: authorization-header}`` for the servers now connected.

        Replaced wholesale rather than merged: a server the user has just
        signed out of must stop receiving its old token.
        """
        self._auth = dict(origins or {})

    def _headers_for(self, url):
        """Headers for an artwork request: our user agent always, and the
        server's ``Authorization`` when the url belongs to a server we are
        signed in to.

        **Per origin, and as a header rather than a query-string token** -- a
        token in the query string breaks a caching proxy, and it lands in
        anybody's access log. See docs/artwork-pipeline.md section 10.6.
        """
        from ..constants import USER_AGENT
        headers = {'User-Agent': USER_AGENT}
        try:
            parts = urlparse(url)
        except Exception:
            return headers
        header = self._auth.get((parts.scheme, parts.hostname, parts.port))
        if header:
            headers['Authorization'] = header
        return headers

    def _read_cached(self, path):
        """The cached bytes at ``path``, or None to fetch."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'rb') as fh:
                return fh.read()
        except OSError:
            return None

    def _touch_cached(self, path):
        try:
            os.utime(path, None)
        except OSError:
            pass

    def _drop_cached(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _fetch(self, url):
        resp = self._session.get(url, timeout=(5, 20), verify=self.verify_ssl, headers=self._headers_for(url))
        resp.raise_for_status()
        return resp.content

    def _persist(self, path, data):
        """Write ``data`` to the cache -- only ever after it has decoded."""
        if self.max_disk_bytes <= 0 or len(data) > self.max_disk_bytes:
            return
        tmp = '%s.%d.%d.tmp' % (path, os.getpid(), threading.get_ident())
        written = False
        try:
            with open(tmp, 'wb') as fh:
                fh.write(data)
            os.replace(tmp, path)
            written = True
        except OSError:
            log.debug('Could not write thumbnail cache %s', path, exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
        if written:
            self._note_written(len(data))

    def _note_written(self, nbytes):
        """Count bytes towards the next prune, and prune when they add up.

        The budget used to be applied once, in ``__init__``, against a
        directory that had just been created -- so it measured an empty dir,
        found nothing to do, and the cache then grew without limit for the
        whole session. A long browse writes every poster and backdrop it
        ever fetched, which on a RAM-backed cache dir is a session filling
        the tmpfs the rest of the desktop shares (#3).

        Counting writes rather than pruning per write, because a prune
        stats the whole directory: at PRUNE_EVERY it happens a few times
        per browsing session instead of a few times per screen.
        """
        with self._lock:
            self._unpruned += nbytes
            due = self._unpruned >= self.PRUNE_EVERY
            if due:
                self._unpruned = 0
        if due:
            self._prune_disk()

    def _disk_budget(self, held):
        """The cache's ceiling right now, in bytes, given the ``held`` we
        are already using.

        Usually ``max_disk_bytes``. But free space is not ours alone --
        something else can take it while we are running, and the configured
        budget then describes a cache that no longer fits. So the ceiling is
        also never more than LOW_DISK_SHARE of what is available, and the
        smaller of the two wins. A cache that shrinks as a disk fills gives
        space back rather than merely stopping where it is, which matters
        because somebody else filling it is the common case and holding
        still does not help them.

        MIN_DISK_BYTES is the floor -- a cache too small to hold one
        screenful re-fetches every tile on every scroll, which is worse for
        everyone than the megabytes it saves -- but not an unconditional
        one: on a filesystem where even that is a quarter of everything
        left, the floor is the rudeness, so it gives way too.
        """
        try:
            free = shutil.disk_usage(self.cache_dir).free
        except OSError:
            return self.max_disk_bytes
        room = max(0, held + free)
        share = int(room * self.LOW_DISK_SHARE)
        floor = min(self.MIN_DISK_BYTES, room // 4)
        return min(self.max_disk_bytes, max(share, floor))

    def _prune_disk(self, now=None):
        """One pruner at a time; the work is in _prune_disk_locked.

        Two workers -- or two app instances sharing the persistent cache --
        each scan, each compute the same total, and each delete towards it.
        But a file the other already removed raises ENOENT, so without this
        the loser never decremented its own running total and kept deleting:
        between them they evicted roughly twice the excess.
        """
        if not self._prune_lock.acquire(blocking=False):
            return
        try:
            self._prune_disk_locked(now)
        finally:
            self._prune_lock.release()

    def _prune_disk_locked(self, now=None):
        """Reap by age, then by size. Both, because they answer different
        questions: the size bound decides what a busy cache may keep, and
        the age bound decides what an idle one is still *for* (see
        MAX_AGE_SECS -- a cache pinned at its ceiling by artwork nobody can
        reach any more is a cache doing no good at its full price)."""
        now = now or time.time()
        try:
            entries = []
            total = 0
            for name in os.listdir(self.cache_dir):
                full = os.path.join(self.cache_dir, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if now - st.st_mtime > self.MAX_AGE_SECS:
                    try:
                        os.remove(full)
                        continue
                    except OSError:
                        pass
                entries.append((st.st_mtime, st.st_size, full))
                total += st.st_size
            budget = self._disk_budget(total)
            if total <= budget:
                return
            log.debug('Thumbnail cache at %d MiB, pruning to %d MiB', total // (1024 * 1024), budget // (1024 * 1024))
            entries.sort()
            for _mtime, size, full in entries:
                if total <= budget:
                    break
                try:
                    os.remove(full)
                    total -= size
                except FileNotFoundError:
                    total -= size
                except OSError:
                    pass
        except OSError:
            log.debug('Disk cache prune failed', exc_info=True)

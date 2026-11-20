from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from io import BytesIO
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    Image = None

class BadTile(ValueError):
    """A tile that is not the mosaic its manifest describes: the wrong size,
    or not a decodable image. Deterministic -- fetching it again gets the
    same bytes -- which is what separates it from a network failure.

    ``index`` counts the tiles this call pulled, from 0; ``size`` is what
    arrived (None when it would not even open) and ``expected`` the mosaic
    the manifest promised. ``on_grid`` says whether ``size`` still divides
    into the manifest's grid, the signature of tiles left in one directory by
    two generations of the server's extraction (docs/jellyfin-api-notes.md).
    """

    def __init__(self, index, size, expected, grid, cause=None):
        self.index, self.size, self.expected = (index, size, expected)
        self.on_grid = bool(size) and (size[0] % grid[0] == 0 and size[1] % grid[1] == 0)
        got = '%dx%d' % size if size else 'unreadable'
        super().__init__('tile %d is %s, expected %dx%d%s' % (index, got, expected[0], expected[1], ' (%s)' % cause if cause else ''))

def decompress_tiles(width, height, tile_width, tile_height, count, tiles, fh, skip=0):
    """Write up to `count` BGRA frames from `tiles` into `fh`.

    `skip` drops that many frames off the FRONT of the stream without
    writing them. The server hands trickplay out one mosaic per
    `tile_width * tile_height` thumbnails, so a caller that wants a window
    of the video has to fetch whole tiles either way -- but a decoded frame
    is `width * height * 4` bytes and a tile of them is tens of megabytes,
    which is the half worth not keeping. `skip` is what lets the file hold
    the frames the window asked for rather than everything the tiles it
    came in happened to carry.

    Returns the number of frames ACTUALLY written, which can be fewer than
    `count` when the tile source ends early. Which sources do that is worth
    being precise about: `OfflineVideo.get_hls_tile_images` returns on a
    missing file, so a partial download runs short here, but `media.Video`'s
    RAISES -- an online tile failure aborts the whole window in
    `TrickPlay.run` and never reaches this return.

    The caller must report this number rather than the one it asked for. A
    consumer seeks to `frame * width * height * 4` inside the file, so an
    over-reported count is a read past the end of it: a failed `overlay-add`
    on a current mpv, and a SIGBUS in the mpv process on one old enough to
    still mmap what it is handed (docs/artwork-pipeline.md section 11).
    """
    if not PIL_AVAILABLE:
        raise ImportError('Pillow (PIL) is required for trickplay thumbnails. Install with: pip install pillow')
    seen = 0
    image_count = 0
    tiles = iter(tiles)
    expected = (width * tile_width, height * tile_height)
    grid = (tile_width, tile_height)
    index = -1
    while image_count < count:
        try:
            image = next(tiles)
        except StopIteration:
            break
        index += 1
        try:
            image = Image.open(BytesIO(image))
        except Exception as exc:
            raise BadTile(index, None, expected, grid, exc) from exc
        rows = image.size[1] // height
        if image.size != expected and (not (image.size[0] == expected[0] and image.size[1] == rows * height and (0 < rows < tile_height))):
            raise BadTile(index, image.size, expected, grid)
        for y in range(tile_height):
            for x in range(tile_width):
                if image_count >= count:
                    return image_count
                if y >= rows:
                    raise BadTile(index, image.size, expected, grid)
                if seen < skip:
                    seen += 1
                    continue
                seen += 1
                image_count += 1
                try:
                    frame = image.crop((x * width, y * height, (x + 1) * width, (y + 1) * height))
                    pixels = frame.convert('RGBA').tobytes('raw', 'BGRA')
                except Exception as exc:
                    raise BadTile(index, image.size, expected, grid, exc) from exc
                fh.write(pixels)
    return image_count

def decompress_bif(images, fh):
    if not PIL_AVAILABLE:
        raise ImportError('Pillow (PIL) is required for trickplay thumbnails. Install with: pip install pillow')
    height = None
    width = None
    image_count = 0
    for image in images:
        image_count += 1
        image = Image.open(BytesIO(image)).convert('RGBA')
        if height is None:
            height = image.height
            width = image.width
        elif height != image.height or width != image.width:
            raise ValueError('BIF image sizes mismatch.')
        r, g, b, a = image.split()
        image = Image.merge('RGBA', (b, g, r, a))
        fh.write(image.tobytes())
    return {'count': image_count, 'height': height, 'width': width}

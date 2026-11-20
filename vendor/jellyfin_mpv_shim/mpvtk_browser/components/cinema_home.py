"""A native cinema hero with fixed artwork and a clear scrolling foreground.

mpv bitmaps sit above ASS text. Separate artwork and foreground ImageMaps keep
the artwork anchored while the title and controls move with the content, with
ordinary renderer hit regions for keyboard, mouse and remote controls.
The page supplies complete, already-authorized movie DTOs; this module starts
no data queries or workers and changes no global theme. Appearance state is
kept on that page's route so its background can use the same ambient colour.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
import math
from ...mpvtk import pilfont
from ...mpvtk.layout import SCROLLBAR_W
from ...mpvtk.scaling import dip, px, raster
from ...mpvtk.widgets import Box, CinemaBackdrop, ImageMap, Stack, Text, reduced_motion
from .. import theme
from ..thumbnails import make_key
from . import chrome, detail
HERO_ART_REVISION = 'landscape-subject-centered-v3'

def hero_image_key(spec, width):
    """Invalidate old hero/prewarm pixels without touching ordinary posters."""
    return make_key(*spec, width, fit=HERO_ART_REVISION)

def _box(size):
    width = max(1, int(size[0] - SCROLLBAR_W))
    height = min(860, max(350, int(size[1] * 0.72)))
    if width < 680:
        height = min(700, max(400, int(size[1] * 0.75)))
    if size[1] < 480:
        height = min(height, max(200, int(size[1] * 0.7)))
    return (width, height)

def artwork_fetch_width(tiles, box):
    """Fetch enough real artwork for the display plus modest elastic zoom."""
    return tiles._banner_fetch_w(min(4096, max(1920, round(raster(*box)[0] * 1.25))))

def _wrap(draw, text, font, width, maximum):
    """Bounded wrapping that works for Chinese without whitespace as well."""
    text = ' '.join(str(text or '').split())
    if not text or width <= 0 or maximum <= 0:
        return []
    lines = []
    current = ''
    for char in text:
        candidate = current + char
        if current and pilfont.text_length(draw, candidate, font) > width:
            lines.append(current.rstrip())
            current = char.lstrip()
            if len(lines) == maximum:
                tail = lines[-1]
                while tail and pilfont.text_length(draw, tail + '…', font) > width:
                    tail = tail[:-1]
                lines[-1] = tail.rstrip() + '…'
                return lines
        else:
            current = candidate
    if current and len(lines) < maximum:
        lines.append(current.rstrip())
    return lines

def _rgb(value, alpha=255):
    value = str(value).lstrip('#')
    return tuple((int(value[index:index + 2], 16) for index in (0, 2, 4))) + (alpha,)

def blur_step(offset, height):
    """Quantize blur work independently of the moving artwork boundary."""
    try:
        return min(12, max(0, int(float(offset) / max(1, float(height)) * 12)))
    except (TypeError, ValueError, OverflowError):
        return 0

def ambient_color(page, fallback=None):
    return (getattr(page, 'route', {}) or {}).get('_cinema_ambient') or fallback or theme.WINDOW_BG

def image_ambient(image, spec, page):
    from .cinema_composition import choose_ambient
    route = getattr(page, 'route', None)
    if image is None:
        return theme.WINDOW_BG
    cache = route.setdefault('_cinema_ambient_cache', {}) if isinstance(route, dict) else {}
    key = str(spec)
    if key not in cache:
        cache[key] = choose_ambient(image)
        if len(cache) > 8:
            cache.pop(next(iter(cache)))
    return cache[key]

def select_hero_artwork(page, item, box, spec, image, poster):
    """Keep the chosen landscape source; a similar poster is not a replacement.

    Poster lettering and cropped compositions can resemble a backdrop while
    still duplicating the separately drawn Logo. Both pages use the original
    Backdrop whenever available, and only their explicit missing-art fallback
    may supply Primary. Preserve the full source for elastic pull-to-reveal.
    """
    from .subject_framing import attach_reviewed_profile
    reviewed = attach_reviewed_profile(image, item.get('Id'), spec)
    return (image, (HERO_ART_REVISION, reviewed))

def _current_item(page, item):
    """A playback callback reads refreshed watched progress when clicked."""
    data = (getattr(page, 'route', {}) or {}).get('_data') or {}
    return next((current for current in data.get('featured', []) if current.get('Id') == item.get('Id')), item)

def _hero_geometry(box, count):
    from .cinema_presentation import geometry
    return geometry(box, count, home=True)

def prepare_background_layers(image, box, ambient):
    from PIL import Image
    from .cinema_composition import fit_background, framing_center_y
    from .subject_framing import subject_profile
    physical = raster(*box)
    width, height = physical
    profile = subject_profile(image) if image is not None else {}
    focal_y = framing_center_y(image, physical, profile)
    base = Image.new('RGBA', physical, _rgb(ambient)) if image is None else fit_background(image, physical, focal_y)
    work_w = min(960, width)
    work_h = max(1, round(height * work_w / width))
    work = base.resize((work_w, work_h), Image.Resampling.BILINEAR) if image is not None else None
    tone = _rgb(ambient)[:3]
    bottom = Image.new('L', (1, height))
    blend = [max(0, min(1, (y / max(1, height - 1) - 0.42) / 0.575)) for y in range(height)]
    bottom.putdata([round(255 * (p * p * (3 - 2 * p)) ** 1.5) for p in blend])
    scrim = Image.new('RGBA', physical, tone + (0,))
    scrim.putalpha(bottom.resize(physical))
    return {'base': base, 'work': work, 'scrim': scrim, 'physical': physical, 'source': image, 'focal_y': focal_y, 'subject_box': profile.get('subject_box')}

def prepare_background(image, box, ambient, step=0, prepared=None):
    """Return a shaded background only; text must be composited afterwards."""
    from PIL import Image, ImageFilter
    layers = prepared if prepared is not None else prepare_background_layers(image, box, ambient)
    if layers['work'] is not None and step:
        work = layers['work']
        width = layers['physical'][0]
        canvas = work.filter(ImageFilter.GaussianBlur(px(20) * step / 12 * work.width / width))
        canvas = canvas.resize(layers['physical'], Image.Resampling.BILINEAR)
    else:
        canvas = layers['base'].copy()
    canvas.alpha_composite(layers['scrim'])
    return canvas

def _scroll_frame(offset, height):
    """The background boundary moves with content; its artwork origin does not."""
    try:
        offset = float(offset)
        if not math.isfinite(offset):
            offset = 0.0
        offset = min(float(height), offset)
    except (TypeError, ValueError, OverflowError):
        offset = 0.0
    return (max(0, round(height - offset)), blur_step(offset, height))

def prepare_fixed_background(prepared, ambient, visible_height, step=0):
    """Crop a fixed composition, fading to the surface at its moving boundary."""
    from PIL import Image, ImageFilter
    full_w, full_h = prepared['physical']
    visible_h = max(1, px(visible_height))
    if visible_h > full_h:
        from .cinema_composition import fit_background
        source = prepared.get('source')
        base = fit_background(source, (full_w, visible_h), prepared.get('focal_y', 0.5), reference_size=(full_w, full_h)) if source is not None else Image.new('RGBA', (full_w, visible_h), _rgb(ambient))
        step = 0
    else:
        base = None
    cached = prepared.get('_fixed_blur')
    if base is not None:
        pass
    elif cached is None or cached[0] != step:
        if prepared['work'] is not None and step:
            work = prepared['work']
            base = work.filter(ImageFilter.GaussianBlur(px(20) * step / 12 * work.width / full_w))
            base = base.resize((full_w, full_h), Image.Resampling.BILINEAR)
        else:
            base = prepared['base']
        prepared['_fixed_blur'] = (step, base)
    else:
        base = cached[1]
    canvas = base.crop((0, 0, full_w, visible_h))
    fade = round(178 * max(0, 1 - visible_h / full_h))
    mask = Image.new('L', (1, visible_h))
    mask.putdata([round(fade + (255 - fade) * max(0, min(1, (y / max(1, visible_h - 1) - 0.4) / 0.6)) ** 1.12) for y in range(visible_h)])
    tint = Image.new('RGBA', canvas.size, _rgb(ambient, 0))
    tint.putalpha(mask.resize(canvas.size))
    canvas.alpha_composite(tint)
    return canvas

def remember_background(page, key, layers, box, ambient, scroll_id, available):
    page.route['_cinema_immersive'] = bool(available)
    if available:
        page.route['_cinema_background'] = {'key': key, 'layers': layers, 'box': box, 'ambient': ambient, 'scroll_id': scroll_id}
    else:
        page.route.pop('_cinema_background', None)

def clear_background(page):
    page.route['_cinema_immersive'] = False
    page.route['_cinema_hero_pending'] = False
    page.route.pop('_cinema_background', None)
    page.route.pop('_cinema_scroll_frame', None)

def image_pending(tiles, key, image):
    if image is not None or not key:
        return False
    thumbs = getattr(getattr(tiles, 'art', None), 'thumbs', None)
    if thumbs is None:
        return False
    if thumbs is not None and thumbs.is_gone(key):
        return False
    return key not in getattr(tiles, '_img_retry', {})

def staged_layers(page, key, build):
    """Prepare complete immutable hero pixels off the UI thread, then publish."""
    route = page.route
    cache = route.setdefault('_cinema_prepared', {})
    if key in cache:
        return cache[key]
    if key in route.get('_cinema_layer_errors', {}):
        return False
    runner = getattr(page.ctx, 'run', None)
    if runner is None or not hasattr(runner, 'run'):
        return build()
    jobs = route.setdefault('_cinema_layer_jobs', {})
    if key in jobs or len(jobs) >= 2:
        return None
    epoch = runner.epoch
    jobs[key] = epoch

    def publish(layers):
        cache = route.setdefault('_cinema_prepared', {})
        cache[key] = layers

        def cost(value, seen):
            if id(value) in seen:
                return 0
            seen.add(id(value))
            if hasattr(value, 'getbands'):
                return value.width * value.height * len(value.getbands())
            if isinstance(value, dict):
                return sum((cost(v, seen) for v in value.values()))
            if isinstance(value, (list, tuple)):
                return sum((cost(v, seen) for v in value))
            return 0
        while len(cache) > 1 and (len(cache) > 3 or cost(cache, set()) > 192 * 1024 * 1024):
            cache.pop(next(iter(cache)))

    def release():
        if jobs.get(key) == epoch:
            jobs.pop(key, None)

    def failed(error):
        if runner.epoch != epoch:
            return
        errors = route.setdefault('_cinema_layer_errors', {})
        errors[key] = type(error).__name__
        while len(errors) > 8:
            errors.pop(next(iter(errors)))
    runner.run(build, publish, epoch, on_error=failed, always=release)
    return None

def finish_layers(layers, ambient):
    """Prewarm the exact clear-frame assets consumed by the next UI commit."""
    background = layers['background']
    prepare_sharp_source(background, ambient)
    foreground = background['scrim'].copy()
    foreground.alpha_composite(layers['foreground'])
    bounds = foreground.getchannel('A').getbbox() or (0, 0, 1, 1)
    compact = foreground.crop(bounds)
    layers.update(foreground=compact, composite_foreground=compact, foreground_bounds=bounds, foreground_origin=bounds[:2])
    background.pop('work', None)
    background.pop('scrim', None)
    return layers

def prepare_sharp_source(prepared, ambient):
    """One source geometry for both immediate display and the blur atlas."""
    cached = prepared.get('_motion_sharp')
    if cached is not None:
        return cached
    from PIL import Image
    source = prepared.get('source')
    full_w, full_h = prepared['physical']
    focal_x = 0.5
    if source is None:
        source = Image.new('RGBA', (32, 32), _rgb(ambient))
    sharp = source.convert('RGBA')
    sharp.thumbnail((min(3840, max(1920, round(full_w * 1.25))), 3840), Image.Resampling.LANCZOS)
    prepared['_motion_sharp'] = (sharp, focal_x)
    return (sharp, focal_x)

def _motion_assets(prepared, ambient):
    """Immutable sharp and early/late blur buffers, built once per art.

    Animation never calls this or Pillow. Lua crops/scales these buffers at
    display cadence; all frames in an atlas share one protected cache entry.
    """
    from PIL import Image, ImageFilter
    full_w, full_h = prepared['physical']
    sharp, focal_x = prepare_sharp_source(prepared, ambient)
    early = sharp.copy()
    early.thumbnail((min(1920, full_w), 1920), Image.Resampling.LANCZOS)
    small = sharp.copy()
    small.thumbnail((640, 640), Image.Resampling.LANCZOS)
    count = 24
    early_count = 3
    motion_budget = 100 * 1024 * 1024

    def total_bytes():
        return 4 * (sharp.width * sharp.height + early.width * early.height * early_count + small.width * small.height * (count - 1 - early_count))
    while early_count > 1 and total_bytes() > motion_budget:
        early_count -= 1
    early_atlas = Image.new('RGBA', (early.width, early.height * early_count))
    atlas = Image.new('RGBA', (small.width, small.height * (count - 1 - early_count)))
    display_scale = max(full_w / sharp.width, full_h / sharp.height)
    for index in range(1, count):
        image = early if index <= early_count else small
        radius = px(20) * image.width / sharp.width / display_scale
        blurred = image.filter(ImageFilter.GaussianBlur(radius * index / (count - 1)))
        if index <= early_count:
            early_atlas.paste(blurred, (0, (index - 1) * early.height))
        else:
            atlas.paste(blurred, (0, (index - 1 - early_count) * small.height))
    return {'sharp': sharp, 'blur_early': early_atlas, 'blur': atlas, 'early_count': early_count, 'early_frame_size': early.size, 'frame_size': small.size, 'count': count, 'focal_x': focal_x}

def _motion_buffers(page, record):
    if reduced_motion():
        sharp, focal_x = prepare_sharp_source(record['layers'], record['ambient'])
        key = 'cinema-static-' + HERO_ART_REVISION + '|' + record['key']
        return ([page.ctx.art.strips.bitmap(key, lambda: sharp)], [], focal_x)
    assets = record['layers'].get('_motion_assets')
    if assets is None:
        runner = getattr(page.ctx, 'run', None)
        if runner is not None and hasattr(runner, 'submit'):
            prepared = record['layers']
            sharp, focal_x = prepare_sharp_source(prepared, record['ambient'])
            if not prepared.get('_motion_pending'):
                prepared['_motion_pending'] = True

                def build_motion():
                    try:
                        result = _motion_assets(prepared, record['ambient'])
                        if (page.route.get('_cinema_background') or {}).get('key') == record['key']:
                            prepared['_motion_assets'] = result
                    finally:
                        prepared.pop('_motion_pending', None)
                        page.ctx.invalidate()
                runner.submit(build_motion)
            assets = {'sharp': sharp, 'count': 1, 'early_count': 0, 'early_frame_size': (1, 1), 'frame_size': (1, 1), 'focal_x': focal_x}
        else:
            assets = _motion_assets(record['layers'], record['ambient'])
            record['layers']['_motion_assets'] = assets
    prefix = 'cinema-motion-' + HERO_ART_REVISION + '|' + record['key']
    store = page.ctx.art.strips
    buffers = {part: store.bitmap(prefix + '|' + part, lambda part=part: assets[part]) for part in ('sharp', 'blur_early', 'blur') if part in assets}
    frames = [buffers['sharp']]
    iw, ih = assets['early_frame_size']
    for index in range(assets['early_count']):
        frames.append({**buffers['blur_early'], 'iw': iw, 'ih': ih, 'base': index * iw * ih * 4})
    iw, ih = assets['frame_size']
    for index in range(assets['count'] - 1 - assets['early_count']):
        frames.append({**buffers['blur'], 'iw': iw, 'ih': ih, 'base': index * iw * ih * 4})
    return (frames, [], assets['focal_x'])

def page_surface(page, children, scroll_id):
    """Keep artwork outside the scrolling tree and all clear content inside it."""
    surface = Box(children, bg=ambient_color(page), flex=1, align='stretch')
    record = page.route.get('_cinema_background')
    if not page.route.get('_cinema_immersive') or not record:
        return surface
    width, full_height = record['box']
    frames, masks, focal_x = _motion_buffers(page, record)
    background = CinemaBackdrop(frames, masks, scroll_id=scroll_id, focal_x=focal_x, focal_y=record['layers'].get('focal_y', 0.5), w=width, h=full_height, anchor='nw', id=scroll_id + '-cinema-background', mask_in_foreground=True)
    return Stack([background, surface], flex=1)

def scrolled(page, scroll_id, offset, maximum):
    page.ctx.art.scroll.on_scroll(scroll_id, offset, maximum, edges_only=offset <= 0)

def _foreground(box, item, index, count, geometry, logo=None):
    from .cinema_presentation import foreground
    return foreground(box, item, logo, index=index, count=count, home=True)

def foreground_surface(store, key, layers, box, regions, ident):
    """Keep the hero's layout, uploading only its nonempty foreground area.

    mpv copies bitmap data even for an overlay that only changes position.
    The empty upper half of an immersive hero must not be copied on every
    drag frame. Control rectangles remain inside the cropped image map.
    """
    image = layers['foreground']
    background = layers.get('background')
    if background is not None:
        image = layers.get('composite_foreground')
        if image is None:
            image = background['scrim'].copy()
            image.alpha_composite(layers['foreground'])
            layers['composite_foreground'] = image
    bounds = layers.get('foreground_bounds')
    if bounds is None:
        bounds = image.getchannel('A').getbbox() or (0, 0, 1, 1)
        layers['foreground_bounds'] = bounds
    left, top, right, bottom = bounds
    for region in regions:
        left = min(left, px(region['x']))
        top = min(top, px(region['y']))
        right = max(right, px(region['x'] + region['w']))
        bottom = max(bottom, px(region['y'] + region['h']))
    left = max(0, left)
    top = max(0, top)
    right = min(px(box[0]), right)
    bottom = min(px(box[1]), bottom)
    width, height = (dip(right - left), dip(bottom - top))
    origin_x, origin_y = layers.get('foreground_origin', (0, 0))
    bitmap = store.bitmap('compact-foreground-v2|' + key, lambda: image.crop((left - origin_x, top - origin_y, right - origin_x, bottom - origin_y)), lsize=(width, height))
    shifted = [{**region, 'x': region['x'] - dip(left), 'y': region['y'] - dip(top)} for region in regions]
    picture = ImageMap(bitmap['src'], bitmap['iw'], bitmap['ih'], regions=shifted, id=ident, v=bitmap.get('v', 0), w=width, h=height, anchor='nw', dx=dip(left), dy=dip(top))
    return Stack([picture], w=box[0], h=box[1])

def _compose(image, box, item, index, count, geometry, ambient, blur=0, prepared=None, foreground=None, logo=None):
    canvas = prepare_background(image, box, ambient, blur, prepared=prepared)
    canvas.alpha_composite(foreground if foreground is not None else _foreground(box, item, index, count, geometry, logo))
    return canvas

def preload_featured(page, size, items, current=0):
    """Warm current/next through the real render keys, without a second pipeline."""
    if not items:
        return
    for index in dict.fromkeys((current % len(items), (current + 1) % len(items))):
        render_featured(page, size, items, index, lambda _: None, prewarm=True)

def render_featured(page, size, items, index, on_change, prewarm=False):
    """Return the native hero; ``on_change`` receives an absolute slide index.

    The only image lookup uses the existing TileRenderer image store (including
    its bounded retries and repaint notification). It never fetches metadata.
    """
    if not items:
        if not prewarm:
            clear_background(page)
        return None
    index = int(index or 0) % len(items)
    item = items[index]
    if not isinstance(item, dict) or not item.get('Id'):
        if not prewarm:
            clear_background(page)
        return None
    art = page.ctx.art
    tiles = art.tiles
    source = page.ctx.source
    server = (getattr(page, 'route', {}) or {}).get('server') or page.ctx.server
    box = _box(size)
    geometry = _hero_geometry(box, len(items))
    spec = source.backdrop_spec(item) if server is not None else None
    primary_fallback = False
    if not spec and server is not None and (item.get('ImageTags') or {}).get('Primary'):
        spec = source.image_spec(item, 'Primary', raster(*box)[0], inherit=False)
        primary_fallback = bool(spec)
    image = None
    key = None
    if spec:
        owner, image_type, tag = spec
        fetch_w = artwork_fetch_width(tiles, box)
        key = hero_image_key(spec, fetch_w)
        url = source.image_url(server, owner, image_type, tag, fetch_w, fill=False) if primary_fallback else source.backdrop_url(server, item, width=fetch_w, fill=False)
        image = tiles._request_image(key, url, (fetch_w, fetch_w))
    from .cinema_presentation import logo_image
    logo_spec, logo = logo_image(page, item, box)
    logo_key = make_key(*logo_spec, min(1200, max(500, raster(*box)[0] // 2))) if logo_spec else None
    route = page.route
    if image_pending(tiles, key, image) or image_pending(tiles, logo_key, logo):
        if prewarm:
            return None
        route['_cinema_hero_pending'] = True
        return route.get('_cinema_visible_surface') or Box([], w=box[0], h=box[1])
    image, hero_variant = select_hero_artwork(page, item, box, spec, image, None)
    ambient = image_ambient(image, ('hero-background-v2', spec, hero_variant), page)
    content = [item.get('Id'), item.get('Type'), spec, hero_variant, logo_spec, logo is not None, item.get('CriticRating'), item.get('ExternalRatings'), box, raster(*box), index, len(items), item.get('Name'), detail.meta_line(item), chrome.overview(item)[:700], bool((item.get('UserData') or {}).get('PlaybackPositionTicks')), theme.ACCENT, theme.ACCENT_FG, ambient, theme.baked_text(px(20)), image is not None]
    layer_key = hashlib.sha256(json.dumps(content, ensure_ascii=False, default=str).encode()).hexdigest()
    layers = route.get('_cinema_layers')
    if not layers or layers.get('key') != layer_key:

        def build():
            return finish_layers({'key': layer_key, 'background': prepare_background_layers(image, box, ambient), 'foreground': _foreground(box, item, index, len(items), geometry, logo)}, ambient)
        layers = staged_layers(page, layer_key, build)
    if prewarm:
        return None
    if layers is False:
        route['_cinema_hero_pending'] = False
        return route.get('_cinema_visible_surface') or Box([Text(item.get('Name', ''), size='hero'), Text('海报暂不可用')], w=box[0], h=box[1])
    if layers is None:
        route['_cinema_hero_pending'] = True
        return route.get('_cinema_visible_surface') or Box([], w=box[0], h=box[1])
    route.update(_cinema_layers=layers, _cinema_ambient=ambient, _cinema_hero_height=box[1], _cinema_hero_pending=False)
    remember_background(page, layer_key, layers['background'], box, ambient, 'home', spec)

    def play():
        current = _current_item(page, item)
        server = (getattr(page, 'route', {}) or {}).get('server') or page.ctx.server
        if current.get('Type') == 'Series':
            page.ctx.actions.play_next_up(current['Id'], server)
            return
        position = (current.get('UserData') or {}).get('PlaybackPositionTicks') or 0
        page.ctx.actions.play(current, server, offset_ticks=position or None)
    callbacks = {'play': play, 'details': lambda: tiles.on_open(_current_item(page, item))}
    if len(items) > 1:
        callbacks['previous'] = lambda: on_change((index - 1) % len(items))
        callbacks['next'] = lambda: on_change((index + 1) % len(items))
        for number in range(len(items)):
            callbacks['dot-' + str(number)] = lambda number=number: on_change(number)
    regions = []
    for name, callback in callbacks.items():
        x, y, w, h = geometry['controls'][name]
        regions.append({'id': 'cinema-hero-' + name, 'x': x, 'y': y, 'w': w, 'h': h, 'radius': h / 2, 'on_click': callback, 'hover': {}})
    surface = foreground_surface(art.strips, 'cinema-hero|' + layer_key, layers, box, regions, 'cinema-hero')
    route['_cinema_visible_surface'] = surface
    return surface

"""Small Pillow-only artwork composition, evaluated once per cached image.

Artwork fills the hero with one horizontally centered proportional crop.
Static and animated layers share the same geometry; no duplicate image is
synthesized at either edge. Colour analysis only chooses the surface tint.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import colorsys
import math
from PIL import Image, ImageChops, ImageFilter, ImageStat
MAX_CROP_LOSS = 0.24
_SAMPLE_SIZE = (160, 96)

def _sample(image, size=_SAMPLE_SIZE):
    small = image.copy()
    small.thumbnail(size, Image.Resampling.BILINEAR)
    return small.convert('RGB')

def _axis_energy(image, horizontal):
    """Return a saliency projection along the axis that needs cropping."""
    small = _sample(image)
    width, height = small.size
    gray = small.convert('L')
    edge = gray.filter(ImageFilter.FIND_EDGES)
    rgb = list(small.getdata())
    gradients = list(edge.getdata())
    mean = ImageStat.Stat(small).mean
    entropy = {}
    block = 12
    for top in range(0, height, block):
        for left in range(0, width, block):
            tile = gray.crop((left, top, min(width, left + block), min(height, top + block)))
            entropy[left // block, top // block] = tile.entropy() / 8.0
    energy = [0.0] * (width if horizontal else height)
    for y in range(1, max(1, height - 1)):
        for x in range(1, max(1, width - 1)):
            index = y * width + x
            contrast = sum((abs(channel - average) for channel, average in zip(rgb[index], mean))) / 765
            value = 2.0 * gradients[index] / 255 + 0.4 * contrast + 0.2 * entropy[x // block, y // block]
            energy[x if horizontal else y] += value
    return energy

def _crop_origin(image, length, horizontal):
    energy = _axis_energy(image, horizontal)
    axis = image.width if horizontal else image.height
    span = max(1, min(len(energy), round(length / axis * len(energy))))
    if span >= len(energy):
        return max(0.0, (axis - length) / 2)
    prefix = [0.0]
    for value in energy:
        prefix.append(prefix[-1] + value)
    maximum = axis - length
    if not horizontal:
        maximum = min(maximum, axis * 0.02)
    last = min(len(energy) - span, max(0, round(maximum / axis * len(energy))))
    middle = last / 2
    total = prefix[-1]
    best = max(range(last + 1), key=lambda start: (prefix[start + span] - prefix[start] - total * 0.015 * abs(start - middle) / len(energy), -abs(start - middle)))
    return min(max(0.0, best / max(1, len(energy) - span) * (axis - length)), max(0.0, maximum))

def _focal_y(value):
    try:
        value = float(value)
        return min(1.0, max(0.0, value)) if math.isfinite(value) else 0.5
    except (TypeError, ValueError, OverflowError):
        return 0.5

def framing_center_y(image, physical_size, profile=None):
    """Place a known subject in the clear area, returning source-space center.

    This consumes a cached detector/review result; it does not infer people
    from texture. Missing evidence gives an ordinary centered cover crop.
    """
    profile = profile or {}
    focus = _focal_y(profile.get('focal_y', 0.5))
    if image is None or image.width <= 0 or image.height <= 0:
        return focus
    width, height = physical_size
    scale = max(width / image.width, height / image.height)
    span = min(float(image.height), height / scale)
    maximum = max(0.0, image.height - span)
    top = min(maximum, max(0.0, focus * image.height - span / 2))
    subject = profile.get('subject_box')
    if isinstance(subject, (tuple, list)) and len(subject) == 4:
        upper, lower = (_focal_y(subject[1]), _focal_y(subject[3]))
        if upper < lower:
            padding = span * 0.06
            low = max(0.0, lower * image.height + padding - span * 0.68)
            high = min(maximum, upper * image.height - padding)
            if low <= high:
                top = min(high, max(low, top))
    return (top + span / 2) / image.height

def fit_background(image, physical_size, focal_y=0.5, reference_size=None):
    """Fill the hero with one proportional cover crop.

    The caller owns caching. ``physical_size`` is the final raster size,
    independent of toolkit logical dimensions or display scaling.
    """
    size = tuple((max(1, int(value)) for value in physical_size))
    if image is None or image.width <= 0 or image.height <= 0:
        return Image.new('RGBA', size, (135, 119, 112, 255))
    width, height = size
    scale = max(width / image.width, height / image.height)
    source_w, source_h = (width / scale, height / scale)
    left = max(0.0, (image.width - source_w) / 2.0)
    ref_w, ref_h = reference_size or size
    ref_scale = max(ref_w / image.width, ref_h / image.height)
    ref_span = min(float(image.height), ref_h / ref_scale)
    ref_top = min(max(0.0, image.height - ref_span), max(0.0, _focal_y(focal_y) * image.height - ref_span / 2))
    center = ref_top + ref_span / 2
    top = min(max(0.0, image.height - source_h), max(0.0, center - source_h / 2))
    return image.resize(size, Image.Resampling.LANCZOS, box=(left, top, left + source_w, top + source_h)).convert('RGBA')

def choose_ambient(image):
    """A representative lower-image colour, softened into a midtone surface."""
    if image is None or image.width <= 0 or image.height <= 0:
        return '877770'
    lower = image.crop((0, int(image.height * 0.42), image.width, max(1, int(image.height * 0.96))))
    lower.thumbnail((80, 48), Image.Resampling.BILINEAR)
    if lower.mode == 'RGBA':
        pixels = [pixel[:3] for pixel in lower.getdata() if pixel[3] >= 64]
    else:
        pixels = list(lower.convert('RGB').getdata())
    if not pixels:
        return '877770'
    strip = Image.new('RGB', (len(pixels), 1))
    strip.putdata(pixels)
    quantized = strip.quantize(colors=6)
    palette = quantized.getpalette()
    candidates = []
    for count, index in quantized.getcolors():
        rgb = palette[index * 3:index * 3 + 3]
        hue, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
        if value < 0.16 or (value > 0.93 and saturation < 0.08):
            continue
        candidates.append((count * (0.7 + saturation * 0.7), hue, saturation, value))
    if not candidates:
        return '877770'
    _, hue, saturation, value = max(candidates)
    if saturation < 0.07:
        hue = 0.065
    softened = colorsys.hsv_to_rgb(hue, min(0.4, max(0.1, saturation * 0.55)), min(0.7, max(0.55, 0.52 + value * 0.18)))
    return ''.join((f'{round(channel * 255):02x}' for channel in softened))

def primary_extension_fraction(backdrop, primary):
    """Use a poster only when it proves the same scene has missing upper art.

    This compares small matching image crops, not faces or character identities.
    The returned upper crop excludes the unrelated poster footer/title block.
    """
    if backdrop is None or primary is None:
        return None
    aspect = backdrop.width / max(1, backdrop.height)
    if aspect < 1.25 or primary.width / max(1, primary.height) > 1.05:
        return None
    crop_h = primary.width / aspect
    if crop_h >= primary.height * 0.85:
        return None
    target = backdrop.convert('RGB')
    target.thumbnail((80, 56), Image.Resampling.BILINEAR)
    if sum(ImageStat.Stat(target).stddev) / 3 < 16:
        return None
    best = (1.0, 0)
    for step in range(21):
        top = (primary.height - crop_h) * step / 20
        candidate = primary.crop((0, round(top), primary.width, round(top + crop_h)))
        candidate = candidate.resize(target.size, Image.Resampling.BILINEAR).convert('RGB')
        difference = sum(ImageStat.Stat(ImageChops.difference(target, candidate)).mean) / (3 * 255)
        if difference < best[0]:
            best = (difference, top)
    if best[0] > 0.105:
        return None
    if best[1] < primary.height * 0.09:
        return 0.0
    return min(0.9, max(0.55, (best[1] + crop_h) / primary.height + 0.04))

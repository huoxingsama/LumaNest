"""Small antialiased bitmap shapes; callers cache the completed UI bitmap."""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import math

def _paint(canvas, bounds, painter, samples=4, resample=None):
    from PIL import Image, ImageDraw
    left = max(0, math.floor(bounds[0]) - 2)
    top = max(0, math.floor(bounds[1]) - 2)
    right = min(canvas.width, math.ceil(bounds[2]) + 3)
    bottom = min(canvas.height, math.ceil(bounds[3]) + 3)
    if right <= left or bottom <= top:
        return
    size = (right - left, bottom - top)
    layer = Image.new('RGBA', (size[0] * samples, size[1] * samples), (0, 0, 0, 0))

    def point(x, y):
        return ((x - left) * samples, (y - top) * samples)
    painter(ImageDraw.Draw(layer), point, samples)
    canvas.alpha_composite(layer.resize(size, Image.Resampling.LANCZOS if resample is None else resample), (left, top))

def rounded(canvas, box, radius, fill=None, outline=None, width=1, resample=None):

    def paint(draw, point, scale):
        draw.rounded_rectangle((*point(box[0], box[1]), *point(box[2], box[3])), radius=radius * scale, fill=fill, outline=outline, width=max(1, round(width * scale)))
    _paint(canvas, box, paint, resample=resample)

def ellipse(canvas, box, fill=None, outline=None, width=1):

    def paint(draw, point, scale):
        draw.ellipse((*point(box[0], box[1]), *point(box[2], box[3])), fill=fill, outline=outline, width=max(1, round(width * scale)))
    _paint(canvas, box, paint)

def polygon(canvas, points, fill):
    bounds = (min((p[0] for p in points)), min((p[1] for p in points)), max((p[0] for p in points)), max((p[1] for p in points)))
    _paint(canvas, bounds, lambda draw, point, scale: draw.polygon([point(*p) for p in points], fill=fill))

def line(canvas, points, fill, width=1):
    bounds = (min((p[0] for p in points)) - width, min((p[1] for p in points)) - width, max((p[0] for p in points)) + width, max((p[1] for p in points)) + width)
    _paint(canvas, bounds, lambda draw, point, scale: draw.line([point(*p) for p in points], fill=fill, width=max(1, round(width * scale)), joint='curve'))

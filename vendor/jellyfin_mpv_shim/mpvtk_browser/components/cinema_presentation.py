"""Centered, artwork-led presentation shared by native home and detail pages."""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import math
from functools import lru_cache
from ...mpvtk import pilfont
from ...mpvtk.scaling import px, raster
from .. import theme, cinema_paint
from ..thumbnails import make_key
from . import chrome
PLAY_HEIGHT = 48
PLAY_WIDTH = 156
PLAY_FONT_SIZE = 18
PLAY_ICON_SIZE = 16
PLAY_RADIUS = 24
PLAY_BG = 'faf8f5'
PLAY_FG = '202228'

def logo_image(page, item, box):
    """Request an actual wordmark; never substitute a poster for a missing logo."""
    source = page.ctx.source
    server = page.route.get('server') or page.ctx.server
    if server is None:
        return (None, None)
    spec = source.image_spec(item, 'Logo', 800)
    if not spec or spec[1] != 'Logo':
        return (None, None)
    width = min(1200, max(500, raster(*box)[0] // 2))
    owner, image_type, tag = spec
    image = page.ctx.art.tiles._request_image(make_key(owner, image_type, tag, width), source.image_url(server, owner, image_type, tag, width, fill=False), (width, max(240, width // 2)), cover=False)
    return (spec, image)

def number(value, maximum):
    try:
        result = float(value)
        return result if math.isfinite(result) and 0 <= result <= maximum else None
    except (TypeError, ValueError):
        return None

def rating_records(item):
    """Only explicit, scope-matched scores can carry an external source logo."""
    sources = {'imdb': ('IMDb', 'f5c518', 10), 'trakt': ('Trakt', 'a24ce8', 10), 'tomatoes': ('烂番茄影评人', 'fa333e', 100), 'audience': ('烂番茄观众', 'ffcd57', 100), 'metacritic': ('Metacritic', '66cc33', 100), 'letterboxd': ('Letterboxd', '00e054', 5), 'myanimelist': ('MyAnimeList', '2e51a2', 10), 'bangumi': ('Bangumi 本季', 'f4a6c0', 10)}
    scope = {'Movie': 'movie', 'Series': 'series', 'Season': 'season'}.get(item.get('Type'))
    matched = {}
    for entry in item.get('ExternalRatings') or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get('source')
        scale = entry.get('scale')
        if source not in sources or source in matched or (not scope) or (entry.get('scope') != scope):
            continue
        if scope == 'season' and entry.get('season') != item.get('IndexNumber'):
            continue
        if isinstance(scale, bool) or scale not in (5, 10, 100) or isinstance(entry.get('value'), bool):
            continue
        raw = number(entry.get('value'), scale)
        if raw is None:
            continue
        label, color, target = sources[source]
        score = raw / scale * target
        low = source in ('tomatoes', 'audience') and score < 60
        if source == 'metacritic':
            color = '66cc33' if score >= 61 else 'ffcc33' if score >= 40 else 'ff5555'
        value = f'{score:.1f}' if target in (5, 10) else f'{score:g}%' if source in ('tomatoes', 'audience') else f'{score:g}'
        matched[source] = {'label': label, 'value': value, 'color': '69c84a' if source == 'tomatoes' and low else color, 'kind': 'season_rating' if source == 'bangumi' else source, 'low': low, 'source': source, 'url': entry.get('url'), 'votes': entry.get('votes'), 'fetched_at': entry.get('fetched_at'), 'scope': scope}
    records = list(matched.values())
    if not records and scope != 'season':
        critics = number(item.get('CriticRating'), 100)
        if critics is not None:
            records.append({'label': '影评人', 'value': f'{critics:g}%', 'color': 'ffab7a', 'kind': 'critic'})
    return records

def _community_record(item):
    if item.get('Type') == 'Season' and item.get('SeasonRatingStatus') == 'no-independent-rating':
        return None
    value = number(item.get('CommunityRating'), 10)
    return {'label': '评分', 'value': f'{value:.1f}', 'color': 'ffd24a', 'kind': 'star'} if value is not None else None

def geometry(box, count=1, home=True):
    width, height = box
    narrow = width < 680
    compact = height < 420
    button_h = PLAY_HEIGHT
    play_w = PLAY_WIDTH
    detail_w = 110 if narrow else 132
    gap = 12
    total = play_w + detail_w + gap
    button_y = height - (51 if count > 1 else 24) - button_h
    controls = {}
    if home:
        controls['play'] = ((width - total) / 2, button_y, play_w, button_h)
        controls['details'] = ((width - total) / 2 + play_w + gap, button_y, detail_w, button_h)
        if count > 1:
            diameter = 34 if narrow else 42
            controls['previous'] = (14, height * 0.4, diameter, diameter)
            controls['next'] = (width - diameter - 14, height * 0.4, diameter, diameter)
            step = 26
            start = (width - count * step) / 2
            for index in range(count):
                controls[f'dot-{index}'] = (start + index * step, height - 32, step, 24)
    return {'margin': 26 if narrow else 48, 'controls': controls, 'logo_w': min(width * 0.58 if narrow else width * 0.32, 560), 'logo_h': min(150, height * 0.22), 'title_size': 24 if narrow else 30 if width < 1500 else 38, 'body_size': 14 if narrow else 17 if width < 1500 else 19, 'text_width': min(width - 2 * (26 if narrow else 48), 1660), 'overview_lines': 1 if compact else 2, 'bottom': button_y - 17 if home else height - 35, 'home': home, 'compact': compact}

def _font(size, text, bold=False):
    from ...imageutil import pil_font
    return pil_font(theme.baked_text(px(size)), bold=bold, text=str(text))

def _height(font):
    return sum(font.getmetrics())

def _text(draw, position, text, font, fill=(255, 255, 255, 255), shadow=True, anchor=None):
    x, y = position
    if shadow:
        pilfont.draw_text(draw, (x + px(1), y + px(1)), str(text), font, fill=(12, 15, 20, 145), anchor=anchor)
    pilfont.draw_text(draw, (x, y), str(text), font, fill=fill, anchor=anchor)

def _center(draw, width, y, text, font, fill=(255, 255, 255, 255)):
    _text(draw, ((width - pilfont.text_length(draw, str(text), font)) / 2, y), text, font, fill)

def _star(draw, center, radius, color, canvas=None):
    points = []
    for index in range(10):
        angle = -math.pi / 2 + math.pi * index / 5
        distance = radius if index % 2 == 0 else radius * 0.44
        points.append((center[0] + math.cos(angle) * distance, center[1] + math.sin(angle) * distance))
    if canvas is None:
        draw.polygon(points, fill=color)
    else:
        cinema_paint.polygon(canvas, points, fill=color)

def _rating_width(draw, record, font):
    label = (record['label'] + '  ' if record['kind'] in ('critic', 'season_rating') else '') + record['value']
    return _rating_lead(record, font) + pilfont.text_length(draw, label, font)

def _rating_icon_size(font):
    bounds = font.getbbox('0123456789.%', anchor='ls')
    return max(px(14), min(px(22), bounds[3] - bounds[1] + px(2)))

def _rating_line_height(font, text=''):
    bounds = font.getbbox('0123456789.%' + str(text), anchor='ls')
    return max(_rating_icon_size(font), bounds[3] - bounds[1])

def _rating_baseline(font, y, text=''):
    bounds = font.getbbox('0123456789.%' + str(text), anchor='ls')
    return y + (_rating_line_height(font, text) - (bounds[3] - bounds[1])) / 2 - bounds[1]

def _rating_lead(record, font):
    if record['kind'] == 'season_rating':
        return 0
    if record['kind'] in ('star', 'critic'):
        return _rating_icon_size(font) + px(6)
    size = _rating_icon_size(font)
    return _optical_source_icon(record['kind'], size, bool(record.get('low'))).width + px(6)

@lru_cache(maxsize=64)
def _source_icon(kind, size, low=False):
    """Small source marks, supersampled once and reused by cached foregrounds."""
    from PIL import Image, ImageDraw
    from ...imageutil import pil_font
    scale = 4
    width = round(size * 1.8) if kind in ('imdb', 'myanimelist') else size
    icon = Image.new('RGBA', (width * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    k = size * scale / 24

    def box(coords):
        return tuple((v * k for v in coords))

    def points(coords):
        return [(x * k, y * k) for x, y in coords]
    if kind == 'imdb':
        draw.rounded_rectangle((0, 2 * k, width * scale - 1, 22 * k), radius=2 * k, fill='#f5c518')
        font = pil_font(max(1, round(17 * k)), bold=True, text='IMDb')
        bbox = draw.textbbox((0, 0), 'IMDb', font=font)
        draw.text(((width * scale - (bbox[2] - bbox[0])) / 2 - bbox[0], (size * scale - (bbox[3] - bbox[1])) / 2 - bbox[1]), 'IMDb', font=font, fill='#171719')
    elif kind == 'trakt':
        draw.ellipse(box((1, 1, 23, 23)), fill='#a24ce8')
        draw.ellipse(box((4, 4, 20, 20)), outline='#f4e9ff', width=max(1, round(1.2 * k)))
        draw.line(points([(7, 12), (11, 16), (21, 6)]), fill='white', width=max(1, round(2.6 * k)), joint='curve')
    elif kind == 'myanimelist':
        draw.rounded_rectangle((0, 2 * k, width * scale - 1, 22 * k), radius=2 * k, fill='#2e51a2')
        font = pil_font(max(1, round(15 * k)), bold=True, text='MAL')
        bbox = draw.textbbox((0, 0), 'MAL', font=font)
        draw.text(((width * scale - (bbox[2] - bbox[0])) / 2 - bbox[0], (size * scale - (bbox[3] - bbox[1])) / 2 - bbox[1]), 'MAL', font=font, fill='white')
    elif kind == 'metacritic':
        draw.ellipse(box((1, 1, 23, 23)), fill='#15171a', outline='#e9e9e9', width=max(1, round(k)))
        font = pil_font(max(1, round(17 * k)), bold=True, text='m')
        bbox = draw.textbbox((0, 0), 'm', font=font)
        draw.text(((width * scale - (bbox[2] - bbox[0])) / 2 - bbox[0], (size * scale - (bbox[3] - bbox[1])) / 2 - bbox[1]), 'm', font=font, fill='white')
    elif kind == 'letterboxd':
        for cx, color in ((5, '#ff8000'), (12, '#00e054'), (19, '#40bcf4')):
            draw.ellipse(box((cx - 5, 7, cx + 5, 17)), fill=color)
    elif kind == 'tomatoes' and low:
        splat = [(12, 3), (15, 7), (21, 4), (20, 10), (24, 13), (18, 15), (20, 21), (14, 19), (11, 24), (8, 18), (2, 20), (4, 14), (0, 10), (7, 9), (6, 3)]
        draw.polygon(points(splat), fill='#69c84a')
        draw.ellipse(box((7, 9, 15, 16)), fill='#8bda61')
        draw.ellipse(box((1, 0, 4, 3)), fill='#69c84a')
    elif kind == 'tomatoes':
        draw.ellipse(box((2, 6, 22, 23)), fill='#fa333e')
        draw.ellipse(box((5, 8, 10, 12)), fill='#ff6870')
        draw.polygon(points([(12, 1), (14, 6), (21, 4), (16, 9), (17, 12), (12, 9), (6, 12), (8, 8), (3, 5), (10, 6)]), fill='#63b74a')
        draw.line(points([(12, 6), (14, 1)]), fill='#63b74a', width=max(1, round(2 * k)))
    elif kind == 'audience':
        popcorn = Image.new('RGBA', icon.size, (0, 0, 0, 0))
        pd = ImageDraw.Draw(popcorn)
        pd.polygon(points([(4, 9), (20, 9), (17, 23), (7, 23)]), fill='#fff4d5')
        pd.polygon(points([(5, 9), (8, 9), (10, 23), (7, 23)]), fill='#f4484d')
        pd.polygon(points([(11, 9), (14, 9), (14, 23), (11, 23)]), fill='#f4484d')
        pd.polygon(points([(18, 9), (20, 9), (17, 23), (16, 23)]), fill='#f4484d')
        for cx, cy, r in [(5, 8, 3), (9, 5, 3), (13, 7, 3), (16, 4, 3), (19, 8, 3), (12, 3, 3)]:
            pd.ellipse(box((cx - r, cy - r, cx + r, cy + r)), fill='#ffdf7e')
        if low:
            popcorn = popcorn.rotate(112, resample=Image.Resampling.BICUBIC)
        icon.alpha_composite(popcorn)
    return icon.resize((width, size), Image.Resampling.LANCZOS)

@lru_cache(maxsize=64)
def _optical_source_icon(kind, size, low=False):
    from PIL import Image, ImageOps
    icon = _source_icon(kind, size, low)
    bounds = icon.getchannel('A').getbbox()
    if bounds:
        icon = icon.crop(bounds)
    width = round(size * (2 if kind in ('imdb', 'myanimelist', 'letterboxd') else 1.2))
    return ImageOps.contain(icon, (max(1, width), max(1, size)), Image.Resampling.LANCZOS)

def _draw_rating(draw, x, y, record, font, canvas=None, baseline=None):
    color = tuple((int(record['color'][i:i + 2], 16) for i in (0, 2, 4))) + (255,)
    baseline = _rating_baseline(font, y) if baseline is None else baseline
    if record['kind'] == 'season_rating':
        _text(draw, (x, baseline), record['label'] + '  ' + record['value'], font, color, anchor='ls')
        return
    ink = font.getbbox('0123456789.%', anchor='ls')
    size = _rating_icon_size(font)
    center = (x + size / 2, baseline + (ink[1] + ink[3]) / 2)
    canvas = canvas if canvas is not None else getattr(draw, '_image', None)
    if record['kind'] in ('imdb', 'trakt', 'tomatoes', 'audience', 'metacritic', 'letterboxd', 'myanimelist'):
        icon = _optical_source_icon(record['kind'], size, bool(record.get('low')))
        if canvas is not None:
            canvas.alpha_composite(icon, (round(x), round(center[1] - icon.height / 2)))
        _text(draw, (x + _rating_lead(record, font), baseline), record['value'], font, color if record['kind'] == 'metacritic' else (255, 255, 255, 255), anchor='ls')
        return
    if record['kind'] == 'star':
        _star(draw, center, size * 0.48, color, canvas)
        label = record['value']
    else:
        top = center[1] - size / 2
        box = (x, top, x + size, top + size)
        lines = [[(x + size * 0.25, top + size * 0.35), (x + size * 0.75, top + size * 0.35)], [(x + size * 0.25, top + size * 0.6), (x + size * 0.65, top + size * 0.6)]]
        if canvas is None:
            draw.rounded_rectangle(box, radius=px(4), fill=color)
            for points in lines:
                draw.line(points, fill=(88, 40, 20, 255), width=max(1, px(2)))
        else:
            cinema_paint.rounded(canvas, box, radius=px(4), fill=color)
            for points in lines:
                cinema_paint.line(canvas, points, fill=(88, 40, 20, 255), width=max(1, px(2)))
        label = record['label'] + '  ' + record['value']
    _text(draw, (x + _rating_lead(record, font), baseline), label, font, anchor='ls')

def _metadata(item):
    parts = []
    year = item.get('ProductionYear')
    if year:
        end = str(item.get('EndDate') or '')[:4]
        parts.append(str(year) + (' – ' + end if end.isdigit() and end != str(year) else ''))
    genres = (item.get('Genres') or [])[:3]
    if genres:
        parts.append('、'.join(genres))
    if item.get('OfficialRating'):
        parts.append(str(item['OfficialRating']))
    if item.get('Type') == 'Season' and item.get('SeasonRatingStatus') == 'no-independent-rating':
        parts.append('本季暂无独立评分')
    return '  |  '.join(parts)

def foreground(box, item, logo=None, *, index=0, count=1, home=True, title=None, context=None):
    from PIL import Image, ImageDraw, ImageOps
    from .cinema_home import _wrap
    g = geometry(box, count, home)
    canvas = Image.new('RGBA', raster(*box), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size
    title = title or item.get('Name') or '未命名作品'
    title_font = _font(g['title_size'], title, True)
    body_font = _font(g['body_size'], '作品简介')
    meta = _metadata(item)
    meta_font = _font(14 if box[0] < 680 else 17, meta)
    rating_font = _font(16 if box[0] < 680 else 19, '评分', True)
    context_font = _font(13 if box[0] < 680 else 16, context or '')
    synopsis = chrome.overview(item)[:600] if home else ''
    descriptions = _wrap(draw, synopsis, body_font, px(g['text_width']), g['overview_lines'])
    community = _community_record(item)
    community_width = _rating_width(draw, community, meta_font) if community else 0
    meta_gap = px(18) if community and meta else 0
    metadata = _wrap(draw, meta, meta_font, max(px(40), px(g['text_width']) - community_width - meta_gap), 1)
    records = rating_records(item)
    rating_gap = px(14 if box[0] < 680 else 22)
    for rating_size in range(16 if box[0] < 680 else 19, 10, -1):
        rating_font = _font(rating_size, '评分', True)
        rating_gap = px(min(14 if box[0] < 680 else 22, max(6, (rating_size - 8) * 2)))
        rating_widths = [_rating_width(draw, r, rating_font) for r in records]
        if sum(rating_widths) + rating_gap * max(0, len(records) - 1) <= px(g['text_width']):
            break
    rating_rows = []
    row = []
    row_width = 0
    for record, record_width in zip(records, rating_widths):
        if row and row_width + rating_gap + record_width > px(g['text_width']):
            rating_rows.append(row)
            row = []
            row_width = 0
        row_width += (rating_gap if row else 0) + record_width
        row.append((record, record_width))
    if row:
        rating_rows.append(row)
    wordmark = None
    if logo is not None:
        wordmark = logo.convert('RGBA')
        bounds = wordmark.getchannel('A').getbbox()
        if bounds:
            wordmark = wordmark.crop(bounds)
            wordmark = ImageOps.contain(wordmark, (max(1, px(g['logo_w'])), max(1, px(g['logo_h']))), Image.Resampling.LANCZOS)
        else:
            wordmark = None
    secondary = title if wordmark and item.get('Type') in ('Season', 'Episode') else None
    if secondary:
        context = None
    subtitle_font = _font(19 if box[0] < 680 else 24, secondary or '', True)
    subtitles = _wrap(draw, secondary or '', subtitle_font, px(g['text_width']), 1)
    subtitle_h = _height(subtitle_font) + px(12) if subtitles else 0
    titles = [] if wordmark else _wrap(draw, title, title_font, px(min(g['text_width'], 1100)), 2)
    title_h = wordmark.height if wordmark else len(titles) * (_height(title_font) + px(3))
    meta_line_height = _rating_line_height(meta_font, metadata[0] if metadata else '')
    rating_line_height = _rating_line_height(rating_font)
    meta_h = meta_line_height + px(13) if metadata or community else 0
    body_h = len(descriptions) * (_height(body_font) + px(3)) + (px(12) if descriptions else 0)
    rating_h = len(rating_rows) * (rating_line_height + px(8)) + px(7) if records else 0
    context_h = _height(context_font) + px(9) if context else 0
    total = title_h + px(18) + subtitle_h + meta_h + body_h + rating_h + context_h
    y = max(px(16), px(g['bottom']) - total)
    if context:
        _center(draw, width, y, context, context_font)
        y += context_h
    if wordmark:
        canvas.alpha_composite(wordmark, (int((width - wordmark.width) / 2), int(y)))
        y += wordmark.height + px(18)
    else:
        for line in titles:
            _center(draw, width, y, line, title_font)
            y += _height(title_font) + px(3)
        y += px(15)
    for line in subtitles:
        _center(draw, width, y, line, subtitle_font)
        y += subtitle_h
    if metadata or community:
        line = metadata[0] if metadata else ''
        row_width = community_width + meta_gap + pilfont.text_length(draw, line, meta_font)
        x = (width - row_width) / 2
        baseline = _rating_baseline(meta_font, y, line)
        if community:
            _draw_rating(draw, x, y, community, meta_font, canvas, baseline=baseline)
            x += community_width + meta_gap
        if line:
            _text(draw, (x, baseline), line, meta_font, (247, 245, 242, 255), anchor='ls')
        y += meta_line_height + px(13)
    for line in descriptions:
        _center(draw, width, y, line, body_font, (255, 253, 250, 255))
        y += _height(body_font) + px(3)
    if descriptions:
        y += px(12)
    for row in rating_rows:
        x = (width - sum((record_width for _, record_width in row)) - rating_gap * (len(row) - 1)) / 2
        for record, record_width in row:
            _draw_rating(draw, x, y, record, rating_font, canvas)
            x += record_width + rating_gap
        y += rating_line_height + px(8)
    if home:
        resume = (item.get('UserData') or {}).get('PlaybackPositionTicks') or 0
        play_label = '开始观看' if item.get('Type') == 'Series' else '继续播放' if resume else '播放'
        for name, label in [('play', play_label), ('details', '查看详情')]:
            x, y, w, h = g['controls'][name]
            rect = (px(x), px(y), px(x + w), px(y + h))
            primary = name == 'play'
            cinema_paint.rounded(canvas, rect, radius=px(PLAY_RADIUS), fill=(250, 248, 245, 255) if primary else (22, 29, 39, 112))
            font = _font(PLAY_FONT_SIZE, label)
            tx = px(x) + (px(w) - pilfont.text_length(draw, label, font)) / 2 + (px(6) if primary else 0)
            label_bounds = font.getbbox(label)
            ty = px(y) + (px(h) - (label_bounds[3] - label_bounds[1])) / 2 - label_bounds[1]
            if primary:
                cx = tx - px(12)
                cy = px(y + h / 2)
                r = px(PLAY_ICON_SIZE) / 2
                cinema_paint.polygon(canvas, [(cx - r * 0.4, cy - r * 0.75), (cx - r * 0.4, cy + r * 0.75), (cx + r * 0.6, cy)], fill=(32, 34, 40, 255))
            _text(draw, (tx, ty), label, font, (32, 34, 40, 255) if primary else (255, 255, 255, 255), shadow=not primary)
        if count > 1:
            for name, direction in [('previous', -1), ('next', 1)]:
                x, y, w, h = g['controls'][name]
                cx, cy = (px(x + w / 2), px(y + h / 2))
                cinema_paint.ellipse(canvas, (px(x), px(y), px(x + w), px(y + h)), fill=(25, 28, 35, 45), outline=(255, 255, 255, 100))
                cinema_paint.line(canvas, [(cx - direction * px(3), cy - px(6)), (cx + direction * px(3), cy), (cx - direction * px(3), cy + px(6))], fill=(255, 255, 255, 230), width=max(1, px(2)))
            for number in range(count):
                x, y, w, h = g['controls'][f'dot-{number}']
                cx, cy = (px(x + w / 2), px(y + h / 2))
                r = px(4 if number == index else 3)
                cinema_paint.ellipse(canvas, (cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 255 if number == index else 105))
    return canvas

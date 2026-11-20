"""Filename evidence and conservative directory rules, independent of provider guesses."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import re
from personal_cinema.runtime_paths import RuntimePath as Path
RULES = {'version': '2026-11-08.5', 'rules': [{'id': 'availability', 'title': '检查来源可用性', 'detail': '连续两次完整扫描确认源文件删除后，同步清理完整缓存与未完成分段；网盘接口故障不算删除。移动改名按稳定文件 ID 保留记录与缓存。'}, {'id': 'episode-labels', 'title': '不要求 SxxExx 命名', 'detail': '识别 S01E02、1x02、第12集／第十二话、EP12、E12、12话；纯数字和“01 - 标题”必须结合目录证据。'}, {'id': 'directory-context', 'title': '父目录、季目录和范围共同判断', 'detail': '识别 Season 1、S01、第一季、第二部等季目录；001-100 等范围目录不作为作品标题。未知绝对集数保留在“编号待核对”，不乱猜季。'}, {'id': 'movie-boundary', 'title': '合集按电影拆分', 'detail': '优先使用最近的单部作品目录；合集中的不同电影名／年份单独建立入口。同一作品不同版本不等于不同作品。'}, {'id': 'title-cleaning', 'title': '去除资源噪声', 'detail': '清理清晰度、编码、字幕组、公众号等信息；繁简仅作匹配别名，发行年份与续集数字分别处理。'}, {'id': 'identity-evidence', 'title': '身份冲突不能靠搜索第一名解决', 'detail': '核对类型、标题、年份、明确来源 ID 和候选差距；多个强候选或年份冲突进入待核对，手动批次内有限重试，结束后不启动定时全盘重试。'}, {'id': 'continuous-series', 'title': '连续长篇保持一个入口', 'detail': '海贼王／火影忍者使用已确认总集数映射；火影含疾风传共720集。未知作品不套用其他作品映射。'}, {'id': 'season-independent', 'title': '多季同入口、各季各资料', 'detail': '保留统一剧集入口，各季分别核对剧情、海报、演员与评分；没有独立季评分时不复制整剧评分。'}, {'id': 'extras', 'title': '电影、电视特别篇、联动与短篇分开', 'detail': '剧场版独立电影入口；特别篇分类且必须有封面。数字特别篇不能混入正片，额外分类不触发普通追剧续集。'}, {'id': 'duplicates', 'title': '一部电影一个入口、一个优选版本', 'detail': '同一电影只显示一个优选版本；用户明确指定的新版优先，不重复比较画质。替换时迁移观看进度、收藏与缓存需求；其他版本保留后台，不生成重复入口。'}, {'id': 'chinese-credits', 'title': '优先中文资料与正确角色名', 'detail': '百度百科、中文维基等按确切作品核对；主要角色按原资料排序，区分现任／曾任配音，不把小角色排成主演。来源不可读时标记待核对。'}, {'id': 'artwork', 'title': '各季海报与每集剧照对应内容', 'detail': '各季使用本季海报，续播采用本集横向剧照；不以第一季或其他地区错版海报冒充另一季。'}, {'id': 'cloud-layout', 'title': '百度与夸克同一套实际目录整理', 'detail': '作品总目录下区分正传、电影、电视特别篇、联动与短篇；电影及附件各有子目录。规范实际文件名，支持 RMVB 等旧格式。'}, {'id': 'cloud-identity', 'title': '文件 ID 跟踪移动与版本替换', 'detail': '按云盘文件 ID 区分改名、移动与新文件；不以路径变化当作删除。仅整理新加或改变的内容，不重复处理整个库。'}, {'id': 'subtitles', 'title': '字幕按内容识别并默认选中文', 'detail': '识别简繁中文、英文、日文及双语，区分次级、强制与听障字幕；优先完整简体中文字幕，随对应视频一起归位。'}, {'id': 'ad-cleanup', 'title': '整理后回收广告与空目录', 'detail': '确认媒体和字幕已归位后，删除广告目录及旧空目录到网盘回收站；仍含未识别媒体的目录保留并显示待处理。'}, {'id': 'manual-ingestion', 'title': '全局扫描与新入库分开', 'detail': '全局扫描检查全部影视目录；扫描新入库只读取根目录和未托管的新文件夹。均由用户手动触发，定时扫描关闭；未完成资料在批次内自动补配。'}, {'id': 'cache-removal', 'title': '手动取消与删除缓存', 'detail': '排队、下载中与已缓存均可移除，只删除本地副本；取消后不因收藏或历史自动重新加入，手动添加可恢复。当前播放与暂停播放中的缓存受保护。'}, {'id': 'franchise-world', 'title': '以作品身份归档系列世界', 'detail': '哈利·波特与神奇动物共享大目录、分子系列；生化危机独立归档，不以通用“电影”分类目录充当作品系列。后续按可信作品及合集身份归组。'}, {'id': 'transparent-status', 'title': '结果和故障持续可见', 'detail': '显示发现、分类、匹配、海报、中文演职员各阶段及更新时间；过期状态标异常，不能用等待掩盖失败。'}]}

def chinese_number(text):
    if text.isdecimal():
        return int(text)
    digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    units = {'十': 10, '百': 100, '千': 1000}
    if not text or any((c not in digits and c not in units for c in text)):
        return None
    if all((c in digits for c in text)):
        return int(''.join((str(digits[c]) for c in text)))
    total = 0
    number = 0
    for c in text:
        if c in digits:
            number = digits[c]
        else:
            total += (number or 1) * units[c]
            number = 0
    return total + number

def season_folder(parts):
    for folder in reversed(parts):
        m = re.fullmatch('(?:season[ ._-]*|s)(\\d{1,2})', folder, re.I)
        if m:
            return int(m[1])
        m = re.fullmatch('第\\s*([零〇一二两三四五六七八九十百\\d]+)\\s*[季部]', folder)
        if m:
            return chinese_number(m[1])
    return None

def episode_label(name, numbered=False):
    stem = Path(name).stem
    m = re.search('S(\\d{1,2})[ ._-]*E(\\d{1,5})(?!\\d)', stem, re.I)
    if not m:
        m = re.search('(?<!\\d)(\\d{1,2})x(\\d{1,5})(?!\\d)', stem, re.I)
    if m:
        return {'season': int(m[1]), 'episode': int(m[2]), 'format': '季集标记'}
    m = re.search('第\\s*([零〇一二两三四五六七八九十百千\\d]+)\\s*[集话話]', stem)
    if not m:
        m = re.search('(?<!\\d)(\\d{1,5})\\s*[集话話]', stem)
    if m:
        n = chinese_number(m[1])
        if n:
            return {'season': None, 'episode': n, 'format': '中文集号'}
    m = re.search('(?:^|[\\W_])(?:EP?|episode)[ ._-]*(\\d{1,5})(?!\\d)', stem, re.I)
    if m:
        return {'season': None, 'episode': int(m[1]), 'format': 'EP／E 集号'}
    if numbered:
        m = re.fullmatch('\\s*(\\d{1,5})(?:\\s*[-–._ ]\\s*[^\\d].*)?', stem)
        if m and int(m[1]) > 0:
            return {'season': None, 'episode': int(m[1]), 'format': '目录连续数字证据'}
    return None

def directory_evidence(listing):
    from smart_metadata import _label
    files = [v['name'] for v in listing if not v.get('is_dir') and Path(v['name']).suffix.lower() in ('.mp4', '.mkv', '.avi', '.mov', '.ts', '.m2ts', '.m4v', '.webm', '.rmvb', '.rm', '.flv')]
    numeric = []
    years = set()
    film_labels = set()
    for name in files:
        label = episode_label(name, True)
        if label:
            numeric.append(label['episode'])
        title, year = _label(name)
        if year:
            years.add(year)
        if title and year:
            film_labels.add((title, year))
    numbered = len(set(numeric)) >= 4 and (not all((1900 <= n <= 2099 for n in numeric)))
    return {'numbered': numbered, 'multiple_movies': len(years) > 1 or (len(film_labels) >= 2 and (not numeric))}

def simplified(text):
    """Use Windows' own Chinese conversion table; preserve original display spelling."""
    from chinese_credits import simplify
    return simplify(text)

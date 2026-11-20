"""Shared truthful foreground work and background-cache priority gate."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import time
from library_tasks import read
LABELS = {'index': '等待新媒体索引', 'discovery': '扫描新文件', 'classify': '分类入库', 'identity': '匹配作品', 'poster': '整理海报与简介', 'credits': '整理演员资料', 'ratings': '核对各季独立评分', 'inventory': '读取目录与字幕', 'organize': '移动并规范命名', 'cleanup': '清理旧目录', 'verify': '核对整理结果', 'quality': '对比视频版本', 'upgrade': '替换旧版与迁移记录', 'prepare': '准备整理', 'portraits': '补齐演职员头像', 'probe': '读取视频与音轨'}

def activities(root, now=None):
    now = time.time() if now is None else now
    rows = []
    for worker in ('versions', 'organizer', 'cleanup', 'metadata', 'credits', 'artwork', 'portraits', 'scan'):
        value = read(root / 'data/task-events' / (worker + '.json'))
        if value.get('state') != 'running' or not 0 <= now - value.get('updated_at', 0) < 90:
            continue
        rows.append({'worker': worker, 'stage': value.get('stage'), 'label': LABELS.get(value.get('stage'), '处理媒体资料'), 'title': str(value.get('title', ''))[:300], 'updated_at': value['updated_at'], 'count': value.get('count'), 'total': value.get('total')})
    batch = read(root / 'data/manual-ingestion.json')
    if not rows and batch.get('state') == 'running' and (0 <= now - batch.get('updated_at', 0) < 180):
        rows.append({'worker': 'batch', 'stage': batch.get('stage', 'identity'), 'label': LABELS.get(batch.get('stage'), '整理本批资源'), 'title': '本批新增资源', 'updated_at': batch['updated_at']})
    return rows

def background_blocked(worker):
    return bool(getattr(worker, 'root', None) and activities(worker.root))

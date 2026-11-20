"""Read-only local status page; no tokens or playback URLs."""
from personal_cinema.runtime_paths import cache_path as _cinema_cache_path, port as _cinema_port, secret_write as _cinema_secret_write, vault as _cinema_vault, jellyfin_connection as _cinema_jellyfin_connection, jellyfin_socket as _cinema_jellyfin_socket
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import datetime
import html

def render_report(status, entries):
    esc = lambda v: html.escape(str(v if v is not None else ''))
    plan = {p['id']: p for p in status.get('plan', [])}
    downloading = status.get('downloading')
    shown = [e for e in entries if e.get('ready') or e['id'] in plan or e.get('favorite') or e.get('download_paused') or (e.get('download_error') and e.get('last_played_at'))]
    shown.sort(key=lambda e: (e['id'] != downloading, not e.get('favorite'), e.get('ready', False), -e.get('last_played_at', 0)))
    rows = []
    for e in shown:
        if e['id'] == downloading:
            state = f"下载中 {100 * status.get('downloaded_bytes', 0) / max(1, e['size']):.1f}%"
        elif e.get('ready'):
            state = '已缓存 · 本地播放'
        elif e.get('playback_active') and (not e.get('playback_paused')):
            state = '播放中 · 复用播放缓冲'
        elif e.get('download_paused'):
            state = f"已暂停 · 保留 {100 * e.get('partial_bytes_completed', 0) / max(1, e['size']):.1f}%"
        elif e.get('download_error'):
            state = '暂缓 · 自动重试'
        else:
            state = '等待下载'
        title = e.get('display_title') or e.get('series') or e['id']
        if e.get('kind') == 'Shows':
            title += f" [S{e.get('season', 0):02}E{e.get('episode', 0):02}]"
        progress = e.get('progress_percent')
        progress = f'{progress:g}%' if progress is not None else '已看' if e.get('watched_at') else '暂无'
        source = '百度' if e.get('remote', '').startswith('/百度/') else '夸克'
        reason = '喜欢 · 保留' if e.get('favorite') else plan.get(e['id'], {}).get('reason', '已有副本')
        if e.get('pinned'):
            reason = '手动保留'
        rows.append('<tr>' + ''.join(('<td>' + esc(v) + '</td>' for v in [state, title, source, progress, f"{e['size'] / 1000000000.0:.2f} GB", reason])) + '</tr>')
    updated = datetime.datetime.fromtimestamp(status['updated_at']).strftime('%Y-%m-%d %H:%M:%S')
    warning = '<p class="error">后台提示：' + esc(status['last_error']) + '</p>' if status.get('last_error') else ''
    mode = '播放优先：独立下载暂停，只保存播放实际收到的完整数据块。暂停影片后补齐当前影片，退出播放后恢复后台队列。' if status.get('playback_exclusive') or status.get('playback_network_priority') else '主界面模式：恢复后台缓存队列，已完成的分段继续使用。'
    if status.get('playback_status_unknown'):
        mode = '暂时无法读取播放状态，缓存任务先暂停，等待状态恢复。'
    if status.get('steam_downloading'):
        mode = 'Steam 下载优先：全部缓存下载已暂停；Steam 下载结束或手动暂停后自动恢复。'
    if status.get('downloads_paused'):
        mode = '后台缓存已手动暂停，视频播放接口继续运行；恢复自动缓存后会继续队列。'
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="15">\n<meta name="viewport" content="width=device-width"><title>影视缓存状态</title><style>\nbody{font:16px/1.65 "Microsoft YaHei",sans-serif;background:#f5f6f8;color:#202832;margin:32px auto;max-width:1200px;padding:0 20px}\nh1{font-size:27px}p{color:#4a5665}strong{color:#202832}table{width:100%;border-collapse:collapse;background:white;border:1px solid #dde2e8}\nth,td{text-align:left;padding:12px;border-bottom:1px solid #e8ebef}th{background:#eaf0f5;white-space:nowrap}td:first-child{white-space:nowrap}.error{color:#a32929}\n</style><h1>影视缓存状态</h1>' + f"\n<p><strong>{status['cache_bytes'] / 1000000000.0:.2f} / {status['max_cache_bytes'] / 1000000000.0:g} GB</strong> · {status['ready_items']} 个完整副本 · 硬盘预留 {status['reserve_free_bytes'] / 1000000000.0:g} GB<br>\n更新于 {updated}，页面每 15 秒刷新。</p>{warning}<p><strong>{mode}</strong></p>\n<p>退出播放页后，按中断的后台任务、喜欢、最近续播和追剧后续恢复下载。剧集准备当前起最多 5 集；没有观看或收藏信号的作品不会无差别下载。<br>\n喜欢的影片会保留，即使已标为已看。已看标记不会立即触发删除，也不按时间到期删除；只有缓存额度或硬盘空闲空间不足时，才优先回收最久未打开的已看副本。<br>\n下载完成后重新打开视频会读取本地原画；未完成时优先复用已校验的播放缓冲块，缺少的数据才从网盘读取。只有本机缓存被清理，网盘原文件、海报和进度保留。</p>\n<table><thead><tr><th>缓存状态</th><th>作品</th><th>来源</th><th>观看进度</th><th>大小</th><th>缓存原因</th></tr></thead><tbody>\n{''.join(rows)}</tbody></table>\n<p>播放记录以 Jellyfin 实际收到的数据为准；VidHub 的手机本地收藏没有回传时无法触发。所有可回收副本均受保护时，后台暂停新下载，缓存总量不会继续增加。</p></html>"

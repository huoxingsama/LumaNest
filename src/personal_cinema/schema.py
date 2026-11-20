"""Versioned, shared descriptions for the settings UI and configuration validator.

Unavailable controls describe planned capabilities, not effective settings. A
runtime adapter must implement a field before marking it available.
"""
from copy import deepcopy

SECTIONS = [
    {'id': key, 'label': label} for key, label in (
        ('foundation', '基础服务'), ('sources', '媒体来源'),
        ('organizing', '整理与入库'), ('metadata', '识别与资料'),
        ('artwork', '海报与人物'), ('ratings', '评分'),
        ('playback', '播放与画面'), ('cache', '缓存与存储'),
        ('network', '网络与远程访问'), ('appearance', '界面与交互'),
        ('automation', '自动化与工作台'), ('maintenance', '账户与维护'))
]


def field(key, label, kind, default, help, *, scope=('global',),
          effect='restart', options=None, minimum=None, maximum=None,
          advanced=False, available=True, target='', private=False, invariant=False):
    value = {'key': key, 'section': key.split('.')[0], 'label': label,
             'kind': kind, 'default': default, 'help': help,
             'scope': list(scope), 'effect': effect, 'advanced': advanced,
             'available': available, 'runtime_target': target,
             'reason': '' if available else '尚未接入运行模块，当前版本不可调整。',
             'private': private or kind == 'path'}
    if invariant:
        value.update(readonly=True, invariant=True, reason='固定正确性规则，不允许关闭。')
    if options is not None:
        value['options'] = [{'value': v, 'label': text} for v, text in options]
    if minimum is not None: value['min'] = minimum
    if maximum is not None: value['max'] = maximum
    return value


DEVICE = ('global', 'device')
MEDIA = ('global', 'source', 'item', 'season')
FIELDS = [
    field('foundation.jellyfin_mode', '媒体库部署方式', 'select', 'existing', '连接已有服务，或管理本程序创建的独立实例。', options=[('existing','连接已有 Jellyfin'),('managed','本机托管')], target='integrations.jellyfin'),
    field('foundation.jellyfin_url', 'Jellyfin 地址', 'text', 'http://127.0.0.1:8096', '支持 HTTP/HTTPS 与子路径；不要在地址中填写账号或令牌。', target='integrations.jellyfin', private=True),
    field('foundation.jellyfin_username', 'Jellyfin 用户名', 'text', '', '密码和令牌单独保存在凭据库。', target='integrations.jellyfin', private=True),
    field('foundation.jellyfin_credential', 'Jellyfin 凭据引用', 'text', 'jellyfin.token', '这是凭据名称，不是令牌内容；通过服务登录保存凭据。', advanced=True, target='integrations.jellyfin'),
    field('foundation.jellyfin_path', 'Jellyfin 程序', 'path', '', '仅托管模式需要；选择受支持的 jellyfin 可执行文件。', target='service_manager'),
    field('foundation.jellyfin_data', 'Jellyfin 数据目录', 'path', '', '为空时使用本实例独立目录；不会接管其他安装的数据目录。', target='service_manager', private=True),
    field('foundation.mpv_path', 'MPV 程序', 'path', '', '选择已有 MPV，或在安装后自动检测。', scope=DEVICE, target='runtime.player', private=True),
    field('foundation.ffprobe_path', '媒体探测程序', 'path', '', '检测视频真实分辨率、轨道、码率与 HDR 信息。', target='runtime.probe', private=True),

    field('sources.openlist_mode', 'OpenList 部署方式', 'select', 'disabled', '仅需要网盘挂载的来源启用。', options=[('disabled','不使用'),('existing','连接已有 OpenList'),('managed','本机托管')], target='integrations.openlist'),
    field('sources.openlist_url', 'OpenList 地址', 'text', 'http://127.0.0.1:5244', '媒体挂载服务地址；密钥不要放进 URL。', target='integrations.openlist', private=True),
    field('sources.openlist_username', 'OpenList 用户名', 'text', '', '用于授权访问所选媒体目录。', target='integrations.openlist', private=True),
    field('sources.openlist_credential', 'OpenList 凭据引用', 'text', 'openlist.token', '由授权窗口保存令牌，只在此记录引用。', advanced=True, target='integrations.openlist'),
    field('sources.openlist_path', 'OpenList 程序', 'path', '', '仅托管模式使用。', target='service_manager', private=True),
    field('sources.entries', '媒体来源目录', 'list', [], '每个来源分别选择目录、启用状态及是否允许整理写入。', effect='next_batch', target='runtime.source_roots', private=True),

    field('organizing.enabled', '允许执行云盘整理', 'bool', False, '还需要来源本身授予写权限；默认只读取媒体。', effect='next_batch', target='cache-config.cloud_normalization_enabled'),
    field('organizing.preview_only', '先预览整理计划', 'bool', True, '先查看将要移动或改名的文件，再执行计划。', effect='next_batch', target='source_policy.approved_plan'),
    field('organizing.cleanup_empty', '回收确认空的旧目录', 'bool', False, '只有移动和索引更新成功，并重新列目录确认空，才允许回收。', effect='next_batch', target='source_policy.cleanup_empty'),
    field('organizing.attachments_in_movie', '附加视频放在主片中', 'bool', True, '主题曲、花絮和预告作为主片附件；该正确性规则不允许关闭。', effect='next_batch', invariant=True, target='legacy.media_attachments'),

    field('metadata.languages', '资料语言顺序', 'list', ['zh-CN','en'], '所有来源按此顺序请求作品资料；当前不支持逐作品或逐季覆盖。', effect='next_batch', target='legacy.metadata_matcher.Matcher.search'),
    field('metadata.search_provider', '作品识别来源', 'select', 'TheMovieDb', '使用受支持的作品识别提供方。', options=[('TheMovieDb','TMDB')], effect='next_batch', target='metadata-config.preferred_provider'),
    field('metadata.lock_manual', '保留人工确认资料', 'bool', True, '人工指定的身份和锁定字段不被后台覆盖。', effect='next_batch', invariant=True, target='legacy.metadata_matcher/manual_match.override_for_item'),

    field('artwork.minimum_width', '竖海报最低宽度', 'int', 600, '真实宽度低于该值时继续寻找高清原图；无合格来源时保留现图，不放大冒充高清。', minimum=200, maximum=8000, effect='next_batch', target='legacy.artwork_quality.inspect'),
    field('artwork.backdrop_width', '横海报最低宽度', 'int', 1920, '筛选并验证同作品的真实高清无字横图。', minimum=640, maximum=16000, effect='next_batch', target='legacy.artwork_quality.candidates/inspect'),
    field('artwork.missing_portrait', '暂无头像时', 'select', 'placeholder', '重新打开详情时显示暂无或隐藏无图人物；不会删除人物资料或停止补图。', options=[('placeholder','显示暂无照片'),('hide','隐藏无图人物卡')], effect='next_page', target='legacy.people_localization/mpvtk_browser.components.detail'),

    field('ratings.enabled', '补全外部评分', 'bool', True, '关闭后停止外部评分查询，并在重新打开详情时隐藏外部分数；保留已有缓存。', effect='next_batch', target='legacy.external_ratings.sync_ratings/CachedRatings.apply'),
    field('ratings.providers', '评分平台顺序', 'list', ['imdb','tomatoes','audience','metacritic','letterboxd','myanimelist','bangumi'], '按此顺序显示实际有来源的分数；Bangumi 仅展示已核实的独立季分，季分不继承整剧分。', effect='next_batch', target='legacy.external_ratings.rating_policy/CachedRatings.apply'),
    field('ratings.credential_ref', 'MDBList 凭据引用', 'text', 'ratings.mdblist', '在凭据管理保存 API key，此处只保存引用名称。', effect='next_batch', target='runtime.mdblist_credential'),
    field('ratings.refresh_days', '评分复查间隔（天）', 'int', 7, 'IMDb 数据集及 MDBList 评分复查间隔；在运行的批次中检查，不自动遍历全库。', minimum=1, maximum=365, effect='next_batch', target='legacy.external_ratings.ensure_imdb_dataset/sync_ratings'),

    field('playback.hdr_output', '画面输出', 'select', 'auto', '自动保留设备输出设置；SDR 将 HDR 色调映射到 SDR，不改变原片。', options=[('auto','自动'),('sdr','转为 SDR 观看')], scope=DEVICE, effect='next_playback', target='mpv.hdr_output'),
    field('playback.brightness', '亮度', 'int', 0, '新播放会话的亮度默认值；播放中可在颜色均衡器打开数值滑杆，HDR 使用播放器对应调节管线。', minimum=-100, maximum=100, scope=DEVICE, effect='next_playback', target='mpv.conf.brightness'),
    field('playback.hwdec', '硬件解码', 'select', 'auto-safe', '按设备能力选择硬件解码；遇到画面问题可以关闭。', options=[('auto-safe','自动（兼容优先）'),('auto','自动'),('no','关闭')], scope=DEVICE, effect='next_playback', target='mpv.hwdec'),
    field('playback.audio_languages', '音轨语言顺序', 'list', ['zho','jpn','eng'], '优先选择匹配的音轨，具体影片仍可临时切换。', scope=DEVICE, effect='next_playback', target='mpv.conf.track_languages'),
    field('playback.subtitle_languages', '字幕语言顺序', 'list', ['zho','eng'], '优先选择匹配字幕，不保证影片含有这些字幕。', scope=DEVICE, effect='next_playback', target='mpv.conf.track_languages'),

    field('cache.playback_gib', '顺序播放缓冲（GiB）', 'int', 10, '从当前读取位置向前缓冲；与完整影片下载分别计量。', minimum=0, maximum=1024, scope=DEVICE, effect='next_playback', target='cache-config.playback_buffer_bytes'),
    field('cache.offline_gib', '离线缓存上限（GiB）', 'int', 100, '完整影片与下载中的文件共同占用该预算。', minimum=0, maximum=32768, scope=DEVICE, effect='next_batch', target='cache-config.max_cache_bytes'),
    field('cache.image_gib', '播放器图片缓存上限（GiB）', 'int', 2, '原生播放器封面与海报的磁盘缓存预算；0 禁止落盘，内存显示缓存与视频另计。', minimum=0, maximum=100, scope=DEVICE, effect='restart', target='mpvtk_browser.thumbnails.disk_cache/ThumbnailStore'),
    field('cache.reserve_gib', '磁盘保留空间（GiB）', 'int', 10, '不足此值时暂停新增缓存，优先保护正在播放的文件。', minimum=1, maximum=32768, scope=DEVICE, effect='next_batch', target='cache-config.reserve_free_bytes'),
    field('cache.directory', '缓存目录', 'path', '', '空值使用本实例缓存目录。迁移必须等待读取结束，不直接移动正在播放的文件。', scope=DEVICE, target='runtime.cache_root', private=True),
    field('cache.next_episodes', '追剧预取集数', 'int', 5, '已缓存集也计入窗口；特别篇不加入正传预取。', minimum=0, maximum=50, effect='next_batch', target='cache-config.next_episodes'),
    field('cache.recent_days', '近期播放时间窗（天）', 'int', 14, '仅有实际播放进度的作品进入近期候选。', minimum=1, maximum=365, advanced=True, effect='next_batch', target='cache-config.recent_playback_seconds'),
    field('cache.downloads_paused', '暂停离线下载', 'bool', False, '保留当前播放的顺序缓冲，不另开完整文件下载。', effect='next_batch', target='cache-config.downloads_paused'),
    field('cache.cleanup_only_on_pressure', '仅空间不足时清理', 'bool', True, '优先回收最久未打开且未保护的已看缓存。', effect='next_batch', target='cache-config.cleanup_only_on_pressure'),
    field('cache.retain_days', '缓存保留天数', 'int', 2, '仅关闭“仅空间不足时清理”后使用；不会删除云盘原件。', minimum=1, maximum=3650, advanced=True, effect='next_batch', target='cache-config.retention_seconds'),

    field('network.mode', '网络访问方式', 'select', 'direct', '直连绕过环境代理；指定代理只作用于本程序支持的请求。', options=[('direct','直连'),('system','跟随系统'),('proxy','指定 HTTP 代理')], scope=DEVICE, target='runtime.network'),
    field('network.proxy_url', 'HTTP 代理地址', 'text', '', '仅指定代理模式需要。认证信息不放入 URL。', scope=DEVICE, target='runtime.network', private=True),
    field('network.hypomux_path', 'HypoMux 程序', 'path', '', '可选增强，不影响普通网络下的影院启动。', scope=DEVICE, target='integrations.hypomux', private=True),
    field('network.preferred_interface', '优先网络接口', 'text', '', '需要兼容的网络适配组件，不能仅凭配置值宣称已绑定出口。', scope=DEVICE, available=False, private=True),
    field('network.fallback', '优先网络不可用时', 'select', 'direct', '外出可回到普通网络，或停止该类请求。', options=[('direct','使用普通直连'),('stop','停止请求')], scope=DEVICE, available=False),

    field('appearance.theme', '主题', 'select', 'default', '使用播放器已提供的统一主题。', options=[('default','默认'),('superdark','深色')], scope=DEVICE, target='mpv.theme'),
    field('appearance.text_scale', '文字缩放', 'float', 1.0, '整体调整字号，不改变每页独立间距。', minimum=0.8, maximum=2.0, scope=DEVICE, target='mpv.ui_text_scale'),
    field('appearance.poster_scale', '封面尺寸', 'float', 1.0, '整体调整卡片尺寸，保持布局比例。', minimum=0.5, maximum=2.0, scope=DEVICE, target='mpv.poster_scale'),
    field('appearance.reduced_motion', '减少海报动态效果', 'bool', False, '关闭影院海报的下拉缩放回弹与滚动虚化；普通列表仍可滚动。', scope=DEVICE, target='mpvtk.widgets.reduced_motion/cinema_home._motion_buffers'),
    field('appearance.home_refresh', '首页推荐更新时机', 'select', 'explicit', '详情返回始终保留推荐与位置；选择新会话后，当前会话点击首页也不换批。', options=[('explicit','主动点击首页时'),('new_session','新会话时')], scope=DEVICE, effect='next_page', target='mpvtk_browser.app.go_home/cinema_featured.refresh_on_home'),

    field('automation.mode', '扫描方式', 'select', 'manual', '手动扫描、定时扫描或启动时扫描；仅扫描已启用来源。', options=[('manual','手动'),('scheduled','定时'),('startup','启动时')], effect='next_batch', target='runtime.automation'),
    field('automation.scan_interval_minutes', '扫描间隔（分钟）', 'int', 30, '仅定时扫描模式使用。', minimum=5, maximum=10080, effect='next_batch', target='cache-config.scan_interval_seconds'),
    field('automation.retry_limit', '进程恢复重试上限', 'int', 3, '仅本实例后台进程退出后的重启次数；资料请求使用各自退避规则。达到上限可手动重新启动。', minimum=0, maximum=10, advanced=True, effect='next_batch', target='runtime.supervise'),

    field('maintenance.data_root', '实例数据目录', 'path', '', '由首次启动选择；迁移整个实例需要专门迁移流程。', advanced=True, available=False, private=True),
    field('maintenance.autostart', '登录后启动后台', 'bool', False, '应用后使用此 Windows 账户的实例专属自启项，后台启动已配置的服务，不自动打开播放器。', target='maintenance.MaintenanceManager.sync_autostart'),
    field('maintenance.diagnostic_retention_days', '诊断记录保留天数', 'int', 14, '启动和应用设置时清理此实例管理目录中的过期日志；不扫描或删除影片。', minimum=1, maximum=365, advanced=True, target='maintenance.MaintenanceManager.prune_diagnostics'),
]

BY_KEY = {entry['key']: entry for entry in FIELDS}


def schema():
    return deepcopy({'version': 1, 'sections': SECTIONS, 'fields': FIELDS})


def defaults():
    return {entry['key']: deepcopy(entry['default']) for entry in FIELDS}

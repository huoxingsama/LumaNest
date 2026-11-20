# 系统脚本迁移清单

此清单依据旧工作目录和已部署脚本的只读代码盘点。旧脚本不是发行物外的可有可无附件：持续运行的入库、资料、缓存和网络逻辑必须迁移或明确由新模块替代。原路径、账号、令牌、网卡名称、影视清单和运行状态不随源码复制。

## 启动、配置与网络脚本

| 旧脚本/脚本族 | 原能力 | 发行版本中的对应实现 | 当前边界或缺口 |
|---|---|---|---|
| `Start-MediaLibrary.ps1` | 启动 Jellyfin、OpenList、读取与缓存接口、人物与匹配任务；设置进程环境 | `integrations.py`、`service_manager.py`、`runtime.py`、`worker.py` | 不原样复制固定 Python 路径、固定端口与个人网卡。只有拥有进程身份记录的实例可启停；具体 worker 是否按所选自动化模式启动需要运行验收 |
| `Stop-MediaLibrary.ps1` | 根据程序路径/脚本名称停止后台，写暂停状态 | 托管服务 `stop` 与本实例运行器 `stop` | 不沿用按进程名批量结束方式；播放/暂停中的后台变更由运行器避让 |
| `Start-MpvShimZh.py` | 中文影院启动、独立配置路径、原始二进制回退 | `player_launcher.py`、运行界面“打开影院” | 账号从加密保险库载入，不写令牌命令行；原始播放器回退包不默认分发 |
| `configure_jellyfin.py` | 首次管理员、观看者、库、访问策略 | Jellyfin 登录、托管计划与应用 | 初次设置仅作用于新托管实例；已有实例只有明确计划的建库动作。独立观看者创建不包含在当前自动设置中，可在 Jellyfin 管理界面完成 |
| `configure_private_library.py` | 私人库路径与访问范围调整 | 明确的建库计划、托管回环网络配置 | 不向已有服务器重写网络或用户权限；远程访问部署不自动开放 |
| `configure_baidu.py` | OpenList 登录、百度盘挂载、验证及目录准备 | OpenList `drivers/browse/plan/apply` | 受支持驱动、授权引用、非秘密字段分开输入。验证码/扫码/刷新授权仍由提供方界面完成；不自动给云盘根目录写权限 |
| `configure_mpv_client.py` | 播放器服务档案、画面与配置生成 | `runtime.py`、`player_launcher.py`、设置页 | 原个人显示器/地址和明文账号文件不搬入发行包 |
| `configure_rating_sources.py` | 本机 API key 输入窗口与凭据写入 | 设置页凭据保险库、评分提供方选项 | 旧临时文件与个人 key 不复制；资料方接口可用性需分别检测 |
| `activate_playback_buffer.py` | 等待停止播放后重启视频接口以应用缓存变更 | 运行器待应用状态、播放避让与再次应用 | 不复制按脚本名寻找/终止进程的逻辑；界面明确显示待应用，不能假称立即生效 |
| `library_supervisor.py` | 每隔一段时间检测并重启后台任务 | 由本实例运行器替代进程所有权及状态检查 | 旧脚本含个人路径、名称探测及外部 PowerShell 重启，不能原样搬入。持续 watchdog 的恢复策略、scheduled 匹配/中文任务必须作为发布验收项，不能只检查 gateway 存活 |
| `steam_gate.py` | 只读探测 Steam 内容下载，决定是否让出后台带宽 | 已保留于 `legacy`，由缓存策略调用 | 不修改 Steam 设置；运行器当前默认不启用该避让。若提供此选项，需同时暴露设置并验证实际生效 |
| `verify_hypomux_egress.py` | 用指定通道下载探针并比较出口 | 不分发个人探针；HypoMux 能力检测与规则建议 | 旧固定端口、探针地址及个人适配不可作为通用路由保证；实际出口验证仍是单独的待实现能力 |
| HypoMux 参考 Go 源码/调试片段 | 调查引擎协议、代理与 TUN 路由 | 文档引用上游协议，`integrations.py` 返回受限能力 | 不分发临时参考树和机器设置快照，不猜测任意版本自动写配置接口 |
| 独立 Kazumi 的网络规则/快捷方式脚本 | 给独立 Kazumi 配置快捷方式与网络 | 不进入本系统发行范围 | 用户已取消融合，不能重新引入在线目录、规则下载和同步任务 |

## 已纳入的业务脚本

生产部署的 Python 业务代码以 `src/personal_cinema/legacy` 中的可追踪源码保存，由新配置、路径与凭据层驱动；不能只给出这些模块文件却不启动相应任务。旧项目中的一次性 `deploy_*`、`repair_*`、特定作品修复脚本用于历史迁移，不作为普通安装入口执行。

| 模块族 | 保留能力 | 发行接口 |
|---|---|---|
| `cache_worker`、`metadata_proxy`、`task_center_api`、`library_tasks` | 读取接口、入库队列、任务与工作台 | `worker` 入口、运行/停止、工作台任务命令 |
| `cloud_sync`、`cloud_organizer`、`cloud_cleanup`、`quark_manage`、`baidu_manage`、`organizer_gateway` | 云盘发现、整理、稳定身份、空目录清理 | 来源允许写入开关、整理策略、明确任务请求；已有服务授权不等于云盘写授权 |
| `source_presence`、`source_bindings`、`incremental_discovery`、`catalog_*` | 源对象绑定、增量扫描、索引一致性和分类 | 来源与整理设置、扫描/重试入口 |
| `metadata_matcher`、`manual_match`、`manual_ingest`、`smart_metadata`、`chinese_*` | 作品匹配、人工绑定、中文人物与资料 | 资料来源、人工匹配和任务修复入口 |
| `person_portraits`、`person_portrait_worker`、`portrait_*`、`people_localization`、`pokepedia_credits` | 演职员补图与身份校验 | 人物规则、补图任务、缺图终态策略 |
| `artwork_quality`、`verified_artwork`、`title_art_assets`、`episode_artwork`、`season_artwork` | 海报、标志、分季与分集图 | 画质、横图和语言设置；不带个人媒体图片 |
| `external_ratings`、`sync_external_ratings`、`season_ratings` | 多来源评分、作品和季分别缓存 | 评分平台选择、凭据与更新任务 |
| `media_probe`、`media_attachments`、`episode_*`、`series_*`、`absolute_series`、`season_release_groups` | 真实媒体参数、附加视频、季集关联 | 探测工具、主片/花絮/季集规则、重试入口 |
| `playback_buffer`、`range_cache`、`playback_cache`、`smart_cache`、`cache_*` | 顺序播放缓冲与完整离线缓存、命名与字幕 | 分离缓存预算、磁盘预留、播放优先策略 |
| `subtitle_rules`、`subtitle_reconcile` | 字幕扫描与选择规则 | 字幕设置与整理任务 |

旧 `auto_versions` 兼容模块可能作为已有脚本内部依赖留存；用户取消的多版本选择功能不能因此自动开放。具体行为须通过设置映射和回归用例约束。

## 正式发布前必须核对

1. 按调用链验证启动模式：手动、启动时扫描、定时扫描各自应启动哪些 worker；某个脚本存在不等于实际被执行。
2. 区分“账号可登录”“能列目录”“实际能读取视频”“有权整理写入”，每一步独立验收。
3. 本地来源由本机 Jellyfin 建库直接读取；WebDAV 先挂到 OpenList。未实现的独立 WebDAV 扫描及本地自动改名不应留为可保存的假选项。
4. 核对所有 `deploy_*` 的最终业务变更已经进入源模块，而不是把一次性个人修复器作为常驻后台。
5. 运行器配置、用户凭据、进程记录、缓存和来源清单都写入用户实例目录，不进入 Git 源码包；日志错误也不能回显授权内容。
6. 托管进程崩溃后的恢复、云盘授权过期、播放中更新、无 E 盘机器首次安装、完整影片播放仍需干净环境验收。当前隔离测试不会操作生产服务代替这些验收。

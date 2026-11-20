# 服务连接与托管配置

Jellyfin 与 MPV 是影院基础。OpenList 按所选来源启用，HypoMux 为可选增强。已有服务的“连接”不会赋予本程序停止、初始化或重写它的权限。

## 连接状态

设置概览读取最后一次检测结果，不在轮询页面时向服务发起大量请求。配置改变会使旧检测结果失效。`ready` 表示已经完成该组件的连接检查，**不代表实际影片已经播放验证**；响应包含 `playback_verified: false`，播放验收由运行界面独立完成。

- Jellyfin：检查公共信息、登录用户和该用户可见的媒体库。
- OpenList：检查公开配置、初始化状态和登录用户。目录浏览是另一项操作；能列目录不证明云盘视频可读取。
- MPV：使用选定的可执行文件运行 `--no-config --version`，不加载用户 MPV 配置。
- ffprobe：使用选定的程序运行 `-version`，验证媒体参数探测工具可启动。
- HypoMux：仅检查用户选择的文件存在。当前版本能生成本程序进程所需的策略建议；不能声称已经自动改写其网络配置或验证出口。

## 内部动作合同

所有调用都通过管理界面的已认证、受 CSRF 保护的接口提交。

| 对象 | 动作 | 请求与结果 |
|---|---|---|
| Jellyfin / OpenList | `probe` | 无参数；返回连接状态，凭据不返回 |
| Jellyfin / OpenList / MPV / ffprobe | `install_plan` | `{url,sha256,version,executable?}`；下载与解压计划，不发起下载 |
| Jellyfin / OpenList / MPV / ffprobe | `install_apply` | `{plan_id}`；校验 ZIP 后安装到本实例，更新程序路径，不启动 |
| Jellyfin / OpenList | `login` | `{username,password}`；成功后凭据写入 DPAPI 保险库，配置只保留用户名与令牌引用 |
| Jellyfin | `libraries` | 普通用户得到可见库；管理员还能看到库路径 |
| OpenList | `browse` | `{path:"/所选目录"}`；最多返回200个首屏条目，路径不允许相对跳转 |
| OpenList | `drivers` | 返回当前实例提供的驱动名称；不返回已有挂载的秘密配置 |
| Jellyfin / OpenList | `plan` | 返回 `plan_id` 和可读变更清单，只保存配置计划；新托管实例可包含管理员用户名、密码，密码立即转成凭据引用 |
| Jellyfin / OpenList | `apply` | `{plan_id}`；检查计划有效期及服务配置未变化，按检查点应用并验证 |
| 托管 Jellyfin / OpenList | `start/stop` | 只管理本安装目录记录且进程身份仍匹配的进程 |
| MPV | `probe` | 检测已配置程序能否返回 MPV 版本 |
| HypoMux | `probe/plan` | 检测路径或生成规则建议，始终标明尚未自动应用/验证网络 |

计划有效期为30分钟。完成的计划可重复提交，返回同一结果；部分失败保留检查点。重试创建媒体库前重新列出现有媒体库，相同名称与目录不重复创建；同名但路径不同则停止，不覆盖用户已有库。

Jellyfin 的显式建库请求格式：

```json
{
  "libraries": [
    {"name": "电影", "kind": "movies", "paths": ["D:/Movies"]}
  ]
}
```

显式 `libraries` 中的路径是 Jellyfin 所在服务器看到的路径，不是浏览器电脑上的任意本地路径。省略此字段时，计划从已启用的本地来源自动提出建库；本地目录必须存在，类型支持电影、剧集、混合。托管实例或已选择 OpenList 来源时，还会提出建立本实例 `runtime/catalog/Movies` 与 `runtime/catalog/Shows` 两个库，和运行器生成 STRM 的位置保持一致。所有变更必须先查看计划再应用，不覆盖同名不同路径的已有库。已有服务没有新增来源时仅验证。

自动引用客户端本地目录只适用于回环地址上的 Jellyfin。远端 Jellyfin 需要服务器可见的共享目录，不能把客户端盘符直接传给远端。WebDAV 经 OpenList 的 WebDAV 驱动挂载后按 OpenList 来源使用；首版不提供脱离 OpenList 的独立 WebDAV 扫描器。本地来源直接由 Jellyfin 读取，不启用本地文件自动移动与改名。

登录成功把 `jellyfin.user_id` 和 `jellyfin.server_id` 写入凭据保险库，供本实例播放器自动登录；令牌引用来自 `foundation.jellyfin_credential`。用户名保存在普通配置，密码与令牌不写入启动参数或明文账号文件。

`validate_setup()` 在完成向导时重新验证来源与建库结果，返回 `state`、`missing:[{id,detail,action}]`、`checked` 及 `playback_verified:false`。选择 OpenList 来源后，该组件成为必需；必须可读取每个已选目录，且相应 STRM 目录已出现在 Jellyfin 媒体库中。当前视频读取使用 WebDAV Basic 授权，所以 OpenList 需完成用户名密码登录，仅 API 令牌不能代替影片读取授权。该检查仍不等于真实影片播放验收。

OpenList 新建挂载请求格式：

```json
{
  "storage": {
    "mount_path": "/媒体",
    "driver": "BaiduNetdisk",
    "credential_ref": "cloud.baidu.refresh_token",
    "credential_field": "refresh_token",
    "addition": {"root_folder_path": "/"}
  }
}
```

驱动名称必须出现在当前服务返回的驱动列表中。凭据需先通过保险库保存；`addition` 不能包含密码、令牌、Cookie等秘密字段，包括嵌套字段。创建后浏览挂载目录验证。不同驱动的授权与配置仍需以提供方支持为准，不承诺任意云盘可以通用自动配置。

## 托管服务的边界

托管模式需要明确选择已安装程序；组件下载和安装由单独的安装模块负责。可执行路径使用参数数组调用，禁用shell。工作目录必须位于本实例的 `managed` 子目录。Jellyfin自选数据目录只有为空或包含本实例所有权标记时才可采用。

托管服务仅绑定回环地址，默认不开放远程访问、UPnP或自动发现。端口若被其他进程占用，提示连接已有服务或更换端口。已有服务模式不创建网络配置。

进程记录保存PID、创建时间和实际可执行文件路径，停止前核对三者，防止PID复用误伤其他进程。托管服务启动参数改变后不能在运行中重写配置，须先停止再应用。未通过健康检查的进程显示启动中或失败，不显示就绪。

## 组件安装

安装器当前接受用户指定的固定版本 Windows ZIP 下载链接、上游 SHA256 和版本标签。它不是自动升级器，不会猜测最新版本或执行一键安装脚本。组件按版本与哈希放入本实例 `components` 目录；已有程序和其他版本保留。MPV 路径写入设备设置，其余组件路径写入实例设置。

来源限定为 Jellyfin 官方仓库、OpenList 官方 GitHub 发布、MPV 官网列出的构建仓库，以及 FFmpeg 官网推荐的 BtbN/Gyan 构建和 Jellyfin FFmpeg。初始地址必须 HTTPS、无凭据、无查询串、无 `latest` 目录；GitHub 重定向只接受指定发行附件 CDN，不转发服务凭据。

下载后先验证 SHA256，再检查 ZIP 路径、Windows 保留名、大小写重复、符号链接、特殊文件、文件数量和解压大小。默认下载上限 768 MiB、解压上限 3 GiB、最多 30,000 个条目。所选 ZIP 必须含组件对应的 Windows 程序；有多个候选时要求提供 ZIP 内相对路径。安装不运行程序，检测和服务启动是后续独立步骤。当前不接受 EXE/MSI 安装包、7z 或系统包管理脚本。

每次安装都保存收据与程序哈希；配置写回失败可复用已校验的安装目录重试。完成的计划重复提交不会重复下载。

上游来源：[Jellyfin Windows 下载](https://jellyfin.org/downloads/windows/)、[OpenList 手动安装](https://openlistteam.github.io/OpenList-Docs/guide/installation/manual)、[MPV 安装与 Windows 构建说明](https://mpv.io/installation/)、[FFmpeg 下载与 Windows 构建说明](https://ffmpeg.org/download.html)。MPV 和 FFmpeg 的部分 Windows 构建由官网推荐的第三方提供，不能称为项目官方二进制。

## 初次设置的官方依据

OpenList 采用 [初始化状态接口](https://fox.oplist.org/538914387e0) 和 [初始化接口](https://fox.oplist.org/538914388e0)。仅当服务明确尚未初始化、且由本程序托管启动时，才创建首个管理员。已有实例的初始化必须由其原管理流程完成。

HypoMux 的[引擎协议](https://github.com/Hypostasis-Cat/HypoMux/blob/main/engine/README.md)有版本及能力协商；当前接入没有据此假设能控制任意已运行桌面实例。网络自动应用仍需受支持版本适配。

## 本轮验证

`tests/test_integrations.py` 使用真实本地 HTTP 测试服务器覆盖登录、脱敏、Jellyfin与OpenList新实例初始化、已有服务保护、建库幂等、配置变化拒绝、错误响应和拒绝带凭据重定向。`tests/test_service_manager.py` 使用隔离短进程验证进程身份、重复启动、停止与目录约束。

`tests/test_component_installer.py` 使用内存生成的假程序 ZIP，覆盖 SHA256 不匹配、来源白名单、目录穿越、链接、资源限额、重复安装和配置写回失败后的恢复；不会下载或运行真实组件。

这些测试不会连接用户媒体服务、移动云盘文件或停止现有播放器。真正的服务安装和影片播放仍需发行验收流程完成。

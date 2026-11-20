# 内置播放器运行组件

LumaNest 使用 `libmpv` 在影院窗口内播放，不要求安装独立的 MPV 应用。
Windows 安装流程把运行组件放在当前 Python 环境的 `player` 子目录，
不会写入 Windows 系统目录或修改其他播放器。

## 固定版本与来源

播放器核心从 [shinchiro 的 mpv Windows 构建发行页](https://github.com/shinchiro/mpv-winbuild-cmake/releases/tag/20261017)
直接下载。当前固定附件为 `mpv-dev-x86_64-20261017-git-dde0671072.7z`，
SHA256 为 `d6638b2b40561e6071627f39ad60a38e15721dc87a49f244b897911ea2edfd9f`。
安装器读取其中唯一的 `libmpv-2.dll` 并命名为 `mpv-2.dll`，不运行压缩包中的程序。
LumaNest 不把这个核心 DLL 重新上传为自己的发行附件。

构建流程和依赖来源见 [mpv-winbuild-cmake](https://github.com/shinchiro/mpv-winbuild-cmake)，
播放器的许可说明见 [mpv Copyright](https://github.com/mpv-player/mpv/blob/master/Copyright)。
上游各组件保留自己的版权及许可；它们不因被 LumaNest 调用而变成 LumaNest 自有代码。

另一个固定支持包 `lumanest-player-support-1-win-x64.zip` 由本项目的
`tools/package_player_support.py` 生成，SHA256 为
`3e4493277990362e27a93064338cc0be9609b36383b1d9fea1037d9c940b175a`。
其中包含 Vulkan Loader 与 FriBiDi 的 DLL、许可文件和 FriBiDi 1.0.16 对应源码。
支持包的 `support-manifest.json` 与 `NOTICE.txt` 记录来源；安装后保留其
`licenses`、`sources` 目录，不在解压时丢弃这些文件。

## 安装与离线安装

在已经安装 LumaNest 的 Windows 64 位 Python 环境中执行：

```powershell
python -m personal_cinema.embedded_runtime
```

`--prefix` 可以指定另一环境目录。下载使用 HTTPS、固定来源、连接超时、
总时限和大小上限，直接连接上游，不继承系统代理。下载不可达时会退出并保留原组件。

已有可信离线组件时，可以把完整的 `player` 目录复制到离线介质，然后运行：

```powershell
python -m personal_cinema.embedded_runtime --from-directory "D:\Offline\LumaNest-player"
```

离线目录必须包含 `manifest.json`：

```json
{
  "format": 1,
  "architecture": "x86_64",
  "files": {
    "mpv-2.dll": "对应文件的64位小写SHA256"
  },
  "sources": [{"name": "用户选择的可信离线组件"}]
}
```

实际在线安装会自动生成包含来源和每个文件哈希的清单，可直接用于离线复制。
手工清单只能核对文件完整性，不能替代对 DLL 来源的信任；离线目录应来自自己的
已验证安装或对应上游发行包。

安装器只接受允许列表中的 DLL 和版权文件，拒绝路径穿越、目录联接、符号链接、
重复文件及错误架构。它先在独立临时目录中核对哈希，再启动一个无窗口的短进程，
加载 DLL、读取 libmpv API 版本并以空音视频输出完成创建、初始化与销毁；
测试禁用外部配置和脚本，不会创建播放窗口或打开影片。
验证通过后才替换具有安装清单的 `player` 目录。已有目录包含清单外文件时停止，
以免覆盖用户手动加入的内容；替换失败时恢复旧目录。

播放器 DLL 正在被影院占用时，Windows 可能拒绝替换。退出影院后重新执行安装即可。
同一环境只允许一个安装进程，重复安装相同文件会复用已验证的组件。

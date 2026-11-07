> 本页由机器翻译，可能存在错误。请以[英文版](https://github.com/ADCDS/quak/blob/main/docs/install.md)为准。[欢迎提出修正](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations)。

# 安装 Quak

试用不需要相机：每条命令都支持 `--sim`，它会使用模拟相机。

## Linux

您需要：

- Python 3.11 或更高版本
- 连接真实相机时：一个 Bluetooth 适配器和 BlueZ（大多数桌面发行版都已自带），以及带 `nmcli` 的 NetworkManager，用于加入相机的 Wi-Fi
- FFmpeg，用于桌面应用中的实时预览和 `quak preview --play`，后者还需要 `ffplay`

```bash
pipx install 'quak-camera[gui]'     # or 'quak-camera' for just the command line
```

软件包名为 `quak-camera`，因为 PyPI 上的 `quak` 是一个无关的项目。命令是 `quak` 和 `quak-gui`。

也可以从 [Releases](https://github.com/ADCDS/quak/releases) 下载 `.tar.gz`，解压后运行 `./quak`。它需要 glibc 2.34 或更高版本（Ubuntu 22.04、Debian 12）、GTK 3 和 libmpv：在 Debian 和 Ubuntu 上，运行 `sudo apt install libgtk-3-0 libmpv2`。

首次运行 `quak-gui` 时，会将约 45 MB 的 Flet 桌面客户端下载到 `~/.flet`。

最好再准备一块 Wi-Fi 网卡，比如便宜的 USB 网卡，因为加入相机网络的网卡会离开原来所在的网络。可以用 `--wifi-adapter NAME` 或 `QUAK_WIFI_ADAPTER` 指定网卡。如果只有一块网卡，Quak 会先询问您。

## Windows（实验性）

此版本从未与相机一起运行过，甚至从未启动过，因此预计会出现问题，请向我们报告。

```powershell
py -m venv quak-env
quak-env\Scripts\activate
pip install "quak-camera[gui]"
```

也可以从 [Releases](https://github.com/ADCDS/quak/releases) 下载 `.zip`，解压后运行 `quak.exe`。它没有代码签名，因此 Windows 可能会发出警告。

## Android

从 [Releases](https://github.com/ADCDS/quak/releases) 下载 APK 并安装。Android 会要求您允许从浏览器或文件管理器安装应用。它需要 Android 10 或更高版本，并会请求 Bluetooth 和位置权限：Android 要求具备位置权限才能进行 Bluetooth 扫描。

要确认 APK 来自本项目，请将 `apksigner verify --print-certs` 输出的证书 SHA-256 摘要与下面的值进行比较：

```text
fec9c12dc325dd08272a9163c60e033519d74ebd98a31e133b7b145b259bd59b
```

Quak 首次加入相机的 Wi-Fi 时，Android 会要求您确认连接。

下载的文件会留在应用内，直到您点击文件进行分享或保存。卸载应用会删除所有尚未分享的文件。

## 查找您的相机

```bash
quak scan                   # lists cameras nearby, with their addresses
quak pair                   # pairs the one on its pairing screen
quak pair --address AA:BB:CC:DD:EE:FF    # or choose one
```

要在相机上打开配对界面，请前往：**首选项 > 无线连接 > 连接设备 > GoPro Quik 应用（Preferences > Connections > Connect Device > GoPro Quik App）**。

## 构建安装包

大多数人不需要自己构建：打包好的版本都在 [Releases](https://github.com/ADCDS/quak/releases) 页面上，并附有用于校验下载文件的 `SHA256SUMS` 文件。要自行构建：

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
tools/build_app.sh linux      # needs clang, cmake, ninja, pkg-config, GTK 3 headers, liblzma and libmpv
tools/build_app.sh windows    # on Windows, from Git Bash
tools/build_app.sh apk        # needs Java 17
```

Flet 会在首次运行时下载 Flutter 和 Android SDK。构建输出位于 `app/build/`。

维护者也可以在代码仓库的 Actions 标签页中运行 `build` 工作流。

使用不同密钥签名的 APK 无法相互更新，因此要在不同构建之间切换，请先卸载旧版本。这会移除应用的配对信息及其下载的文件。

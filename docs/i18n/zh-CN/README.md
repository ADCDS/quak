> 本页由机器翻译，可能存在错误。请以[英文版](https://github.com/ADCDS/quak/blob/main/README.md)为准。[欢迎提出修正](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations)。

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/src/quak/gui/assets/logo.png" width="128" alt="Quak 徽标：一只黄色的鸭子，眼睛是相机镜头"></p>

<h1 align="center">Quak</h1>

<p align="center">适用于 GoPro HERO11 Black 的开源遥控工具：包含命令行工具，以及桌面端和 Android 应用。</p>

<p align="center"><a href="https://github.com/ADCDS/quak/blob/main/README.md">English</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · <a href="../fr/README.md">Français</a> · <a href="../de/README.md">Deutsch</a> · <a href="../ru/README.md">Русский</a> · <b>简体中文</b> · <a href="../ja/README.md">日本語</a></p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/hero.png" alt="桌面端和手机上的 Quak，显示来自模拟相机的实时预览"></p>

Quak 通过 Bluetooth 和相机自身的 Wi-Fi 与相机通信，使用的是 GoPro 公开的 [Open GoPro API](https://gopro.github.io/OpenGoPro/)。无需账号，也不使用云服务。

> **状态：早期版本，只在一台相机上测试过**。它是围绕一台 HERO11 Black（固件 2.20）开发的。其他型号会收到警告，并且可能无法正常工作。在依赖它之前，请先查看[测试情况](#测试情况)。

## 没有相机也能试用

Quak 内置一台使用真实协议通信的模拟相机，您可以先试用所有功能。需要 Python 3.11 或更高版本。

```bash
pipx install 'quak-camera[gui]'               # or pip, in a virtual environment

quak --sim info                               # the command line
quak --sim schedule arm +2m --duration 15s
quak-gui --sim                                # the app (the first launch downloads about 45 MB)
```

您也可以从 [Releases](https://github.com/ADCDS/quak/releases) 下载适用于 Linux、Windows 或 Android 的现成应用。各系统的详细说明请见[安装指南](install.md)。

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/terminal.gif" width="720" alt="quak 命令读取模拟相机并设定定时拍摄"></p>

## 功能

- **遥控**：录制、拍照、标记 HiLight，更改预设和 HERO11 的所有设置。
- **定时拍摄**：先同步相机时钟，再为相机设定拍摄时间和时长。相机会自动开始和停止拍摄，即使您在设定后将其关闭也一样。
- **实时预览**：通过相机的 Wi-Fi 进行。相机录制时无法传输画面，因此预览会暂停，直到您停止录制。
- **媒体**：浏览存储卡，下载片段，也可以删除片段，删除前的确认提示会写明文件名。
- **支持您的语言**：应用会跟随系统语言，您也可以在**更多**中选择：English、Português (Brasil)、Español、Français、Deutsch、Русский、简体中文或日本語。命令行始终使用英文。
- **直播**：推流到 RTMP 服务器，例如 YouTube 或 Twitch。此功能处于**实验阶段**，详见[已知问题](troubleshooting.md#已知问题)。

<p align="center">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-cameras.png" width="230" alt="“相机”界面，显示电量、存储卡空间和时钟">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-schedule.png" width="230" alt="已设定的定时拍摄，带倒计时">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-settings.png" width="230" alt="相机设置：仅限专业控制的设置显示为灰色，并说明原因">
</p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/desktop-media.png" width="760" alt="桌面端“媒体”界面：片段网格和所选片段的详细信息"></p>

截图来自模拟相机，画面是绘制的场景，并非真实拍摄的素材。

## 连接您的相机使用

按上述方式安装，并安装[安装指南](install.md)中针对您的系统列出的其他组件。然后配对一次。在相机上打开 **首选项 > 无线连接 > 连接设备 > GoPro Quik 应用（Preferences > Connections > Connect Device > GoPro Quik App）**，保持该界面打开，同时运行：

```bash
quak pair          # finds the camera on its pairing screen and remembers it
quak info
quak record --seconds 5
quak schedule arm +2m --duration 15s
quak media list
```

`quak --help` 会列出所有命令。在应用中，请使用**添加相机**，它会引导您完成相同的步骤。

**Android**：从 [Releases](https://github.com/ADCDS/quak/releases) 下载 APK。需要 Android 10 或更高版本。

正式使用前，请阅读 [Quak 会对您的电脑和相机做哪些更改](safety.md)。简而言之：

- 在电脑上，查看媒体或预览时，会有一块 Wi-Fi 网卡暂时转到相机的网络。Quak 会避开您正在用来上网的网卡；如果只有这一块网卡，会先询问您。
- 定时拍摄会更改相机的时长设置，并且不会改回去，这也会限制您手动开始的录制。发生这种情况时，Quak 会告诉您。

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/how-it-connects.svg" width="760" alt="Bluetooth 传输控制指令和设置。相机自身的 Wi-Fi 传输预览和媒体。直播时，相机会加入您的 Wi-Fi。"></p>

## 测试情况

已在一台真实的 HERO11 Black 上测试，在 Linux 上运行了端到端测试套件，在一部 Android 手机上进行了手动测试：

| 功能 | Linux | Android | Windows |
|---|---|---|---|
| 配对和重新连接 | 已测试 | 已测试 | 未测试 |
| 状态、时钟同步 | 已测试 | 已测试 | 未测试 |
| 录制 | 已测试 | 已测试 | 未测试 |
| HiLight、预设、设置 | 已测试 | 未测试 | 未测试 |
| 定时拍摄 | 已测试 | 未测试 | 未测试 |
| 实时预览 | 已测试 | 已测试 | 未测试 |
| 媒体：列出、下载、删除 | 已测试 | 已测试 | 未测试 |
| 直播 | 实验性 | 未测试 | 未测试 |

**Windows 版处于实验阶段**。Windows 版可以编译，单元测试也能通过，但从未启动过，也从未与相机一起使用过。直播曾经端到端跑通，之后在测试相机上停止工作，原因目前尚不清楚。详情请见 [`docs/testing.md`](https://github.com/ADCDS/quak/blob/main/docs/testing.md)。

## 文档

- [安装指南](install.md)：Linux、Windows 和 Android，以及构建安装包
- [安全说明](safety.md)：Quak 会更改和存储什么、可以删除什么，以及如何恢复
- [故障排除](troubleshooting.md)：常见问题和已知问题
- [架构](https://github.com/ADCDS/quak/blob/main/docs/architecture.md)：代码的组织方式
- [测试](https://github.com/ADCDS/quak/blob/main/docs/testing.md)：测试套件和硬件测试结果
- [协议笔记](https://github.com/ADCDS/quak/blob/main/docs/protocol.md)：官方文档之外，我们了解到的相机相关信息
- [参与贡献](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md)

## 它是如何开发的

Quak 的开发大量借助了 AI。代码、测试、文档和徽标大部分由 Claude 编写，测试套件也由 Claude 在真实相机上运行；这一切都在一个人的指导下进行，此人也亲自在相机和手机上试用过。上述硬件方面的说法仅来自这些测试，没有其他依据。

## 许可证和免责声明

MIT 许可证：详见 [LICENSE](https://github.com/ADCDS/quak/blob/main/LICENSE)。打包的应用包含遵循各自许可证的其他软件，其中包括 LGPL 库：详见 [THIRD_PARTY_NOTICES.md](https://github.com/ADCDS/quak/blob/main/THIRD_PARTY_NOTICES.md)。

Quak 是一个独立项目，与 GoPro, Inc. 没有关联，也未获其认可或赞助。GoPro、HERO 和 Quik 是 GoPro, Inc. 的商标。详见 [NOTICE](https://github.com/ADCDS/quak/blob/main/NOTICE)。

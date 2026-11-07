> 本页由机器翻译，可能存在错误。请以[英文版](https://github.com/ADCDS/quak/blob/main/docs/troubleshooting.md)为准。[欢迎提出修正](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations)。

# 故障排除

## 配对和连接

**“没有相机处于配对界面”（No camera is on its pairing screen）**。除非打开此界面，否则相机会忽略它不认识的设备：**首选项 > 无线连接 > 连接设备 > GoPro Quik 应用（Preferences > Connections > Connect Device > GoPro Quik App）**。请打开该界面，然后再次运行 `quak pair`。

**配对手机后再配对电脑失败**。从另一台设备连接会关闭相机的配对界面。请一次只配对一台设备，并在配对下一台之前重新打开该界面。

**提示相机“45 秒内未发现”（not seen within 45 s）**。请确认相机已开机且在附近。不在配对状态时，HERO11 的广播间隔可能长达 10–20 秒，休眠中的相机可能需要更长时间。请重试，或按相机上的按钮将其唤醒。

**能连接，但一直无法完成设置**。曾有一次，一台相机在连接 USB 时断电，之后便不再提供 Bluetooth 服务，直到重启才恢复。请关闭相机再重新打开。

**相机拒绝录制或更改设置，且没有给出原因**。它可能卡在了 Turbo Transfer 模式，该模式会阻止录制、设置和预览。Quak 在下载时会开启此模式，因此如果下载过程中连接中断，此模式可能会一直开启。应用会在下次连接时将其清除；命令行则会在错误信息中提示：运行 `quak turbo off`。

## Bluetooth 错误

**“Bluetooth 停止响应”（Bluetooth stopped responding）**。请确认 Bluetooth 已开启。如果在 Linux 上反复出现，请查看内核日志：

```bash
journalctl -k | grep "tx timeout"
```

如果出现 `command 0x2041 tx timeout` 这样的行，并且随后 USB 断开，说明 Bluetooth 适配器崩溃了。Quak 无法阻止这种情况。曾有一个 USB Bluetooth 5.4 适配器一天内崩溃超过 20 次，换了不同端口也一样，即使没有其他程序在使用它。适配器恢复后处于关闭状态，请用 `bluetoothctl power on` 将其打开。如果这种情况经常发生，请换一个适配器试试。

**适配器崩溃后提示“45 秒内未发现”（Not seen within 45 s）**。同一个适配器有时恢复后显示已开启，却什么也收不到，日志中也没有错误。如果您的相机已开机，而 Bluetooth 扫描也找不到附近的其他设备，请关闭 Bluetooth 再重新打开：先运行 `bluetoothctl power off`，再运行 `bluetoothctl power on`。

## Wi-Fi、媒体和预览

**“Wi-Fi 网卡 X 是这台电脑的网络连接”（Wi-Fi adapter X is this computer's network connection）**。未经询问，Quak 不会占用您的网络连接。请插入第二块网卡，或用 `--wifi-adapter X` 允许此操作。应用则会直接询问您。

**手机上“正在加入相机的 Wi-Fi”（Joining the camera's Wi-Fi）耗时很长**。如果相机的 Wi-Fi 之前是关闭的，手机可能需要 30 秒或更长时间才能发现它。

**手机提示未加入，并询问“是否拒绝了连接提示？”（was the Connect prompt declined?）**。Android 在加入相机的网络之前会询问一次。如果您没有看到提示，请重试：这也可能意味着手机没能及时找到该网络。

**预览是空白的**。相机录制时会停止传输画面，所以面板上会有相应提示。停止录制或更改模式后，视频流会在约 10 秒内恢复，画面会在约 20 秒内恢复。

**“相机未能启动预览”（The camera didn't start its preview）**。相机拒绝了请求。请稍后重试。对于被强制结束的应用遗留的视频流，Quak 已经会自动清除。

**“相机没有传来画面”（No picture came from the camera）**。Quak 已启动预览，但 30 秒内没有收到视频。请重试。如果在电脑上反复出现，请检查防火墙是否允许端口 8554 的 UDP 入站流量，相机会将视频发送到该端口。

**相机的 Wi-Fi 一直开启**。预览期间被强制结束的应用无法将其关闭。请运行 `quak ap off`。

**Quak 被强制结束后，我的电脑断了 Wi-Fi**。请参阅[如果 Quak 被强制结束](safety.md#如果-quak-被强制结束)。

## 应用

**`quak-gui` 提示没有显示器**。您所在的机器没有屏幕，例如通过 SSH 连接时。`quak-gui --web` 可以将应用提供给同一台机器上的浏览器。

**首次启动时似乎卡住了**。桌面客户端约 45 MB，只需下载一次。

**Android 应用无法更新**。使用不同密钥签名的 APK 无法相互更新。请先卸载旧版本，这会移除它的配对信息和下载的文件。

## 已知问题

**直播在加入网络时可能会卡住（实验性功能）**。相机连接到 Wi-Fi 网络并完成握手，但从不请求 IP 地址，因此加入操作会在 60 秒后超时。直播曾在测试相机上端到端跑通，包括问题出现当天的两次，但之后每次尝试都会卡住。在受影响的相机上：

| 尝试 | 结果 |
|---|---|
| 让相机休眠再唤醒 | 仍然卡住 |
| 重启相机 | 仍然卡住 |
| 重新插拔电脑的 Wi-Fi 网卡 | 仍然卡住 |
| 使用相机从未见过的网络名称 | 仍然卡住 |
| 用手机加入同一网络 | 正常 |

网络本身没有问题，所以是相机这边没有动静。目前不清楚原因，也不清楚是否由 Quak 发送的某些内容引起。相机上的**重置连接（Reset Connections）** 尚未尝试过，因为它也会移除所有配对。如果您遇到此问题或成功解决，请[提交 issue](https://github.com/ADCDS/quak/issues)。

**偶尔可能点击无效**。相机报告变更时，某些界面会完全重绘，此时的点击会丢失。请再点一次。“媒体”和“诊断”界面已不再有此问题。

**Windows 版从未与相机一起运行过**。它处于实验阶段，请报告您发现的问题。

> このページは機械翻訳であり、誤りが含まれている可能性があります。基準となるのは[英語版](https://github.com/ADCDS/quak/blob/main/docs/install.md)です。[修正を歓迎します](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations)。

# Quakのインストール

試すだけならカメラは不要です。すべてのコマンドで`--sim`を指定でき、シミュレーションカメラを使用します。

## Linux

必要なもの:

- Python 3.11以降
- 実機のカメラを使う場合: BluetoothアダプターとBlueZ(ほとんどのデスクトップディストリビューションに含まれています)、およびカメラのWi-Fiに接続するための、`nmcli`を備えたNetworkManager
- FFmpeg(デスクトップアプリのライブプレビューと`quak preview --play`で使用します。後者には`ffplay`も必要です)

```bash
pipx install 'quak-camera[gui]'     # or 'quak-camera' for just the command line
```

パッケージ名が`quak-camera`なのは、PyPIの`quak`が無関係のプロジェクトだからです。コマンドは`quak`と`quak-gui`です。

または、[Releases](https://github.com/ADCDS/quak/releases)から`.tar.gz`をダウンロードして展開し、`./quak`を実行します。glibc 2.34以降(Ubuntu 22.04、Debian 12)、GTK 3、libmpvが必要です。DebianとUbuntuでは`sudo apt install libgtk-3-0 libmpv2`でインストールできます。

初回の`quak-gui`では、Fletデスクトップクライアント(約45 MB)が`~/.flet`にダウンロードされます。

安価なUSBアダプターなど、2つ目のWi-Fiアダプターがあると便利です。カメラに接続するアダプターは、それまで接続していたネットワークから離れてしまうためです。`--wifi-adapter NAME`または`QUAK_WIFI_ADAPTER`で選択できます。アダプターが1つしかない場合は、Quakが先に確認します。

## Windows(実験的)

カメラで実行したことも、そもそも起動したこともないため、問題が起きるものと考えてください。問題があればぜひご報告ください。

```powershell
py -m venv quak-env
quak-env\Scripts\activate
pip install "quak-camera[gui]"
```

または、[Releases](https://github.com/ADCDS/quak/releases)から`.zip`をダウンロードして展開し、`quak.exe`を実行します。コード署名されていないため、Windowsが警告を表示する場合があります。

## Android

[Releases](https://github.com/ADCDS/quak/releases)からAPKをダウンロードしてインストールします。ブラウザーまたはファイルマネージャーからのインストールを許可するよう、Androidから求められます。Android 10以降が必要です。また、Bluetoothと位置情報の権限を求められます。AndroidではBluetoothスキャンに位置情報の権限が必要なためです。

APKがこのプロジェクトのものであることを確認するには、`apksigner verify --print-certs`で表示される証明書のSHA-256ダイジェストを次の値と比較してください。

```text
fec9c12dc325dd08272a9163c60e033519d74ebd98a31e133b7b145b259bd59b
```

Quakが初めてカメラのWi-Fiに接続するとき、Androidから接続の確認を求められます。

ダウンロードしたファイルは、タップして共有または保存するまでアプリ内に保管されます。アプリをアンインストールすると、共有していないファイルは削除されます。

## カメラを探す

```bash
quak scan                   # lists cameras nearby, with their addresses
quak pair                   # pairs the one on its pairing screen
quak pair --address AA:BB:CC:DD:EE:FF    # or choose one
```

カメラでペアリング画面を開くには、**ユーザー設定 > ワイヤレス接続 > デバイスを接続 > GoPro Quikアプリ(Preferences > Connections > Connect Device > GoPro Quik App)** を開きます。

## パッケージのビルド

ほとんどの方には必要ありません。ビルド済みパッケージは[Releases](https://github.com/ADCDS/quak/releases)ページにあり、ダウンロードの確認用に`SHA256SUMS`ファイルも用意されています。自分でビルドする場合は、次を実行します。

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
tools/build_app.sh linux      # needs clang, cmake, ninja, pkg-config, GTK 3 headers, liblzma and libmpv
tools/build_app.sh windows    # on Windows, from Git Bash
tools/build_app.sh apk        # needs Java 17
```

初回はFletがFlutterとAndroid SDKをダウンロードします。出力先は`app/build/`です。

メンテナーは、リポジトリのActionsタブから`build`ワークフローを実行することもできます。

異なる鍵で署名されたAPK同士は互いに更新できないため、ビルドを切り替えるには先に古いものをアンインストールしてください。アンインストールすると、アプリのペアリング情報とダウンロードしたファイルは削除されます。

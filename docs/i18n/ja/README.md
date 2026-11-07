> このページは機械翻訳であり、誤りが含まれている可能性があります。基準となるのは[英語版](https://github.com/ADCDS/quak/blob/main/README.md)です。[修正を歓迎します](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations)。

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/src/quak/gui/assets/logo.png" width="128" alt="Quakのロゴ: カメラのレンズを目にした黄色いアヒル"></p>

<h1 align="center">Quak</h1>

<p align="center">GoPro HERO11 Black用のオープンソースのリモコンです。コマンドラインツールと、デスクトップおよびAndroid向けのアプリがあります。</p>

<p align="center"><a href="https://github.com/ADCDS/quak/blob/main/README.md">English</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · <a href="../fr/README.md">Français</a> · <a href="../de/README.md">Deutsch</a> · <a href="../ru/README.md">Русский</a> · <a href="../zh-CN/README.md">简体中文</a> · <b>日本語</b></p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/hero.png" alt="デスクトップとスマートフォンで動作するQuak。シミュレーションカメラのライブプレビューを表示している"></p>

QuakはBluetoothとカメラ自体のWi-Fiを介してカメラと通信し、GoProが公開している[Open GoPro API](https://gopro.github.io/OpenGoPro/)を使用します。アカウントもクラウドも不要です。

> **ステータス: 開発の初期段階で、テストしたカメラは1台だけです。** HERO11 Black 1台(ファームウェア2.20)を基準に作られています。他のモデルでは警告が表示され、動作しない場合があります。頼りにする前に、[テスト済みの範囲](#テスト済みの範囲)をご確認ください。

## カメラなしで試す

Quakには実際のプロトコルで通信するシミュレーションカメラが含まれているので、まずはすべての機能を試せます。Python 3.11以降が必要です。

```bash
pipx install 'quak-camera[gui]'               # or pip, in a virtual environment

quak --sim info                               # the command line
quak --sim schedule arm +2m --duration 15s
quak-gui --sim                                # the app (the first launch downloads about 45 MB)
```

または、[Releases](https://github.com/ADCDS/quak/releases)からLinux、Windows、Android向けのビルド済みアプリをダウンロードできます。各システムの詳細は[インストールガイド](install.md)をご覧ください。

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/terminal.gif" width="720" alt="シミュレーションカメラの情報を読み取り、スケジュールキャプチャーを予約するquakコマンド"></p>

## できること

- **リモート操作:** 録画、写真撮影、HiLightのタグ付け、プリセットの変更、HERO11のすべての設定の変更ができます。
- **スケジュールキャプチャー:** カメラの時計を先に同期したうえで、時刻と長さを指定してカメラを予約します。予約後にカメラの電源を切っても、カメラが自動で開始・停止します。
- **ライブプレビュー**はカメラのWi-Fi経由で表示します。カメラは録画中にストリーミングできないため、録画を停止するまでプレビューは一時停止します。
- **メディア:** カードの内容を閲覧し、クリップをダウンロードしたり削除したりできます。削除時には、ファイル名を示した確認が表示されます。
- **お使いの言語で:** アプリはシステムの言語に従います。**その他**画面で English、Português (Brasil)、Español、Français、Deutsch、Русский、简体中文、日本語から選ぶこともできます。コマンドラインは英語のままです。
- **ライブ配信:** YouTubeやTwitchなどのRTMPサーバーへ配信できます。これは**実験的**な機能です。[既知の問題](troubleshooting.md#既知の問題)をご覧ください。

<p align="center">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-cameras.png" width="230" alt="バッテリー、カードの空き容量、時計を表示しているカメラ画面">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-schedule.png" width="230" alt="カウントダウンを表示している、予約済みのスケジュールキャプチャー">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-settings.png" width="230" alt="カメラ設定。プロコントロール専用の設定がグレー表示され、その理由が示されている">
</p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/desktop-media.png" width="760" alt="デスクトップのメディア画面: クリップのグリッドと、選択したクリップの詳細"></p>

スクリーンショットはシミュレーションカメラのもので、実際の映像ではなく描画したシーンを使用しています。

## お使いのカメラで使う

上記の手順でインストールし、[インストールガイド](install.md)に記載されている、お使いのシステムに必要なものも用意してください。その後、ペアリングを一度行います。カメラで **ユーザー設定 > ワイヤレス接続 > デバイスを接続 > GoPro Quikアプリ(Preferences > Connections > Connect Device > GoPro Quik App)** を開き、その画面を開いたまま次を実行してください。

```bash
quak pair          # finds the camera on its pairing screen and remembers it
quak info
quak record --seconds 5
quak schedule arm +2m --duration 15s
quak media list
```

`quak --help`ですべてのコマンドを確認できます。アプリでは**カメラを追加**を使うと、同じ手順を案内します。

**Android:** [Releases](https://github.com/ADCDS/quak/releases)からAPKをダウンロードしてください。Android 10以降が必要です。

実際に使用する前に、[Quakがコンピューターとカメラに加える変更](safety.md)をお読みください。要点は次のとおりです。

- コンピューターでは、メディアやプレビューを表示すると、Wi-Fiアダプターがしばらくカメラのネットワークに切り替わります。Quakはインターネット接続に使っているアダプターを避け、それしかない場合は先に確認します。
- スケジュールキャプチャーはカメラの撮影時間の設定を変更し、変更したままにします。これにより、手動で開始する録画にも制限がかかります。この変更が行われると、Quakが通知します。

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/how-it-connects.svg" width="760" alt="Bluetoothは操作と設定を伝送します。カメラ自体のWi-Fiはプレビューとメディアを伝送します。ライブ配信では、カメラがお使いのWi-Fiに接続します。"></p>

## テスト済みの範囲

実機のHERO11 Black 1台でテストしました。Linuxではエンドツーエンドのテストスイートで、Androidスマートフォン1台では手動でテストしています。

| 機能 | Linux | Android | Windows |
|---|---|---|---|
| ペアリングと再接続 | テスト済み | テスト済み | 未テスト |
| ステータス、時計の同期 | テスト済み | テスト済み | 未テスト |
| 録画 | テスト済み | テスト済み | 未テスト |
| HiLight、プリセット、設定 | テスト済み | 未テスト | 未テスト |
| スケジュールキャプチャー | テスト済み | 未テスト | 未テスト |
| ライブプレビュー | テスト済み | テスト済み | 未テスト |
| メディア: 一覧、ダウンロード、削除 | テスト済み | テスト済み | 未テスト |
| ライブ配信 | 実験的 | 未テスト | 未テスト |

**Windowsは実験的です。** Windowsビルドはコンパイルでき、単体テストにも合格していますが、起動したことも、カメラで使用したことも一度もありません。ライブ配信はエンドツーエンドで動作していましたが、その後テスト用カメラで動作しなくなりました。原因はまだわかっていません。詳細は[`docs/testing.md`](https://github.com/ADCDS/quak/blob/main/docs/testing.md)をご覧ください。

## ドキュメント

- [インストールガイド](install.md): Linux、Windows、Androidでの手順と、パッケージのビルド
- [安全性](safety.md): Quakが変更・保存するもの、削除できるもの、復旧方法
- [トラブルシューティング](troubleshooting.md): よくある問題と既知の問題
- [アーキテクチャ](https://github.com/ADCDS/quak/blob/main/docs/architecture.md): コードの構成
- [テスト](https://github.com/ADCDS/quak/blob/main/docs/testing.md): テストスイートとハードウェアでの結果
- [プロトコルノート](https://github.com/ADCDS/quak/blob/main/docs/protocol.md): 公式ドキュメント以外でカメラについて判明したこと
- [コントリビューション](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md)

## 開発方法

QuakはAIの支援を大きく受けて開発されました。コード、テスト、ドキュメントの大部分とロゴはClaudeが作成し、実機のカメラでのテストスイートの実行もClaudeが行いました。指示を出したのは1人の人間で、その人もカメラとスマートフォンで実際に試しています。上記のハードウェアに関する記述はこれらのテストのみに基づくものであり、それ以外の根拠はありません。

## ライセンスと免責事項

MITライセンスです。[LICENSE](https://github.com/ADCDS/quak/blob/main/LICENSE)をご覧ください。パッケージ版のアプリには、LGPLライブラリをはじめ、それぞれ独自のライセンスを持つ他のソフトウェアが含まれています。[THIRD_PARTY_NOTICES.md](https://github.com/ADCDS/quak/blob/main/THIRD_PARTY_NOTICES.md)をご覧ください。

Quakは独立したプロジェクトであり、GoPro, Inc.と提携しておらず、GoPro, Inc.による承認や後援も受けていません。GoPro、HERO、QuikはGoPro, Inc.の商標です。[NOTICE](https://github.com/ADCDS/quak/blob/main/NOTICE)をご覧ください。

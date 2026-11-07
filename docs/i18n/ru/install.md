> Эта страница — машинный перевод, в ней могут быть ошибки. Основной считается [английская версия](https://github.com/ADCDS/quak/blob/main/docs/install.md). [Исправления приветствуются](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Установка Quak

Чтобы попробовать Quak, камера не нужна: каждая команда принимает `--sim`, и тогда используется симулятор камеры.

## Linux

Что нужно:

- Python 3.11 или новее
- Для настоящей камеры: адаптер Bluetooth и BlueZ (есть в большинстве настольных дистрибутивов), а также NetworkManager с `nmcli` для
  подключения к Wi-Fi камеры
- FFmpeg для живого превью в настольном приложении и для `quak preview --play`, которому также нужен `ffplay`

```bash
pipx install 'quak-camera[gui]'     # or 'quak-camera' for just the command line
```

Пакет называется `quak-camera`, потому что `quak` на PyPI — это другой, не связанный с нами проект. Команды называются `quak` и `quak-gui`.

Или скачайте `.tar.gz` из раздела [Releases](https://github.com/ADCDS/quak/releases), распакуйте его и запустите `./quak`. Нужны glibc 2.34 или
новее (Ubuntu 22.04, Debian 12), GTK 3 и libmpv: в Debian и Ubuntu выполните `sudo apt install libgtk-3-0 libmpv2`.

При первом запуске `quak-gui` скачивает настольный клиент Flet, около 45 МБ, в `~/.flet`.

Полезно иметь второй адаптер Wi-Fi, например недорогой USB-адаптер, потому что адаптер, подключающийся к камере, покидает сеть, в которой
был. Выберите его с помощью `--wifi-adapter NAME` или `QUAK_WIFI_ADAPTER`. Если адаптер один, Quak сначала спросит.

## Windows (экспериментально)

Эту версию не запускали с камерой и вообще ни разу не запускали, поэтому ожидайте проблем и, пожалуйста, сообщайте о них.

```powershell
py -m venv quak-env
quak-env\Scripts\activate
pip install "quak-camera[gui]"
```

Или скачайте `.zip` из раздела [Releases](https://github.com/ADCDS/quak/releases), распакуйте его и запустите `quak.exe`. У него нет цифровой подписи кода,
поэтому Windows может выдать предупреждение.

## Android

Скачайте APK из раздела [Releases](https://github.com/ADCDS/quak/releases) и установите его. Android попросит разрешить установку из
браузера или файлового менеджера. Нужен Android 10 или новее, а приложение запрашивает доступ к Bluetooth и местоположению: Android требует
разрешение на местоположение для поиска по Bluetooth.

Чтобы убедиться, что APK получен из этого проекта, сравните SHA-256-хеш сертификата из вывода `apksigner verify --print-certs` с этим:

```text
fec9c12dc325dd08272a9163c60e033519d74ebd98a31e133b7b145b259bd59b
```

Когда Quak впервые подключается к Wi-Fi камеры, Android просит подтвердить подключение.

Скачанные файлы остаются внутри приложения, пока вы не нажмёте на один из них, чтобы поделиться им или сохранить. При удалении приложения
удаляются все файлы, которыми вы не поделились.

## Как найти камеру

```bash
quak scan                   # lists cameras nearby, with their addresses
quak pair                   # pairs the one on its pairing screen
quak pair --address AA:BB:CC:DD:EE:FF    # or choose one
```

Чтобы открыть экран сопряжения на камере: **Параметры > Подключения > Подключить устройство > GoPro Quik (Preferences > Connections > Connect Device > GoPro Quik App)**.

## Сборка пакетов

Большинству людей это не нужно: готовые сборки есть на странице [Releases](https://github.com/ADCDS/quak/releases) вместе с файлом `SHA256SUMS`
для проверки скачанного. Чтобы собрать самостоятельно:

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
tools/build_app.sh linux      # needs clang, cmake, ninja, pkg-config, GTK 3 headers, liblzma and libmpv
tools/build_app.sh windows    # on Windows, from Git Bash
tools/build_app.sh apk        # needs Java 17
```

При первом запуске Flet скачивает Flutter и Android SDK. Результат появится в `app/build/`.

Сопровождающие также могут запустить рабочий процесс `build` на вкладке Actions репозитория.

APK, подписанные разными ключами, не могут обновлять друг друга, поэтому, чтобы перейти на другую сборку, сначала удалите старую. При этом
удаляются сопряжение приложения и его скачанные файлы.

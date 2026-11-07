> Эта страница — машинный перевод, в ней могут быть ошибки. Основной считается [английская версия](https://github.com/ADCDS/quak/blob/main/README.md). [Исправления приветствуются](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/src/quak/gui/assets/logo.png" width="128" alt="Логотип Quak: жёлтая утка с объективом камеры вместо глаза"></p>

<h1 align="center">Quak</h1>

<p align="center">Пульт с открытым исходным кодом для GoPro HERO11 Black: инструмент командной строки и приложение для компьютера и Android.</p>

<p align="center"><a href="https://github.com/ADCDS/quak/blob/main/README.md">English</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · <a href="../fr/README.md">Français</a> · <a href="../de/README.md">Deutsch</a> · <b>Русский</b> · <a href="../zh-CN/README.md">简体中文</a> · <a href="../ja/README.md">日本語</a></p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/hero.png" alt="Quak на компьютере и телефоне: живое превью с симулятора камеры"></p>

Quak общается с камерой по Bluetooth и через собственный Wi-Fi камеры, используя публичный
[Open GoPro API](https://gopro.github.io/OpenGoPro/) от GoPro. Без аккаунта и без облака.

> **Статус: ранняя версия, проверена на одной камере.** Quak создавался для одной HERO11 Black (прошивка 2.20). На других
> моделях вы увидите предупреждение, и они могут не работать. Прежде чем полагаться на него, посмотрите, [что было проверено](#что-было-проверено).

## Как попробовать без камеры

В Quak есть симулятор камеры, который работает по настоящему протоколу, так что сначала можно попробовать всё. Нужен
Python 3.11 или новее.

```bash
pipx install 'quak-camera[gui]'               # or pip, in a virtual environment

quak --sim info                               # the command line
quak --sim schedule arm +2m --duration 15s
quak-gui --sim                                # the app (the first launch downloads about 45 MB)
```

Или скачайте готовое приложение для Linux, Windows или Android из раздела [Releases](https://github.com/ADCDS/quak/releases).
В [руководстве по установке](install.md) есть подробности для каждой системы.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/terminal.gif" width="720" alt="Команда quak считывает данные симулятора камеры и планирует отложенную съёмку"></p>

## Что умеет Quak

- **Дистанционное управление:** запись, фото, метки HiLight, смена пресетов и всех настроек HERO11.
- **Отложенная съёмка:** задайте камере время и длительность, её часы сначала синхронизируются. Камера сама начнёт и остановит
  запись, даже если выключить её после планирования.
- **Живое превью** через Wi-Fi камеры. Во время записи камера не может передавать поток, поэтому превью приостанавливается, пока
  вы не остановите запись.
- **Медиа:** просмотр карты, скачивание клипов и их удаление с подтверждением, в котором указано имя файла.
- **На вашем языке:** приложение следует языку системы, или язык можно выбрать в разделе **Ещё**: English, Português (Brasil),
  Español, Français, Deutsch, Русский, 简体中文 или 日本語. Командная строка остаётся на английском.
- **Прямая трансляция** на RTMP-сервер, например YouTube или Twitch. Эта функция **экспериментальная**: см. [известные проблемы](troubleshooting.md#известные-проблемы).

<p align="center">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-cameras.png" width="230" alt="Экран «Камеры»: заряд батареи, место на карте и часы">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-schedule.png" width="230" alt="Запланированная отложенная съёмка с обратным отсчётом">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-settings.png" width="230" alt="Настройки камеры: параметры только для Pro выделены серым, причина указана">
</p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/desktop-media.png" width="760" alt="Экран «Медиа» на компьютере: сетка клипов и сведения о выбранном"></p>

Снимки экрана сделаны на симуляторе камеры: вместо реальной съёмки на них нарисованная сцена.

## Как использовать с вашей камерой

Установите Quak, как описано выше, а также то, что для вашей системы указано в [руководстве по установке](install.md). Затем один раз
выполните сопряжение. На камере откройте **Параметры > Подключения > Подключить устройство > GoPro Quik (Preferences > Connections > Connect Device > GoPro Quik App)** и не закрывайте этот экран, пока выполняете:

```bash
quak pair          # finds the camera on its pairing screen and remembers it
quak info
quak record --seconds 5
quak schedule arm +2m --duration 15s
quak media list
```

`quak --help` показывает все команды. В приложении используйте **Добавить камеру**: эта функция проведёт вас через те же шаги.

**Android:** скачайте APK из раздела [Releases](https://github.com/ADCDS/quak/releases). Нужен Android 10 или новее.

Прежде чем использовать Quak всерьёз, прочитайте, [что Quak меняет на вашем компьютере и камере](safety.md). Вкратце:

- На компьютере просмотр медиа или превью на время переводит адаптер Wi-Fi в сеть камеры. Quak избегает адаптера, через который вы
  выходите в интернет, а если он единственный, сначала спрашивает.
- Отложенная съёмка меняет настройку длительности (Duration) на камере и оставляет её изменённой, а это ограничивает и записи,
  которые вы запускаете вручную. Quak сообщает, когда это происходит.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/how-it-connects.svg" width="760" alt="По Bluetooth идут управление и настройки. Через собственный Wi-Fi камеры идут превью и медиа. Для прямой трансляции камера подключается к вашей сети Wi-Fi."></p>

## Что было проверено

Проверено на одной настоящей HERO11 Black: сквозными наборами тестов в Linux и вручную на одном телефоне с Android:

| Функция | Linux | Android | Windows |
|---|---|---|---|
| Сопряжение и переподключение | Проверено | Проверено | Не проверено |
| Статус, синхронизация часов | Проверено | Проверено | Не проверено |
| Запись | Проверено | Проверено | Не проверено |
| HiLight, пресеты, настройки | Проверено | Не проверено | Не проверено |
| Отложенная съёмка | Проверено | Не проверено | Не проверено |
| Живое превью | Проверено | Проверено | Не проверено |
| Медиа: список, скачивание, удаление | Проверено | Проверено | Не проверено |
| Прямая трансляция | Экспериментально | Не проверено | Не проверено |

**Windows — экспериментальная платформа.** Сборка для Windows компилируется, и её модульные тесты проходят, но она ни разу не
запускалась и не использовалась с камерой. Прямая трансляция работала от начала до конца, а затем перестала работать на тестовой камере
по причине, которая пока неизвестна. Подробности — в [`docs/testing.md`](https://github.com/ADCDS/quak/blob/main/docs/testing.md).

## Документация

- [Руководство по установке](install.md): Linux, Windows и Android, а также сборка пакетов
- [Безопасность](safety.md): что Quak меняет и хранит, что может удалить и как всё восстановить
- [Устранение неполадок](troubleshooting.md): частые проблемы и известные неполадки
- [Архитектура](https://github.com/ADCDS/quak/blob/main/docs/architecture.md): как устроен код
- [Тестирование](https://github.com/ADCDS/quak/blob/main/docs/testing.md): наборы тестов и результаты на реальном оборудовании
- [Заметки о протоколе](https://github.com/ADCDS/quak/blob/main/docs/protocol.md): что мы узнали о камере помимо официальной документации
- [Участие в разработке](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md)

## Как это создавалось

Quak создан при значительной помощи ИИ. Claude написал большую часть кода, тестов, документации и логотип, а также запускал наборы
тестов на настоящей камере под руководством человека, который тоже пробовал приложение на камере и телефоне. Приведённые выше
утверждения об оборудовании основаны на этих тестах и ни на чём другом.

## Лицензия и отказ от ответственности

MIT: см. [LICENSE](https://github.com/ADCDS/quak/blob/main/LICENSE). Собранные приложения включают другое программное обеспечение под его собственными лицензиями, в том числе библиотеки LGPL: см.
[THIRD_PARTY_NOTICES.md](https://github.com/ADCDS/quak/blob/main/THIRD_PARTY_NOTICES.md).

Quak — независимый проект, не связанный с GoPro, Inc., не одобренный и не спонсируемый ею. GoPro, HERO и Quik — товарные знаки
GoPro, Inc. См. [NOTICE](https://github.com/ADCDS/quak/blob/main/NOTICE).

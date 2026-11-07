> Diese Seite ist eine maschinelle Übersetzung und kann Fehler enthalten. Maßgeblich ist die [englische Version](https://github.com/ADCDS/quak/blob/main/README.md). [Korrekturen sind willkommen](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/src/quak/gui/assets/logo.png" width="128" alt="Quak-Logo: eine gelbe Ente mit einem Kameraobjektiv als Auge"></p>

<h1 align="center">Quak</h1>

<p align="center">Eine Open-Source-Fernbedienung für die GoPro HERO11 Black: ein Kommandozeilen-Tool und eine App für Desktop und Android.</p>

<p align="center"><a href="https://github.com/ADCDS/quak/blob/main/README.md">English</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · <a href="../fr/README.md">Français</a> · <b>Deutsch</b> · <a href="../ru/README.md">Русский</a> · <a href="../zh-CN/README.md">简体中文</a> · <a href="../ja/README.md">日本語</a></p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/hero.png" alt="Quak auf Desktop und Smartphone mit einer Live-Vorschau von einer simulierten Kamera"></p>

Quak spricht mit der Kamera über Bluetooth und das eigene Wi-Fi der Kamera und nutzt dafür GoPros öffentliche
[Open GoPro API](https://gopro.github.io/OpenGoPro/). Kein Konto und keine Cloud.

> **Status: frühes Stadium, an nur einer Kamera getestet.** Quak wurde rund um eine einzige HERO11 Black (Firmware 2.20) entwickelt. Andere Modelle
> bekommen eine Warnung und funktionieren vielleicht nicht. Lies, [was getestet wurde](#was-getestet-wurde), bevor du dich darauf verlässt.

## Ausprobieren ohne Kamera

Quak enthält eine simulierte Kamera, die das echte Protokoll spricht, sodass du zuerst alles ausprobieren kannst. Du brauchst Python 3.11 oder
neuer.

```bash
pipx install 'quak-camera[gui]'               # or pip, in a virtual environment

quak --sim info                               # the command line
quak --sim schedule arm +2m --duration 15s
quak-gui --sim                                # the app (the first launch downloads about 45 MB)
```

Oder lade eine fertige App für Linux, Windows oder Android von [Releases](https://github.com/ADCDS/quak/releases) herunter. Die
[Installationsanleitung](install.md) hat die Details für jedes System.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/terminal.gif" width="720" alt="Der Befehl quak liest eine simulierte Kamera aus und aktiviert eine geplante Aufnahme"></p>

## Was es kann

- **Fernsteuerung:** aufnehmen, Fotos machen, HiLights setzen, Voreinstellungen und jede HERO11-Einstellung ändern.
- **Geplante Aufnahme:** Aktiviere die Kamera für eine Uhrzeit und eine Länge, vorher wird ihre Uhr synchronisiert. Sie startet und stoppt von selbst,
  auch wenn du sie nach dem Aktivieren ausschaltest.
- **Live-Vorschau** über das Wi-Fi der Kamera. Die Kamera kann während der Aufnahme nicht streamen, deshalb pausiert die Vorschau, bis du stoppst.
- **Medien:** die Karte durchsuchen, Clips herunterladen und löschen, mit einer Bestätigung, die die Datei nennt.
- **In deiner Sprache:** Die App folgt der Sprache deines Systems, oder du wählst sie unter **Mehr** aus: English, Português (Brasil),
  Español, Français, Deutsch, Русский, 简体中文 oder 日本語. Die Kommandozeile bleibt auf Englisch.
- **Livestream** zu einem RTMP-Server wie YouTube oder Twitch. Das ist **experimentell**: siehe [bekannte Probleme](troubleshooting.md#bekannte-probleme).

<p align="center">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-cameras.png" width="230" alt="Der Bildschirm Kameras mit Akku, Kartenspeicher und Uhr">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-schedule.png" width="230" alt="Eine aktivierte geplante Aufnahme mit Countdown">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-settings.png" width="230" alt="Kameraeinstellungen, ausgegraute Einstellungen sind nur mit Profi-Bedienfunktionen verfügbar, der Grund wird angezeigt">
</p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/desktop-media.png" width="760" alt="Der Bildschirm Medien auf dem Desktop: ein Raster aus Clips und die Details des ausgewählten Clips"></p>

Die Screenshots stammen von der simulierten Kamera, mit einer gezeichneten Szene statt echter Aufnahmen.

## Mit deiner Kamera nutzen

Installiere es wie oben, dazu das, was [die Installationsanleitung](install.md) für dein System aufführt. Kopple die Kamera dann einmal. Öffne auf der Kamera
**Einstellungen > Verbindungen > Gerät verbinden > GoPro Quik App** und lass diesen Bildschirm offen, während du Folgendes ausführst:

```bash
quak pair          # finds the camera on its pairing screen and remembers it
quak info
quak record --seconds 5
quak schedule arm +2m --duration 15s
quak media list
```

`quak --help` listet alle Befehle auf. In der App nimmst du **Kamera hinzufügen**, das führt dich durch dieselben Schritte.

**Android:** Lade die APK von [Releases](https://github.com/ADCDS/quak/releases) herunter. Sie braucht Android 10 oder neuer.

Bevor du es ernsthaft einsetzt, lies, [was Quak an deinem Computer und deiner Kamera ändert](safety.md). Kurz gesagt:

- Am Computer wechselt beim Ansehen von Medien oder der Vorschau ein Wi-Fi-Adapter für eine Weile ins Netzwerk der Kamera. Quak meidet den
  Adapter, über den du online bist, und fragt vorher nach, wenn es der einzige ist.
- Eine geplante Aufnahme ändert die Einstellung Dauer der Kamera und lässt sie geändert. Das begrenzt auch Aufnahmen, die du von Hand startest.
  Quak sagt dir, wenn das passiert.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/how-it-connects.svg" width="760" alt="Bluetooth überträgt Steuerung und Einstellungen. Das eigene Wi-Fi der Kamera überträgt Vorschau und Medien. Für einen Livestream verbindet sich die Kamera mit deinem Wi-Fi."></p>

## Was getestet wurde

Getestet an einer echten HERO11 Black, mit den End-to-End-Testsuites unter Linux und von Hand auf einem Android-Smartphone:

| Funktion | Linux | Android | Windows |
|---|---|---|---|
| Koppeln und erneutes Verbinden | Getestet | Getestet | Nicht getestet |
| Status, Uhr synchronisieren | Getestet | Getestet | Nicht getestet |
| Aufnahme | Getestet | Getestet | Nicht getestet |
| HiLights, Voreinstellungen, Einstellungen | Getestet | Nicht getestet | Nicht getestet |
| Geplante Aufnahme | Getestet | Nicht getestet | Nicht getestet |
| Live-Vorschau | Getestet | Getestet | Nicht getestet |
| Medien: auflisten, herunterladen, löschen | Getestet | Getestet | Nicht getestet |
| Livestream | Experimentell | Nicht getestet | Nicht getestet |

**Windows ist experimentell.** Der Windows-Build lässt sich kompilieren und seine Unit-Tests bestehen, aber er wurde noch nie gestartet oder mit einer
Kamera benutzt. Der Livestream hat vollständig funktioniert und dann an der Testkamera aufgehört zu funktionieren, aus einem noch unbekannten Grund.
In [`docs/testing.md`](https://github.com/ADCDS/quak/blob/main/docs/testing.md) stehen die Details.

## Dokumentation

- [Installationsanleitung](install.md): Linux, Windows und Android sowie das Bauen der Pakete
- [Sicherheit](safety.md): was Quak ändert und speichert, was es löschen kann und wie du es wieder in Ordnung bringst
- [Fehlerbehebung](troubleshooting.md): häufige Probleme und bekannte Probleme
- [Architektur](https://github.com/ADCDS/quak/blob/main/docs/architecture.md): wie der Code aufgebaut ist
- [Tests](https://github.com/ADCDS/quak/blob/main/docs/testing.md): die Testsuites und Ergebnisse an echter Hardware
- [Protokollnotizen](https://github.com/ADCDS/quak/blob/main/docs/protocol.md): was wir über die Kamera gelernt haben, über die offizielle Doku hinaus
- [Mitwirken](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md)

## Wie es entstanden ist

Quak ist mit starker KI-Unterstützung entstanden. Claude hat den Großteil von Code, Tests und Dokumentation sowie das Logo erstellt und die Testsuites
an einer echten Kamera ausgeführt, angeleitet von einem Menschen, der es auch selbst an Kamera und Smartphone ausprobiert hat. Die Angaben zur Hardware
oben stammen aus diesen Tests und aus nichts anderem.

## Lizenz und rechtliche Hinweise

MIT: siehe [LICENSE](https://github.com/ADCDS/quak/blob/main/LICENSE). Die gepackten Apps enthalten weitere Software unter eigenen Lizenzen, darunter LGPL-Bibliotheken: siehe
[THIRD_PARTY_NOTICES.md](https://github.com/ADCDS/quak/blob/main/THIRD_PARTY_NOTICES.md).

Quak ist ein unabhängiges Projekt, das nicht mit GoPro, Inc. verbunden ist und weder von GoPro, Inc. unterstützt noch gesponsert wird. GoPro, HERO und Quik sind
Marken von GoPro, Inc. Siehe [NOTICE](https://github.com/ADCDS/quak/blob/main/NOTICE).

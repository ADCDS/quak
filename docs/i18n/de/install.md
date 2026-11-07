> Diese Seite ist eine maschinelle Übersetzung und kann Fehler enthalten. Maßgeblich ist die [englische Version](https://github.com/ADCDS/quak/blob/main/docs/install.md). [Korrekturen sind willkommen](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Quak installieren

Du brauchst keine Kamera, um es auszuprobieren: Jeder Befehl akzeptiert `--sim`, das eine simulierte Kamera verwendet.

## Linux

Du brauchst:

- Python 3.11 oder neuer
- Für eine echte Kamera: einen Bluetooth-Adapter und BlueZ (haben die meisten Desktop-Distributionen) sowie NetworkManager mit `nmcli`, um dich mit
  dem Wi-Fi der Kamera zu verbinden
- FFmpeg, für die Live-Vorschau in der Desktop-App und für `quak preview --play`, das außerdem `ffplay` braucht

```bash
pipx install 'quak-camera[gui]'     # or 'quak-camera' for just the command line
```

Das Paket heißt `quak-camera`, weil `quak` auf PyPI ein anderes, nicht verwandtes Projekt ist. Die Befehle sind `quak` und `quak-gui`.

Oder lade die `.tar.gz` von [Releases](https://github.com/ADCDS/quak/releases) herunter, entpacke sie und führe `./quak` aus. Sie braucht glibc 2.34 oder
neuer (Ubuntu 22.04, Debian 12), GTK 3 und libmpv: unter Debian und Ubuntu `sudo apt install libgtk-3-0 libmpv2`.

Beim ersten Start lädt `quak-gui` den Flet-Desktop-Client herunter, etwa 45 MB, nach `~/.flet`.

Ein zweiter Wi-Fi-Adapter hilft, zum Beispiel ein günstiger USB-Adapter, denn der Adapter, der sich mit der Kamera verbindet, verlässt das Netzwerk, in dem
er gerade war. Wähle ihn mit `--wifi-adapter NAME` oder `QUAK_WIFI_ADAPTER` aus. Mit nur einem Adapter fragt Quak vorher nach.

## Windows (experimentell)

Das wurde noch nie mit einer Kamera ausgeführt und überhaupt noch nie gestartet, also rechne mit Problemen und melde sie bitte.

```powershell
py -m venv quak-env
quak-env\Scripts\activate
pip install "quak-camera[gui]"
```

Oder lade die `.zip` von [Releases](https://github.com/ADCDS/quak/releases) herunter, entpacke sie und führe `quak.exe` aus. Sie ist nicht codesigniert,
deshalb warnt Windows vielleicht davor.

## Android

Lade die APK von [Releases](https://github.com/ADCDS/quak/releases) herunter und installiere sie. Android bittet dich, die Installation aus deinem Browser
oder Dateimanager zu erlauben. Sie braucht Android 10 oder neuer und fragt nach Bluetooth und Standort: Android verlangt die Standortberechtigung
für einen Bluetooth-Scan.

Um zu prüfen, ob eine APK aus diesem Projekt stammt, vergleiche den SHA-256-Digest des Zertifikats aus `apksigner verify --print-certs` mit diesem:

```text
fec9c12dc325dd08272a9163c60e033519d74ebd98a31e133b7b145b259bd59b
```

Wenn sich Quak zum ersten Mal mit dem Wi-Fi der Kamera verbindet, bittet dich Android, die Verbindung zu bestätigen.

Downloads bleiben in der App, bis du einen antippst, um ihn zu teilen oder zu speichern. Wenn du die App deinstallierst, werden alle gelöscht, die du nicht geteilt hast.

## Deine Kamera finden

```bash
quak scan                   # lists cameras nearby, with their addresses
quak pair                   # pairs the one on its pairing screen
quak pair --address AA:BB:CC:DD:EE:FF    # or choose one
```

So öffnest du den Kopplungsbildschirm auf der Kamera: **Einstellungen > Verbindungen > Gerät verbinden > GoPro Quik App**.

## Pakete bauen

Die meisten brauchen das nicht: Fertige Builds gibt es auf der Seite [Releases](https://github.com/ADCDS/quak/releases), mit einer Datei `SHA256SUMS`
zum Prüfen der Downloads. So baust du selbst:

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
tools/build_app.sh linux      # needs clang, cmake, ninja, pkg-config, GTK 3 headers, liblzma and libmpv
tools/build_app.sh windows    # on Windows, from Git Bash
tools/build_app.sh apk        # needs Java 17
```

Flet lädt beim ersten Mal Flutter und das Android SDK herunter. Die Ausgabe landet in `app/build/`.

Maintainer können auch den Workflow `build` im Tab Actions des Repositorys starten.

APKs, die mit verschiedenen Schlüsseln signiert sind, können einander nicht aktualisieren. Um zwischen Builds zu wechseln, deinstalliere also zuerst den alten.
Dabei werden die Kopplung der App und ihre Downloads entfernt.

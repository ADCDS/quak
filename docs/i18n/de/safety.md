> Diese Seite ist eine maschinelle Übersetzung und kann Fehler enthalten. Maßgeblich ist die [englische Version](https://github.com/ADCDS/quak/blob/main/docs/safety.md). [Korrekturen sind willkommen](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Was Quak ändert, speichert und löschen kann

Quak steuert eine echte Kamera und kann in das Wi-Fi deines Computers eingreifen. Diese Seite sagt genau, was es tut.

## Auf deinem Computer

**Bluetooth:** Quak koppelt sich ganz normal über das Bluetooth deines Systems mit der Kamera. Sonst ändert es nichts.

**Wi-Fi:** Vorschau, Medien und Downloads brauchen das eigene Wi-Fi-Netzwerk der Kamera.

- **Smartphone:** Android verbindet sich nur für die App, und deine anderen Apps bleiben in deinem normalen Netzwerk.
- **Computer:** Ein Wi-Fi-Adapter verbindet sich für eine Weile mit dem Netzwerk der Kamera und verlässt jedes Netzwerk, in dem er war, bis Quak fertig ist.

Am Computer macht Quak Folgendes:

- Es bevorzugt einen Adapter, über den du nicht online bist. Wenn du nur den einen Adapter hast und ihn gerade benutzt, fragt die App vorher nach, und die
  Kommandozeile stoppt mit einem Fehler, bis du ihn mit `--wifi-adapter NAME` auswählst.
- Unter Linux erstellt es ein temporäres NetworkManager-Profil namens `quak-camap`, das niemals deine Standardroute oder dein DNS übernimmt, und
  löscht es danach. Deine anderen Profile bearbeitet es nicht.
- Unter Linux schaltet es auf diesem Adapter die automatische Verbindung aus, solange es ihn benutzt, weil sich sonst ein gespeichertes Profil für die Kamera
  den Adapter zurückholen könnte. Die automatische Verbindung wird danach wiederhergestellt.
- Es verbindet dich wieder mit deinem vorherigen Netzwerk, wenn es fertig ist. Die App macht das etwa eine Minute, nachdem du Medien verlässt oder die Vorschau stoppst.

### Wenn Quak zwangsweise beendet wird

Die Kommandozeile räumt bei Ctrl-C und bei `kill` auf und stellt wieder her, was sie geändert hat. Wird sie härter beendet oder verliert der Rechner während
der Nutzung den Strom, merkt sich Quak, was es geändert hat, und macht es beim nächsten Start unter Linux rückgängig. Unter Windows gibt es keine automatische
Wiederherstellung.

So stellst du es unter Linux von Hand wieder her, wobei `DEVICE` dein Wi-Fi-Adapter ist:

```bash
nmcli connection delete quak-camap
nmcli device set DEVICE autoconnect yes
nmcli device connect DEVICE
```

Ein Neustart setzt auch die Änderung an der automatischen Verbindung zurück, dann bleibt nur noch das Profil `quak-camap` zu löschen.

### Was gespeichert wird

| Was | Wo | Inhalt |
|---|---|---|
| Gekoppelte Kameras, Einstellungen | `~/.config/quak` unter Linux, `%APPDATA%\quak` unter Windows, der private Speicher der App unter Android | Kameraadressen, Modelle, App-Einstellungen |
| Downloads | `~/Videos/Quak` für die Desktop-App, der private Speicher der App unter Android, und `--out` oder der aktuelle Ordner für die Kommandozeile | Deine Clips |
| Testdaten | `~/.local/state/quak` unter Linux, `%LOCALAPPDATA%\quak` unter Windows | Nur wenn du die Testsuites ausführst |

Quak speichert deine Wi-Fi-Passwörter und Stream-Schlüssel nicht. Es sendet sie an die Kamera, wenn du Quak anweist, sich mit einem Netzwerk zu verbinden oder zu streamen.

### Was in den Logs steht

Der Bildschirm Diagnose, sein exportiertes Log und `quak -v` zeigen die mit der Kamera ausgetauschten Bytes. Passwörter, Stream-Schlüssel, Netzwerknamen,
die Seriennummer der Kamera und der Name deines Computers werden ausgelassen, und es wird angegeben, wie viele Bytes verborgen wurden.

Die Bluetooth-Adresse der Kamera kann trotzdem in Fehlermeldungen und Logs auftauchen, also prüfe ein Log, bevor du es öffentlich teilst.

### Der Netzwerkzugriff der App

`quak-gui --web` stellt die App in einem Browser bereit, nur auf diesem Computer. **Es gibt keine Anmeldung**, und jeder, der sie erreichen kann, kann auf
deiner Kamera aufnehmen und von ihr löschen. `--host` ist also nur in einem Netzwerk sinnvoll, dem du voll vertraust.

## Auf der Kamera

**Quak teilt der Kamera beim Koppeln den Namen deines Computers mit**, als den Namen, den die Kamera für dieses Gerät anzeigt.

**Eine geplante Aufnahme ändert Einstellungen und lässt sie geändert.** Beim Aktivieren:

- Wechselt die Kamera zu den Profi-Bedienfunktionen, weil sie die geplante Aufnahme bei den einfachen Bedienfunktionen ausblendet
- Wird die gewählte Video-Voreinstellung geladen
- Wird die Videodauer gesetzt, was **auch Aufnahmen begrenzt, die du von Hand startest**
- Wird die Uhr der Kamera synchronisiert

Quak sagt dir, wenn das Aktivieren diese Dinge ändert. Stell die Dauer in den Kameraeinstellungen oder in Quak zurück, wenn du fertig bist.

**Livestream:**

- Die Kamera verbindet sich mit dem Wi-Fi-Netzwerk, das du wählst, und speichert es. Nur **Verbindungen zurücksetzen** auf der Kamera entfernt ein gespeichertes Netzwerk,
  und das entfernt auch alle ihre Kopplungen.
- Das Passwort deines Netzwerks und deine Stream-URL, die deinen Stream-Schlüssel enthält, werden über Bluetooth an die Kamera gesendet.
- Bei einer einfachen `rtmp://`-URL gehen Stream-Schlüssel und Video unverschlüsselt an den Server.

**Kamera-Wi-Fi** wird bei Bedarf eingeschaltet und wieder ausgeschaltet, wenn Quak fertig ist, falls Quak es eingeschaltet hat. Eine App, die während der Vorschau
zwangsweise beendet wird, kann es eingeschaltet lassen: Führe `quak ap off` aus oder schalte es auf der Kamera aus.

## Was Quak löschen kann

Nur Clips, die du auswählst. Beim Löschen fragt die App immer nach einer Bestätigung, und in der Kommandozeile braucht es `--yes`. Gelöschte Clips
lassen sich nicht wiederherstellen.

Quak hat keine Möglichkeit, eine Karte zu formatieren, alles zu löschen oder eine Kamera auf Werkseinstellungen zurückzusetzen.

Downloads ersetzen nie eine vorhandene Datei: Eine zweite `GX010001.MP4` wird als `GX010001_1.MP4` gespeichert. Ein abgebrochener Download wird als Fehler
gemeldet und nicht gespeichert, als wäre er vollständig.

## Für Entwickler

`QUAK_DEV=1` zeigt den Selbsttest der App und den Testmodus von Live. Sie ändern viel mehr, also lies zuerst [testing.md](https://github.com/ADCDS/quak/blob/main/docs/testing.md):

- **Selbsttest:** dauert etwa 30 Minuten, nimmt Clips auf, ändert Einstellungen und stellt sie wieder her und nutzt den Wi-Fi-Adapter.
- **Testmodus:** macht diesen Computer zu einem Hotspot, und die Kamera speichert dieses Netzwerk.

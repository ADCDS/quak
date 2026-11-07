> Diese Seite ist eine maschinelle Übersetzung und kann Fehler enthalten. Maßgeblich ist die [englische Version](https://github.com/ADCDS/quak/blob/main/docs/troubleshooting.md). [Korrekturen sind willkommen](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Fehlerbehebung

## Koppeln und Verbinden

**„No camera is on its pairing screen“.** Eine Kamera ignoriert unbekannte Geräte, solange dieser Bildschirm nicht geöffnet ist:
**Einstellungen > Verbindungen > Gerät verbinden > GoPro Quik App**. Öffne ihn und führe dann `quak pair` noch einmal aus.

**Erst das Smartphone und dann den Computer zu koppeln schlägt fehl.** Eine Verbindung von einem anderen Gerät schließt den Kopplungsbildschirm der Kamera. Kopple immer
nur ein Gerät und öffne den Bildschirm erneut, bevor du das nächste koppelst.

**Die Kamera wird „innerhalb von 45 s nicht gefunden“.** Prüfe, ob sie an und in der Nähe ist. Wenn sie nicht gerade koppelt, macht sich eine HERO11 manchmal nur alle
10–20 Sekunden per Bluetooth bemerkbar, und eine Kamera im Ruhemodus kann länger brauchen. Versuch es noch mal oder drück eine Taste an der Kamera, um sie aufzuwecken.

**Die Verbindung steht, aber die Einrichtung wird nie fertig.** Einmal hat eine Kamera, nachdem sie am USB den Strom verloren hatte, ihre Bluetooth-Dienste nicht mehr
angeboten, bis sie neu gestartet wurde. Schalte die Kamera aus und wieder ein.

**Die Kamera verweigert Aufnahmen oder Änderungen an Einstellungen, ohne einen Grund zu nennen.** Sie steckt vielleicht im Modus Turbo Transfer fest, der Aufnahme, Einstellungen
und Vorschau blockiert. Quak schaltet ihn für Downloads ein, daher kann eine Verbindung, die dabei abreißt, ihn eingeschaltet lassen. Die App beendet ihn beim nächsten
Verbinden, und die Kommandozeile sagt es in ihrer Fehlermeldung: Führe `quak turbo off` aus.

## Bluetooth-Fehler

**„Bluetooth hat beim Verbinden nicht mehr reagiert“.** Prüfe, ob Bluetooth an ist. Wenn das unter Linux immer wieder passiert, sieh ins Kernel-Log:

```bash
journalctl -k | grep "tx timeout"
```

Eine Zeile wie `command 0x2041 tx timeout`, gefolgt von einer USB-Trennung, bedeutet, dass der Bluetooth-Adapter abgestürzt ist. Quak kann das nicht verhindern.
Ein USB-Bluetooth-5.4-Dongle hat das an einem Tag mehr als 20-mal gemacht, an verschiedenen Ports, selbst wenn nichts anderes ihn benutzt hat. Der Adapter
kommt ausgeschaltet zurück, also schalte ihn mit `bluetoothctl power on` ein. Wenn das oft passiert, probier einen anderen Adapter.

**„Innerhalb von 45 s nicht gefunden“ nach einem Adapter-Absturz.** Derselbe Dongle kam manchmal zurück und behauptete, an zu sein, während er gar nichts empfing,
ohne Fehler im Log. Wenn deine Kamera an ist und auch sonst nichts in der Nähe bei einem Bluetooth-Scan auftaucht, schalte Bluetooth aus und wieder ein:
`bluetoothctl power off`, dann `bluetoothctl power on`.

## Wi-Fi, Medien und Vorschau

**„Wi-Fi-Adapter X ist die Netzwerkverbindung dieses Computers“.** Quak nimmt dir deine Verbindung nicht, ohne zu fragen. Schließ einen zweiten
Adapter an oder erlaube es mit `--wifi-adapter X`. Die App fragt dich stattdessen.

**„Verbindung zum Wi-Fi der Kamera“ dauert auf dem Smartphone lange.** Wenn das Wi-Fi der Kamera aus war, kann das Smartphone 30 Sekunden oder länger brauchen,
um es zu sehen.

**Das Smartphone sagt, es habe sich nicht verbunden, „wurde die Verbindungsabfrage abgelehnt?“** Android fragt einmal nach, bevor es sich mit dem Netzwerk der Kamera
verbindet. Wenn du die Abfrage nicht gesehen hast, versuch es noch mal: Es kann auch bedeuten, dass das Smartphone das Netzwerk nicht rechtzeitig gefunden hat.

**Die Vorschau bleibt leer.** Die Kamera stoppt ihren Stream während der Aufnahme, deshalb steht das auch im Vorschaufeld. Nachdem du die Aufnahme gestoppt oder
den Modus gewechselt hast, kommt der Stream innerhalb von etwa 10 Sekunden zurück und das Bild innerhalb von etwa 20.

**„Die Kamera hat die Vorschau nicht gestartet“.** Die Kamera hat abgelehnt. Versuch es gleich noch mal. Quak beendet bereits einen Stream, der von einer
zwangsweise beendeten App übrig geblieben ist.

**„Von der Kamera kam kein Bild“.** Quak hat die Vorschau gestartet, aber innerhalb von 30 Sekunden kam kein Video an. Versuch es noch mal. Wenn das an einem
Computer immer wieder passiert, prüfe, ob eine Firewall eingehendes UDP auf Port 8554 zulässt, denn dorthin sendet die Kamera ihr Video.

**Das Wi-Fi der Kamera bleibt an.** Eine App, die während der Vorschau zwangsweise beendet wird, kann es nicht ausschalten. Führe `quak ap off` aus.

**Mein Computer hat sein Wi-Fi verloren, nachdem Quak zwangsweise beendet wurde.** Siehe [Wenn Quak zwangsweise beendet wird](safety.md#wenn-quak-zwangsweise-beendet-wird).

## Die App

**`quak-gui` meldet, dass es kein Display gibt.** Du bist auf einem Rechner ohne Bildschirm, zum Beispiel über SSH. `quak-gui --web` stellt die App für einen Browser
auf demselben Rechner bereit.

**Der erste Start scheint zu hängen.** Der Desktop-Client ist etwa 45 MB groß und wird einmal heruntergeladen.

**Die Android-App lässt sich nicht aktualisieren.** APKs, die mit verschiedenen Schlüsseln signiert sind, können einander nicht aktualisieren. Deinstalliere zuerst die alte,
dabei werden ihre Kopplung und ihre Downloads entfernt.

## Bekannte Probleme

**Der Livestream kann beim Verbinden mit einem Netzwerk hängen bleiben (experimentell).** Die Kamera verbindet sich mit dem Wi-Fi-Netzwerk und schließt den Handshake ab,
fragt aber nie nach einer IP-Adresse, sodass das Verbinden nach 60 Sekunden abbricht. Der Livestream hat an der Testkamera vollständig funktioniert, auch zweimal an dem Tag,
an dem das begann, danach blieb jeder weitere Versuch hängen. An der betroffenen Kamera:

| Versucht | Ergebnis |
|---|---|
| Kamera in den Ruhemodus versetzen und aufwecken | Bleibt weiter hängen |
| Kamera neu starten | Bleibt weiter hängen |
| Wi-Fi-Adapter des Computers neu einstecken | Bleibt weiter hängen |
| Ein Netzwerkname, den die Kamera noch nie gesehen hatte | Bleibt weiter hängen |
| Ein Smartphone verbindet sich mit demselben Netzwerk | Funktioniert |

Das Netzwerk selbst ist in Ordnung, die Stille liegt also auf der Seite der Kamera. Es ist nicht bekannt, warum, oder ob etwas, das Quak gesendet hat, die Ursache war.
**Verbindungen zurücksetzen** auf der Kamera wurde nicht ausprobiert, weil das auch jede Kopplung entfernt. Wenn dir das passiert oder du es lösen kannst,
[eröffne bitte ein Issue](https://github.com/ADCDS/quak/issues).

**Ein Tippen kann gelegentlich verloren gehen.** Manche Bildschirme zeichnen sich komplett neu, wenn die Kamera eine Änderung meldet, und ein Tippen in genau diesem Moment
geht verloren. Tipp noch mal. Medien und Diagnose machen das nicht mehr.

**Windows wurde noch nie mit einer Kamera ausgeführt.** Es ist experimentell, also melde bitte, was du findest.

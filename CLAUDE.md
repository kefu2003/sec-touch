# SEC-Touch Lüftung → Home Assistant

Projektkontext für Claude Code. Sprache: Deutsch. Stand 2026-11-10.

## Anlage
- SEVentilation-Zentralregler SEC-Touch, Firmware **SWZ0021E21D**, 6 Lüfterpaare (12 V DC).
- TFT-Touch-Bedienteil (BDE), Firmware **ETA0032E21C**, am BUS A (RS485), Busadresse **04**.
- Räume, Paar 1–6: Wohnzimmer, Schlafzimmer, Kinderzimmer 2, Chef-Büro, Kinderzimmer 1, Abstellraum.
- Die ESPHome-Komponente `sec_touch` (PC-Port, 28800 Baud) passt **nicht** zu dieser Firmware. Der PC-Port sendet hier 9600 Baud ASCII (`0103A005FCF10000010105\r\n`) und ist nicht weiter untersucht.

## Server (Ubuntu 24, Minisforum MS-A2)
- Waveshare USB TO 2CH RS485 (isoliert, FTDI FT2232H, ohne Seriennummer). Er hängt hinten am Mainboard; am Frontport über den internen Genesys-Hub gab es USB-Abbrüche.
- udev-Aliase in `/etc/udev/rules.d/99-usb-serial.rules`:
  - `/dev/rs485-bus` → if00 (ttyUSB1), Port 1 an BUS A (A↔A1, B↔B1, „–“↔PE)
  - `/dev/rs485-ohr` → if01 (ttyUSB2), Port 2 parallel als passiver Mithörer
- HA läuft als Docker (compose). Mosquitto ist per apt auf dem Host installiert, mit Benutzer `lueftung`.
- Bridge: `/opt/sec-touch/sec_touch_mqtt.py`, Konfiguration `/etc/sec-touch-mqtt.conf`, systemd-Dienst `sec-touch-mqtt`.

## BUS-A-Protokoll (selbst entschlüsselt)
- **9600 Baud, 9 Datenbits** (9. Bit = Adressbit): Die ersten 2 Bytes jedes Telegramms haben Bit 9 = 1, alle übrigen 0. Mit dem FTDI umgesetzt als Parität Mark für Byte 0–1, dann Space. Umschalten per `TCSETS2` (`set_parity`), **nicht** über pyserial. Vor dem Umschalten etwa 2 Zeichenzeiten warten, weil `flush()` beim FTDI zu früh zurückkehrt.
- Lesen geht mit 9600 und Parität S (oder M/E/O). Mit 8N1 bei 9400 liest es sich nur zufällig sauber, das war anfangs der Irrweg.
- Rahmen: `[b0][b1][cmd][len: Bit0-6 Länge, Bit7 Flag][CRC lo][CRC hi][Daten]`. CRC16-XMODEM (Poly 0x1021, Init 0) über die 4 Kopfbytes und die Daten.
- Die Steuerung ist Master. Rundruf `01 ff 84 00` kommt alle 10 s, Abfrage `01 <adr> 84 00` etwa alle 50 ms.
- Anmeldung im laufenden Betrieb, in dieser Reihenfolge:
  1. Auf den Rundruf antwortet das Gerät mit `01 01 80 81 <adr>`.
  2. Steuerung `01 <adr> 80 05 00 00 01 01 05` → Gerät `01 01 81 12 00 00 01 01 05 0a 00 "ETA0032E21C"`.
  3. Steuerung `01 <adr> 81 00` → Gerät `01 00 85 0d 0a 00 "ETA0032E21C"`.
  4. Danach beginnen die Abfragen.
- Antwort auf eine Abfrage: Befehl oder Keepalive `01 00 85 03 .. .. 0b 00 00`. Die Antwort kommt etwa 9 ms nach der Anfrage.
- Befehl `01 00 85` mit Tripeln `[Property][hi][lo]`. Lüfterpaar n = Property `0xAC+n` (0xAD … 0xB2).
  - Wert 1–6 = Stufe, 0x0A = Automatik.
  - Laut sec_touch vermutlich 7–11 = Stoß, Auto Feuchte, Auto CO2, Auto Zeit, Schlummer. Das ist noch nicht verifiziert.
- Quittung: Nach **jedem** Daten-85 auf dem Bus, auch nach den Rundsendungen der Steuerung, fragt sie jede BDE mit `01 <adr> 86 00`. Antwort: `01 01 87 83 00 <CRC-Bytes genau dieses 85>`. Bei falscher CRC wiederholt die Steuerung die 86 endlos.
- `01 <adr> 88 00` = Fehler/NAK.
- Beim Boot scannt die Steuerung die Adressen 02..0C. Ist die Bridge beim Start der Steuerung aktiv, meldet sich keiner an. Deshalb schweigt die Bridge nach mehr als 2 s Busstille für `anlaufzeit` (60 s).
- Weitere Telegramme: `21 ff d2 80` alle 0,78 s, `00 00 00 0e …` = Uhrzeit alle 5 s, Properties 0xA7/0xA8 minütlich. Property 9 = BDE-Name.

## Dateien in diesem Ordner
- `sec_touch_mqtt.py`: Bridge (Bus-Emulation als BDE 05, MQTT-Discovery, 6 `select`-Entitäten).
- `sec-touch-mqtt.conf` / `.service`: Vorlage für Konfiguration und Dienst (Passwort steht nur auf dem Server).
- `bus_sniff.py`: Werkzeug zum Analysieren und Testen.
  - Optionen: `--decode --new`, `--parity-test`, `--bits`, `--emulate ADR`.
  - Funktionierender Aufruf: `--emulate 5 --baud 9600 --parity S --nine-bit 2`.
  - Achtung: Standard-Baud ist noch 9400.
- `ANLEITUNG-MQTT.md`: Einrichtung von Mosquitto und Bridge.
- `lueftung*.yaml`: alte ESPHome-Versuche (PC-Port), nicht mehr in Gebrauch.

## Offene Ideen
- Werte für Stoßlüftung, Schlummer und die Auto-Modi am Touch mitschneiden und in `MODES` aufnehmen.
- Sommerlüftung (Property 48 laut sec_touch) und weitere Properties untersuchen.
- Nur ein Programm darf `/dev/rs485-bus` öffnen: Für Tests mit `bus_sniff.py` vorher `sudo systemctl stop sec-touch-mqtt`.

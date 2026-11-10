# sec-touch

Anbindung einer SEVentilation-Lüftungssteuerung **SEC-Touch** an **Home Assistant** über MQTT.

Die Bridge hängt per USB-RS485-Adapter am internen Bus der Steuerung (BUS A) und meldet sich dort als zusätzliches Touch-Bedienteil an. In Home Assistant erscheint ein Gerät mit einer Auswahl-Entität pro Lüfterpaar (Stufe 1–6, Automatik). Stufenwechsel am Original-Touch werden mitgelesen und nach HA zurückgemeldet.

Das Busprotokoll ist nicht dokumentiert und wurde durch Mitschneiden selbst entschlüsselt (siehe [Protokoll auf BUS A](#protokoll-auf-bus-a)).

## Getestete Anlage

| Komponente | Version |
|---|---|
| Zentralregler SEC-Touch | Firmware `SWZ0021E21D`, 6 Lüfterpaare (12 V DC) |
| TFT-Touch-Bedienteil (BDE) | Firmware `ETA0032E21C`, Busadresse `04` |
| RS485-Adapter | Waveshare USB TO 2CH RS485 (FTDI FT2232H, isoliert) |
| Server | Ubuntu 24.04, Home Assistant in Docker, Mosquitto auf dem Host |

Andere Firmwarestände wurden nicht getestet.

> Die ESPHome-Komponente `sec_touch` (PC-Port, 28800 Baud) passt nicht zu dieser Firmware. Der PC-Port sendet hier mit 9600 Baud ASCII (z. B. `0103A005FCF10000010105\r\n`) und wurde nicht weiter untersucht.

## Dateien

| Datei | Zweck |
|---|---|
| `sec_touch_mqtt.py` | Bridge: Bus-Emulation als Bedienteil (Adresse 05), MQTT-Discovery, 6 `select`-Entitäten |
| `sec-touch-mqtt.conf` | Vorlage für die Konfiguration (Port, Busadresse, MQTT-Zugang, Raumnamen) |
| `sec-touch-mqtt.service` | systemd-Unit |
| `bus_sniff.py` | Werkzeug zum Mitlesen, Dekodieren und Testen am Bus |
| `ANLEITUNG-MQTT.md` | Schritt-für-Schritt-Einrichtung von Mosquitto, HA-Integration und Bridge |
| `lueftung*.yaml` | Alte ESPHome-Versuche am PC-Port, nicht mehr in Gebrauch |

## Verkabelung

Port 1 des Adapters an BUS A der Steuerung, parallel zum vorhandenen Touch:

| Adapter | SEC-Touch BUS A |
|---|---|
| A | A1 |
| B | B1 |
| – (GND) | PE |

Port 2 kann zusätzlich parallel als passiver Mithörer angeschlossen werden.

Empfehlung: Feste Gerätenamen per udev-Regel vergeben (z. B. `/dev/rs485-bus`), da sich `ttyUSBx` nach einem Neustart ändern kann. Den Adapter möglichst direkt am Mainboard anschließen; über interne USB-Hubs gab es Verbindungsabbrüche.

## Schnellstart

Ausführlich in [ANLEITUNG-MQTT.md](ANLEITUNG-MQTT.md). Kurzfassung:

```bash
sudo apt install mosquitto python3-serial python3-paho-mqtt
sudo mkdir -p /opt/sec-touch && sudo cp sec_touch_mqtt.py /opt/sec-touch/
sudo cp sec-touch-mqtt.conf /etc/ && sudo chmod 600 /etc/sec-touch-mqtt.conf
sudo nano /etc/sec-touch-mqtt.conf     # Port, MQTT-Passwort, Raumnamen
sudo cp sec-touch-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sec-touch-mqtt
journalctl -u sec-touch-mqtt -f
```

Nach spätestens etwa 10 s (bzw. 60 s nach einem Neustart der Steuerung) sollte im Log `am Bus angemeldet (Adresse 5)` stehen.

### Konfiguration

```ini
[bus]
port = /dev/rs485-bus     # serielles Gerät an BUS A
adresse = 5               # eigene Busadresse, darf nicht mit dem Touch (04) kollidieren
anlaufzeit = 60           # Sekunden Schweigen nach Busstille (Neustart der Steuerung)

[mqtt]
host = localhost
port = 1883
user = lueftung
password = ...
topic = sec_touch
discovery_prefix = homeassistant

[anlage]
raeume = Raum 1, Raum 2, Raum 3, Raum 4, Raum 5, Raum 6   # Paar 1 bis 6
```

### MQTT-Topics

| Topic | Inhalt |
|---|---|
| `sec_touch/status` | `online` / `offline` (Bridge am Bus angemeldet) |
| `sec_touch/paar<n>/state` | aktueller Modus, z. B. `Stufe 3` oder `Automatik` (retained) |
| `sec_touch/paar<n>/set` | gewünschter Modus, gleiche Texte wie bei `state` |
| `homeassistant/select/sec_touch/paar<n>/config` | Discovery-Konfiguration |

Wichtig: Nur ein Programm darf den Bus-Port öffnen. Für Tests mit `bus_sniff.py` vorher `sudo systemctl stop sec-touch-mqtt`.

---

## Protokoll auf BUS A

Alles in diesem Abschnitt ist durch Mitschneiden und Ausprobieren ermittelt. Was nicht sicher ist, steht unter [Unbekannt / offen](#unbekannt--offen).

### Physikalische Schicht

- RS485, halbduplex, **9600 Baud, 9 Datenbits, 1 Stoppbit**.
- Das 9. Bit ist ein Adress-/Rahmenbit: Bei den **ersten 2 Bytes** jedes Telegramms ist es 1, bei allen weiteren 0.
- Mit einem normalen UART (FTDI) wird das als Parität nachgebildet: Byte 0–1 mit Parität **Mark**, der Rest mit Parität **Space**.
  - Umschalten der Parität per `ioctl(TCSETS2)` direkt am Dateideskriptor; pyserial ist dafür zu langsam bzw. setzt den Port neu auf.
  - Vor dem Umschalten etwa 2 Zeichenzeiten (≈ 2,3 ms) warten, weil `flush()` beim FTDI zurückkehrt, bevor die Bytes wirklich draußen sind.
- **Lesen** funktioniert mit 9600 Baud und beliebiger Parität (S, M, E oder O); Paritätsfehler werden einfach ignoriert.
- Mit 8N1 bei ~9400 Baud lässt sich der Bus zufällig auch halbwegs lesen. Das ist ein Irrweg: Das 9. Bit fällt dabei nur zufällig nicht auf.

### Rahmenaufbau

```
 Byte:  0     1     2     3          4        5        6 …
      [b0]  [b1]  [cmd] [len|flag] [CRC lo] [CRC hi] [Daten …]
       └─9. Bit = 1─┘    └──────────── 9. Bit = 0 ───────────┘
```

| Feld | Bedeutung |
|---|---|
| `b0` | meist `01`; siehe unten bei weiteren Telegrammen |
| `b1` | Zieladresse bei Anfragen der Steuerung (`ff` = Rundruf), `00`/`01` bei Antworten |
| `cmd` | Befehlscode |
| `len` | Bit 0–6: Anzahl Datenbytes; Bit 7: Flag (Bedeutung unklar) |
| CRC | CRC16-XMODEM (Poly `0x1021`, Init `0x0000`), little endian, über die 4 Kopfbytes **und** die Daten (die CRC-Bytes selbst ausgenommen) |

```python
def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
```

### Busablauf

Die Steuerung ist **Master**, Bedienteile antworten nur auf Anfrage. Antworten kommen etwa **9 ms** nach der Anfrage.

| Telegramm (ohne CRC) | Richtung | Bedeutung |
|---|---|---|
| `01 ff 84 00` | Steuerung → alle | Rundruf zur Anmeldung, alle 10 s |
| `01 <adr> 84 00` | Steuerung → BDE | Abfrage (Poll), etwa alle 50 ms je angemeldetem Gerät |
| `01 <adr> 86 00` | Steuerung → BDE | Quittungsanforderung nach jedem Daten-85 |
| `01 <adr> 88 00` | Steuerung → BDE | Fehler / NAK |
| `01 00 85 …` | beide | Daten (Property-Tripel), siehe unten |

Befehlscodes im Überblick: `80`/`81` Anmeldung, `84` Abfrage, `85` Daten, `86`/`87` Quittung, `88` Fehler.

### Anmeldung eines Bedienteils

Im laufenden Betrieb, in dieser Reihenfolge (`<adr>` = eigene Adresse, z. B. `05`):

```
1. Steuerung: 01 ff 84 00                          Rundruf
   BDE:       01 01 80 81 <adr>                    „ich bin <adr>“ (len 1, Flag gesetzt)
2. Steuerung: 01 <adr> 80 05 00 00 01 01 05
   BDE:       01 01 81 12 00 00 01 01 05 0a 00 "ETA0032E21C"
3. Steuerung: 01 <adr> 81 00
   BDE:       01 00 85 0d 0a 00 "ETA0032E21C"
4. Ab jetzt:  01 <adr> 84 00  (Polls)
```

Bleiben die Polls mehr als 5 s aus, meldet sich die Bridge neu an.

**Anlauf der Steuerung:** Beim Booten scannt die Steuerung die Adressen `02` bis `0C`. Antwortet die Bridge in dieser Phase, meldet sich (auch der Original-Touch) nicht korrekt an. Die Bridge erkennt einen Neustart an mehr als 2 s Busstille und liest danach für `anlaufzeit` (Standard 60 s) nur mit.

### Abfrage und Daten (`85`)

Auf jede Abfrage antwortet das Bedienteil entweder mit einem Befehl oder mit dem Keepalive:

```
01 00 85 03 <crc> 0b 00 00          Keepalive („nichts zu melden“)
```

Datentelegramme enthalten eine Folge von Tripeln `[Property][Wert hi][Wert lo]`:

```
01 00 85 03 <crc> af 00 04          Lüfterpaar 3 auf Stufe 4
```

Daten-85 werden auch von der Steuerung selbst als Rundsendung verschickt (z. B. wenn am Touch eine Stufe geändert wird). Darüber liest die Bridge den aktuellen Zustand mit.

**Lüfterpaare:** Paar *n* = Property `0xAC + n`, also `0xAD` (Paar 1) bis `0xB2` (Paar 6).

| Wert | Bedeutung | Status |
|---|---|---|
| `1`–`6` | Stufe 1–6 | verifiziert |
| `0x0A` (10) | Automatik | verifiziert |
| `7` | Stoßlüftung | vermutet (laut ESPHome `sec_touch`) |
| `8` | Auto Feuchte | vermutet |
| `9` | Auto CO2 | vermutet |
| `10` | Auto Zeit | vermutet; kollidiert mit `0x0A` = Automatik, also eher falsch |
| `11` | Schlummer | vermutet |

Die Bridge bietet in HA nur die verifizierten Werte an.

### Quittung (`86` / `87`)

Nach **jedem** Daten-85 auf dem Bus, auch nach den Rundsendungen der Steuerung und nach Telegrammen anderer Bedienteile, fragt die Steuerung jedes angemeldete Bedienteil mit `01 <adr> 86 00` nach einer Quittung. Die Antwort enthält die CRC genau dieses letzten 85-Telegramms:

```
01 01 87 83 <crc> 00 <CRC lo des letzten 85> <CRC hi des letzten 85>
```

Stimmt die CRC nicht, wiederholt die Steuerung die `86`-Anfrage endlos.

### Weitere beobachtete Telegramme

| Telegramm | Intervall | Bedeutung |
|---|---|---|
| `21 ff d2 80 …` | alle 0,78 s | unbekannt |
| `00 00 00 0e …` | alle 5 s | Uhrzeit/Datum (Kodierung nicht entschlüsselt) |
| 85 mit Property `0xA7`, `0xA8` | minütlich | unbekannt |
| 85 mit Property `0x09` | – | Name des Bedienteils |

### Unbekannt / offen

- **Kopfbytes `b0`/`b1`:** Die Bedeutung ist nur teilweise klar. `b1` ist bei Anfragen die Zieladresse, bei Antworten aber mal `00` (bei `85`) und mal `01` (bei `80`, `81`, `87`). Ob `01` die Adresse der Steuerung ist und `00` „an alle“ bedeutet, ist nicht belegt. `b0` ist meist `01`, bei einigen Telegrammen aber `21` oder `00`.
- **Flag-Bit 7 im Längenbyte:** gesetzt bei `80 81` und `87 83`, sonst nicht. Bedeutung unklar.
- **Nutzdaten der Anmeldung:** `00 00 01 01 05` (Anfrage `80` / Antwort `81`) und das Präfix `0a 00` vor der Firmwarekennung werden 1:1 nachgebildet, ihre Bedeutung ist unbekannt. `0a` könnte die Property für die Firmwareversion sein.
- **Keepalive `0b 00 00`:** Ob `0x0B` eine echte Property ist oder nur ein Platzhalter, ist offen.
- **`88` (Fehler):** wann genau die Steuerung das sendet und ob Daten folgen, ist nicht untersucht.
- **`21 ff d2 80 …`:** Inhalt und Zweck unbekannt (eventuell Kommunikation mit Erweiterungsmodulen).
- **Uhrzeit-Telegramm `00 00 00 0e …`:** Aufbau der 14 Datenbytes nicht entschlüsselt; ebenso, ob es mit dem normalen Rahmenformat (CRC) übereinstimmt.
- **Properties `0xA7`, `0xA8`:** minütlich gesendet, Bedeutung unbekannt (Laufzeit? Sensorwerte?).
- **Weitere Betriebsarten:** Werte für Stoßlüftung, Schlummer und die Auto-Modi (Feuchte, CO2, Zeit) sind nicht am Gerät mitgeschnitten.
- **Sommerlüftung:** laut ESPHome-Komponente Property `48` (`0x30`), nicht verifiziert.
- **Rückmeldungen und Sensoren:** Ob die Steuerung Ist-Werte (Drehzahl, Feuchte, Fehlerzustände) auf BUS A verschickt, ist offen.
- **PC-Port:** liefert ASCII mit 9600 Baud, Format nicht untersucht.

Mitschnitte und Ergänzungen sind willkommen.

## Werkzeug `bus_sniff.py`

```bash
# Telegramme mit CRC-Prüfung dekodieren, nur neue/geänderte anzeigen
python3 bus_sniff.py /dev/rs485-ohr --baud 9600 --parity S --decode --new

# Bedienteil mit Adresse 5 emulieren (funktionierender Aufruf)
python3 bus_sniff.py /dev/rs485-bus --emulate 5 --baud 9600 --parity S --nine-bit 2
```

Weitere Optionen: `--parity-test` (alle Paritäten je 10 s), `--bits` (9. Bit je Byte prüfen), `--scan` (Baudraten durchprobieren), `--set PAAR STUFE`.

Achtung: Die Standard-Baudrate von `bus_sniff.py` ist noch 9400, daher `--baud 9600` immer angeben.

## Haftung

Inoffizielles Projekt, keine Verbindung zu SEVentilation. Eingriffe in die Lüftungsanlage erfolgen auf eigene Gefahr.

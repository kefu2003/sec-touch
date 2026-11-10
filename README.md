# sec-touch

Connects a SEVentilation **SEC-Touch** ventilation controller to **Home Assistant** via MQTT.

The bridge is attached to the controller's internal bus (BUS A) through a USB-RS485 adapter and joins it as an additional touch control panel. Home Assistant shows one device with a select entity per fan pair (Level 1–6, Auto). Level changes made on the original touch panel are read back and reported to HA.

The bus protocol is undocumented and was reverse-engineered from captures (see [Protocol on BUS A](#protocol-on-bus-a)).

## Tested system

| Component | Version |
|---|---|
| SEC-Touch central controller | Firmware `SWZ0021E21D`, 6 fan pairs (12 V DC) |
| TFT touch control panel (BDE) | Firmware `ETA0032E21C`, bus address `04` |
| RS485 adapter | Waveshare USB TO 2CH RS485 (FTDI FT2232H, isolated) |
| Server | Ubuntu 24.04, Home Assistant in Docker, Mosquitto on the host |

Other firmware versions have not been tested.

> The ESPHome component `sec_touch` (PC port, 28800 baud) does not match this firmware. Here the PC port sends ASCII at 9600 baud (e.g. `0103A005FCF10000010105\r\n`) and has not been investigated further.

## Files

| File | Purpose |
|---|---|
| `sec_touch_mqtt.py` | Bridge: bus emulation as a control panel (address 05), MQTT discovery, 6 `select` entities |
| `sec-touch-mqtt.conf` | Configuration template (port, bus address, MQTT login, room names) |
| `sec-touch-mqtt.service` | systemd unit |
| `bus_sniff.py` | Tool for listening, decoding and testing on the bus |
| `SETUP-MQTT.md` | Step-by-step setup of Mosquitto, the HA integration and the bridge |
| `lueftung*.yaml` | Old ESPHome attempts on the PC port, no longer in use |

## Wiring

Port 1 of the adapter to BUS A of the controller, in parallel with the existing touch panel:

| Adapter | SEC-Touch BUS A |
|---|---|
| A | A1 |
| B | B1 |
| – (GND) | PE |

Port 2 can additionally be connected in parallel as a passive listener.

Recommendation: assign fixed device names with a udev rule (e.g. `/dev/rs485-bus`), because `ttyUSBx` can change after a reboot. Connect the adapter directly to the mainboard if possible; internal USB hubs caused disconnects.

## Quick start

Details in [SETUP-MQTT.md](SETUP-MQTT.md). Short version:

```bash
sudo apt install mosquitto python3-serial python3-paho-mqtt
sudo mkdir -p /opt/sec-touch && sudo cp sec_touch_mqtt.py /opt/sec-touch/
sudo cp sec-touch-mqtt.conf /etc/ && sudo chmod 600 /etc/sec-touch-mqtt.conf
sudo nano /etc/sec-touch-mqtt.conf     # port, MQTT password, room names
sudo cp sec-touch-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sec-touch-mqtt
journalctl -u sec-touch-mqtt -f
```

After about 10 s at most (or 60 s after a controller restart) the log should show `joined the bus (address 5)`.

### Configuration

```ini
[bus]
port = /dev/rs485-bus     # serial device on BUS A
address = 5               # own bus address, must not collide with the touch panel (04)
startup_wait = 60         # seconds of silence after the bus was quiet (controller restart)

[mqtt]
host = localhost
port = 1883
user = lueftung
password = ...
topic = sec_touch
discovery_prefix = homeassistant

[system]
rooms = Room 1, Room 2, Room 3, Room 4, Room 5, Room 6   # pair 1 to 6
```

The older German key names (`adresse`, `anlaufzeit`, `[anlage] raeume`) are still accepted.

### MQTT topics

| Topic | Content |
|---|---|
| `sec_touch/status` | `online` / `offline` (bridge has joined the bus) |
| `sec_touch/pair<n>/state` | current mode, e.g. `Level 3` or `Auto` (retained) |
| `sec_touch/pair<n>/set` | requested mode, same texts as `state` |
| `homeassistant/select/sec_touch/paar<n>/config` | discovery configuration |

Important: only one program may open the bus port. Before testing with `bus_sniff.py`, run `sudo systemctl stop sec-touch-mqtt`.

---

## Protocol on BUS A

Everything in this section was worked out by capturing and trial and error. Anything uncertain is listed under [Unknown / open](#unknown--open).

### Physical layer

- RS485, half duplex, **9600 baud, 9 data bits, 1 stop bit**.
- The 9th bit is an address/frame bit: it is 1 for the **first 2 bytes** of every frame and 0 for all others.
- With a normal UART (FTDI) this is emulated with parity: bytes 0–1 with parity **Mark**, the rest with parity **Space**.
  - The parity is switched with `ioctl(TCSETS2)` directly on the file descriptor; pyserial is too slow for this or reconfigures the port.
  - Wait about 2 character times (≈ 2.3 ms) before switching, because on the FTDI `flush()` returns before the bytes are actually out.
- **Reading** works at 9600 baud with any parity (S, M, E or O); parity errors are simply ignored.
- With 8N1 at ~9400 baud the bus can also be read more or less by chance. That is a dead end: the 9th bit just happens not to show up.

### Frame layout

```
 Byte:  0     1     2     3          4        5        6 …
      [b0]  [b1]  [cmd] [len|flag] [CRC lo] [CRC hi] [data …]
       └─9th bit = 1┘    └─────────── 9th bit = 0 ────────────┘
```

| Field | Meaning |
|---|---|
| `b0` | usually `01`; see other frames below |
| `b1` | target address in controller requests (`ff` = broadcast), `00`/`01` in responses |
| `cmd` | command code |
| `len` | bits 0–6: number of data bytes; bit 7: flag (meaning unclear) |
| CRC | CRC16-XMODEM (poly `0x1021`, init `0x0000`), little endian, over the 4 header bytes **and** the data (excluding the CRC bytes themselves) |

```python
def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
```

### Bus sequence

The controller is the **master**; control panels only answer when asked. Answers come about **9 ms** after the request.

| Frame (without CRC) | Direction | Meaning |
|---|---|---|
| `01 ff 84 00` | controller → all | join broadcast, every 10 s |
| `01 <addr> 84 00` | controller → BDE | poll, about every 50 ms per joined device |
| `01 <addr> 86 00` | controller → BDE | acknowledge request after every data 85 |
| `01 <addr> 88 00` | controller → BDE | error / NAK |
| `01 00 85 …` | both | data (property triples), see below |

Command codes at a glance: `80`/`81` join, `84` poll, `85` data, `86`/`87` acknowledge, `88` error.

### Joining as a control panel

During operation, in this order (`<addr>` = own address, e.g. `05`):

```
1. Controller: 01 ff 84 00                          broadcast
   BDE:        01 01 80 81 <addr>                   "I am <addr>" (len 1, flag set)
2. Controller: 01 <addr> 80 05 00 00 01 01 05
   BDE:        01 01 81 12 00 00 01 01 05 0a 00 "ETA0032E21C"
3. Controller: 01 <addr> 81 00
   BDE:        01 00 85 0d 0a 00 "ETA0032E21C"
4. From now on: 01 <addr> 84 00  (polls)
```

If no polls arrive for more than 5 s, the bridge joins again.

**Controller startup:** while booting, the controller scans addresses `02` to `0C`. If the bridge answers during this phase, devices (including the original touch panel) do not join correctly. The bridge detects a restart by more than 2 s of bus silence and then only listens for `startup_wait` (default 60 s).

### Poll and data (`85`)

The control panel answers every poll either with a command or with the keepalive:

```
01 00 85 03 <crc> 0b 00 00          keepalive ("nothing to report")
```

Data frames contain a sequence of triples `[property][value hi][value lo]`:

```
01 00 85 03 <crc> af 00 04          fan pair 3 to level 4
```

The controller itself also sends data 85 frames as broadcasts (e.g. when a level is changed on the touch panel). This is how the bridge keeps track of the current state.

**Fan pairs:** pair *n* = property `0xAC + n`, i.e. `0xAD` (pair 1) to `0xB2` (pair 6).

| Value | Meaning | Status |
|---|---|---|
| `1`–`6` | level 1–6 | verified |
| `0x0A` (10) | auto | verified |
| `7` | boost ventilation | assumed (per ESPHome `sec_touch`) |
| `8` | auto humidity | assumed |
| `9` | auto CO2 | assumed |
| `10` | auto time | assumed; collides with `0x0A` = auto, so probably wrong |
| `11` | sleep mode | assumed |

The bridge only offers the verified values in HA.

### Acknowledge (`86` / `87`)

After **every** data 85 on the bus, including the controller's broadcasts and frames from other control panels, the controller asks every joined control panel for an acknowledgement with `01 <addr> 86 00`. The answer contains the CRC of exactly that last 85 frame:

```
01 01 87 83 <crc> 00 <CRC lo of the last 85> <CRC hi of the last 85>
```

If the CRC is wrong, the controller repeats the `86` request forever.

### Other observed frames

| Frame | Interval | Meaning |
|---|---|---|
| `21 ff d2 80 …` | every 0.78 s | unknown |
| `00 00 00 0e …` | every 5 s | time/date (encoding not decoded) |
| 85 with property `0xA7`, `0xA8` | every minute | unknown |
| 85 with property `0x09` | – | name of the control panel |

### Unknown / open

- **Header bytes `b0`/`b1`:** only partly understood. In requests `b1` is the target address, in responses it is sometimes `00` (for `85`) and sometimes `01` (for `80`, `81`, `87`). Whether `01` is the controller's address and `00` means "to all" is not confirmed. `b0` is usually `01`, but `21` or `00` in some frames.
- **Flag bit 7 in the length byte:** set for `80 81` and `87 83`, otherwise not. Meaning unclear.
- **Join payload:** `00 00 01 01 05` (request `80` / response `81`) and the prefix `0a 00` before the firmware ID are copied 1:1; their meaning is unknown. `0a` might be the property for the firmware version.
- **Keepalive `0b 00 00`:** whether `0x0B` is a real property or just a placeholder is open.
- **`88` (error):** when exactly the controller sends it and whether data follows has not been investigated.
- **`21 ff d2 80 …`:** content and purpose unknown (possibly communication with expansion modules).
- **Time frame `00 00 00 0e …`:** layout of the 14 data bytes not decoded; also unclear whether it follows the normal frame format (CRC).
- **Properties `0xA7`, `0xA8`:** sent every minute, meaning unknown (run time? sensor values?).
- **Other operating modes:** the values for boost, sleep mode and the auto modes (humidity, CO2, time) have not been captured on the device.
- **Summer ventilation:** property `48` (`0x30`) according to the ESPHome component, not verified.
- **Feedback and sensors:** whether the controller sends actual values (fan speed, humidity, error states) on BUS A is open.
- **PC port:** sends ASCII at 9600 baud, format not investigated.

Captures and additions are welcome.

## Tool `bus_sniff.py`

```bash
# decode frames with CRC check, show only new/changed ones
python3 bus_sniff.py /dev/rs485-ohr --baud 9600 --parity S --decode --new

# emulate a control panel with address 5 (working invocation)
python3 bus_sniff.py /dev/rs485-bus --emulate 5 --baud 9600 --parity S --nine-bit 2
```

Other options: `--parity-test` (each parity for 10 s), `--bits` (check the 9th bit of every byte), `--scan` (try baud rates), `--set PAIR LEVEL`.

Note: the default baud rate of `bus_sniff.py` is still 9400, so always pass `--baud 9600`.

## Disclaimer

Unofficial project, not affiliated with SEVentilation. Working on your ventilation system is at your own risk.

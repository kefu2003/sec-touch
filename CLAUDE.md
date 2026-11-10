# SEC-Touch ventilation → Home Assistant

Project context for Claude Code. Language: English. As of 2026-11-10.

## System
- SEVentilation central controller SEC-Touch, firmware **SWZ0021E21D**, 6 fan pairs (12 V DC).
- TFT touch control panel (BDE), firmware **ETA0032E21C**, on BUS A (RS485), bus address **04**.
- Rooms, pair 1–6: living room, bedroom, kids' room 2, home office, kids' room 1, storage room.
- The ESPHome component `sec_touch` (PC port, 28800 baud) does **not** match this firmware. Here the PC port sends ASCII at 9600 baud (`0103A005FCF10000010105\r\n`) and has not been investigated further.

## Server (Ubuntu 24, Minisforum MS-A2)
- Waveshare USB TO 2CH RS485 (isolated, FTDI FT2232H, no serial number). It is plugged into the back of the mainboard; the front port via the internal Genesys hub caused USB disconnects.
- udev aliases in `/etc/udev/rules.d/99-usb-serial.rules`:
  - `/dev/rs485-bus` → if00 (ttyUSB1), port 1 on BUS A (A↔A1, B↔B1, "–"↔PE)
  - `/dev/rs485-ohr` → if01 (ttyUSB2), port 2 in parallel as a passive listener
- HA runs in Docker (compose). Mosquitto is installed on the host via apt, with user `lueftung`.
- Bridge: `/opt/sec-touch/sec_touch_mqtt.py`, config `/etc/sec-touch-mqtt.conf`, systemd service `sec-touch-mqtt`.
- The config on the server may still use the old German keys (`adresse`, `anlaufzeit`, `[anlage] raeume`); the bridge accepts both.

## BUS A protocol (reverse-engineered)
- **9600 baud, 9 data bits** (9th bit = address bit): the first 2 bytes of every frame have bit 9 = 1, all others 0. Done on the FTDI as parity Mark for bytes 0–1, then Space. Switch with `TCSETS2` (`set_parity`), **not** via pyserial. Wait about 2 character times before switching, because `flush()` returns too early on the FTDI.
- Reading works with 9600 and parity S (or M/E/O). With 8N1 at 9400 it only reads cleanly by chance; that was the initial dead end.
- Frame: `[b0][b1][cmd][len: bits 0-6 length, bit 7 flag][CRC lo][CRC hi][data]`. CRC16-XMODEM (poly 0x1021, init 0) over the 4 header bytes and the data.
- The controller is master. Broadcast `01 ff 84 00` every 10 s, poll `01 <addr> 84 00` about every 50 ms.
- Joining during operation, in this order:
  1. The device answers the broadcast with `01 01 80 81 <addr>`.
  2. Controller `01 <addr> 80 05 00 00 01 01 05` → device `01 01 81 12 00 00 01 01 05 0a 00 "ETA0032E21C"`.
  3. Controller `01 <addr> 81 00` → device `01 00 85 0d 0a 00 "ETA0032E21C"`.
  4. Then the polls start.
- Answer to a poll: command or keepalive `01 00 85 03 .. .. 0b 00 00`. The answer comes about 9 ms after the request.
- Command `01 00 85` with triples `[property][hi][lo]`. Fan pair n = property `0xAC+n` (0xAD … 0xB2).
  - Value 1–6 = level, 0x0A = auto.
  - According to sec_touch, 7–11 are probably boost, auto humidity, auto CO2, auto time, sleep. Not verified yet.
- Acknowledge: after **every** data 85 on the bus, including the controller's broadcasts, it asks every BDE with `01 <addr> 86 00`. Answer: `01 01 87 83 00 <CRC bytes of exactly that 85>`. With a wrong CRC the controller repeats the 86 forever.
- `01 <addr> 88 00` = error/NAK.
- On boot the controller scans addresses 02..0C. If the bridge is active while the controller starts, nobody joins. So after more than 2 s of bus silence the bridge stays quiet for `startup_wait` (60 s).
- Other frames: `21 ff d2 80` every 0.78 s, `00 00 00 0e …` = time every 5 s, properties 0xA7/0xA8 every minute. Property 9 = BDE name.

## Files in this folder
- `sec_touch_mqtt.py`: bridge (bus emulation as BDE 05, MQTT discovery, 6 `select` entities).
- `sec-touch-mqtt.conf` / `.service`: templates for config and service (the password only exists on the server).
- `bus_sniff.py`: tool for analysis and testing.
  - Options: `--decode --new`, `--parity-test`, `--bits`, `--emulate ADDR`.
  - Working invocation: `--emulate 5 --baud 9600 --parity S --nine-bit 2`.
  - Note: the default baud is still 9400.
- `SETUP-MQTT.md`: setup of Mosquitto and the bridge.
- `lueftung*.yaml`: old ESPHome attempts (PC port), no longer in use.

## Open ideas
- Capture the values for boost, sleep and the auto modes on the touch panel and add them to `MODES`.
- Investigate summer ventilation (property 48 per sec_touch) and other properties.
- Only one program may open `/dev/rs485-bus`: before testing with `bus_sniff.py`, run `sudo systemctl stop sec-touch-mqtt`.

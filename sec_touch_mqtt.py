#!/usr/bin/env python3
"""SEC-Touch (SEVentilation) <-> MQTT bridge for Home Assistant.

Joins BUS A (RS485, 9600 baud, 9-bit mode) as an additional control panel,
creates one select entity per fan pair in HA via MQTT discovery and sets levels.
Level changes made on the original touch panel are read back.

Requires: sudo apt install python3-serial python3-paho-mqtt
Run:      python3 sec_touch_mqtt.py --config /etc/sec-touch-mqtt.conf
"""
import argparse, configparser, fcntl, json, queue, struct, sys, termios, threading, time

import serial
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------- bus protocol
def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

def build_frame(head3, data, flag=0):
    body = bytes(head3) + bytes([len(data) | flag]) + bytes(data)
    crc = crc16_xmodem(body)
    return body[:4] + bytes([crc & 0xFF, crc >> 8]) + body[4:]

def set_parity(fd, par):
    """Switch only the parity (M/S), the baud rate stays untouched."""
    TCGETS2, TCSETS2 = 0x802C542A, 0x402C542B
    PARENB, PARODD, CMSPAR = termios.PARENB, termios.PARODD, 0o10000000000
    buf = bytearray(44)
    fcntl.ioctl(fd, TCGETS2, buf)
    cflag = struct.unpack_from("I", buf, 8)[0] & ~(PARENB | PARODD | CMSPAR)
    cflag |= PARENB | CMSPAR | (PARODD if par == "M" else 0)
    struct.pack_into("I", buf, 8, cflag)
    fcntl.ioctl(fd, TCSETS2, bytes(buf))

FIRST_PROP = 0xAD          # pair 1 = 0xAD (173) ... pair 6 = 0xB2 (178)
MODES = {                  # value -> label in HA (only values verified on the device)
    1: "Level 1", 2: "Level 2", 3: "Level 3", 4: "Level 4", 5: "Level 5", 6: "Level 6",
    10: "Auto",
}
MODE_VALUES = {v: k for k, v in MODES.items()}

def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)

class Bus:
    """Behaves like a touch control panel with address <addr>."""
    FW = b"ETA0032E21C"
    JOIN_REQ = bytes.fromhex("01 ff 84 00 8b 6e")
    KEEPALIVE = build_frame([0x01, 0x00, 0x85], [0x0B, 0x00, 0x00])

    def __init__(self, port, addr, on_level, on_online, turnaround=0.009, startup_wait=60):
        self.addr = addr
        self.on_level, self.on_online = on_level, on_online
        self.turnaround = turnaround
        self.s = serial.Serial(port, 9600, parity="S", timeout=0.002)
        try:
            self.s.set_low_latency_mode(True)
        except Exception:
            pass
        self.scan_req = build_frame([0x01, addr, 0x80], bytes.fromhex("00 00 01 01 05"))
        self.poll_req = build_frame([0x01, addr, 0x84], b"")
        self.todo = queue.Queue()
        self.last85 = self.KEEPALIVE
        self.state = "join"
        self.last_poll = 0.0
        # After the controller restarts, only listen at first so the touch panel can
        # join first (if both start at the same time, nobody manages to join).
        self.startup_wait = startup_wait
        self.last_frame = 0.0
        self.quiet_until = 0.0

    def send(self, frame):
        time.sleep(self.turnaround)
        set_parity(self.s.fd, "M"); self.s.write(frame[:2]); self.s.flush()
        time.sleep(2 * 11 / 9600 + 0.0015)     # flush() returns too early on the FTDI
        set_parity(self.s.fd, "S"); self.s.write(frame[2:]); self.s.flush()

    def set_level(self, pair, value):
        self.todo.put(build_frame([0x01, 0x00, 0x85], [FIRST_PROP + pair - 1, 0x00, value]))

    def read_levels(self, f):
        if f[2] == 0x85 and f[6:] != b"\x0b\x00\x00":
            self.last85 = f
            d = f[6:]
            if len(d) % 3 == 0:                 # triples [property][hi][lo]
                for i in range(0, len(d), 3):
                    prop, hi, lo = d[i], d[i + 1], d[i + 2]
                    if FIRST_PROP <= prop < FIRST_PROP + 6 and hi == 0 and lo in MODES:
                        self.on_level(prop - FIRST_PROP + 1, lo)

    def handle(self, f):
        self.read_levels(f)
        a = self.addr
        if f == self.JOIN_REQ and self.state != "polled":
            self.send(build_frame([0x01, 0x01, 0x80], [a], flag=0x80))
            self.state = "scan"
        elif f == self.scan_req:
            self.send(build_frame([0x01, 0x01, 0x81], bytes.fromhex("00 00 01 01 05 0a 00") + self.FW))
        elif f[:4] == bytes([0x01, a, 0x81, 0x00]):
            self.send(build_frame([0x01, 0x00, 0x85], bytes.fromhex("0a 00") + self.FW))
        elif f == self.poll_req:
            if self.state != "polled":
                log(f"joined the bus (address {a})")
                self.state = "polled"
                self.on_online(True)
            self.last_poll = time.time()
            frame = self.KEEPALIVE if self.todo.empty() else self.todo.get()
            if frame != self.KEEPALIVE:
                self.last85 = frame
                log(f"command sent: {frame.hex(' ')}")
            self.send(frame)
        elif f[:4] == bytes([0x01, a, 0x86, 0x00]):
            self.send(build_frame([0x01, 0x01, 0x87], b"\x00" + self.last85[4:6], flag=0x80))
        elif f[:3] == bytes([0x01, a, 0x88]):
            log("controller reports an error (88)")

    def run(self):
        log("waiting for the controller's broadcast ...")
        buf = b""
        while True:
            buf += self.s.read(64)
            if self.state == "polled" and time.time() - self.last_poll > 5:
                log("no poll for 5 s, joining again")
                self.state = "join"
                self.on_online(False)
            while len(buf) >= 6:
                n = buf[3] & 0x7F
                if n > 64:
                    buf = buf[1:]; continue
                if len(buf) < 6 + n:
                    break
                if crc16_xmodem(buf[:4] + buf[6:6 + n]) != buf[4] | (buf[5] << 8):
                    buf = buf[1:]; continue
                f, buf = buf[:6 + n], buf[6 + n:]
                now = time.time()
                if now - self.last_frame > 2:       # bus was silent -> controller (re)started
                    self.quiet_until = now + self.startup_wait
                    if self.state != "join":
                        self.state = "join"
                        self.on_online(False)
                    log(f"bus traffic detected, waiting {self.startup_wait} s before answering")
                self.last_frame = now
                if now < self.quiet_until:
                    self.read_levels(f)             # listen only
                else:
                    self.handle(f)

# ---------------------------------------------------------------- MQTT / HA
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/sec-touch-mqtt.conf")
    cfg = configparser.ConfigParser()
    cfg.read(ap.parse_args().config, encoding="utf-8")
    bus_cfg, mq = cfg["bus"], cfg["mqtt"]
    # The old German key names (adresse, anlaufzeit, [anlage] raeume) are still accepted.
    if cfg.has_option("system", "rooms"):
        rooms_raw = cfg.get("system", "rooms")
    else:
        rooms_raw = cfg.get("anlage", "raeume")
    rooms = [r.strip() for r in rooms_raw.split(",")]
    address = bus_cfg.getint("address", bus_cfg.getint("adresse", 5))
    startup_wait = bus_cfg.getint("startup_wait", bus_cfg.getint("anlaufzeit", 60))
    base = mq.get("topic", "sec_touch")
    disc = mq.get("discovery_prefix", "homeassistant")
    avail = f"{base}/status"

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="sec-touch-bridge")
    except AttributeError:                      # paho-mqtt 1.x
        client = mqtt.Client(client_id="sec-touch-bridge")
    if mq.get("user"):
        client.username_pw_set(mq.get("user"), mq.get("password"))
    client.will_set(avail, "offline", retain=True)

    online = {"bus": False}
    def publish_online(flag):
        online["bus"] = flag
        client.publish(avail, "online" if flag else "offline", retain=True)
    def publish_level(pair, value):
        client.publish(f"{base}/pair{pair}/state", MODES[value], retain=True)

    bus = Bus(bus_cfg.get("port", "/dev/ttyUSB1"), address,
              publish_level, publish_online, startup_wait=startup_wait)

    device = {"identifiers": ["sec_touch_swz0021e21d"], "name": "Ventilation SEC-Touch",
              "manufacturer": "SEVentilation", "model": "SEC-Touch SWZ0021E21D"}
    def on_connect(c, userdata, flags, rc):
        log(f"MQTT connected (rc={rc})")
        for i, room in enumerate(rooms, 1):
            # unique_id keeps the old "paar" name so existing HA entities stay linked
            conf = {"name": room, "unique_id": f"sec_touch_paar{i}",
                    "command_topic": f"{base}/pair{i}/set", "state_topic": f"{base}/pair{i}/state",
                    "options": list(MODES.values()), "availability_topic": avail,
                    "icon": "mdi:fan", "device": device}
            c.publish(f"{disc}/select/sec_touch/paar{i}/config", json.dumps(conf), retain=True)
            c.subscribe(f"{base}/pair{i}/set")
        publish_online(online["bus"])
    def on_message(c, userdata, msg):
        pair = int(msg.topic.split("/")[-2][4:])
        value = MODE_VALUES.get(msg.payload.decode())
        if value is None:
            log(f"unknown mode: {msg.payload!r}")
            return
        log(f"HA: pair {pair} -> {MODES[value]}")
        bus.set_level(pair, value)
        publish_level(pair, value)              # optimistic, reading the touch panel corrects it if needed
    client.on_connect, client.on_message = on_connect, on_message
    client.connect_async(mq.get("host", "localhost"), mq.getint("port", 1883))
    client.loop_start()
    try:
        bus.run()
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(avail, "offline", retain=True)
        client.loop_stop()

if __name__ == "__main__":
    main()

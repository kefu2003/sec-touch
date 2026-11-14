#!/usr/bin/env python3
"""SEC-Touch (SEVentilation) <-> MQTT bridge for Home Assistant.

Joins BUS A (RS485, 9600 baud, 9-bit mode) as an additional control panel,
creates one select entity per fan pair in HA via MQTT discovery and sets levels.
Level changes made on the original touch panel are read back.

Requires: sudo apt install python3-serial python3-paho-mqtt
Run:      python3 sec_touch_mqtt.py --config /etc/sec-touch-mqtt.conf
"""
import argparse, configparser, datetime, fcntl, json, queue, struct, sys, termios, threading, time

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

    def __init__(self, port, addr, on_level, on_online, turnaround=0.009, startup_wait=60,
                 on_clock=None, clock_fix=True, clock_tolerance=60, on_stall=None):
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
        # Check the controller's clock and set it if it drifts (server local time)
        self.on_clock, self.clock_fix, self.clock_tolerance = on_clock, clock_fix, clock_tolerance
        self.last_clock_fix = 0.0
        self.joins = 0
        # Detect a hanging controller: the time frame still arrives, but no broadcasts/polls
        self.on_stall = on_stall
        self.last_poll_any = time.time()
        self.stalled = None
        self.quiet_until = 0.0

    def send(self, frame):
        time.sleep(self.turnaround)
        set_parity(self.s.fd, "M"); self.s.write(frame[:2]); self.s.flush()
        time.sleep(2 * 11 / 9600 + 0.0015)     # flush() returns too early on the FTDI
        set_parity(self.s.fd, "S"); self.s.write(frame[2:]); self.s.flush()

    def set_level(self, pair, value):
        self.todo.put(build_frame([0x01, 0x00, 0x85], [FIRST_PROP + pair - 1, 0x00, value]))

    def set_clock(self, now=None):
        """Set date and time exactly like the touch panel does:
        date: 85 | 01 00 00 DD MM YY    time: 85 | 02 00 SS MM HH"""
        n = now or datetime.datetime.now()
        self.todo.put(build_frame([0x01, 0x00, 0x85], [0x01, 0x00, 0x00, n.day, n.month, n.year - 2000]))
        self.todo.put(build_frame([0x01, 0x00, 0x85], [0x02, 0x00, n.second, n.minute, n.hour]))

    @staticmethod
    def parse_clock(f):
        """Controller time frame (every 5 s): 00 00 00 0e | 01 00 00 DD MM YY 02 00 SS MM HH 04 00 00"""
        d = f[6:]
        if f[:4] != b"\x00\x00\x00\x0e" or len(d) < 11 or d[0] != 1 or d[6] != 2:
            return None
        try:
            return datetime.datetime(2000 + d[5], d[4], d[3], d[10], d[9], d[8])
        except ValueError:
            return None

    def read_levels(self, f):
        if f[0] == 0x01 and f[2] == 0x84:          # broadcast or poll of any device
            self.last_poll_any = time.time()
        clock = self.parse_clock(f)
        if clock and self.on_clock:
            self.on_clock(clock)
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
        clock = self.parse_clock(f)
        if clock and self.clock_fix and self.state == "polled" and time.time() - self.last_clock_fix > 600:
            diff = (clock - datetime.datetime.now()).total_seconds()
            if abs(diff) > self.clock_tolerance:
                log(f"controller clock is off by {diff:+.0f} s ({clock}), setting it")
                self.set_clock()
                self.last_clock_fix = time.time()
        a = self.addr
        if f == self.JOIN_REQ and self.state != "polled":
            self.send(build_frame([0x01, 0x01, 0x80], [a], flag=0x80))
            self.joins += 1
            if self.joins <= 3 or self.joins % 6 == 0:
                log(f"answered broadcast (attempt {self.joins})")
            self.state = "scan"
        elif f == self.scan_req:
            log("controller requests identification, answering")
            self.send(build_frame([0x01, 0x01, 0x81], bytes.fromhex("00 00 01 01 05 0a 00") + self.FW))
        elif f[:4] == bytes([0x01, a, 0x81, 0x00]):
            log("join confirmed (81)")
            self.send(build_frame([0x01, 0x00, 0x85], bytes.fromhex("0a 00") + self.FW))
        elif f == self.poll_req:
            if self.state != "polled":
                log(f"joined the bus (address {a}) after {self.joins} broadcasts")
                self.joins = 0
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
            now = time.time()
            stalled = now - self.last_frame < 3 and now - self.last_poll_any > 90
            if stalled != self.stalled and now > self.quiet_until:
                self.stalled = stalled
                if stalled:
                    log("controller has not polled any device for 90 s, it is probably hanging")
                if self.on_stall:
                    self.on_stall(stalled)
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
                    self.last_poll_any = now
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
    # The old German key names (adresse, anlaufzeit, uhr_stellen, uhr_toleranz,
    # [anlage] raeume) are still accepted.
    if cfg.has_option("system", "rooms"):
        rooms_raw = cfg.get("system", "rooms")
    else:
        rooms_raw = cfg.get("anlage", "raeume")
    rooms = [r.strip() for r in rooms_raw.split(",")]
    address = bus_cfg.getint("address", bus_cfg.getint("adresse", 5))
    startup_wait = bus_cfg.getint("startup_wait", bus_cfg.getint("anlaufzeit", 60))
    clock_fix = bus_cfg.getboolean("clock_fix", bus_cfg.getboolean("uhr_stellen", True))
    clock_tolerance = bus_cfg.getint("clock_tolerance", bus_cfg.getint("uhr_toleranz", 60))
    base = mq.get("topic", "sec_touch")
    disc = mq.get("discovery_prefix", "homeassistant")
    avail = f"{base}/status"

    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)   # paho-mqtt 2.x, callback API v1
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
    last_clock = {"v": None}
    def publish_clock(dt):
        v = dt.strftime("%Y-%m-%d %H:%M")
        if v != last_clock["v"]:                # only publish when the minute changes
            last_clock["v"] = v
            client.publish(f"{base}/clock", v, retain=True)
    def publish_stall(stalled):
        client.publish(f"{base}/stalled", "ON" if stalled else "OFF", retain=True)

    bus = Bus(bus_cfg.get("port", "/dev/ttyUSB1"), address,
              publish_level, publish_online, startup_wait=startup_wait,
              on_clock=publish_clock, clock_fix=clock_fix, clock_tolerance=clock_tolerance,
              on_stall=publish_stall)

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
        # unique_ids keep the original German names so existing HA entities stay linked
        conf = {"name": "Controller clock", "unique_id": "sec_touch_uhrzeit",
                "state_topic": f"{base}/clock", "availability_topic": avail,
                "icon": "mdi:clock-outline", "entity_category": "diagnostic", "device": device}
        c.publish(f"{disc}/sensor/sec_touch/uhrzeit/config", json.dumps(conf), retain=True)
        conf = {"name": "Controller hanging", "unique_id": "sec_touch_haengt",
                "state_topic": f"{base}/stalled", "device_class": "problem",
                "entity_category": "diagnostic", "device": device}
        c.publish(f"{disc}/binary_sensor/sec_touch/haengt/config", json.dumps(conf), retain=True)
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

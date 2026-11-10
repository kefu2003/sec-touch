#!/usr/bin/env python3
"""Passively listens to the RS485 bus (BUS A) between the SEC-Touch and the touch panel.
Sends NOTHING (except with --set / --emulate).
Requires: pip install pyserial  (or: sudo apt install python3-serial)

Find baud rate:  python3 bus_sniff.py /dev/ttyUSB1 --scan   (saves scan_*.bin)
Record:          python3 bus_sniff.py /dev/ttyUSB1 --baud 9600 --parity S > bus.txt
Decode:          python3 bus_sniff.py /dev/ttyUSB1 --baud 9600 --parity S --decode --new > bus_dec.txt
"""
import argparse, sys, time
import serial

BAUDS = [1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200]

def scan(port, bauds, seconds=8):
    """Reads a few seconds per baud rate, saves the raw data as scan_<baud>.bin
    and measures how often the most frequent 10-byte pattern repeats exactly."""
    import collections
    print("  Baud   Bytes     Hits  most frequent pattern")
    for b in bauds:
        with serial.Serial(port, b, timeout=0.2) as s:
            s.reset_input_buffer()
            data = b""
            end = time.time() + seconds
            while time.time() < end:
                data += s.read(512)
        open(f"scan_{b}.bin", "wb").write(data)
        n = 10
        grams = collections.Counter(data[i:i + n] for i in range(max(0, len(data) - n)))
        top, cnt = grams.most_common(1)[0] if grams else (b"", 0)
        hit = cnt * n / len(data) if data else 0
        print(f"{b:>6} {len(data):7d}   {hit:5.0%}   {top.hex(' ')}")
        sys.stdout.flush()

def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

def decode(port, baud, only_new):
    """Frame format (derived from captures):
    [b0][b1][b2][length: bits 0-6, bit 7 = flag][CRC16-XMODEM low][high][data ...]
    CRC over the 4 header bytes + data."""
    seen = set()
    buf = b""
    with serial.Serial(port, baud, timeout=0.05) as s:
        while True:
            buf += s.read(256)
            while len(buf) >= 6:
                n = buf[3] & 0x7F
                if n > 48:          # implausible length -> not a frame start
                    buf = buf[1:]
                    continue
                if len(buf) < 6 + n:
                    break
                body = buf[:4] + buf[6:6 + n]
                if crc16_xmodem(body) == buf[4] | (buf[5] << 8):
                    frame, buf = buf[:6 + n], buf[6 + n:]
                    if only_new and frame in seen:
                        continue
                    seen.add(frame)
                    t = time.time()
                    ts = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
                    data = frame[6:]
                    text = "".join(chr(c) if 32 <= c < 127 else "." for c in data)
                    print(f"{ts}  {frame[0]:02x} {frame[1]:02x} {frame[2]:02x} {frame[3]:02x}  "
                          f"len={n:2d}  {data.hex(' ')}  |{text}|")
                    sys.stdout.flush()
                else:
                    buf = buf[1:]   # no valid frame here, try one byte further

def build_frame(head3, data, flag=0):
    body = bytes(head3) + bytes([len(data) | flag]) + bytes(data)
    crc = crc16_xmodem(body)
    return body[:4] + bytes([crc & 0xFF, crc >> 8]) + body[4:]

TRIGGERS = {
    "idle": None,                              # just wait for a quiet gap
    "poll": bytes.fromhex("01 04 84 00 28 7d"),  # regular poll (otherwise the touch panel answers)
    "broadcast": bytes.fromhex("01 ff 84 00 8b 6e"),  # broadcast every ~10 s
}

def set_level(port, baud, fan, level, trigger, repeat):
    """EXPERIMENTAL: sends 'fan pair <fan> to level <level>'
    (property 0xAC+fan, as observed from the touch panel) and listens for 3 s."""
    prop = 0xAC + fan                      # pair 1 = 0xAD (173) ... pair 6 = 0xB2 (178)
    frame = build_frame([0x01, 0x00, 0x85], [prop, 0x00, level])
    trig = TRIGGERS[trigger]
    with serial.Serial(port, baud, timeout=0.005) as s:
        s.reset_input_buffer()
        if trig is None:
            quiet_since = time.time()
            while time.time() - quiet_since < 0.03:
                if s.read(64):
                    quiet_since = time.time()
        else:
            print(f"waiting for {trig.hex(' ')} ...")
            buf, deadline = b"", time.time() + 30
            while trig not in buf:
                buf = (buf + s.read(64))[-64:]
                if time.time() > deadline:
                    print("Trigger not seen, nothing sent.")
                    return
        for _ in range(repeat):
            s.write(frame)
            s.flush()
        print(f"sent ({repeat}x):", frame.hex(" "))
        end, buf = time.time() + 3, b""
        while time.time() < end:
            buf += s.read(256)
    # split the response, hide routine frames
    while len(buf) >= 6:
        n = buf[3] & 0x7F
        if n <= 48 and len(buf) >= 6 + n and crc16_xmodem(buf[:4] + buf[6:6 + n]) == buf[4] | (buf[5] << 8):
            f, buf = buf[:6 + n], buf[6 + n:]
            routine = f[:4] in (bytes.fromhex("01 04 84 00"), bytes.fromhex("21 ff d2 80")) or f[6:] == b"\x0b\x00\x00"
            if not routine:
                print("  ", f.hex(" "))
        else:
            buf = buf[1:]

def bits(port, baud, seconds=60):
    """Checks the 9th bit of every byte (stop bit position). With plain 8N1 it is always 1.
    If it is 0, the adapter reports a framing error -> the byte is marked with *.
    This reveals parity (8E1/8O1) or 9-bit address mode, which pyserial would otherwise hide."""
    import termios, collections
    with serial.Serial(port, baud, timeout=0.01) as s:
        a = termios.tcgetattr(s.fd)
        a[0] |= termios.INPCK | termios.PARMRK
        a[0] &= ~(termios.IGNPAR | termios.ISTRIP)
        termios.tcsetattr(s.fd, termios.TCSANOW, a)
        s.reset_input_buffer()
        raw = b""
        end = time.time() + seconds
        print(f"Reading for {seconds} s ...")
        while time.time() < end:
            raw += s.read(4096)
    # PARMRK: ff 00 xx = error byte xx, ff ff = literal ff
    data, err, i = [], [], 0
    while i < len(raw):
        if raw[i] == 0xFF and i + 1 < len(raw) and raw[i + 1] == 0xFF:
            data.append(0xFF); err.append(False); i += 2
        elif raw[i] == 0xFF and i + 2 < len(raw) and raw[i + 1] == 0x00:
            data.append(raw[i + 2]); err.append(True); i += 3
        else:
            data.append(raw[i]); err.append(False); i += 1
    n = len(data); ne = sum(err)
    print(f"{n} bytes, {ne} of them with 9th bit = 0 ({ne / max(n, 1):.1%})")
    # find frames by CRC and show the error pattern per frame type
    seen = collections.Counter(); i = 0
    while i + 6 <= n:
        ln = data[i + 3] & 0x7F
        j = i + 6 + ln
        if ln <= 64 and j <= n:
            body = bytes(data[i:i + 4] + data[i + 6:j])
            c = crc16_xmodem(body)
            if data[i + 4] == c & 0xFF and data[i + 5] == c >> 8:
                key = " ".join(f"{data[k]:02x}" + ("*" if err[k] else "") for k in range(i, min(j, i + 10)))
                seen[key] += 1
                i = j
                continue
        i += 1
    print("     Count  frame start (* = 9th bit was 0)")
    for k, v in seen.most_common(25):
        print(f"{v:6d}       {k}")

def parity_test(port, baud, seconds=10):
    """Reads a few seconds per parity setting N/E/O/M/S and counts error bytes
    and valid frames. The correct setting has (almost) no errors."""
    import termios
    print("Parity     Bytes  Errors  valid frames")
    for par in "NEOMS":
        with serial.Serial(port, baud, parity=par, timeout=0.01) as s:
            a = termios.tcgetattr(s.fd)
            a[0] |= termios.INPCK | termios.PARMRK
            a[0] &= ~(termios.IGNPAR | termios.ISTRIP)
            termios.tcsetattr(s.fd, termios.TCSANOW, a)
            s.reset_input_buffer()
            raw = b""
            end = time.time() + seconds
            while time.time() < end:
                raw += s.read(4096)
        data, nerr, i = [], 0, 0
        while i < len(raw):
            if raw[i] == 0xFF and i + 1 < len(raw) and raw[i + 1] == 0xFF:
                data.append(0xFF); i += 2
            elif raw[i] == 0xFF and i + 2 < len(raw) and raw[i + 1] == 0x00:
                data.append(raw[i + 2]); nerr += 1; i += 3
            else:
                data.append(raw[i]); i += 1
        ok, i = 0, 0
        while i + 6 <= len(data):
            ln = data[i + 3] & 0x7F; j = i + 6 + ln
            if ln <= 64 and j <= len(data):
                c = crc16_xmodem(bytes(data[i:i + 4] + data[i + 6:j]))
                if data[i + 4] == c & 0xFF and data[i + 5] == c >> 8:
                    ok += 1; i = j; continue
            i += 1
        print(f"   {par}    {len(data):6d}  {nerr:6d}  {ok:6d}")
        sys.stdout.flush()

def set_parity_fast(fd, par):
    """Switches only the parity (M/S/N) without touching the baud rate.
    At 9400 baud (not a standard rate) pyserial briefly sets 38400 baud, which garbles bytes.
    So this uses TCGETS2/TCSETS2 directly (Linux x86/ARM)."""
    import fcntl, struct, termios
    TCGETS2, TCSETS2 = 0x802C542A, 0x402C542B
    PARENB, PARODD, CMSPAR = termios.PARENB, termios.PARODD, 0o10000000000
    buf = bytearray(44)
    fcntl.ioctl(fd, TCGETS2, buf)
    cflag = struct.unpack_from("I", buf, 8)[0] & ~(PARENB | PARODD | CMSPAR)
    if par == "M":
        cflag |= PARENB | CMSPAR | PARODD
    elif par == "S":
        cflag |= PARENB | CMSPAR
    struct.pack_into("I", buf, 8, cflag)
    fcntl.ioctl(fd, TCSETS2, bytes(buf))

def emulate(port, baud, addr, logfile, turnaround=0.009, join_data=None, parity="N", mark_bytes=0):
    """EXPERIMENTAL: joins as an additional control panel with address <addr>,
    the same way the real touch panel does during operation:
      controller: 01 ff 84 00 (broadcast, every 10 s) -> us: 01 01 80 81 <addr>
      controller: 01 <addr> 80 05 ...                 -> us: 01 01 81 12 <ID>
      controller: 01 <addr> 81 00                     -> us: 01 00 85 0d 0a 00 <ID>
      controller: 01 <addr> 84 00 (poll)              -> us: command or 01 00 85 03 0b 00 00
      controller: 01 <addr> 86 00                     -> us: 01 01 87 83 00 <CRC of the last 85>
    Terminal input: '<pair> <level>', e.g. '1 2'. Everything is logged with ms to <logfile>."""
    import threading, queue
    fw = b"ETA0032E21C"
    join_req = bytes.fromhex("01 ff 84 00 8b 6e")
    scan_req = build_frame([0x01, addr, 0x80], bytes.fromhex("00 00 01 01 05"))
    poll_req = build_frame([0x01, addr, 0x84], b"")
    keepalive = build_frame([0x01, 0x00, 0x85], [0x0B, 0x00, 0x00])
    todo = queue.Queue()

    def ui():
        for line in sys.stdin:
            try:
                fan, level = (int(x) for x in line.split())
            except ValueError:
                if line.strip():
                    print("Format: <pair> <level>, e.g. 1 2")
                continue
            todo.put(build_frame([0x01, 0x00, 0x85], [0xAC + fan, 0x00, level]))
            print(f"-> pair {fan} level {level} will be sent at the next poll")
    threading.Thread(target=ui, daemon=True).start()

    log = open(logfile, "a")
    def note(direction, frame, extra=""):
        t = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
        log.write(f"{ts} {direction} {frame.hex(' ')}{extra}\n"); log.flush()

    print(f"Waiting for the controller's broadcast (every ~10 s), address {addr:02x} ...")
    state = "join"            # join -> scan -> polled
    last85 = keepalive
    last_ack = None
    with serial.Serial(port, baud, parity=parity, timeout=0.002) as s:
        try:
            s.set_low_latency_mode(True)
        except Exception:
            pass
        def send(frame, text=None):
            # The real touch panel waits ~9 ms after the end of the request before answering.
            # The controller probably drops immediate answers (its transmitter is still active).
            time.sleep(turnaround)
            if mark_bytes:
                # 9-bit mode: the first <mark_bytes> bytes with 9th bit = 1 (Mark), the rest with 0 (Space)
                set_parity_fast(s.fd, "M"); s.write(frame[:mark_bytes]); s.flush()
                # flush() returns too early on the FTDI -> wait until the bytes are really out
                time.sleep(mark_bytes * 11 / baud + 0.0015)
                t0 = time.perf_counter()
                set_parity_fast(s.fd, "S")
                gap = (time.perf_counter() - t0) * 1000
                s.write(frame[mark_bytes:]); s.flush()
                set_parity_fast(s.fd, parity)
                note(">", frame, f"   (9-bit, switching {gap:.1f} ms)")
            else:
                s.write(frame); s.flush()
                note(">", frame)
            if text:
                print(f"   {text}: {frame.hex(' ')}")
        buf = b""
        while True:
            buf += s.read(64)
            while len(buf) >= 6:
                n = buf[3] & 0x7F
                if n > 48:
                    buf = buf[1:]; continue
                if len(buf) < 6 + n:
                    break
                if crc16_xmodem(buf[:4] + buf[6:6 + n]) != buf[4] | (buf[5] << 8):
                    buf = buf[1:]; continue
                f, buf = buf[:6 + n], buf[6 + n:]
                note("<", f)
                if f[2] == 0x85 and f[6:] != b"\x0b\x00\x00":
                    last85 = f      # 87 acknowledges the last data frame on the bus (including the controller's own)
                if f == join_req and state != "polled":   # retry on every broadcast
                    send(build_frame([0x01, 0x01, 0x80], [addr if join_data is None else join_data], flag=0x80), "broadcast answered")
                    state = "scan"
                elif f == scan_req:
                    send(build_frame([0x01, 0x01, 0x81], bytes.fromhex("00 00 01 01 05 0a 00") + fw),
                         "ID sent")
                elif f[:4] == bytes([0x01, addr, 0x81, 0x00]):
                    print("   join confirmed (81)")
                    send(build_frame([0x01, 0x00, 0x85], bytes.fromhex("0a 00") + fw), "firmware reported")
                elif f == poll_req:
                    if state != "polled":
                        print("   first poll received: joined!")
                        state = "polled"
                    frame = todo.get() if not todo.empty() else keepalive
                    if frame != keepalive:
                        last85 = frame
                    send(frame, None if frame == keepalive else "command sent")
                elif f[:4] == bytes([0x01, addr, 0x86, 0x00]):
                    ack = build_frame([0x01, 0x01, 0x87], b"\x00" + last85[4:6], flag=0x80)
                    send(ack, None if ack == last_ack else "acknowledge request (86) answered")
                    last_ack = ack
                elif f[:3] == bytes([0x01, addr, 0x88]):
                    print("   REJECTED (88)")

def sniff(port, baud, parity):
    char_time = 11.0 / baud
    gap = max(3.5 * char_time, 0.002)          # gap that separates two frames
    with serial.Serial(port, baud, parity=parity, timeout=gap) as s:
        frame, t0 = b"", None
        while True:
            chunk = s.read(256)
            if chunk:
                if not frame:
                    t0 = time.time()
                frame += chunk
                continue
            if frame:
                ts = time.strftime("%H:%M:%S", time.localtime(t0)) + f".{int(t0 % 1 * 1000):03d}"
                text = "".join(chr(c) if 32 <= c < 127 else "." for c in frame)
                print(f"{ts}  {len(frame):3d}  {frame.hex(' ')}  |{text}|")
                sys.stdout.flush()
                frame = b""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=9400)
    ap.add_argument("--parity", choices=list("NEOMS"), default="N", help="N/E/O/M(ark)/S(pace), also used by --emulate")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--parity-test", action="store_true", help="test parity N/E/O/M/S for 10 s each")
    ap.add_argument("--bits", action="store_true", help="check the 9th bit/parity of every byte (60 s)")
    ap.add_argument("--decode", action="store_true", help="decode frames with CRC check")
    ap.add_argument("--new", action="store_true", help="with --decode: only new/changed frames")
    ap.add_argument("--set", nargs=2, type=int, metavar=("PAIR", "LEVEL"),
                    help="EXPERIMENTAL: send one frame, e.g. --set 1 2")
    ap.add_argument("--trigger", choices=list(TRIGGERS), default="broadcast",
                    help="with --set: when to send")
    ap.add_argument("--repeat", type=int, default=1, help="with --set: send the frame n times")
    ap.add_argument("--emulate", type=lambda x: int(x, 0), metavar="ADDR",
                    help="EXPERIMENTAL: join as a control panel with address ADDR, e.g. --emulate 5")
    ap.add_argument("--wait", type=float, default=9, help="with --emulate: delay before each answer in ms")
    ap.add_argument("--join-data", type=lambda x: int(x, 0), default=None,
                    help="with --emulate: data byte in the join frame (default = ADDR)")
    ap.add_argument("--nine-bit", type=int, default=0, metavar="N",
                    help="with --emulate: 9-bit mode, first N bytes with Mark, the rest with Space (e.g. 2)")
    ap.add_argument("--log", default="emulate.log", help="with --emulate: log file")
    ap.add_argument("--bauds", default="9000,9200,9400,9500,9600,9700,9800,10000,10200,19200",
                    help="baud rates for --scan, comma-separated")
    a = ap.parse_args()
    try:
        if a.scan:
            scan(a.port, [int(x) for x in a.bauds.split(',')])
        elif a.parity_test:
            parity_test(a.port, a.baud)
        elif a.bits:
            bits(a.port, a.baud)
        elif a.emulate:
            emulate(a.port, a.baud, a.emulate, a.log, a.wait / 1000, a.join_data, a.parity, a.nine_bit)
        elif a.set:
            set_level(a.port, a.baud, a.set[0], a.set[1], a.trigger, a.repeat)
        elif a.decode:
            decode(a.port, a.baud, a.new)
        else:
            sniff(a.port, a.baud, a.parity)
    except KeyboardInterrupt:
        pass

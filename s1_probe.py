import json, struct, time
import usb.core, usb.util
import libusb_package
from usb.backend import libusb1

VID, PID = 0x302C, 0x3101
# (interface, OUT endpoint, IN endpoint) -- read from your inspect output
CANDIDATES = [(2, 0x06, 0x86), (3, 0x04, 0x84)]


def build_packet(connect_type, interactive, encoding, msg_sn, body):
    attr = len(body) & 0x3FF
    pkt = bytearray([0x7E, 100, 0, connect_type, interactive, encoding])
    pkt += b"\x00\x00\x00\x00"                 # terminalId
    pkt += struct.pack("<I", msg_sn)           # msgSn
    pkt += struct.pack("<HH", 1, 1)            # pkgTotal, pkgNum
    pkt += struct.pack("<H", attr)             # attributes (body length)
    pkt += body
    pkt.append(sum(pkt[1:]) & 0xFF)            # checksum
    pkt.append(0x7E)
    return bytes(pkt)


def parse_body(data):
    start = data.find(0x7E)
    if start < 0:
        return None
    d = data[start:]
    if len(d) < 22:
        return None
    n = (d[18] | (d[19] << 8)) & 0x3FF
    return d[20:20 + n].decode("utf-8", errors="replace")


backend = libusb1.get_backend(find_library=libusb_package.find_library)
dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
if dev is None:
    raise SystemExit("S1 not found")

body = json.dumps({"id": 1, "method": "device_status", "params": {}},
                  separators=(",", ":")).encode()
packet = build_packet(1, 6, 3, 1, body)
print("Request:", packet.hex(" "))

for intf, ep_out, ep_in in CANDIDATES:
    print(f"\n--- Interface {intf} (OUT 0x{ep_out:02x}, IN 0x{ep_in:02x}) ---")
    try:
        usb.util.claim_interface(dev, intf)
    except usb.core.USBError as e:
        print("Could not claim interface:", e)
        print("Is the Liene Photo app fully quit?")
        continue
    try:
        for _ in range(5):  # drain anything already waiting
            try:
                stale = bytes(dev.read(ep_in, 4096, timeout=300))
                print("Unsolicited data:", stale.hex(" "))
            except usb.core.USBTimeoutError:
                break
        dev.write(ep_out, packet, timeout=3000)
        got = b""
        deadline = time.time() + 4
        while time.time() < deadline:
            try:
                got += bytes(dev.read(ep_in, 4096, timeout=1000))
            except usb.core.USBTimeoutError:
                if got:
                    break
        if got:
            print("Raw reply:", got.hex(" "))
            print("Parsed:", parse_body(got))
        else:
            print("No reply")
    except usb.core.USBError as e:
        print("USB error:", e)
    finally:
        usb.util.release_interface(dev, intf)

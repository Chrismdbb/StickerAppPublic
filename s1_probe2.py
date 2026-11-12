import json, time
import usb.core, usb.util
import libusb_package
from usb.backend import libusb1

VID, PID = 0x302C, 0x3101
CANDIDATES = [(2, 0x06, 0x86), (3, 0x04, 0x84)]
PREFIX = b"cmd json\n"
PROPS = ["printer-state", "printer-sub-state", "printer-state-alerts",
         "firmware-version", "device-info"]

arr = '{"method":"get-prop","id":1,"params":%s}' % json.dumps(PROPS, separators=(",", ":"))
obj = '{"method":"get-prop","id":1,"params":{}}'
VARIANTS = [
    ("array", PREFIX + arr.encode()),
    ("array+newline", PREFIX + arr.encode() + b"\n"),
    ("empty-params+newline", PREFIX + obj.encode() + b"\n"),
]


def read_reply(dev, ep_in, wait=3.0):
    got, deadline = b"", time.time() + wait
    while time.time() < deadline:
        try:
            got += bytes(dev.read(ep_in, 4096, timeout=800))
        except usb.core.USBTimeoutError:
            if got:
                break
    return got


backend = libusb1.get_backend(find_library=libusb_package.find_library)
dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
if dev is None:
    raise SystemExit("S1 not found")

done = False
for intf, ep_out, ep_in in CANDIDATES:
    if done:
        break
    print(f"\n=== Interface {intf} (OUT 0x{ep_out:02x}, IN 0x{ep_in:02x}) ===")
    try:
        usb.util.claim_interface(dev, intf)
    except usb.core.USBError as e:
        print("Could not claim interface:", e, "(is Liene Photo fully quit?)")
        continue
    try:
        for name, payload in VARIANTS:
            print(f"\n[{name}] sending: {payload!r}")
            dev.write(ep_out, payload, timeout=3000)
            got = read_reply(dev, ep_in)
            if not got:
                print("  no reply")
                continue
            print("  raw:", got.hex(" "))
            text = got.decode("utf-8", errors="replace")
            print("  text:", repr(text))
            if "{" in text:
                try:
                    print("  parsed JSON:", json.loads(text[text.index("{"):]))
                except Exception:
                    print("  (contains JSON-like text but did not parse)")
                done = True
                break
    except usb.core.USBError as e:
        print("USB error:", e)
    finally:
        usb.util.release_interface(dev, intf)

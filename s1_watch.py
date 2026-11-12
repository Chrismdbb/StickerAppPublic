import json, time
import usb.core, usb.util
import libusb_package
from usb.backend import libusb1

VID, PID = 0x302C, 0x3101
INTF, EP_OUT, EP_IN = 2, 0x06, 0x86
PROPS = ["printer-state", "printer-sub-state", "printer-state-alerts"]

backend = libusb1.get_backend(find_library=libusb_package.find_library)
dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
if dev is None:
    raise SystemExit("S1 not found (wake it up and retry)")
usb.util.claim_interface(dev, INTF)
print("Watching. Press Ctrl+C to stop.")
last, n = None, 0
try:
    while True:
        n += 1
        req = {"method": "get-prop", "id": n, "params": PROPS}
        dev.write(EP_OUT, b"cmd json\n" + json.dumps(req, separators=(",", ":")).encode(), timeout=3000)
        got = b""
        try:
            got = bytes(dev.read(EP_IN, 4096, timeout=3000))
        except usb.core.USBTimeoutError:
            pass
        try:
            result = json.loads(got.decode())["result"]
        except Exception:
            result = ("unparsed", got)
        if result != last:
            print(time.strftime("%H:%M:%S"), result)
            last = result
        time.sleep(2)
except KeyboardInterrupt:
    pass
finally:
    usb.util.release_interface(dev, INTF)

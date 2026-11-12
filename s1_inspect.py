import usb.core, usb.util
import libusb_package
from usb.backend import libusb1

backend = libusb1.get_backend(find_library=libusb_package.find_library)
dev = usb.core.find(idVendor=0x302c, idProduct=0x3101, backend=backend)
if dev is None:
    raise SystemExit("S1 not found")

for cfg in dev:
    for intf in cfg:
        try:
            name = usb.util.get_string(dev, intf.iInterface)
        except Exception:
            name = "?"
        print(f"Interface {intf.bInterfaceNumber} class={intf.bInterfaceClass} "
              f"sub={intf.bInterfaceSubClass} proto={intf.bInterfaceProtocol} name={name!r}")
        for ep in intf:
            d = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
            t = {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}[usb.util.endpoint_type(ep.bmAttributes)]
            print(f"    endpoint 0x{ep.bEndpointAddress:02x} {d} {t} maxpacket={ep.wMaxPacketSize}")
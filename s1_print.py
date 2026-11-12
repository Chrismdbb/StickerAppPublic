import json, struct, sys, time
import usb.core, usb.util
import libusb_package
from usb.backend import libusb1

VID, PID = 0x302C, 0x3101
CMD_IF, CMD_OUT, CMD_IN = 2, 0x06, 0x86
DATA_IF, DATA_OUT, DATA_IN = 3, 0x04, 0x84
CHUNK = 8192
_id = 0


def read_json(dev, expect_id=None, wait=5.0):
    got, end = b"", time.time() + wait
    while time.time() < end:
        try:
            got += bytes(dev.read(CMD_IN, 4096, timeout=500))
        except usb.core.USBTimeoutError:
            if got:
                break
            continue
        try:
            reply = json.loads(got.decode())
        except ValueError:
            continue
        if expect_id is None or reply.get("id") == expect_id:
            return reply
        got = b""
    raise RuntimeError(f"no valid reply: {got!r}")


def command(dev, method, params):
    global _id
    _id += 1
    body = json.dumps({"method": method, "params": params, "id": _id}, separators=(",", ":"))
    dev.write(CMD_OUT, b"cmd json\n" + body.encode(), timeout=3000)
    return read_json(dev, _id)


def drain(dev):
    for ep in (CMD_IN, DATA_IN):
        while True:
            try:
                dev.read(ep, 4096, timeout=200)
            except usb.core.USBTimeoutError:
                break


def printer_state(dev):
    r = command(dev, "get-prop", ["printer-state", "printer-sub-state", "printer-state-alerts"])
    return r["result"][:3]


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit("usage: python3 s1_print.py <image> <cut.plt> [image-first]")
    image, plt = open(args[0], "rb").read(), open(args[1], "rb").read()
    image_first = "image-first" in args[2:]
    payload = image + plt if image_first else plt + image

    backend = libusb1.get_backend(find_library=libusb_package.find_library)
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        sys.exit("S1 not found (wake it up and retry)")

    usb.util.claim_interface(dev, CMD_IF)
    usb.util.claim_interface(dev, DATA_IF)
    try:
        drain(dev)
        for _ in range(8):
            st = printer_state(dev)
            if st[0] == "20":
                break
            time.sleep(2)
        print("Printer state:", st)
        if st[0] != "20":
            sys.exit("Printer is not idle. Aborting.")
        print(f"Image {len(image)} bytes, cut file {len(plt)} bytes, "
              f"order: {'image then cut' if image_first else 'cut then image'}")
        if input("This will print and cut ONE sticker sheet. Continue? [y/N] ").lower() != "y":
            sys.exit("Cancelled.")

        job = [
            {"method": "print-job", "params": {"channel": 0, "copies": 1, "file-size": len(image),
             "media-size": 5013, "media-type": 2030, "job-type": 600}},
            {"method": "cut-job", "params": {"channel": 0, "copies": 1, "file-size": len(plt),
             "media-size": 5013, "media-type": 2030, "job-type": 600}},
        ]
        reply = command(dev, "combo-job", job)
        print("Create reply:", reply)
        job_id = reply["result"]["job_id"]

        total = (len(payload) + CHUNK - 1) // CHUNK
        replies = 0
        for i in range(total):
            chunk = payload[i * CHUNK:(i + 1) * CHUNK]
            pkt = b"cmd data EXTLEN=" + str(len(chunk) + 4).encode() + b"\n" + struct.pack("<I", job_id) + chunk
            dev.write(DATA_OUT, pkt, timeout=5000)
            try:
                ack = bytes(dev.read(DATA_IN, 4096, timeout=300))
            except usb.core.USBTimeoutError:
                ack = b""
            if i < 3:
                print("data-channel reply:", ack[:80])
            if b"unsupported" in ack:
                raise RuntimeError(f"packet {i + 1}: printer said {ack!r}")
        print(f"Sent {total} packets for job {job_id}. Monitoring...")

        last, end = None, time.time() + 240
        while time.time() < end:
            time.sleep(3)
            info = command(dev, "get-job-info", {"job-id": job_id})["result"][0]
            key = (info.get("job-state"), info.get("job-sub-state"),
                   info.get("printing-page-number"), info.get("cutting-progress"))
            if key != last:
                print(time.strftime("%H:%M:%S"), key)
                last = key
            if info.get("job-state") == 9:
                print("Job finished. Printer state:", printer_state(dev))
                return
        print("Timed out waiting for the job. Power-cycle the printer if it is stuck.")
    finally:
        for intf in (CMD_IF, DATA_IF):
            try:
                usb.util.release_interface(dev, intf)
            except Exception:
                pass


main()

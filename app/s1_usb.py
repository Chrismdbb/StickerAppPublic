"""Low-level driver for the Liene PixCut S1 (DHP700) over USB.

Everything here mirrors what was proven by hand with s1_print.py on Mac and Windows:

  * commands:  b"cmd json\\n" + JSON, on interface 2 (OUT 0x06 / IN 0x86)
  * job data:  b"cmd data EXTLEN=<n>\\n" + job id (4 bytes, little-endian) + chunk,
               on interface 3 (OUT 0x04 / IN 0x84). n counts the 4 job-id bytes.
               The cut file is sent first, then the image.

The protocol code (S1Link) talks to a "transport" with write()/read(), so the same
code runs against real USB (UsbTransport) or the built-in simulator.
"""
import json
import struct
import time

VID, PID = 0x302C, 0x3101
CMD_IF, CMD_OUT, CMD_IN = 2, 0x06, 0x86
DATA_IF, DATA_OUT, DATA_IN = 3, 0x04, 0x84
CHUNK = 8192
MEDIA_SIZE, MEDIA_TYPE, JOB_TYPE_COMBO = 5013, 2030, 600

STATE_IDLE, STATE_STARTING, STATE_BUSY = "20", "30", "40"
JOB_DONE, JOB_FAILED = 9, 7

PAPER_WAIT = 1800.0     # longest a print waits for someone to reload paper before the app gives up
PAPER_PROBLEM ="Paper problem: out of paper, or the paper was pulled out. Reload the paper, then press Resume."


def friendly_state(raw):
    """Plain-language text for a printer state we have learned, else None.

    Seen on the real S1 (amber light) after the paper was pulled out mid-print:
    printer-state 60, sub-state 3015, alert 5306.
    """
    if raw and raw[0] == "60" and len(raw) > 2 and "5306" in raw[2]:
        return PAPER_PROBLEM
    return None


class PrinterError(Exception):
    """The printer did something unexpected.

    `stage` says how far a job got: idle-wait, create, transfer, print, finish.
    Failures in idle-wait/create are safe to retry on another printer because
    nothing has been printed yet.
    """

    def __init__(self, message, stage="unknown"):
        super().__init__(message)
        self.stage = stage


class PrinterGone(PrinterError):
    """USB I/O failed: unplugged, asleep, or reset."""


# --------------------------------------------------------------------------- #
# Real USB
# --------------------------------------------------------------------------- #

class UsbTransport:
    def __init__(self, dev):
        self.dev = dev
        self.claimed = []

    def open(self):
        import usb.core
        import usb.util
        try:
            for intf in (CMD_IF, DATA_IF):
                usb.util.claim_interface(self.dev, intf)
                self.claimed.append(intf)
        except usb.core.USBError as e:
            self.close()
            raise PrinterGone(
                f"found the printer but could not open it ({e}). "
                "Is the Liene Photo app running?", "connect")

    def close(self):
        import usb.util
        for intf in self.claimed:
            try:
                usb.util.release_interface(self.dev, intf)
            except Exception:
                pass
        self.claimed = []
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    def write(self, ep, data, timeout_ms):
        import usb.core
        try:
            self.dev.write(ep, data, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            raise TimeoutError("USB write timed out")
        except usb.core.USBError as e:
            raise PrinterGone(f"USB write failed: {e}")

    def read(self, ep, size, timeout_ms):
        import usb.core
        try:
            return bytes(self.dev.read(ep, size, timeout=timeout_ms))
        except usb.core.USBTimeoutError:
            raise TimeoutError("USB read timed out")
        except usb.core.USBError as e:
            raise PrinterGone(f"USB read failed: {e}")


class UsbBackend:
    """Finds real S1 printers on USB."""

    def __init__(self):
        self._backend = None
        self._serials = {}  # (bus, address) -> serial

    def _libusb(self):
        if self._backend is None:
            import libusb_package
            from usb.backend import libusb1
            self._backend = libusb1.get_backend(find_library=libusb_package.find_library)
        return self._backend

    def scan(self):
        """Return {serial: device} for every S1 currently visible on USB."""
        import usb.core
        import usb.util
        found, keys = {}, set()
        devs = usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=self._libusb())
        for dev in devs or []:
            key = (dev.bus, dev.address)
            keys.add(key)
            serial = self._serials.get(key)
            if serial is None:
                try:
                    serial = usb.util.get_string(dev, dev.iSerialNumber)
                except Exception:
                    serial = None
                serial = serial or f"bus{key[0]}-dev{key[1]}"
                self._serials[key] = serial
            found[serial] = dev
        for key in list(self._serials):
            if key not in keys:
                del self._serials[key]
        return found

    def open(self, serial, token):
        return UsbTransport(token)     # the caller calls .open() to claim the interfaces

    def forget(self, serial):
        for key, s in list(self._serials.items()):
            if s == serial:
                del self._serials[key]


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #

class S1Link:
    def __init__(self, transport):
        self.t = transport
        self._id = 0

    def drain(self):
        for ep in (CMD_IN, DATA_IN):
            for _ in range(20):
                try:
                    self.t.read(ep, 4096, 100)
                except TimeoutError:
                    break

    def _read_reply(self, expect_id, wait):
        got, end = b"", time.time() + wait
        while time.time() < end:
            try:
                got += self.t.read(CMD_IN, 4096, 500)
            except TimeoutError:
                continue
            text = got.decode("utf-8", "replace")
            brace = text.find("{")
            if brace < 0:
                if "unsupported" in text:
                    raise PrinterError("printer rejected the command (unsupported cmd)", "command")
                continue
            try:
                reply = json.loads(text[brace:])
            except ValueError:
                continue  # partial reply, keep reading
            if reply.get("id") == expect_id:
                return reply
            got = b""  # stale reply from an earlier request
        raise PrinterError("no reply from printer", "command")

    def command(self, method, params, wait=5.0):
        self._id += 1
        body = json.dumps({"method": method, "params": params, "id": self._id},
                          separators=(",", ":"))
        self.t.write(CMD_OUT, b"cmd json\n" + body.encode(), 3000)
        return self._read_reply(self._id, wait)

    def get_state(self):
        r = self.command("get-prop", ["printer-state", "printer-sub-state", "printer-state-alerts"])
        res = r.get("result")
        if not isinstance(res, list) or len(res) < 3:
            raise PrinterError(f"unexpected status reply: {r}", "status")
        return tuple(str(x) for x in res[:3])

    def get_prop(self, name):
        """Read one printer property (read-only). Returns the raw value, or None if it has none."""
        r = self.command("get-prop", [name])
        res = r.get("result")
        val = res[0] if isinstance(res, list) and res else None
        if isinstance(val, dict) and name in val:      # the S1 answers {"auto-off-interval": 1200}
            val = val[name]
        return val

    def device_info(self):
        r = self.command("get-prop", ["device-info"])
        for item in r.get("result") or []:
            if isinstance(item, dict) and item:
                return item
        return {}

    def job_info(self, job_id):
        r = self.command("get-job-info", {"job-id": job_id})
        res = r.get("result")
        if isinstance(res, list) and res and isinstance(res[0], dict):
            return res[0]
        raise PrinterError(f"unexpected job info reply: {r}", "print")

    def create_job(self, image_len, plt_len):
        def sub(method, size):
            return {"method": method, "params": {
                "channel": 0, "copies": 1, "file-size": size,
                "media-size": MEDIA_SIZE, "media-type": MEDIA_TYPE, "job-type": JOB_TYPE_COMBO}}
        r = self.command("combo-job", [sub("print-job", image_len), sub("cut-job", plt_len)])
        try:
            return int(r["result"]["job_id"])
        except Exception:
            raise PrinterError(f"printer refused the job: {r}", "create")

    def send_payload(self, job_id, payload, progress=None):
        total = (len(payload) + CHUNK - 1) // CHUNK
        jid = struct.pack("<I", job_id)
        for i in range(total):
            chunk = payload[i * CHUNK:(i + 1) * CHUNK]
            pkt = b"cmd data EXTLEN=" + str(len(chunk) + 4).encode() + b"\n" + jid + chunk
            self.t.write(DATA_OUT, pkt, 5000)
            try:
                ack = self.t.read(DATA_IN, 4096, 300)
            except TimeoutError:
                ack = b""
            if b"unsupported" in ack:
                raise PrinterError(f"printer rejected data packet {i + 1}/{total}", "transfer")
            if progress:
                progress(i + 1, total)

    def run_job(self, image, plt, on_event=None, idle_wait=20.0, job_timeout=420.0,
                slow_after=150.0, check_every=10.0):
        """Print and cut one sheet. Blocks until the printer reports it finished.

        A sheet normally takes about 2 min 15 s. Once the print stage has run for `slow_after`
        seconds, the printer's own status is read every `check_every` seconds and reported as a
        "slow" event, so a problem (no paper, jam) shows up long before `job_timeout`.
        """
        emit = on_event or (lambda e: None)
        stage = "idle-wait"
        try:
            self.drain()
            end = time.time() + idle_wait
            while True:
                st = self.get_state()
                if st[0] == STATE_IDLE:
                    break
                if time.time() > end:
                    raise PrinterError(f"printer is not idle (state {'/'.join(st)})", stage)
                time.sleep(2)

            stage = "create"
            job_id = self.create_job(len(image), len(plt))
            emit({"type": "created", "job_id": job_id})

            stage = "transfer"
            self.send_payload(job_id, plt + image,
                              lambda i, n: emit({"type": "transfer", "done": i, "total": n}))

            stage = "print"
            print_start = time.time()
            deadline = print_start + job_timeout
            next_check = print_start + slow_after
            info = {}
            while True:
                time.sleep(3)
                info = self.job_info(job_id)
                emit({"type": "progress", "info": info})
                if info.get("job-state") not in (JOB_DONE, JOB_FAILED) and time.time() >= next_check:
                    next_check = time.time() + check_every
                    try:
                        st = self.get_state()
                        emit({"type": "slow", "state": st, "elapsed": time.time() - print_start})
                        if friendly_state(st):
                            # The S1 holds the job while the paper is missing and carries on once it
                            # is reloaded, so keep waiting (up to PAPER_WAIT) instead of timing out.
                            deadline = max(deadline, min(time.time() + job_timeout, print_start + PAPER_WAIT))
                    except PrinterGone:
                        raise
                    except PrinterError:
                        pass            # a failed status check must never fail the job
                state = info.get("job-state")
                if state == JOB_DONE:
                    break
                if state == JOB_FAILED:
                    raise PrinterError(
                        f"printer reported the job failed (job-state 7, reason "
                        f"{info.get('job-state-reason')})", stage)
                if time.time() > deadline:
                    raise PrinterError("timed out waiting for the job to finish. The printer may still "
                                       "finish this sheet, so check it before pressing Retry", stage)
        except PrinterError as e:
            if e.stage in ("unknown", "command", "status"):
                e.stage = stage
            raise

        # The sheet is done. Give the printer a moment to eject and go idle, but a
        # hiccup here must not turn a finished sticker into a failed job.
        try:
            end = time.time() + 60
            while time.time() < end:
                if self.get_state()[0] == STATE_IDLE:
                    break
                time.sleep(2)
        except PrinterError:
            pass
        return {"job_id": job_id, "info": info}

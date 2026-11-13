"""Simulated S1 printers.

FakeS1 speaks the same wire format as the real printer (as far as we have mapped it),
so S1Link, the job worker and the pool all run their real code against it.
Start the app with  --simulate 2  to get two fake printers.
"""
import json
import threading
import time
from collections import deque

from s1_usb import (CMD_IN, CMD_OUT, DATA_IN, DATA_OUT, PrinterGone, STATE_BUSY,
                    STATE_IDLE)


class FakeS1:
    def __init__(self, serial, job_seconds=25.0):
        self.serial = serial
        self.job_seconds = job_seconds
        self.asleep = False
        self.fail_next = False
        self.stall_next = False
        self._lock = threading.Lock()
        self._cmd_out = deque()
        self._data_out = deque()
        self._next_job = 1
        self.job = None  # dict while a job exists
        self.jobs_done = 0

    # --- transport interface ------------------------------------------------
    def open(self):
        return None

    def close(self):
        return None

    def write(self, ep, data, timeout_ms):
        self._check_awake()
        with self._lock:
            if ep == CMD_OUT:
                self._handle_command(data)
            elif ep == DATA_OUT:
                self._handle_data(data)

    def read(self, ep, size, timeout_ms):
        self._check_awake()
        with self._lock:
            q = self._cmd_out if ep == CMD_IN else self._data_out
            if q:
                return q.popleft()
        time.sleep(min(timeout_ms, 40) / 1000.0)
        raise TimeoutError("simulated read timeout")

    # --- emulation ----------------------------------------------------------
    def _check_awake(self):
        if self.asleep:
            raise PrinterGone("simulated printer went to sleep")

    def _handle_command(self, data):
        if not data.startswith(b"cmd json\n"):
            self._cmd_out.append(b"unsupported cmd\n")
            return
        try:
            req = json.loads(data[len(b"cmd json\n"):].decode())
        except ValueError:
            self._cmd_out.append(b"unsupported cmd\n")
            return
        method, rid, params = req.get("method"), req.get("id"), req.get("params")
        if method == "get-prop":
            self._reply(rid, [self._prop(p) for p in params])
        elif method == "combo-job":
            if self.job and not self.job["finished"]:
                self._reply(rid, {"error-code": 1})
                return
            jid = self._next_job
            self._next_job += 1
            sizes = [p["params"]["file-size"] for p in params]
            self.job = {"id": jid, "expected": sum(sizes), "received": 0, "started": None,
                        "finished": False, "will_fail": self.fail_next, "fail_at": None,
                        "stall": self.stall_next}
            self.fail_next = self.stall_next = False
            self._reply(rid, {"job_id": jid})
        elif method == "get-job-info":
            self._reply(rid, [self._job_info(params.get("job-id"))])
        else:
            self._cmd_out.append(b"unsupported cmd\n")

    def _reply(self, rid, result):
        self._cmd_out.append(json.dumps({"result": result, "id": rid}).encode())

    def _handle_data(self, data):
        head, sep, rest = data.partition(b"\n")
        if not (head.startswith(b"cmd data EXTLEN=") and sep and self.job):
            self._data_out.append(b"unsupported cmd\n")
            return
        try:
            n = int(head[len(b"cmd data EXTLEN="):])
        except ValueError:
            self._data_out.append(b"unsupported cmd\n")
            return
        job_id = int.from_bytes(rest[:4], "little")
        if n != len(rest) or job_id != self.job["id"]:
            self.job["will_fail"] = True
            self._data_out.append(b"unsupported cmd\n")
            return
        self.job["received"] += n - 4
        self._data_out.append(b"cmd data EXTLEN=%d OK\r\n" % n)
        if self.job["received"] >= self.job["expected"] and self.job["started"] is None:
            self.job["started"] = time.time()

    def _phase(self):
        """Return (printer_state, sub_state, job_state, page, cut_progress)."""
        j = self.job
        if not j or j["started"] is None:
            return STATE_IDLE, "2000", 3, 0, 0
        t = time.time() - j["started"]
        total = self.job_seconds
        if j.get("stall") and t > 3:       # like the real S1 with the paper pulled out mid-print
            return "60", "3015", 3, 1, 0
        if j["will_fail"]:
            if t > 2:
                j["finished"] = True
                return STATE_IDLE, "2000", 7, 0, 0
            return STATE_BUSY, "3016", 3, 0, 0
        if t < total * 0.1:
            return STATE_BUSY, "3016", 3, 0, 0
        if t < total * 0.8:
            return STATE_BUSY, "3015", 3, 1, 0
        if t < total:
            return STATE_BUSY, "3018", 3, 1, min(3, int((t - total * 0.8) / (total * 0.2) * 3) + 1)
        if t < total * 1.1:
            return STATE_BUSY, "9000", 9, 1, 0
        if not j["finished"]:
            j["finished"] = True
            self.jobs_done += 1
        return STATE_IDLE, "2000", 9, 1, 0

    def _prop(self, name):
        ps, sub, *_ = self._phase()
        if name == "printer-state":
            return ps
        if name == "printer-sub-state":
            return sub
        if name == "printer-state-alerts":
            return "::5306" if ps == "60" else "::0"
        if name == "auto-off-interval":
            return 900
        if name == "device-info":
            return {"fw_ver": "SIM", "model": "DHP700", "sn_all": self.serial}
        return {}

    def _job_info(self, job_id):
        ps, sub, js, page, cut = self._phase()
        j = self.job or {}
        return {"job-id": job_id, "job-state": js, "job-sub-state": 3005 if js == 3 else 9000,
                "printing-page-number": page, "cutting-progress": cut,
                "transfer-size": j.get("received", 0)}


class SimBackend:
    """Same interface as UsbBackend, but with fake printers."""

    sim = True

    def __init__(self, count, job_seconds):
        self.printers = {f"SIM{i + 1:04d}": FakeS1(f"SIM{i + 1:04d}", job_seconds)
                         for i in range(count)}

    def scan(self):
        return {s: p for s, p in self.printers.items() if not p.asleep}

    def open(self, serial, token):
        return token

    def forget(self, serial):
        pass

    def control(self, serial, action):
        p = self.printers.get(serial)
        if not p:
            raise KeyError(serial)
        if action == "sleep":
            p.asleep = True
        elif action == "wake":
            p.asleep = False
        elif action == "fail-next":
            p.fail_next = True
        elif action == "stall-next":
            p.stall_next = True
        elif action == "paper-back":       # the stalled sheet carries on, as the real S1 does
            if p.job:
                p.job["stall"] = False
        else:
            raise ValueError(action)

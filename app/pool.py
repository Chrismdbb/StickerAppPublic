"""Config, job database, folder watcher and the printer pool."""
import collections
import copy
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import sheetgen
from s1_usb import (PrinterError, PrinterGone, S1Link, STATE_BUSY, STATE_IDLE,
                    STATE_STARTING, friendly_state)

log = logging.getLogger("stickerapp")

# Job statuses
PREPARING, HELD, QUEUED, PRINTING = "preparing", "held", "queued", "printing"
DONE, FAILED, CANCELLED = "done", "failed", "cancelled"

DEFAULTS = {
    "watch_folder": "",
    "recursive": False,
    "extensions": [".jpg", ".jpeg", ".png"],
    "exact_size_only": True,   # only images exactly 1200x2100 become stickers; others are skipped
    "network_access": False,   # True: phones/tablets on the same network can open the dashboard (needs restart)
    "auto_print": True,       # False: new photos wait as "held" until you press Print
    "copies": 1,               # sheets printed per new photo
    "paused": False,
    "settle_seconds": 2.0,     # a file must stop changing this long before it is used
    "cut_mode": "contour",     # contour | rect
    "cut_offset_px": 20,
    "non_4x7": "reject",       # reject | fit
    "edge_policy": "shrink",   # shrink | clamp | fail
    "config_version": 2,
    "max_attempts": 2,
    "paper_capacity": 15,      # sheets in a full load of paper
    "paper_low_at": 3,         # "low on paper" when this many sheets or fewer are left
    "history_days": 30,
    "slow_print_seconds": 150, # a print running longer than this triggers live status checks
    "job_timeout_seconds": 420,  # give up on a print after this long (paper problems pause the clock)
    "printers": {},           # serial -> {name, enabled, sheets_since_reload, total_sheets}
}
CHOICES = {"cut_mode": ("contour", "rect"), "non_4x7": ("reject", "fit"),
           "edge_policy": ("shrink", "clamp", "fail")}
INT_RANGES = {"copies": (1, 10), "cut_offset_px": (0, 80), "max_attempts": (1, 5),
              "paper_capacity": (1, 100), "paper_low_at": (1, 50), "history_days": (1, 365), "slow_print_seconds": (5, 600),
              "job_timeout_seconds": (20, 3600)}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if k in DEFAULTS:
                        self.data[k] = v
                if "paper_capacity" not in saved and "paper_warn_at" in saved:
                    self.data["paper_capacity"] = saved["paper_warn_at"]   # older name
                if saved.get("config_version", 1) < 2:       # older default was "clamp"
                    if self.data["edge_policy"] == "clamp":
                        self.data["edge_policy"] = "shrink"
                    self.data["config_version"] = 2
            except Exception as e:
                log.error("Could not read %s (%s); starting with defaults", path, e)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self.data[key])

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def printer(self, serial):
        with self.lock:
            p = self.data["printers"].setdefault(serial, {})
            p.setdefault("name", "S1 " + serial[-4:])
            p.setdefault("enabled", True)
            p.setdefault("sheets_since_reload", 0)
            p.setdefault("total_sheets", 0)
            if "paper_left" not in p:      # older configs only counted sheets used
                p["paper_left"] = max(0, self.data["paper_capacity"] - p["sheets_since_reload"])
            return p

    def paper_level(self, left):
        """ok | low | last | out, from the number of sheets left."""
        if left <= 0:
            return "out"
        if left == 1:
            return "last"
        if left <= self.get("paper_low_at"):
            return "low"
        return "ok"

    def update(self, changes):
        """Validate and apply settings. Returns the list of keys that changed."""
        changed = []
        with self.lock:
            try:
                cap = int(changes.get("paper_capacity", self.data["paper_capacity"]))
                low = int(changes.get("paper_low_at", self.data["paper_low_at"]))
                if low >= cap:
                    raise ValueError("The low-paper warning must be lower than the number of sheets in a full load")
            except TypeError:
                pass
            for k, v in changes.items():
                if k not in DEFAULTS or k in ("printers", "config_version"):
                    continue
                if isinstance(DEFAULTS[k], bool):
                    v = bool(v)
                elif k in INT_RANGES:
                    lo, hi = INT_RANGES[k]
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        raise ValueError(f"{k} must be a whole number")
                    if not lo <= v <= hi:
                        raise ValueError(f"{k} must be between {lo} and {hi}")
                elif k in CHOICES:
                    if v not in CHOICES[k]:
                        raise ValueError(f"{k} must be one of {', '.join(CHOICES[k])}")
                elif k == "settle_seconds":
                    v = float(v)
                    if not 0 <= v <= 60:
                        raise ValueError("settle_seconds must be between 0 and 60")
                elif k == "watch_folder":
                    v = str(v).strip().strip('"')
                    if v and not os.path.isdir(v):
                        raise ValueError(f"Folder not found: {v}")
                    if v:
                        v = os.path.abspath(v)
                elif k == "extensions":
                    v = [e if e.startswith(".") else "." + e for e in
                         (str(x).strip().lower() for x in v) if e]
                if self.data[k] != v:
                    self.data[k] = v
                    changed.append(k)
            if changed:
                self.save()
        return changed


# --------------------------------------------------------------------------- #
# Job database
# --------------------------------------------------------------------------- #

class Db:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created REAL, source_path TEXT, name TEXT, status TEXT,
                printer TEXT, error TEXT DEFAULT '', warning TEXT DEFAULT '',
                attempts INTEGER DEFAULT 0, failed_on TEXT DEFAULT '',
                kind TEXT DEFAULT 'auto', parent_id INTEGER,
                started REAL, finished REAL, stickers INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS seen (
                path TEXT, mtime_ns INTEGER, size INTEGER,
                PRIMARY KEY (path, mtime_ns, size));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)
            self.conn.commit()

    def _run(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def add_job(self, **f):
        f.setdefault("created", time.time())
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        return self._run(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(f.values())).lastrowid

    def update_job(self, job_id, **f):
        sets = ", ".join(f"{k}=?" for k in f)
        self._run(f"UPDATE jobs SET {sets} WHERE id=?", (*f.values(), job_id))

    def get_job(self, job_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(r) if r else None

    def jobs(self, status=None, limit=150, ascending=False):
        order = "ASC" if ascending else "DESC"
        with self.lock:
            if status:
                rows = self.conn.execute(
                    f"SELECT * FROM jobs WHERE status=? ORDER BY id {order} LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT * FROM jobs ORDER BY id {order} LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def count(self, where, args=()):
        with self.lock:
            return self.conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", args).fetchone()[0]

    def is_seen(self, path, mtime_ns, size):
        with self.lock:
            return self.conn.execute(
                "SELECT 1 FROM seen WHERE path=? AND mtime_ns=? AND size=?",
                (path, mtime_ns, size)).fetchone() is not None

    def mark_seen(self, path, mtime_ns, size):
        self._run("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (path, mtime_ns, size))

    def meta(self, key, default=None):
        with self.lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r[0] if r else default

    def set_meta(self, key, value):
        self._run("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))


# --------------------------------------------------------------------------- #
# Printer handle
# --------------------------------------------------------------------------- #

class Printer:
    def __init__(self, serial):
        self.serial = serial
        self.transport = None
        self.link = None
        self.io = threading.Lock()       # one conversation with the device at a time
        self.state = "offline"           # offline | starting | idle | busy | attention
        self.detail = "Not found on USB. If it went to sleep, press its power button."
        self.raw = None
        self.job_id = None               # app job currently running on this printer
        self.progress = ""
        self.needs_ack = False           # failed job: wait for a person to check and press Resume
        self.last_poll = 0.0
        self.last_unfamiliar = None


# --------------------------------------------------------------------------- #
# Manager: finds printers, keeps their status fresh, hands queued jobs to them
# --------------------------------------------------------------------------- #

class Manager(threading.Thread):
    def __init__(self, cfg, db, backend, data_dir):
        super().__init__(daemon=True, name="manager")
        self.cfg, self.db, self.backend, self.data_dir = cfg, db, backend, data_dir
        self.lock = threading.RLock()
        self.printers = {}
        self.rr = 0
        self.stop_event = threading.Event()
        self.last_scan = 0.0
        self.last_beat = 0.0
        self.scan_error = ""
        self.progress = {}               # job id -> text for the dashboard

    # --- lifecycle ----------------------------------------------------------
    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("manager tick failed")
            self.stop_event.wait(0.5)
        for p in list(self.printers.values()):
            self._disconnect(p, "app stopped", quiet=True)

    def tick(self):
        now = time.time()
        if now - self.last_scan >= 3:
            self.last_scan = now
            self.scan()
        self.poll_statuses()
        self.dispatch()
        if now - self.last_beat >= 10:
            self.last_beat = now
            self.db.set_meta("heartbeat", now)

    # --- printers -----------------------------------------------------------
    def scan(self):
        try:
            found = self.backend.scan()
            self.scan_error = ""
        except Exception as e:
            found = {}
            msg = str(e)
            if msg != self.scan_error:
                log.error("USB scan failed: %s", msg)
            self.scan_error = msg
        with self.lock:
            for serial, token in found.items():
                p = self.printers.get(serial)
                if p is None:
                    p = self.printers[serial] = Printer(serial)
                    self.cfg.printer(serial)
                    self.cfg.save()
                    log.info("Printer found: %s", serial)
                if p.link is None and p.job_id is None:
                    self._connect(p, token)
            for serial, p in self.printers.items():
                if serial not in found and p.link is not None and p.job_id is None:
                    self._disconnect(p, "Not found on USB. If it went to sleep, press its power button.")

    def _connect(self, p, token):
        try:
            t = self.backend.open(p.serial, token)
            t.open()
            p.transport, p.link = t, S1Link(t)
            p.state, p.detail, p.needs_ack = "starting", "Connected, checking status...", False
            p.last_poll = 0
            log.info("Printer %s connected", p.serial)
        except PrinterError as e:
            p.state, p.detail = "offline", str(e)

    def _disconnect(self, p, reason, quiet=False):
        if p.transport is not None:
            try:
                p.transport.close()
            except Exception:
                pass
        p.transport = p.link = None
        p.state, p.detail, p.raw = "offline", reason, None
        self.backend.forget(p.serial)
        if not quiet:
            log.info("Printer %s offline: %s", p.serial, reason)

    def poll_statuses(self):
        now = time.time()
        with self.lock:
            printers = list(self.printers.values())
        for p in printers:
            if p.link is None or p.job_id is not None or now - p.last_poll < 3:
                continue
            if not p.io.acquire(blocking=False):
                continue
            try:
                p.last_poll = now
                raw = p.link.get_state()
                self._classify(p, raw)
            except PrinterGone as e:
                with self.lock:
                    self._disconnect(p, f"Lost connection ({e})")
            except PrinterError as e:
                p.state, p.detail = "attention", f"Status check failed: {e}"
            finally:
                p.io.release()

    def _classify(self, p, raw):
        p.raw = raw
        if raw[0] == STATE_IDLE:
            if p.needs_ack:
                p.state = "attention"
                p.detail = "A job failed here. Check paper, ink and for jams, then press Resume."
            else:
                p.state, p.detail = "idle", "Ready"
            p.last_unfamiliar = None
        elif raw[0] in ("10", STATE_STARTING):        # 10 then 30 are seen while waking up
            p.state, p.detail = "starting", "Warming up..."
        elif raw[0] == STATE_BUSY:
            p.state, p.detail = "busy", "Busy (printing something that did not come from this app)"
        elif friendly_state(raw):
            p.state, p.detail = "attention", friendly_state(raw)
            if p.last_unfamiliar != raw:
                log.warning("Printer %s: %s (state %s)", p.serial, p.detail, "/".join(raw))
                p.last_unfamiliar = raw
        else:
            p.state = "attention"
            p.detail = f"Unfamiliar printer state {'/'.join(raw)}. Check the printer."
            if p.last_unfamiliar != raw:
                log.warning("Printer %s reports unfamiliar state %s", p.serial, raw)
                p.last_unfamiliar = raw

    # --- dispatch -----------------------------------------------------------
    def _ready(self, p):
        return (p.state == "idle" and p.link is not None and p.job_id is None
                and self.cfg.printer(p.serial)["paper_left"] > 0)

    def pick_printer(self, job):
        with self.lock:
            plist = sorted((p for p in self.printers.values()
                            if self.cfg.printer(p.serial)["enabled"]), key=lambda p: p.serial)
            if not plist:
                return None
            failed_on = set(filter(None, (job.get("failed_on") or "").split(",")))
            others_possible = any(p.serial not in failed_on and p.state != "offline" for p in plist)
            n = len(plist)
            for k in range(n):
                p = plist[(self.rr + k) % n]
                if not self._ready(p):
                    continue
                if p.serial in failed_on and others_possible:
                    continue
                self.rr = (self.rr + k + 1) % n
                return p
        return None

    def dispatch(self):
        if self.cfg.get("paused"):
            return
        for job in self.db.jobs(QUEUED, limit=50, ascending=True):
            p = self.pick_printer(job)
            if p is None:
                if not any(self._ready(x) for x in self.printers.values()):
                    return
                continue
            with self.lock:
                p.job_id, p.state, p.detail = job["id"], "busy", f"Printing job #{job['id']}"
            self.db.update_job(job["id"], status=PRINTING, printer=p.serial, started=time.time(), error="")
            threading.Thread(target=self.run_job, args=(p, job), daemon=True,
                             name=f"job-{job['id']}").start()

    def run_job(self, p, job):
        jid = job["id"]
        base = os.path.join(self.data_dir, "sheets", str(jid))
        try:
            with open(base + ".jpg", "rb") as f:
                image = f.read()
            with open(base + ".plt", "rb") as f:
                plt = f.read()
        except OSError as e:
            self._finish(p, job, f"Sheet files are missing ({e})", "create", gone=False)
            return

        def on_event(e):
            t = e["type"]
            if t == "transfer":
                self.progress[jid] = f"Sending to printer {e['done']}/{e['total']}"
            elif t == "progress":
                info = e["info"]
                page, cut = info.get("printing-page-number", 0), info.get("cutting-progress", 0)
                if cut:
                    self.progress[jid] = f"Cutting ({cut}/{info.get('cut-contours') or '?'})"
                elif page:
                    self.progress[jid] = "Printing"
                else:
                    self.progress[jid] = "Starting"
            elif t == "created":
                self.progress[jid] = "Job accepted by printer"
            elif t == "slow":
                # Past the normal print time: the printer's own status says what is wrong.
                st = e["state"]
                msg = friendly_state(st) or (
                    f"This print is taking longer than normal (printer state {'/'.join(st)}). "
                    "Check the printer's light, paper and for a jam.")
                with self.lock:
                    p.raw = st
                    if p.detail != msg:
                        log.warning("Job %s on %s after %ds: %s", jid, p.serial, e["elapsed"], msg)
                    p.state, p.detail = "attention", msg

        log.info("Job %s -> %s", jid, p.serial)
        try:
            with p.io:
                p.link.run_job(image, plt, on_event, slow_after=self.cfg.get("slow_print_seconds"),
                               job_timeout=float(self.cfg.get("job_timeout_seconds")))
        except PrinterGone as e:
            self._finish(p, job, f"Lost the printer: {e}", e.stage, gone=True)
            return
        except PrinterError as e:
            self._finish(p, job, str(e), e.stage, gone=False)
            return
        except Exception as e:  # a bug must not leave a job stuck in "printing"
            log.exception("unexpected error in job %s", jid)
            self._finish(p, job, f"Internal error: {e}", "unknown", gone=False)
            return

        self.progress.pop(jid, None)
        self.db.update_job(jid, status=DONE, finished=time.time(), error="")
        with self.cfg.lock:
            pc = self.cfg.printer(p.serial)
            pc["sheets_since_reload"] += 1
            pc["total_sheets"] += 1
            pc["paper_left"] = max(0, pc["paper_left"] - 1)
            self.cfg.save()
        with self.lock:
            p.job_id, p.progress = None, ""
            p.state, p.detail, p.last_poll = "starting", "Finishing up...", 0
        log.info("Job %s finished on %s", jid, p.serial)

    def _finish(self, p, job, message, stage, gone):
        jid = job["id"]
        self.progress.pop(jid, None)
        log.error("Job %s failed on %s at stage %s: %s", jid, p.serial, stage, message)
        attempts = (job.get("attempts") or 0) + 1
        failed_on = set(filter(None, (job.get("failed_on") or "").split(",")))
        failed_on.add(p.serial)
        max_attempts = self.cfg.get("max_attempts")
        with self.lock:
            others = any(x.serial != p.serial and self.cfg.printer(x.serial)["enabled"]
                         and x.state != "offline" for x in self.printers.values())
        safe_to_retry = stage in ("idle-wait", "create", "connect")
        if safe_to_retry and others and attempts < max_attempts:
            self.db.update_job(jid, status=QUEUED, attempts=attempts, error="",
                               failed_on=",".join(sorted(failed_on)), printer=None)
            log.info("Job %s re-queued for another printer", jid)
        else:
            self.db.update_job(jid, status=FAILED, attempts=attempts, finished=time.time(),
                               error=message, failed_on=",".join(sorted(failed_on)))
        with self.lock:
            p.job_id, p.progress = None, ""
            if gone:
                self._disconnect(p, f"Lost connection during a job ({message})")
            else:
                p.needs_ack = True
                p.state, p.detail, p.last_poll = "attention", "A job failed here. Check the printer, then press Resume.", 0

    # --- user actions -------------------------------------------------------
    def resume_printer(self, serial):
        with self.lock:
            p = self.printers.get(serial)
            if not p:
                raise KeyError(serial)
            p.needs_ack = False
            p.last_poll = 0
            if p.link is not None and p.job_id is None:
                p.state, p.detail = "starting", "Checking status..."

    def snapshot_printers(self):
        out = []
        with self.lock:
            for p in sorted(self.printers.values(), key=lambda p: p.serial):
                pc = self.cfg.printer(p.serial)
                enabled = pc["enabled"]
                left = pc["paper_left"]
                state, detail = (p.state, p.detail) if enabled else (
                    "disabled", "Disabled (no jobs will be sent here)")
                if enabled and left <= 0 and p.state == "idle":
                    state, detail = "nopaper", "Out of paper. Load paper, then press Paper loaded."
                out.append({
                    "serial": p.serial, "name": pc["name"], "enabled": enabled,
                    "state": state, "detail": detail,
                    "paper": {"left": left, "capacity": self.cfg.get("paper_capacity"),
                              "level": self.cfg.paper_level(left)},
                    "raw": "/".join(p.raw) if p.raw else "",
                    "job_id": p.job_id,
                    "progress": self.progress.get(p.job_id, "") if p.job_id else "",
                    "total_sheets": pc["total_sheets"],
                })
        return out


# --------------------------------------------------------------------------- #
# Folder watcher
# --------------------------------------------------------------------------- #

class Watcher(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True, name="watcher")
        self.app = app
        self.pending = {}                # path -> [size, mtime_ns, last_change, first_seen]
        self.stop_event = threading.Event()
        self.message = ""
        self.rescan_requested = threading.Event()

    def files(self, folder):
        exts = tuple(self.app.cfg.get("extensions"))
        if self.app.cfg.get("recursive"):
            for root, _dirs, names in os.walk(folder):
                for n in names:
                    if n.lower().endswith(exts):
                        yield os.path.join(root, n)
        else:
            for n in os.listdir(folder):
                if n.lower().endswith(exts):
                    yield os.path.join(folder, n)

    def baseline(self, folder):
        """Mark everything already in the folder as seen so it is not printed."""
        count = 0
        for path in self.files(folder):
            try:
                st = os.stat(path)
            except OSError:
                continue
            self.app.db.mark_seen(path, st.st_mtime_ns, st.st_size)
            count += 1
        log.info("Watching %s (%d existing files ignored)", folder, count)

    def startup_scan(self):
        """After a restart, only files that arrived while we were stopped are printed."""
        folder = self.app.cfg.get("watch_folder")
        if not folder or not os.path.isdir(folder):
            return
        if self.app.db.meta("baselined_folder") != folder:
            self.baseline(folder)
            self.app.db.set_meta("baselined_folder", folder)
            return
        beat = float(self.app.db.meta("heartbeat", 0) or 0)
        ignored = 0
        for path in self.files(folder):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not self.app.db.is_seen(path, st.st_mtime_ns, st.st_size) and st.st_mtime < beat - 60:
                self.app.db.mark_seen(path, st.st_mtime_ns, st.st_size)
                ignored += 1
        if ignored:
            log.info("%d older unprinted files in the folder were ignored at startup", ignored)

    def run(self):
        try:
            self.startup_scan()
        except Exception:
            log.exception("startup scan failed")
        while not self.stop_event.is_set():
            try:
                self.scan()
            except Exception:
                log.exception("watcher scan failed")
            self.stop_event.wait(1.5)

    def scan(self):
        folder = self.app.cfg.get("watch_folder")
        if not folder:
            self.message = "No folder chosen yet"
            return
        if not os.path.isdir(folder):
            self.message = f"Folder not found: {folder}"
            return
        self.message = ""
        now = time.time()
        settle = self.app.cfg.get("settle_seconds")
        present = set()
        for path in self.files(folder):
            present.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if self.app.db.is_seen(path, st.st_mtime_ns, st.st_size):
                self.pending.pop(path, None)
                continue
            entry = self.pending.get(path)
            if entry is None or entry[0] != st.st_size or entry[1] != st.st_mtime_ns:
                self.pending[path] = [st.st_size, st.st_mtime_ns, now, entry[3] if entry else now]
                continue
            if now - entry[2] < settle:
                continue
            if self.app.new_photo(path, st):
                self.pending.pop(path, None)
            elif now - entry[3] > 30:   # unreadable for 30s: give up on this version
                self.app.db.mark_seen(path, st.st_mtime_ns, st.st_size)
                self.pending.pop(path, None)
        for path in list(self.pending):
            if path not in present:
                del self.pending[path]


# --------------------------------------------------------------------------- #
# App: ties everything together for the web server
# --------------------------------------------------------------------------- #

class LogRing(logging.Handler):
    def __init__(self, size=300):
        super().__init__()
        self.lines = collections.deque(maxlen=size)

    def emit(self, record):
        self.lines.append(self.format(record))


class App:
    def __init__(self, data_dir, backend, cfg=None):
        self.data_dir = data_dir
        for sub in ("sheets", "thumbs"):
            os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
        self.cfg = cfg or Config(os.path.join(data_dir, "config.json"))
        self.db = Db(os.path.join(data_dir, "jobs.db"))
        self.backend = backend
        self.sim = bool(getattr(backend, "sim", False))
        self.manager = Manager(self.cfg, self.db, backend, data_dir)
        self.watcher = Watcher(self)
        self.logring = LogRing()
        self.started = time.time()
        self.lan_urls = []           # set by main.py when the dashboard listens on the network
        self._recover_jobs()
        self.cleanup()

    def start(self):
        self.manager.start()
        self.watcher.start()

    def stop(self):
        self.watcher.stop_event.set()
        self.manager.stop_event.set()
        self.manager.join(timeout=5)

    def _recover_jobs(self):
        for status in (PRINTING, PREPARING):
            for j in self.db.jobs(status, limit=1000):
                self.db.update_job(j["id"], status=FAILED, finished=time.time(),
                                   error="The app stopped while this job was running. "
                                         "Check whether it printed, then press Retry if needed.")

    def cleanup(self):
        cutoff = time.time() - self.cfg.get("history_days") * 86400
        old = [j for j in self.db.jobs(limit=100000) if (j["created"] or 0) < cutoff
               and j["status"] in (DONE, FAILED, CANCELLED)]
        for j in old:
            for path in self.job_files(j["id"]):
                try:
                    os.remove(path)
                except OSError:
                    pass
            self.db._run("DELETE FROM jobs WHERE id=?", (j["id"],))
        self.db._run("DELETE FROM seen WHERE rowid NOT IN "
                     "(SELECT rowid FROM seen ORDER BY rowid DESC LIMIT 20000)")

    def job_files(self, job_id):
        d = self.data_dir
        return [os.path.join(d, "sheets", f"{job_id}.jpg"), os.path.join(d, "sheets", f"{job_id}.plt"),
                os.path.join(d, "thumbs", f"{job_id}.jpg")]

    # --- photos -> jobs -----------------------------------------------------
    def new_photo(self, path, st):
        """Called by the watcher once a file has settled. Returns False if unreadable (retry)."""
        from PIL import Image
        try:
            with Image.open(path) as im:
                size = im.size
                im.load()
        except Exception as e:
            log.debug("%s not readable yet: %s", path, e)
            return False
        self.db.mark_seen(path, st.st_mtime_ns, st.st_size)
        name = os.path.basename(path)
        if self.cfg.get("exact_size_only") and size != (sheetgen.W, sheetgen.H):
            total = int(self.db.meta("skipped_total", 0) or 0) + 1
            self.db.set_meta("skipped_total", total)
            log.info("Skipped %s (%dx%d, not %dx%d)", name, size[0], size[1], sheetgen.W, sheetgen.H)
            return True
        copies = self.cfg.get("copies")
        status = QUEUED if self.cfg.get("auto_print") else HELD
        log.info("New photo: %s", name)
        first = None
        for i in range(copies):
            jid = self.db.add_job(source_path=path, name=name, status=PREPARING, kind="auto")
            if first is None:
                try:
                    result = sheetgen.prepare_sheet(path, *self.job_files(jid), self.cfg.snapshot())
                except sheetgen.SheetError as e:
                    self.db.update_job(jid, status=FAILED, error=str(e), finished=time.time())
                    log.error("%s: %s", name, e)
                    return True
                except Exception as e:
                    log.exception("preparing %s failed", name)
                    self.db.update_job(jid, status=FAILED, error=f"Internal error: {e}", finished=time.time())
                    return True
                first = jid
            else:
                for src, dst in zip(self.job_files(first), self.job_files(jid)):
                    shutil.copyfile(src, dst)
            self.db.update_job(jid, status=status, stickers=result["stickers"],
                               warning="; ".join(result["warnings"]))
        return True

    # --- actions from the dashboard -----------------------------------------
    def update_settings(self, body):
        body = dict(body)
        print_existing = bool(body.pop("print_existing", False))
        old_folder = self.cfg.get("watch_folder")
        changed = self.cfg.update(body)
        new_folder = self.cfg.get("watch_folder")
        if new_folder and ({"watch_folder", "recursive", "extensions"} & set(changed)):
            # Files that newly count as "in the folder" (a new folder, sub-folders, other
            # file types) are treated as already there, so nothing old gets printed.
            if not print_existing:
                self.watcher.baseline(new_folder)
            self.db.set_meta("baselined_folder", new_folder)
            self.watcher.pending.clear()
        return {"changed": changed, "old_folder": old_folder}

    def job_action(self, job_id, action):
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(f"No job #{job_id}")
        s = job["status"]
        if action == "print" and s == HELD:
            self.db.update_job(job_id, status=QUEUED)
        elif action == "cancel" and s in (QUEUED, HELD):
            self.db.update_job(job_id, status=CANCELLED, finished=time.time())
        elif action == "retry" and s in (FAILED, CANCELLED):
            if not os.path.exists(self.job_files(job_id)[0]):
                raise ValueError("The sheet files for this job were deleted; reprint from the original photo instead")
            self.db.update_job(job_id, status=QUEUED, error="", failed_on="", finished=None, printer=None)
        elif action == "reprint":
            if s in (PREPARING, HELD):
                raise ValueError("This job has not been printed yet")
            if not os.path.exists(self.job_files(job_id)[0]):
                raise ValueError("The sheet files for this job were deleted")
            new = self.db.add_job(source_path=job["source_path"], name=job["name"], status=QUEUED,
                                  kind="reprint", parent_id=job_id, stickers=job["stickers"],
                                  warning=job["warning"], failed_on=job.get("printer") or "")
            for src, dst in zip(self.job_files(job_id), self.job_files(new)):
                shutil.copyfile(src, dst)
        else:
            raise ValueError(f"Cannot {action} a job that is {s}")

    def printer_action(self, serial, action, body):
        if action == "resume":
            self.manager.resume_printer(serial)
        elif action in ("enable", "disable"):
            self.cfg.printer(serial)["enabled"] = action == "enable"
            self.cfg.save()
        elif action == "rename":
            name = str(body.get("name", "")).strip()[:40]
            if not name:
                raise ValueError("Name cannot be empty")
            self.cfg.printer(serial)["name"] = name
            self.cfg.save()
        elif action == "reset-paper":          # the "Paper loaded" button
            pc = self.cfg.printer(serial)
            pc["paper_left"] = self.cfg.get("paper_capacity")
            pc["sheets_since_reload"] = 0
            self.cfg.save()
        elif action == "set-paper":            # type in the exact number of sheets loaded
            try:
                n = int(body.get("sheets"))
            except (TypeError, ValueError):
                raise ValueError("Enter a whole number of sheets")
            if not 0 <= n <= 500:
                raise ValueError("Sheets must be between 0 and 500")
            self.cfg.printer(serial)["paper_left"] = n
            self.cfg.save()
        else:
            raise ValueError(f"Unknown printer action {action}")

    def browse_folder(self):
        code = ("import tkinter as tk\nfrom tkinter import filedialog\n"
                "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
                "print(filedialog.askdirectory(title='Choose the folder to watch') or '')")
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):
            exe = exe[:-len("pythonw.exe")] + "python.exe"
        flags = 0x08000000 if os.name == "nt" else 0     # no console window
        try:
            out = subprocess.run([exe, "-c", code], capture_output=True, text=True,
                                 timeout=300, creationflags=flags)
        except Exception as e:
            raise ValueError(f"Could not open a folder picker ({e}). Type the path instead.")
        if out.returncode != 0:
            raise ValueError("Could not open a folder picker. Type the path instead.")
        return out.stdout.strip()

    # --- state for the dashboard --------------------------------------------
    def state(self):
        cfg = self.cfg.snapshot()
        printers = self.manager.snapshot_printers()
        queued = self.db.count("status=?", (QUEUED,))
        held = self.db.count("status=?", (HELD,))
        banners = []
        if not cfg["watch_folder"]:
            banners.append(("warn", "Choose a folder to watch in Settings."))
        elif self.watcher.message:
            banners.append(("bad", self.watcher.message))
        if cfg["paused"]:
            banners.append(("warn", "Printing is paused. New photos are queued but nothing is sent to the printers."))
        if not cfg["auto_print"]:
            banners.append(("info", "Auto-print is off. New photos wait as Held until you press Print."))
        ready = [p for p in printers if p["state"] == "idle"]
        if not printers:
            banners.append(("warn", "No printer found. Plug in the S1 over USB and press its power button."))
        elif queued and not ready and not any(p["state"] == "busy" for p in printers):
            if any(p["state"] == "nopaper" for p in printers):
                banners.append(("bad", "Jobs are waiting because no printer has paper. "
                                       "Load paper, then press Paper loaded."))
            else:
                banners.append(("warn", "Jobs are waiting but no printer is ready. "
                                        "If it went to sleep, press its power button."))
        for p in printers:
            if p["state"] == "attention":
                banners.append(("bad", f"{p['name']}: {p['detail']}"))
            elif p["enabled"] and p["paper"]["level"] == "out":
                banners.append(("bad", f"{p['name']} is out of paper. Load paper, then press Paper loaded."))
            elif p["enabled"] and p["paper"]["level"] == "last":
                banners.append(("bad", f"{p['name']} has 1 page left."))
            elif p["enabled"] and p["paper"]["level"] == "low":
                banners.append(("warn", f"{p['name']} is low on paper ({p['paper']['left']} left)."))
        if self.manager.scan_error:
            banners.append(("bad", f"USB problem: {self.manager.scan_error}"))
        try:
            import cv2  # noqa: F401
        except ImportError:
            banners.append(("warn", "OpenCV is not installed, so die-cut shapes fall back to one rectangle."))

        jobs = self.db.jobs(limit=100)
        for j in jobs:
            j["progress"] = self.manager.progress.get(j["id"], "") if j["status"] == PRINTING else ""
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return {
            "now": time.time(), "sim": self.sim, "config": {k: v for k, v in cfg.items() if k != "printers"},
            "banners": [{"level": l, "text": t} for l, t in banners],
            "printers": printers, "jobs": jobs,
            "counts": {"queued": queued, "held": held,
                       "printing": self.db.count("status=?", (PRINTING,)),
                       "done_today": self.db.count("status=? AND finished>=?", (DONE, midnight)),
                       "failed_today": self.db.count("status=? AND finished>=?", (FAILED, midnight))},
            "skipped_total": int(self.db.meta("skipped_total", 0) or 0),
            "lan_urls": self.lan_urls,
            "watcher_pending": len(self.watcher.pending),
            "log": list(self.logring.lines)[-120:],
        }

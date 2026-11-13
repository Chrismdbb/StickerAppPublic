"""Keeps StickerApp running: starts app\\main.py and starts it again if it crashes or stops answering.

  python app/supervisor.py                 same options as main.py are passed through
  python app/supervisor.py --simulate 2

Closing it (Ctrl+C, or closing its window) stops the app too. If the app exits on purpose
(Ctrl+C inside it, exit code 0) or because the dashboard port is already taken (exit code 2),
the supervisor stops instead of restarting it in a loop.
"""
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "main.py")
GRACE = 60            # seconds a new process gets before it must answer on the dashboard port
CHECK_EVERY = 15      # seconds between health checks
MAX_MISSES = 4        # this many failed checks in a row (about a minute) = frozen, restart it
CRASH_WINDOW = 120    # more than 5 crashes in this many seconds = wait longer between restarts
PORT_BUSY = 2


def arg_value(args, name, default):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def healthy(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
            json.load(r)
        return True
    except Exception:
        return False


def main():
    args = sys.argv[1:]
    data = arg_value(args, "--data", os.path.join(HERE, "data"))
    os.makedirs(data, exist_ok=True)
    log = logging.getLogger("supervisor")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(os.path.join(data, "supervisor.log"), maxBytes=200_000,
                                              backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)

    crashes = []
    first = True
    while True:
        port = int(arg_value(args, "--port", 8080))
        # --open only on the first start, so a restart does not pop up a new browser tab
        cmd = [sys.executable, MAIN] + [a for a in args if first or a != "--open"]
        log.info("Starting StickerApp%s", "" if first else " again")
        first = False
        started = time.time()
        proc = subprocess.Popen(cmd)
        misses, reason = 0, ""
        try:
            last_check = time.time()
            while proc.poll() is None:
                time.sleep(1)
                now = time.time()
                if proc.poll() is not None or now - started < GRACE or now - last_check < CHECK_EVERY:
                    continue
                last_check = now
                misses = 0 if healthy(port) else misses + 1
                if misses >= MAX_MISSES:
                    reason = "it stopped answering"
                    log.error("StickerApp is not answering on port %d, restarting it", port)
                    proc.kill()
                    proc.wait()
        except KeyboardInterrupt:
            log.info("Stopping")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 0
        code = proc.returncode
        if not reason:
            if code == 0:
                log.info("StickerApp exited normally")
                return 0
            if code == PORT_BUSY:
                log.error("The dashboard port is already in use (is StickerApp already running?). Not restarting.")
                return PORT_BUSY
            reason = f"it crashed (exit code {code})"
        now = time.time()
        crashes = [t for t in crashes if now - t < CRASH_WINDOW] + [now]
        wait = 3 if len(crashes) <= 5 else 30
        log.error("Restarting in %ds because %s", wait, reason)
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())

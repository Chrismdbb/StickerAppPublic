"""StickerApp: watch a folder of booth photos and print + cut them on Liene PixCut S1 printers.

  python app/main.py                    use real printers
  python app/main.py --simulate 2       two fake printers (no hardware needed)
  python app/main.py --open             also open the dashboard in your browser
"""
import argparse
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pool import App, Config  # noqa: E402
from server import make_server, serve_in_thread  # noqa: E402


def lan_urls(port):
    """Addresses other devices can use to reach the dashboard (the PC's name and its network IP)."""
    urls = [f"http://{socket.gethostname()}:{port}/"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # UDP connect sends nothing; it just picks the route
            ip = s.getsockname()[0]
        if not ip.startswith("127."):
            urls.insert(0, f"http://{ip}:{port}/")
    except OSError:
        pass
    return urls


def main():
    ap =argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulate", type=int, metavar="N", help="use N simulated printers instead of USB")
    ap.add_argument("--sim-seconds", type=float, default=25.0,
                    help="how long a simulated sticker takes (a real S1 needs about 135)")
    ap.add_argument("--data", default=os.path.join(HERE, "data"), help="where settings, history and sheets are kept")
    ap.add_argument("--port", type=int, help="dashboard port (default 8080, or the saved setting)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    args = ap.parse_args()

    os.makedirs(args.data, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger("stickerapp")
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(os.path.join(args.data, "stickerapp.log"),
                                              maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if args.simulate:
        from simulator import SimBackend
        backend = SimBackend(args.simulate, args.sim_seconds)
    else:
        from s1_usb import UsbBackend
        backend = UsbBackend()

    cfg = Config(os.path.join(args.data, "config.json"))
    network = cfg.get("network_access")
    host = "0.0.0.0" if network else "127.0.0.1"   # 127.0.0.1 = this machine only
    port = args.port or 8080
    url = f"http://localhost:{port}/"

    app = App(args.data, backend, cfg)
    if network:
        app.lan_urls = lan_urls(port)
    root.addHandler(app.logring)
    app.logring.setFormatter(fmt)

    try:
        server = make_server(app, host, port)
    except OSError:
        root.error("Port %d is already in use. StickerApp may already be running: %s", port, url)
        if args.open:
            webbrowser.open(url)
        return 1

    app.start()
    serve_in_thread(server)
    root.info("StickerApp running at %s%s", url, "  (SIMULATED printers)" if args.simulate else "")
    for u in app.lan_urls:
        root.info("Other devices on this network can open %s", u)
    if args.open:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        root.info("Shutting down...")
    finally:
        server.shutdown()
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

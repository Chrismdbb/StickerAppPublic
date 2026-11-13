"""Tiny local web server: the dashboard page plus a JSON API."""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    server_version = "StickerApp"
    app = None  # set by make_server

    def log_message(self, fmt, *args):
        pass

    # --- helpers ------------------------------------------------------------
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, {"ok": False, "error": "not found"})
            return
        self._send(200, data, ctype)

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    # --- GET ----------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._file(os.path.join(HERE, "dashboard.html"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, {**self.app.state(), "local": self._is_local()})
        else:
            m = re.fullmatch(r"/(thumb|sheet)/(\d+)\.jpg", path)
            if not m:
                self._send(404, {"ok": False, "error": "not found"})
                return
            sub = "thumbs" if m.group(1) == "thumb" else "sheets"
            self._file(os.path.join(self.app.data_dir, sub, m.group(2) + ".jpg"), "image/jpeg")

    # --- POST ---------------------------------------------------------------
    def do_POST(self):
        # A custom header cannot be sent cross-site, so other web pages cannot drive this API.
        if self.headers.get("X-Requested-With") != "stickerapp":
            self._send(403, {"ok": False, "error": "forbidden"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, {"ok": False, "error": "bad request"})
            return
        path = urlparse(self.path).path
        app = self.app
        try:
            result = {}
            if path == "/api/settings":
                result = app.update_settings(body)
            elif path in ("/api/pause", "/api/resume"):
                app.cfg.update({"paused": path == "/api/pause"})
            elif path == "/api/browse":
                if not self._is_local():     # the folder dialog opens on the booth PC's screen
                    raise ValueError("Browse only works on the booth PC itself")
                result = {"path": app.browse_folder()}
            elif (m := re.fullmatch(r"/api/jobs/(\d+)/(print|cancel|retry|reprint)", path)):
                app.job_action(int(m.group(1)), m.group(2))
            elif (m := re.fullmatch(r"/api/printers/([^/]+)/(resume|enable|disable|rename|reset-paper|set-paper)", path)):
                app.printer_action(m.group(1), m.group(2), body)
            elif (m := re.fullmatch(r"/api/sim/([^/]+)/(sleep|wake|fail-next|stall-next|paper-back)", path)) and app.sim:
                app.backend.control(m.group(1), m.group(2))
            else:
                self._send(404, {"ok": False, "error": "not found"})
                return
            self._send(200, {"ok": True, **result})
        except (ValueError, KeyError) as e:
            self._send(400, {"ok": False, "error": str(e).strip("'\"")})
        except Exception as e:
            self._send(500, {"ok": False, "error": f"internal error: {e}"})


class Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second copy of the app bind the same port instead of failing,
    # so two copies could run at once and fight over the printer. Elsewhere it only speeds up restarts.
    allow_reuse_address = sys.platform != "win32"


def make_server(app, host, port):
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = Server((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve_in_thread(server):
    t = threading.Thread(target=server.serve_forever, daemon=True, name="http")
    t.start()
    return t

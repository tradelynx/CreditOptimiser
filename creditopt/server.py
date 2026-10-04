"""Local dashboard server. Binds to 127.0.0.1 only: your transcripts never leave the machine."""

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analysis, config, installer, router
from .transcripts import load_sessions

WEB = Path(__file__).parent / "web"


def report(claude_dir=None, days=30):
    cfg = config.load()
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    sessions = load_sessions(claude_dir, since=since)
    settings = analysis.Settings(context_budget=cfg["context_budget"])
    data = analysis.build_report(sessions, settings)
    data["config"] = cfg
    data["install_plan"] = installer.plan(claude_dir)
    return data


def make_handler(claude_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/report":
                days = int(parse_qs(url.query).get("days", ["30"])[0])
                return self._send(200, report(claude_dir, days))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            # Reject cross-site requests: only our own page may change settings.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                return self._send(403, {"error": "forbidden"})
            url = urlparse(self.path)
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "invalid json"})
            if url.path == "/api/route":
                return self._send(200, router.route(str(body.get("task", ""))))
            if url.path == "/api/config":
                clean = {}
                for k, default in config.DEFAULTS.items():
                    if k in body:
                        try:
                            clean[k] = type(default)(body[k])
                        except (TypeError, ValueError):
                            return self._send(400, {"error": f"bad value for {k}"})
                return self._send(200, config.save(clean))
            return self._send(404, {"error": "not found"})

    return Handler


def serve(port=8765, claude_dir=None, open_browser=True):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(claude_dir))
    url = f"http://127.0.0.1:{port}/"
    print(f"CreditOptimiser dashboard: {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

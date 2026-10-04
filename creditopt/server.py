"""Local dashboard server. Binds to 127.0.0.1 only: your transcripts never leave the machine."""

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import analysis, config, discover, installer, router, runner
from .transcripts import filter_repo, load_sessions, repositories

WEB = Path(__file__).parent / "web"


def repository_list(sessions, cfg, rescan=False):
    """Repos Claude Code has been used in, then every other git repo found on disk."""
    used = repositories(sessions)
    known = {r["path"] for r in used}
    for r in used:
        r["used"] = True
    others = [{"path": p, "name": Path(p).name, "cost": 0, "used": False}
              for p in discover.cached_repos(config.repo_roots(cfg), cfg["scan_depth"], rescan)
              if p not in known]
    return used + others


def report(claude_dir=None, days=30, repo=None, rescan=False):
    cfg = config.load()
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    sessions = load_sessions(claude_dir, since=since)
    settings = analysis.Settings(context_budget=cfg["context_budget"])
    data = analysis.build_report(filter_repo(sessions, repo), settings)
    data["config"] = cfg
    data["repo"] = repo or ""
    data["repositories"] = repository_list(sessions, cfg, rescan)
    data["install_plan"] = installer.plan(claude_dir, repo=repo)
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

        def _local_host(self):
            # Blocks DNS-rebinding: only answer requests addressed to this machine.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in ("127.0.0.1", "localhost", "::1")

        def do_GET(self):
            if not self._local_host():
                return self._send(403, {"error": "forbidden"})
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/runs":
                return self._send(200, {"history": runner.history(),
                                        "claude": bool(runner.find_claude()),
                                        "presets": runner.PRESETS,
                                        "defaults": runner.resolve_options()})
            if url.path.startswith("/api/run/"):
                run = runner.RUNS.get(url.path.rsplit("/", 1)[-1])
                if not run:
                    return self._send(404, {"error": "unknown run"})
                after = int(parse_qs(url.query).get("after", ["0"])[0])
                return self._send(200, run.snapshot(after))
            if url.path == "/api/report":
                q = parse_qs(url.query)
                days = int(q.get("days", ["30"])[0])
                return self._send(200, report(claude_dir, days, q.get("repo", [""])[0] or None,
                                              q.get("rescan", [""])[0] == "1"))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            # Reject cross-site requests: only our own page may change settings.
            origin = self.headers.get("Origin")
            if not self._local_host() or (origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost")):
                return self._send(403, {"error": "forbidden"})
            url = urlparse(self.path)
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "invalid json"})
            if url.path == "/api/route":
                return self._send(200, router.route(str(body.get("task", ""))))
            if url.path == "/api/plan":
                return self._send(200, runner.plan_run(str(body.get("task", "")),
                                                       str(body.get("repo", "")),
                                                       options=body.get("options") or {}))
            if url.path == "/api/run":
                try:
                    run = runner.start(str(body.get("task", "")), str(body.get("repo", "")),
                                       options=body.get("options") or {})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, run.snapshot())
            if url.path.startswith("/api/run/") and url.path.endswith("/cancel"):
                run = runner.RUNS.get(url.path.split("/")[3])
                if run:
                    run.cancel()
                return self._send(200, {"ok": bool(run)})
            if url.path == "/api/config":
                clean = {}
                for k in config.DEFAULTS:
                    if k in body:
                        try:
                            clean[k] = config.coerce(k, body[k])
                        except (TypeError, ValueError):
                            return self._send(400, {"error": f"bad value for {k}"})
                return self._send(200, config.save(clean))
            if url.path == "/api/install":
                repo = str(body.get("repo") or "") or None
                if repo and not Path(repo).is_dir():
                    return self._send(400, {"error": f"no such folder: {repo}"})
                if body.get("uninstall"):
                    installer.uninstall(claude_dir, repo=repo)
                else:
                    installer.apply(claude_dir, repo=repo)
                return self._send(200, {"plan": installer.plan(claude_dir, repo=repo)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(port=8765, claude_dir=None, open_browser=True, repo=None):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(claude_dir))
    url = f"http://127.0.0.1:{port}/"
    if repo:
        url += "?repo=" + quote(repo)
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

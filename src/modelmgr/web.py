"""Local dashboard server: JSON API over queries.py plus the static files in web/.

Binds to 127.0.0.1 only. A random token is created per start; the URL printed by the CLI carries
it once, the server swaps it for a SameSite=Strict cookie, and every API call needs that cookie or
the X-MM-Token header. The Host header must name this server (blocks DNS rebinding). Read-only:
config writes come with the settings page.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import sqlite3
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import __version__, config, db, ingest, paths, policy, queries, registry

DEFAULT_PORT = 8765
INGEST_EVERY_S = 30.0
STATIC_DIR = Path(__file__).parent / "web"
COOKIE = "mm_token"
MAX_DAYS = 3650

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


class BadRequest(ValueError):
    pass


def _days(qs: dict[str, list[str]], default: int | None) -> int | None:
    raw = (qs.get("days") or [None])[0]
    if raw is None:
        return default
    if raw == "all":
        return None
    try:
        d = int(raw)
    except ValueError:
        raise BadRequest("days must be a number or 'all'") from None
    if not 1 <= d <= MAX_DAYS:
        raise BadRequest(f"days must be between 1 and {MAX_DAYS}")
    return d


def _limit(qs: dict[str, list[str]], default: int) -> int:
    raw = (qs.get("limit") or [None])[0]
    if raw is None:
        return default
    try:
        n = int(raw)
    except ValueError:
        raise BadRequest("limit must be a number") from None
    if not 1 <= n <= 5000:
        raise BadRequest("limit must be between 1 and 5000")
    return n


# ---------- API handlers: (conn, query string, path parts) -> JSON value ----------

def api_status(conn, qs, parts, state) -> dict[str, Any]:
    pol = policy.active() or {}
    return {
        "version": __version__,
        "mode": config.load().get("mode"),
        "disabled": paths.disabled_flag().exists(),
        "policy_version": pol.get("version"),
        "last_ingest": state.get("last_ingest"),
        "last_ingest_error": state.get("last_ingest_error"),
        "last_mod_request": conn.execute("SELECT MAX(ts) FROM requests WHERE source='mod'").fetchone()[0],
    }


def api_models(conn, qs, parts, state) -> list[dict[str, Any]]:
    seen = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT model, COUNT(*), MAX(ts) FROM requests GROUP BY model")}
    out = []
    for m in registry.MODELS:
        inp, w5, w1, rd, outp, thr, mult = registry.PRICES[m.model_id]
        n, last = seen.get(m.model_id, (0, None))
        out.append({
            "model_id": m.model_id, "alias": m.alias, "tier": m.tier, "context_window": m.context_window,
            "default_effort": m.default_effort, "auto": m.auto, "requests": n, "last_used": last,
            "price": {"input": inp, "cache_write_5m": w5, "cache_write_1h": w1, "cache_read": rd, "output": outp,
                      "long_ctx_threshold": thr, "long_ctx_multiplier": mult},
        })
    return out


def api_config(conn, qs, parts, state) -> dict[str, Any]:
    cfg = config.load()
    cfg.pop("test_patterns", None)
    cfg.pop("build_patterns", None)
    return cfg


def api_session(conn, qs, parts, state) -> list[dict[str, Any]]:
    if len(parts) != 1 or not parts[0]:
        raise BadRequest("session id required")
    return queries.session_timeline(conn, parts[0])


ROUTES: dict[str, Callable[..., Any]] = {
    "status": api_status,
    "overview": lambda c, q, p, s: queries.overview(c, _days(q, 30)),
    "daily": lambda c, q, p, s: queries.daily(c, _days(q, 30)),
    "model-mix": lambda c, q, p, s: queries.model_mix(c, _days(q, 30)),
    "context": lambda c, q, p, s: queries.context_histogram(c, _days(q, 30)),
    "projects": lambda c, q, p, s: queries.projects(c, _days(q, 30)),
    "sessions": lambda c, q, p, s: (api_session(c, q, p, s) if p
                                    else queries.sessions(c, _days(q, 30), _limit(q, 200))),
    "routes": lambda c, q, p, s: queries.routes(c, _limit(q, 200)),
    "categories": lambda c, q, p, s: queries.categories(c, _days(q, 30)),
    "escalations": lambda c, q, p, s: queries.escalations(c, _limit(q, 100)),
    "quota": lambda c, q, p, s: queries.quota(c, _days(q, 7)),
    "models": api_models,
    "config": api_config,
}


# ---------- server ----------

class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = DEFAULT_PORT, token: str | None = None, db_path: Path | None = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token or secrets.token_urlsafe(24)
        self.db_path = db_path
        self.state: dict[str, Any] = {}
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def url(self, with_token: bool = True) -> str:
        base = f"http://127.0.0.1:{self.port}/"
        return f"{base}?token={self.token}" if with_token else base

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def ingest_once(self) -> None:
        try:
            conn = db.open_db(self.db_path)
            try:
                self.state["last_ingest"] = {**ingest.run(conn), "ts": db.now_iso()}
                self.state["last_ingest_error"] = None
            finally:
                conn.close()
        except Exception as exc:  # the dashboard keeps serving stale data rather than dying
            self.state["last_ingest_error"] = f"{type(exc).__name__}: {exc}"

    def start_ingest_loop(self, every_s: float = INGEST_EVERY_S) -> threading.Thread:
        def loop():
            while not self._stop.is_set():
                self.ingest_once()
                self._stop.wait(every_s)
        t = threading.Thread(target=loop, name="mm-ingest", daemon=True)
        t.start()
        return t

    def server_close(self) -> None:
        self._stop.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "model-manager"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet; the CLI prints the URL only
        pass

    # ----- helpers -----

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**_SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, separators=(",", ":")).encode(), "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, msg: str) -> None:
        self._json(status, {"error": msg})

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def _authorized(self) -> bool:
        given = self.headers.get("X-MM-Token") or self._cookie_token() or ""
        return secrets.compare_digest(given, self.server.token)

    # ----- verbs -----

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if self.headers.get("Host") not in self.server.allowed_hosts():
            return self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
        url = urlsplit(self.path)
        qs = parse_qs(url.query)
        given = (qs.get("token") or [None])[0]
        if given is not None:
            if not secrets.compare_digest(given, self.server.token):
                return self._error(HTTPStatus.FORBIDDEN, "wrong token")
            # swap the one-time URL token for a cookie and drop it from the address bar
            cookie = f"{COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
            return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": url.path or "/",
                                                                         "Set-Cookie": cookie})
        if not self._authorized():
            return self._error(HTTPStatus.FORBIDDEN, "open the dashboard with the URL printed by 'model-manager dashboard'")
        if url.path.startswith("/api/"):
            return self._api(url.path[len("/api/"):], qs)
        return self._static(url.path)

    def do_POST(self) -> None:
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "read-only")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _api(self, path: str, qs: dict[str, list[str]]) -> None:
        name, *parts = path.split("/")
        fn = ROUTES.get(name)
        if fn is None:
            return self._error(HTTPStatus.NOT_FOUND, f"unknown endpoint: {name}")
        try:
            conn = self.server.connect()
            try:
                value = fn(conn, qs, parts, self.server.state)
            finally:
                conn.close()
        except BadRequest as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except sqlite3.Error as exc:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, f"database: {exc}")
        self._json(HTTPStatus.OK, value)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        root = STATIC_DIR.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)


def serve(port: int = DEFAULT_PORT, open_browser: bool = False, auto_ingest: bool = True) -> int:
    srv = DashboardServer(port)
    if auto_ingest:
        srv.start_ingest_loop()
    print(f"AI Model Manager dashboard: {srv.url()}", flush=True)
    print("Local only (127.0.0.1). Ctrl+C stops it.", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(srv.url())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0

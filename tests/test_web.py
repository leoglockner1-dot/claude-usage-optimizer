import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MM_HOME"] = str(Path(self.tmp.name) / "mm")
        os.environ["MM_CLAUDE_PROJECTS"] = str(Path(self.tmp.name) / "projects")
        from modelmgr import cli, db, ingest, web
        cli.main(["init"])
        conn = db.open_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with conn:
            ingest.apply_event(conn, {"type": "session", "session_id": "s1", "ts": now, "cwd": "/work/alpha"})
            ingest.apply_event(conn, {"type": "request", "request_key": "k1", "session_id": "s1", "turn_id": "u1",
                                      "ts": now, "model": "claude-opus-5-5",
                                      "usage": {"input_tokens": 1000, "output_tokens": 100}})
        conn.close()
        self.srv = web.DashboardServer(port=0, token="t0ken")
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def get(self, path, headers=None, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        h = {"Host": self.host, **(headers or {})}
        c.request(method, path, headers=h)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def api(self, path):
        r, body = self.get(f"/api/{path}", {"X-MM-Token": "t0ken"})
        return r.status, json.loads(body)

    def test_requires_token(self):
        r, _ = self.get("/api/overview")
        self.assertEqual(r.status, 403)
        r, _ = self.get("/api/overview", {"X-MM-Token": "wrong"})
        self.assertEqual(r.status, 403)
        r, _ = self.get("/?token=wrong")
        self.assertEqual(r.status, 403)

    def test_token_url_becomes_cookie(self):
        r, _ = self.get("/?token=t0ken")
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), "/")
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        r, body = self.get("/", {"Cookie": cookie.split(";")[0]})
        self.assertEqual(r.status, 200)
        self.assertIn(b"AI Model Manager", body)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))

    def test_rejects_foreign_host(self):
        r, _ = self.get("/api/overview", {"Host": "evil.example:80", "X-MM-Token": "t0ken"})
        self.assertEqual(r.status, 403)

    def test_api_endpoints(self):
        status, o = self.api("overview?days=7")
        self.assertEqual(status, 200)
        self.assertEqual(o["requests"], 1)
        self.assertAlmostEqual(o["est_cost_usd"], (1000 * 4 + 100 * 20) / 1e6)
        for name in ("status", "daily", "model-mix", "context", "context-daily", "projects", "sessions", "routes", "categories",
                     "escalations", "quota", "models", "config", "overview?days=all"):
            status, _ = self.api(name)
            self.assertEqual(status, 200, name)
        status, tl = self.api("sessions/s1")
        self.assertEqual(len(tl), 1)
        status, p = self.api("projects")
        self.assertEqual(p[0]["project"], "alpha")
        status, cfg = self.api("config")
        self.assertNotIn("test_patterns", cfg)

    def test_bad_requests(self):
        self.assertEqual(self.api("overview?days=abc")[0], 400)
        self.assertEqual(self.api("overview?days=0")[0], 400)
        self.assertEqual(self.api("routes?limit=99999")[0], 400)
        self.assertEqual(self.api("nope")[0], 404)
        r, _ = self.get("/api/config", {"X-MM-Token": "t0ken"}, method="POST")
        self.assertEqual(r.status, 405)

    def test_static_cannot_escape(self):
        for path in ("/../web.py", "/%2e%2e/web.py", "/missing.css"):
            r, _ = self.get(path, {"X-MM-Token": "t0ken"})
            self.assertEqual(r.status, 404, path)
        r, _ = self.get("/app.js", {"X-MM-Token": "t0ken"})
        self.assertEqual(r.status, 200)

    def test_ingest_loop_records_state(self):
        from modelmgr import paths
        self.srv.ingest_once()
        self.assertIsNone(self.srv.state["last_ingest_error"])
        self.assertIn("ts", self.srv.state["last_ingest"])
        self.assertTrue(paths.summary_path().exists())
        status, s = self.api("status")
        self.assertEqual(s["mode"], "observe")
        self.assertIsNotNone(s["last_ingest"])


if __name__ == "__main__":
    unittest.main()

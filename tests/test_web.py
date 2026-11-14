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

    def post(self, path, value, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        h = {"Host": self.host, "Origin": f"http://{self.host}", "X-MM-Token": "t0ken",
             "Content-Type": "application/json", **(headers or {})}
        c.request("POST", f"/api/{path}", body=body, headers={k: v for k, v in h.items() if v is not None})
        r = c.getresponse()
        out = json.loads(r.read() or b"null")
        c.close()
        return r.status, out

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
        self.assertIn(b"Usage Optimizer", body)
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
        r, _ = self.get("/api/config", {"X-MM-Token": "t0ken"}, method="PUT")
        self.assertEqual(r.status, 405)

    def test_settings_write_config(self):
        from modelmgr import config
        status, cfg = self.post("config", {"mode": "auto", "quota_guard.five_hour": 70, "auto_models": ["haiku", "sonnet"]})
        self.assertEqual(status, 200, cfg)
        self.assertEqual((cfg["mode"], cfg["quota_guard"]["five_hour"], cfg["auto_models"]), ("auto", 70, ["haiku", "sonnet"]))
        self.assertNotIn("test_patterns", cfg)
        saved = config.load()
        self.assertEqual(saved["mode"], "auto")
        self.assertEqual(saved["quota_guard"]["seven_day"], 90)  # siblings of a dotted key stay
        self.assertTrue(saved["test_patterns"])  # keys the page does not show stay too

    def test_settings_reject_bad_values_and_change_nothing(self):
        from modelmgr import config
        before = config.load()
        for patch, msg in [
            ({"mode": "turbo"}, "mode must be one of"),
            ({"auto_models": ["fable"]}, "cannot be routed automatically"),
            ({"auto_models": []}, "at least one model"),
            ({"ceiling": "sonnet"}, "above the ceiling"),
            ({"quota_guard.five_hour": 101}, "from 0 to 100"),
            ({"escalation.enabled": "yes"}, "true or false"),
            ({"test_patterns": ["x"]}, "not editable here"),
            ([], "JSON object"),
        ]:
            status, out = self.post("config", patch)
            self.assertEqual(status, 400, patch)
            self.assertIn(msg, out["error"], patch)
        self.assertEqual(config.load(), before)

    def test_writes_need_origin_json_and_token(self):
        ok = {"mode": "auto"}
        self.assertEqual(self.post("config", ok, {"Origin": None})[0], 403)
        self.assertEqual(self.post("config", ok, {"Origin": "http://evil.test"})[0], 403)
        self.assertEqual(self.post("config", ok, {"X-MM-Token": "wrong"})[0], 403)
        self.assertEqual(self.post("config", ok, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post("config", b"{nope")[0], 400)
        self.assertEqual(self.post("config", b"x" * 20000)[0], 400)
        self.assertEqual(self.post("overview", ok)[0], 405)
        from modelmgr import config
        self.assertEqual(config.load()["mode"], "observe")

    def test_grade_a_routing_decision(self):
        from modelmgr import db
        conn = db.open_db()
        with conn:
            conn.execute("INSERT INTO routes(route_id, task_id, turn_id, ts, chosen_model, chosen_effort, applied)"
                         " VALUES (7, 't1', 'u1', '2026-11-09T10:00:00+00:00', 'sonnet', 'medium', 1)")
            conn.execute("INSERT INTO tasks(task_id, category, complexity) VALUES ('t1', 'visual_design', 3)")
        conn.close()
        status, out = self.post("grade", {"route_id": 7, "verdict": "too_weak", "better": "opus:medium", "note": "generic"})
        self.assertEqual(status, 200, out)
        _, routes = self.api("routes")
        r = next(x for x in routes if x["route_id"] == 7)
        self.assertEqual((r["verdict"], r["better"], r["grade_note"]), ("too_weak", "opus:medium", "generic"))
        _, grades = self.api("grades")
        self.assertEqual(grades, [{"category": "visual_design", "chosen_model": "sonnet", "verdict": "too_weak", "n": 1}])
        self.assertEqual(self.post("grade", {"route_id": 7, "verdict": "right"})[0], 200)
        _, grades = self.api("grades")
        self.assertEqual([g["verdict"] for g in grades], ["right"])
        self.assertEqual(self.post("grade", {"route_id": 7, "verdict": None})[0], 200)
        self.assertEqual(self.api("grades")[1], [])
        for bad in ({"route_id": 99, "verdict": "right"}, {"route_id": 7, "verdict": "meh"},
                    {"route_id": 7, "verdict": "right", "better": "gpt:high"}, {"route_id": "7", "verdict": "right"},
                    {"route_id": 7, "verdict": "right", "note": "x" * 501}):
            self.assertEqual(self.post("grade", bad)[0], 400, bad)
        self.assertEqual(self.post("grade", {"route_id": 7, "verdict": "right"}, {"Origin": None})[0], 403)

    def test_optimizer_endpoints(self):
        from modelmgr import db, optimizers
        from modelmgr.optimizers import Finding
        st, home = self.api("home")
        self.assertEqual(st, 200)
        self.assertIsNone(home["health"]["score"])
        self.assertEqual(self.api("tools")[0], 200)
        conn = db.open_db()
        optimizers.run_all(conn, {"output": [lambda c: [Finding(tool="output", rule="verbose", title="Long replies",
                                                                evidence={"n": 3}, fix="answer first")]]})
        conn.close()
        st, fs = self.api("findings")
        self.assertEqual([f["id"] for f in fs], ["output:verbose"])
        st, out = self.post("finding-status", {"id": "output:verbose", "status": "applied"})
        self.assertEqual((st, out["status"]), (200, "applied"))
        self.assertEqual(self.post("finding-status", {"id": "output:verbose", "status": "done"})[0], 400)
        self.assertEqual(self.post("finding-status", {"id": "nope", "status": "seen"})[0], 400)
        self.assertEqual(self.post("finding-status", {"id": "output:verbose", "status": "seen"}, {"Origin": None})[0], 403)
        for f in ("optimizer.js", "theme.js"):
            r, _ = self.get(f"/{f}", {"X-MM-Token": "t0ken"})
            self.assertEqual(r.status, 200)

    def test_policy_page_and_rollback(self):
        from modelmgr import policy
        status, pol = self.api("policy")
        self.assertEqual(status, 200)
        self.assertEqual(pol["policy_version"], 0)
        self.assertEqual([v["version"] for v in pol["versions"]], [0])
        self.assertTrue(pol["versions"][0]["active"])
        self.assertIn("visual_design", pol["floors"])
        self.assertEqual(pol["thresholds"]["min_n"], 8)
        self.assertTrue(all(d["status"] == "keep" for d in pol["decisions"]))
        v1 = dict(policy.active(), version=1, source="learned", changes=[])
        policy.activate(v1)
        for bad in ({"version": "0"}, {"version": True}, {"version": 5}, {"version": -1}, {}, {"version": 0.5}):
            self.assertEqual(self.post("rollback", bad)[0], 400, bad)
        self.assertEqual(policy.active()["version"], 1)
        status, out = self.post("rollback", {"version": 0})
        self.assertEqual(status, 200, out)
        self.assertEqual(policy.active()["version"], 0)
        self.assertEqual(self.post("rollback", {"version": 0}, {"Origin": None})[0], 403)

    def test_kill_switch(self):
        from modelmgr import paths
        self.assertEqual(self.post("killswitch", {"disabled": True}), (200, {"disabled": True}))
        self.assertTrue(paths.disabled_flag().exists())
        self.assertTrue(self.api("status")[1]["disabled"])
        self.assertEqual(self.post("killswitch", {"disabled": False}), (200, {"disabled": False}))
        self.assertFalse(paths.disabled_flag().exists())
        self.assertEqual(self.post("killswitch", {"disabled": "no"})[0], 400)

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

    def test_handle_file_for_the_pane(self):
        from modelmgr import paths, web
        path = web.write_handle(self.srv)
        self.assertEqual(path, paths.dashboard_path())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        h = json.loads(path.read_text())
        self.assertEqual(h["url"], self.srv.url())
        self.assertIn("t0ken", h["url"])
        self.assertEqual(h["pid"], os.getpid())
        h["pid"] = -1  # another dashboard took over: its file stays
        path.write_text(json.dumps(h))
        web.remove_handle()
        self.assertTrue(path.exists())
        web.write_handle(self.srv)
        web.remove_handle()
        self.assertFalse(path.exists())

    def test_hangup_removes_the_handle(self):
        # a closed terminal sends SIGHUP: the server must still clean up dashboard.json
        import signal
        import subprocess
        import sys
        import time
        from modelmgr import paths
        src = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PYTHONPATH": src}
        proc = subprocess.Popen([sys.executable, "-c",
                                 "from modelmgr import web; web.serve(port=0, auto_ingest=False)"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30  # CI runners can take many seconds to start Python
            while time.time() < deadline and not paths.dashboard_path().exists():
                time.sleep(0.05)
            self.assertTrue(paths.dashboard_path().exists())
            proc.send_signal(signal.SIGHUP)
            proc.wait(timeout=5)
            self.assertFalse(paths.dashboard_path().exists())
        finally:
            proc.kill()

    def test_port_in_use_is_a_message(self):
        import contextlib
        import io
        from modelmgr import web
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(web.serve(port=self.srv.port, auto_ingest=False), 1)
        self.assertIn("in use", err.getvalue())


if __name__ == "__main__":
    unittest.main()

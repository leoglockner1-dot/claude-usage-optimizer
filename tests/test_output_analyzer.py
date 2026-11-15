import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class OutputAnalyzerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        os.environ["MM_HOME"] = str(root / "mm")
        os.environ["MM_CLAUDE_PROJECTS"] = str(root / "projects")
        os.environ["MM_CLAUDE_HOME"] = str(root / "claude")
        (root / "claude").mkdir()
        from modelmgr import cli, db
        cli.main(["init"])
        self.conn = db.open_db()
        self.claude = root / "claude"
        self.n = 0
        self.t0 = datetime.now(timezone.utc) - timedelta(days=2)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("MM_CLAUDE_HOME", None)
        self.tmp.cleanup()

    def request(self, minutes, out=20_000, think=None, model="claude-opus-5-5", days_ago=None):
        from modelmgr import registry
        self.n += 1
        t = self.t0 + timedelta(minutes=minutes) if days_ago is None else datetime.now(timezone.utc) - timedelta(days=days_ago)
        ts = t.isoformat(timespec="seconds").replace("+00:00", "Z")
        cost = registry.estimate_cost(model, 1000, out, 0, 0, 0)
        with self.conn:
            self.conn.execute(
                """INSERT INTO requests (request_key, session_id, ts, model, input_tokens, output_tokens,
                   thinking_tokens, cache_write_5m, cache_write_1h, cache_read, est_cost_usd)
                   VALUES (?,?,?,?,?,?,?,0,0,0,?)""",
                (f"k{self.n}", "s1", ts, model, 1000, out, think, cost))

    def run_output(self):
        from modelmgr.optimizers import run_all
        from modelmgr.optimizers.output import output_findings
        run_all(self.conn, {"output": [output_findings]})
        return {r["id"]: r for r in self.conn.execute("SELECT * FROM findings WHERE active = 1")}

    def test_clean_db_has_no_findings(self):
        self.assertEqual(self.run_output(), {})

    def test_small_output_has_no_findings(self):
        for i in range(20):
            self.request(i, out=50)
        self.assertEqual(self.run_output(), {})

    def test_high_output_found(self):
        for i in range(100):
            self.request(i)
        found = self.run_output()
        f = found["output:high-output"]
        self.assertGreater(f["est_saving_usd"], 5)
        self.assertIn("output tokens", json.loads(f["evidence_json"])["summary"])
        self.assertIn("output:recommend:caveman", found)

    def test_active_tool_not_recommended(self):
        (self.claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"caveman@caveman": True}}))
        for i in range(100):
            self.request(i)
        found = self.run_output()
        self.assertNotIn("output:recommend:caveman", found)
        self.assertNotIn("output:recommend:reply-rule", found)

    def test_installed_but_off_says_enable(self):
        (self.claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"caveman@caveman": False}}))
        (self.claude / "plugins" / "cache" / "caveman").mkdir(parents=True)
        for i in range(100):
            self.request(i)
        self.assertTrue(self.run_output()["output:recommend:caveman"]["title"].startswith("Enable"))

    def test_thinking_heavy(self):
        for i in range(100):
            self.request(i, out=20_000, think=15_000)
        self.assertIn("output:thinking-heavy", self.run_output())

    def test_measures_change_after_applied(self):
        from modelmgr.optimizers import base
        for i in range(100):
            self.request(0, out=20_000, days_ago=14 - i * 0.05)   # before: days 14..9
        self.run_output()
        fid = "output:recommend:caveman"
        base.set_status(self.conn, fid, "applied")
        applied = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.conn:
            self.conn.execute("UPDATE findings SET status_ts = ? WHERE id = ?", (applied, fid))
        for i in range(100):
            self.request(0, out=5_000, days_ago=7.9 - i * 0.05)   # after
        ev = json.loads(self.run_output()[fid]["evidence_json"])
        self.assertLess(ev["measured"]["change"], -0.5)
        self.assertIn("measured", ev["summary"])


if __name__ == "__main__":
    unittest.main()

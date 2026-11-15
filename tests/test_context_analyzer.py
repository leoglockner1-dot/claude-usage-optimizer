import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class ContextAnalyzerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MM_HOME"] = str(Path(self.tmp.name) / "mm")
        os.environ["MM_CLAUDE_PROJECTS"] = str(Path(self.tmp.name) / "projects")
        from modelmgr import cli, db
        cli.main(["init"])
        self.conn = db.open_db()
        self.n = 0
        self.t0 = datetime.now(timezone.utc) - timedelta(days=2)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def session(self, sid, project="alpha"):
        with self.conn:
            self.conn.execute("INSERT INTO sessions (session_id, cwd, project) VALUES (?,?,?)",
                              (sid, f"/work/{project}", project))

    def request(self, sid, minutes, read=0, write5=0, write1=0, inp=1000, out=500, model="claude-opus-5-5"):
        from modelmgr import registry
        self.n += 1
        ts = (self.t0 + timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
        cost = registry.estimate_cost(model, inp, out, write5, write1, read)
        with self.conn:
            self.conn.execute(
                """INSERT INTO requests (request_key, session_id, ts, model, input_tokens, output_tokens,
                   cache_write_5m, cache_write_1h, cache_read, est_cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (f"k{self.n}", sid, ts, model, inp, out, write5, write1, read, cost))

    def run_context(self):
        from modelmgr.optimizers import run_all
        from modelmgr.optimizers.context import context_findings
        run_all(self.conn, {"context": [context_findings]})
        return {r["id"]: r for r in self.conn.execute("SELECT * FROM findings WHERE active = 1")}

    def test_clean_db_has_no_findings(self):
        self.assertEqual(self.run_context(), {})

    def test_small_contexts_have_no_findings(self):
        self.session("s1")
        for i in range(20):
            self.request("s1", i, read=50_000)
        self.assertEqual(self.run_context(), {})

    def test_long_context_found_with_evidence(self):
        self.session("s1")
        for i in range(120):
            self.request("s1", i, read=900_000)
        f = self.run_context()["context:long-context:alpha"]
        self.assertEqual(f["severity"], "high")
        self.assertGreater(f["est_saving_usd"], 5)
        self.assertIn("s1", f["evidence_json"])

    def test_cache_gap_uses_one_hour_lifetime(self):
        self.session("s1")
        # 6 pauses of 30 minutes: inside the 1 h cache lifetime, so not a miss
        for i in range(7):
            self.request("s1", i * 30, write1=300_000, read=0)
        self.assertNotIn("context:cache-gap", self.run_context())
        # same pauses with 5 minute writes are misses
        self.conn.execute("DELETE FROM requests")
        for i in range(8):
            self.request("s1", i * 30, write5=2_000_000)
        self.assertIn("context:cache-gap", self.run_context())


if __name__ == "__main__":
    unittest.main()

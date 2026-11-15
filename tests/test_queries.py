import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def ts(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def usage(inp=1000, out=500, read=0, write=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write}


class QueriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MM_HOME"] = str(Path(self.tmp.name) / "mm")
        os.environ["MM_CLAUDE_PROJECTS"] = str(Path(self.tmp.name) / "projects")
        from modelmgr import cli, db, queries
        cli.main(["init"])
        self.q = queries
        self.conn = db.open_db()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _events(self, events):
        from modelmgr import ingest
        with self.conn:
            for ev in events:
                ingest.apply_event(self.conn, ev)

    def _req(self, key, model="claude-opus-5-5", hours_ago=1, session="s1", turn=None, **u):
        return {"type": "request", "request_key": key, "session_id": session, "turn_id": turn or key,
                "ts": ts(hours_ago), "model": model, "usage": usage(**u)}

    def test_empty_db_reports_not_measured(self):
        o = self.q.overview(self.conn, 30)
        self.assertEqual(o["requests"], 0)
        self.assertIsNone(o["est_cost_usd"])
        self.assertIsNone(o["median_context"])
        self.assertIsNone(o["savings"]["est_savings_usd"])
        self.assertTrue(all(b["requests"] == 0 for b in self.q.context_histogram(self.conn, 30)))
        self.assertEqual(self.q.daily(self.conn, 30), [])

    def test_cost_split_matches_estimate(self):
        from modelmgr import registry
        row = {"model": "claude-haiku-5-5", "input_tokens": 50_000, "output_tokens": 1000,
               "cache_write_5m": 0, "cache_write_1h": 2000, "cache_read": 60_000}
        split = self.q.cost_by_kind(row)
        est = registry.estimate_cost("claude-haiku-5-5", 50_000, 1000, 0, 2000, 60_000)
        self.assertAlmostEqual(sum(split.values()), est)  # long-context 5x applied to every kind
        self.assertEqual(self.q.context_tokens(row), 112_000)
        self.assertIsNone(self.q.cost_by_kind({**row, "model": "unknown-x"}))

    def test_overview_totals_and_unpriced(self):
        self._events([
            {"type": "session", "session_id": "s1", "ts": ts(2), "cwd": "/work/alpha"},
            self._req("a", inp=1000, out=500, read=10_000),
            self._req("b", inp=3000, out=100, read=20_000),
            self._req("c", model="gateway/mystery", inp=10, out=10),
        ])
        o = self.q.overview(self.conn, 30)
        self.assertEqual(o["requests"], 3)
        self.assertEqual(o["sessions"], 1)
        self.assertEqual(o["unpriced_requests"], 1)
        expected = (1000 * 4 + 500 * 20 + 10_000 * 0.2 + 3000 * 4 + 100 * 20 + 20_000 * 0.2) / 1e6
        self.assertAlmostEqual(o["est_cost_usd"], expected, places=6)
        self.assertAlmostEqual(sum(o["cost_by_kind"].values()), expected, places=6)
        self.assertEqual(o["tokens"]["cache_read"], 30_000)
        self.assertEqual(o["median_context"], 11_000)  # medians of 11_000, 23_000, 10 → 11_000
        self.assertIsNone(o["savings"]["routed_requests"] or None)

    def test_savings_only_for_applied_routes(self):
        self._events([
            {"type": "route", "task_id": "t1", "turn_id": "u1", "mode": "auto", "chosen_model": "haiku",
             "chosen_effort": "medium", "applied": True, "reason": "prior"},
            {"type": "route", "task_id": "t1", "turn_id": "u2", "mode": "observe", "chosen_model": "haiku",
             "chosen_effort": "medium", "applied": False, "reason": "observe"},
            self._req("r1", model="claude-haiku-5-5", turn="u1", inp=1000, out=1000),
            self._req("r2", model="claude-opus-5-5", turn="u2", inp=1000, out=1000),
        ])
        s = self.q.overview(self.conn, 30)["savings"]
        self.assertEqual(s["routed_requests"], 1)
        self.assertAlmostEqual(s["baseline_cost_usd"], (1000 * 4 + 1000 * 20) / 1e6)
        self.assertAlmostEqual(s["est_cost_usd"], (1000 * 0.1 + 1000 * 0.5) / 1e6)
        self.assertAlmostEqual(s["est_savings_usd"], s["baseline_cost_usd"] - s["est_cost_usd"])

    def test_context_buckets_and_period(self):
        self._events([
            self._req("small", inp=10_000),
            self._req("edge", inp=200_000),  # boundary belongs to the upper bucket
            self._req("huge", inp=500_000),
            self._req("old", inp=10_000, hours_ago=24 * 40),
        ])
        h = {b["label"]: b for b in self.q.context_histogram(self.conn, 30)}
        self.assertEqual(h["<50K"]["requests"], 1)
        self.assertEqual(h["200–400K"]["requests"], 1)
        self.assertEqual(h[">400K"]["requests"], 1)
        self.assertAlmostEqual(sum(b["cost_share"] for b in h.values()), 1.0, places=3)
        self.assertEqual(sum(b["requests"] for b in self.q.context_histogram(self.conn, None)), 4)

    def test_context_daily(self):
        self._events([
            self._req("a", inp=100_000),
            self._req("b", inp=300_000),
            self._req("c", inp=500_000),
        ])
        d = self.q.context_daily(self.conn, 30)
        self.assertEqual(sum(x["requests"] for x in d), 3)
        if len(d) == 1:  # all three on the same UTC day
            self.assertEqual(d[0]["median_context"], 300_000)
            self.assertEqual(d[0]["p90_context"], 500_000)
            self.assertGreater(d[0]["over_200k_cost_share"], 0.5)

    def test_daily_and_model_mix(self):
        self._events([
            self._req("a", model="claude-opus-5-5"),
            self._req("b", model="claude-sonnet-5-5"),
            self._req("c", model="claude-sonnet-5-5", hours_ago=48),
        ])
        d = self.q.daily(self.conn, 30)
        self.assertEqual(sum(x["requests"] for x in d), 3)
        self.assertTrue(all(set(x["by_model"]) <= {"claude-opus-5-5", "claude-sonnet-5-5"} for x in d))
        mix = {m["model"]: m for m in self.q.model_mix(self.conn, 30)}
        self.assertEqual(mix["claude-sonnet-5-5"]["requests"], 2)
        self.assertAlmostEqual(sum(m["cost_share"] for m in mix.values()), 1.0, places=3)

    def test_project_names_and_grouping(self):
        self.assertEqual(self.q.project_name("/Users/x/Code/alpha"), "alpha")
        self.assertEqual(self.q.project_name(None), "(unknown)")
        self.assertEqual(self.q.project_name("/x/Claude/scratch-workspaces/a/b"), "(scratch)")
        self.assertEqual(self.q.project_name("/private/tmp/claude-1/s"), "(temp)")
        self.assertEqual(self.q.project_name("/a/b", "named"), "named")
        self._events([
            {"type": "session", "session_id": "s1", "ts": ts(3), "cwd": "/work/alpha"},
            {"type": "session", "session_id": "s2", "ts": ts(3), "cwd": "/other/alpha"},
            {"type": "session", "session_id": "s3", "ts": ts(3), "cwd": "/work/beta"},
            self._req("a", session="s1"), self._req("b", session="s2"), self._req("c", session="s3"),
        ])
        p = {x["project"]: x for x in self.q.projects(self.conn, 30)}
        self.assertEqual(p["alpha"]["sessions"], 2)
        self.assertEqual(p["beta"]["requests"], 1)
        tl = self.q.session_timeline(self.conn, "s1")
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0]["context"], 1000)

    def test_ingest_writes_summary(self):
        from modelmgr import ingest, paths
        f = paths.spool_dir() / f"{time.time_ns()}-{os.urandom(4).hex()}.json"
        f.write_text(json.dumps({"v": 1, "events": [self._req("x", hours_ago=0.1)]}))
        old = time.time() - 10
        os.utime(f, (old, old))
        ingest.run()
        s = json.loads(paths.summary_path().read_text())
        self.assertEqual(s["v"], 1)
        self.assertEqual(s["mode"], "observe")
        self.assertEqual(s["today"]["requests"], 1)
        self.assertEqual(len(s["daily"]), 7)
        self.assertIsNone(s["quota"])
        self.assertIsNone(s["last_route"])
        self.assertEqual(list(paths.home().glob(".summary-*")), [])  # no temp files left behind


if __name__ == "__main__":
    unittest.main()

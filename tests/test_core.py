import json
import os
import tempfile
import time
import unittest
from pathlib import Path


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MM_HOME"] = str(Path(self.tmp.name) / "mm")
        os.environ["MM_CLAUDE_PROJECTS"] = str(Path(self.tmp.name) / "projects")
        from modelmgr import cli
        self.cli = cli
        cli.main(["init"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_cost_estimate(self):
        from modelmgr.registry import estimate_cost
        # Opus 5.5: 1M input = $4, 1M output = $20
        self.assertAlmostEqual(estimate_cost("claude-opus-5-5", 1_000_000, 0), 4.0)
        self.assertAlmostEqual(estimate_cost("opus", 0, 1_000_000), 20.0)
        # Haiku long context: 5x once the prompt (incl. cache) exceeds 100K
        short = estimate_cost("claude-haiku-5-5", 50_000, 1000)
        long_ = estimate_cost("claude-haiku-5-5", 50_000, 1000, cache_read=60_000)
        self.assertGreater(long_, short * 4)
        self.assertIsNone(estimate_cost("unknown-model", 10, 10))
        self.assertIsNone(estimate_cost("opus", None, 10))

    def test_normalize(self):
        from modelmgr.registry import normalize_model, tier_of
        self.assertEqual(normalize_model("claude-sonnet-5-5[1m]"), "claude-sonnet-5-5")
        self.assertEqual(normalize_model("sonnet"), "claude-sonnet-5-5")
        self.assertEqual(tier_of("claude-opus-5-5"), 3)

    def test_init_creates_policy_and_config(self):
        from modelmgr import config, paths, policy
        self.assertTrue(paths.db_path().exists())
        self.assertEqual(config.load()["mode"], "observe")
        self.assertEqual(policy.active()["version"], 0)
        self.assertIn("luau", policy.active()["table"])

    def _spool(self, events, age=10):
        from modelmgr import paths
        f = paths.spool_dir() / f"{time.time_ns()}.json"
        f.write_text(json.dumps({"v": 1, "events": events}))
        old = time.time() - age
        os.utime(f, (old, old))
        return f

    def test_ingest_full_task(self):
        from modelmgr import db, ingest
        self._spool([
            {"type": "session", "session_id": "s1", "ts": "2026-11-04T10:00:00Z", "cwd": "/w"},
            {"type": "task_open", "task_id": "t1", "session_id": "s1", "ts": "2026-11-04T10:00:00Z",
             "category": "luau", "complexity": 2, "risk": "low", "flags": [], "prompt_excerpt": "fix"},
            {"type": "route", "task_id": "t1", "turn_id": "u1", "mode": "auto", "chosen_model": "haiku",
             "chosen_effort": "medium", "applied": True, "reason": "prior"},
            {"type": "request", "request_key": "u1:main:0", "task_id": "t1", "session_id": "s1", "turn_id": "u1",
             "model": "claude-haiku-5-5", "usage": {"input_tokens": 1000, "output_tokens": 500,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
            {"type": "turn", "turn_id": "u1", "task_id": "t1", "duration_ms": 1200, "tests_run": 1, "tests_passed": 1},
            {"type": "task_close", "task_id": "t1", "outcome": "verified_success", "outcome_source": "tests"},
            {"type": "future_event_kind", "x": 1},
        ])
        fresh = self._spool([{"type": "feedback", "task_id": "t1", "kind": "good"}], age=0)
        stats = ingest.run()
        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["skipped"], 1)  # the file written "just now" waits
        self.assertTrue(fresh.exists())
        conn = db.open_db()
        t = conn.execute("SELECT * FROM tasks WHERE task_id='t1'").fetchone()
        self.assertEqual(t["outcome"], "verified_success")
        self.assertEqual(t["turns"], 1)
        r = conn.execute("SELECT est_cost_usd FROM requests").fetchone()
        self.assertAlmostEqual(r[0], (1000 * 0.10 + 500 * 0.50) / 1e6)

    def test_partial_file_is_retried_not_lost(self):
        from modelmgr import ingest, paths
        f = paths.spool_dir() / "partial.json"
        f.write_text('{"v":1,"events":[{"type":"ses')
        old = time.time() - 10
        os.utime(f, (old, old))
        stats = ingest.run()
        self.assertEqual(stats["skipped"], 1)
        self.assertTrue(f.exists())

    def test_backfill_dedupes_request_ids(self):
        from modelmgr import backfill, db
        proj = Path(os.environ["MM_CLAUDE_PROJECTS"]) / "-w"
        proj.mkdir(parents=True)
        usage = {"input_tokens": 2, "output_tokens": 100, "cache_read_input_tokens": 5000,
                 "cache_creation_input_tokens": 1000, "cache_creation": {"ephemeral_1h_input_tokens": 1000}}
        lines = [
            {"type": "user", "sessionId": "sx", "cwd": "/w", "version": "2.1.309", "timestamp": "2026-11-01T00:00:00Z"},
            *[{"type": "assistant", "sessionId": "sx", "requestId": "req_1", "timestamp": "2026-11-01T00:00:01Z",
               "message": {"model": "claude-opus-5-5", "usage": usage}} for _ in range(3)],
            {"type": "assistant", "sessionId": "sx", "requestId": "req_2", "timestamp": "2026-11-01T00:00:02Z",
             "message": {"model": "<synthetic>", "usage": usage}},
        ]
        (proj / "sx.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
        s = backfill.run()
        self.assertEqual(s["requests"], 1)
        again = backfill.run()
        self.assertEqual(again["unchanged"], 1)
        conn = db.open_db()
        row = conn.execute("SELECT * FROM requests").fetchone()
        self.assertEqual(row["cache_write_1h"], 1000)
        self.assertEqual(row["source"], "transcript")

    def test_kill_switch_and_mode(self):
        from modelmgr import config, paths
        self.cli.main(["off"])
        self.assertTrue(paths.disabled_flag().exists())
        self.cli.main(["on"])
        self.assertFalse(paths.disabled_flag().exists())
        self.cli.main(["mode", "auto"])
        self.assertEqual(config.load()["mode"], "auto")
        with self.assertRaises(ValueError):
            config.save({**config.load(), "mode": "turbo"})

    def test_rollback(self):
        from modelmgr import db, policy
        conn = db.open_db()
        p = policy.prior_policy()
        p["version"] = 1
        p["source"] = "learned"
        policy.activate(p, conn)
        self.assertEqual(policy.active()["version"], 1)
        policy.rollback(0, conn)
        self.assertEqual(policy.active()["version"], 0)


if __name__ == "__main__":
    unittest.main()

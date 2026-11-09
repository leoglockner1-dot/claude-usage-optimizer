import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def ts(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


MODEL_ID = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}


class BetaTest(unittest.TestCase):
    def test_beta_functions(self):
        from modelmgr import learn
        self.assertAlmostEqual(learn.beta_cdf(0.3, 2, 5), 0.57983, places=4)
        self.assertEqual(learn.beta_cdf(0, 2, 5), 0.0)
        self.assertEqual(learn.beta_cdf(1, 2, 5), 1.0)
        for q in (0.1, 0.5, 0.9):
            x = learn.beta_quantile(q, 3.2, 0.8)
            self.assertAlmostEqual(learn.beta_cdf(x, 3.2, 0.8), q, places=4)
        # prior only (p=0.8, strength 4): wide interval, far below the 0.7 target
        self.assertAlmostEqual(learn.lower_bound(3.2, 0.8), 0.535, places=2)
        # more data, same rate: narrower interval, higher lower bound
        self.assertGreater(learn.lower_bound(32, 8), learn.lower_bound(3.2, 0.8))

    def test_prior_steps_down_below_the_heuristic(self):
        from modelmgr import learn, policy
        self.assertEqual(learn.prior_p("code", "low", "sonnet", "medium"), policy.PRIOR_P)
        self.assertEqual(learn.prior_p("code", "low", "opus", "high"), policy.PRIOR_P)
        self.assertLess(learn.prior_p("code", "low", "haiku", "low"), policy.PRIOR_P)
        self.assertGreaterEqual(learn.prior_p("arch", "high", "haiku", "low"), 0.5)


class LearnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MM_HOME"] = str(Path(self.tmp.name) / "mm")
        os.environ["MM_CLAUDE_PROJECTS"] = str(Path(self.tmp.name) / "projects")
        from modelmgr import cli, db, learn, policy, config
        cli.main(["init"])
        self.learn, self.policy, self.config = learn, policy, config
        self.conn = db.open_db()
        self.n = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def cfg(self, **kw):
        c = self.config.load()
        c.update(kw)
        return c

    def task(self, cat, complexity, model, effort, outcome="verified_success", out_tokens=500, days_ago=1):
        from modelmgr import ingest
        self.n += 1
        tid = f"t{self.n}"
        with self.conn:
            ingest.apply_event(self.conn, {"type": "task_open", "task_id": tid, "session_id": "s1", "ts": ts(days_ago),
                                           "category": cat, "complexity": complexity})
            ingest.apply_event(self.conn, {"type": "request", "request_key": f"r{self.n}", "task_id": tid,
                                           "session_id": "s1", "turn_id": tid, "ts": ts(days_ago),
                                           "model": MODEL_ID[model], "effort": effort,
                                           "usage": {"input_tokens": 2000, "output_tokens": out_tokens}})
            ingest.apply_event(self.conn, {"type": "task_close", "task_id": tid, "ts": ts(days_ago), "outcome": outcome})
        return tid

    def decision(self, res, cat, band):
        return next(d for d in res["decisions"] if d["category"] == cat and d["band"] == band)

    def test_few_tasks_mean_not_enough_evidence(self):
        for _ in range(3):
            self.task("code", 1, "haiku", "low")
        res = self.learn.learn(self.conn, cfg=self.cfg(learning="auto"))
        d = self.decision(res, "code", "low")
        self.assertEqual(d["status"], "keep")
        self.assertIn("not enough evidence", d["reasons"])
        self.assertIsNone(res["written"])
        self.assertEqual(self.policy.active()["version"], 0)
        state = json.loads((Path(os.environ["MM_HOME"]) / "learn.json").read_text())
        self.assertEqual(state["closed_tasks"], 3)

    def test_unknown_outcome_and_old_model_ids_are_skipped(self):
        self.task("code", 1, "haiku", "low", outcome="unknown")
        cells = self.learn.collect(self.conn)
        self.assertEqual(cells, {})

    def test_thin_cell_borrows_the_pooled_rate(self):
        for _ in range(10):
            self.task("code", 3, "haiku", "low", outcome="failure")
        self.task("code", 1, "haiku", "low")
        cells = self.learn.collect(self.conn)
        self.learn.estimate(cells)
        thin = cells[("code", "low", "haiku", "low")]
        self.assertEqual(thin.pooled, "haiku in code")
        self.assertLess(thin.mean, 0.5)  # one success, but haiku fails code elsewhere

    def test_cheaper_model_with_enough_evidence_becomes_the_policy(self):
        for _ in range(12):
            self.task("code", 1, "haiku", "low", out_tokens=500)
        for _ in range(4):
            self.task("code", 1, "sonnet", "medium", out_tokens=5000)
        res = self.learn.learn(self.conn, cfg=self.cfg(learning="auto"))
        d = self.decision(res, "code", "low")
        self.assertEqual((d["status"], d["kind"], d["best"]), ("change", "cheaper", "haiku:low"), d)
        self.assertGreaterEqual(d["evidence"]["cost_gain"], 0.10)
        self.assertEqual(res["written"], 1)
        pol = self.policy.active()
        self.assertEqual(pol["version"], 1)
        self.assertEqual(pol["source"], "learned")
        self.assertEqual(pol["table"]["code"]["low"]["model"], "haiku")
        self.assertEqual(pol["table"]["visual_design"]["low"]["model"], "opus")  # floor kept
        self.assertTrue((Path(os.environ["MM_HOME"]) / "policies" / "policy-v001.json").exists())
        self.assertEqual(pol["changes"][0]["from"], "sonnet:medium")
        # rollback restores v0
        self.policy.rollback(0, self.conn)
        self.assertEqual(self.policy.active()["version"], 0)

    def test_suggest_only_and_dry_run_activate_nothing(self):
        for _ in range(12):
            self.task("code", 1, "haiku", "low", out_tokens=500)
        for _ in range(4):
            self.task("code", 1, "sonnet", "medium", out_tokens=5000)
        for mode, kw in (("suggest-only", {}), ("auto", {"dry_run": True})):
            res = self.learn.learn(self.conn, cfg=self.cfg(learning=mode), **kw)
            d = self.decision(res, "code", "low")
            self.assertEqual(d["status"], "suggestion", mode)
            self.assertTrue(any("not activated" in r for r in d["reasons"]))
            self.assertIsNone(res["written"])
            self.assertEqual(self.policy.active()["version"], 0)

    def test_under_eight_is_only_a_suggestion(self):
        for _ in range(6):
            self.task("code", 1, "haiku", "low", out_tokens=500)
        for _ in range(4):
            self.task("code", 1, "sonnet", "medium", out_tokens=5000)
        cells = self.learn.collect(self.conn)
        self.learn.estimate(cells)
        decs = self.learn.decide(cells, self.policy.active(), self.cfg())
        d = next(x for x in decs if x["category"] == "code" and x["band"] == "low")
        if d["best"] == "haiku:low":
            self.assertEqual(d["status"], "suggestion")
            self.assertTrue(any("needs ≥ 8" in r for r in d["reasons"]))
        else:
            self.assertEqual(d["status"], "keep")

    def test_floor_is_never_crossed(self):
        for _ in range(20):
            self.task("visual_design", 1, "haiku", "low", out_tokens=100)
        for _ in range(3):
            self.task("visual_design", 1, "opus", "medium", out_tokens=5000)
        res = self.learn.learn(self.conn, cfg=self.cfg(learning="auto"))
        d = self.decision(res, "visual_design", "low")
        self.assertEqual(d["best"], "opus:medium")
        self.assertIsNone(res["written"])

    def test_verdicts_count_for_pick_and_better(self):
        with self.conn:
            self.conn.execute("INSERT INTO tasks(task_id, category, complexity) VALUES ('g1', 'code', 1)")
            self.conn.execute("INSERT INTO routes(route_id, task_id, turn_id, ts, chosen_model, chosen_effort, applied)"
                              " VALUES (1, 'g1', 'u1', ?, 'haiku', 'low', 0)", (ts(1),))
            self.conn.execute("INSERT INTO route_grades(route_id, verdict, better, ts) VALUES (1, 'too_weak', 'sonnet:medium', ?)",
                              (ts(1),))
        cells = self.learn.collect(self.conn)
        weak, better = cells[("code", "low", "haiku", "low")], cells[("code", "low", "sonnet", "medium")]
        self.assertGreater(weak.f, 1.5)  # weight 2
        self.assertEqual(weak.s, 0)
        self.assertGreater(better.s, 1.5)
        self.assertEqual((weak.verdicts, better.verdicts), (1, 1))
        with self.conn:
            self.conn.execute("UPDATE route_grades SET verdict='wrong_category' WHERE route_id=1")
        self.assertEqual(self.learn.collect(self.conn), {})

    def test_auto_runs_when_due_and_never_when_off(self):
        self.task("code", 1, "haiku", "low")
        self.config.save(self.cfg(learning="off"))
        self.assertIsNone(self.learn.maybe_auto(self.conn))
        self.config.save(self.cfg(learning="auto"))
        self.assertIsNotNone(self.learn.maybe_auto(self.conn))  # first run: due
        self.assertIsNone(self.learn.maybe_auto(self.conn))  # just ran, < 10 new tasks
        for _ in range(10):
            self.task("code", 1, "haiku", "low")
        self.assertIsNotNone(self.learn.maybe_auto(self.conn))
        self.config.save(self.cfg(learning="suggest-only"))
        for _ in range(10):
            self.task("code", 1, "haiku", "low")
        self.assertIsNone(self.learn.maybe_auto(self.conn))

    def test_to_json_heatmap_and_cli(self):
        import io
        from contextlib import redirect_stdout
        from modelmgr import cli
        for _ in range(3):
            self.task("chat", 1, "opus", "medium")
        out = self.learn.to_json(self.learn.compute(self.conn))
        self.assertEqual(out["heatmap"]["chat"]["opus"]["n"], 3)
        self.assertNotIn("haiku", out["heatmap"]["chat"])
        self.assertEqual(out["thresholds"]["min_n"], 8)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["learn", "--suggest-only"])
            cli.main(["insights"])
        self.assertIn("not enough evidence", buf.getvalue())
        self.assertEqual(self.policy.active()["version"], 0)


if __name__ == "__main__":
    unittest.main()

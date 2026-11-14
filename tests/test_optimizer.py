import os
import tempfile
import unittest
from pathlib import Path


class OptimizerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MM_HOME"] = str(Path(self.tmp.name) / "mm")
        os.environ["MM_CLAUDE_PROJECTS"] = str(Path(self.tmp.name) / "projects")
        from modelmgr import cli, db
        cli.main(["init"])
        self.conn = db.open_db()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def finding(self, rule="bloat", scope="alpha", saving=5.0):
        from modelmgr.optimizers import Finding
        return Finding(tool="context", rule=rule, scope=scope, title=f"{rule} in {scope}",
                       evidence={"summary": "3 sessions", "examples": ["s1"]}, fix="compact earlier",
                       est_saving_usd=saving)

    def test_savings_block(self):
        from modelmgr import queries
        from modelmgr.optimizers import run_all, set_status
        empty = queries.savings(self.conn)
        self.assertIsNone(empty["mm_week_usd"])
        self.assertIsNone(empty["overall_usd"])
        f = self.finding(saving=7.0)
        run_all(self.conn, {"context": [lambda c: [f]]})
        self.assertEqual(queries.savings(self.conn)["uo_open_usd_30d"], 7.0)
        set_status(self.conn, f.id, "applied")
        s = queries.savings(self.conn)
        self.assertEqual((s["uo_applied_usd_30d"], s["uo_applied_count"], s["uo_open_usd_30d"]), (7.0, 1, 0.0))
        self.assertEqual(s["overall_usd"], 7.0)

    def test_schema_v2_has_findings(self):
        from modelmgr import db
        self.assertEqual(self.conn.execute("SELECT version FROM schema_version").fetchone()[0], db.SCHEMA_VERSION)
        self.conn.execute("SELECT * FROM findings").fetchall()

    def test_old_schema_version_is_upgraded(self):
        from modelmgr import db
        self.conn.execute("UPDATE schema_version SET version = 1")
        self.conn.commit()
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT version FROM schema_version").fetchone()[0], 2)

    def test_run_upserts_and_keeps_status(self):
        from modelmgr import optimizers, queries
        run = optimizers.run_all(self.conn, {"context": [lambda c: [self.finding()]]})
        self.assertEqual(run["tools"]["context"], {"findings": 1, "error": None})
        optimizers.set_status(self.conn, "context:bloat:alpha", "ignored")
        optimizers.run_all(self.conn, {"context": [lambda c: [self.finding(saving=7.0)]]})
        [f] = queries.findings(self.conn)
        self.assertEqual(f["status"], "ignored")
        self.assertEqual(f["est_saving_usd"], 7.0)
        self.assertEqual(f["evidence"]["examples"], ["s1"])

    def test_finding_not_produced_again_becomes_inactive(self):
        from modelmgr import optimizers, queries
        optimizers.run_all(self.conn, {"context": [lambda c: [self.finding()]]})
        optimizers.run_all(self.conn, {"context": [lambda c: []]})
        self.assertEqual(queries.findings(self.conn), [])
        self.assertEqual(len(queries.findings(self.conn, include_inactive=True)), 1)

    def test_failing_analyzer_keeps_previous_findings(self):
        from modelmgr import optimizers, queries
        optimizers.run_all(self.conn, {"context": [lambda c: [self.finding()]]})

        def broken(c):
            raise RuntimeError("boom")
        run = optimizers.run_all(self.conn, {"context": [broken]})
        self.assertIn("boom", run["tools"]["context"]["error"])
        self.assertEqual(len(queries.findings(self.conn)), 1)

    def test_finding_without_evidence_is_rejected(self):
        from modelmgr import optimizers
        f = self.finding()
        f.evidence = {}
        run = optimizers.run_all(self.conn, {"context": [lambda c: [f]]})
        self.assertIsNotNone(run["tools"]["context"]["error"])

    def test_bad_status(self):
        from modelmgr import optimizers
        with self.assertRaises(ValueError):
            optimizers.set_status(self.conn, "x", "done")
        with self.assertRaises(KeyError):
            optimizers.set_status(self.conn, "missing", "seen")

    def test_ranking_open_first_then_severity(self):
        from modelmgr import optimizers, queries
        a, b, c = self.finding("a", saving=1), self.finding("b", saving=9), self.finding("c", saving=50)
        b.severity = "high"
        optimizers.run_all(self.conn, {"context": [lambda _: [a, b, c]]})
        optimizers.set_status(self.conn, c.id, "applied")
        self.assertEqual([f["rule"] for f in queries.findings(self.conn)], ["b", "a", "c"])

    def test_health_not_measured_without_runs(self):
        from modelmgr import queries
        self.assertIsNone(queries.health(self.conn, 100.0)["score"])

    def test_health_score_and_cap(self):
        from modelmgr import optimizers, queries
        optimizers.run_all(self.conn, {"context": [lambda c: [self.finding(saving=20.0)]]})
        self.assertEqual(queries.health(self.conn, 100.0)["score"], 80)
        optimizers.run_all(self.conn, {"context": [lambda c: [self.finding(saving=500.0)]]})
        self.assertEqual(queries.health(self.conn, 100.0)["score"], 40)  # capped at 60% avoidable
        optimizers.set_status(self.conn, "context:bloat:alpha", "ignored")
        self.assertEqual(queries.health(self.conn, 100.0)["score"], 100)

    def test_home_and_tools_without_analyzers(self):
        from modelmgr import queries
        home = queries.home(self.conn)
        self.assertIsNone(home["health"]["score"])
        self.assertEqual(home["top"], [])
        self.assertEqual({t["tool"] for t in home["tools"]}, {"context", "output", "waste"})
        self.assertTrue(all(t["built"] is False for t in home["tools"]))

    def test_summary_carries_top_finding(self):
        from modelmgr import optimizers, queries
        self.assertIsNone(queries.summary(self.conn)["top_finding"])
        optimizers.run_all(self.conn, {"context": [lambda c: [self.finding()]]})
        self.assertEqual(queries.summary(self.conn)["top_finding"]["id"], "context:bloat:alpha")

    def test_ingest_runs_optimizers_without_error(self):
        from modelmgr import ingest
        ingest.run(self.conn)


if __name__ == "__main__":
    unittest.main()

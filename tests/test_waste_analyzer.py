import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def iso(t):
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


class WasteAnalyzerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["MM_HOME"] = str(root / "mm")
        os.environ["MM_CLAUDE_PROJECTS"] = str(root / "projects")
        os.environ["MM_CLAUDE_HOME"] = str(root / "claude")
        (root / "projects" / "p").mkdir(parents=True)
        from modelmgr import cli, db
        cli.main(["init"])
        self.conn = db.open_db()
        self.root = root
        self.t0 = datetime.now(timezone.utc) - timedelta(days=2)
        self.lines = []
        self.n = 0

    def tearDown(self):
        self.conn.close()
        os.environ.pop("MM_CLAUDE_HOME", None)
        self.tmp.cleanup()

    def at(self, m):
        return iso(self.t0 + timedelta(minutes=m))

    def tool(self, m, name, target, chars, **inp):
        self.n += 1
        tid = f"t{self.n}"
        use = {"type": "tool_use", "id": tid, "name": name, "input": {"file_path": target, **inp}}
        res = {"type": "tool_result", "tool_use_id": tid, "content": "x" * chars}
        for role, block in (("assistant", use), ("user", res)):
            self.lines.append({"type": role, "sessionId": "s1", "cwd": "/work/alpha", "timestamp": self.at(m),
                               "message": {"content": [block]}})

    def requests(self, count, minutes_from=0, read=50_000):
        from modelmgr import registry
        for i in range(count):
            self.n += 1
            cost = registry.estimate_cost("claude-opus-5-5", 1000, 500, 0, 0, read)
            with self.conn:
                self.conn.execute(
                    """INSERT INTO requests (request_key, session_id, ts, model, input_tokens, output_tokens,
                       cache_write_5m, cache_write_1h, cache_read, est_cost_usd) VALUES (?,?,?,?,?,?,0,0,?,?)""",
                    (f"k{self.n}", "s1", self.at(minutes_from + i), "claude-opus-5-5", 1000, 500, read, cost))

    def run_waste(self):
        (self.root / "projects" / "p" / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in self.lines))
        from modelmgr.optimizers import run_all
        from modelmgr.optimizers.waste import waste_findings
        run_all(self.conn, {"waste": [waste_findings]})
        return {r["id"]: r for r in self.conn.execute("SELECT * FROM findings WHERE active = 1")}

    def test_clean_has_no_findings(self):
        self.assertEqual(self.run_waste(), {})

    def test_repeated_reads_found(self):
        self.requests(2000)
        for i in range(40):
            self.tool(i, "Read", "/work/alpha/big.py", 400_000)
        f = self.run_waste()["waste:repeated-reads:alpha"]
        self.assertGreater(f["est_saving_usd"], 5)
        self.assertEqual(json.loads(f["evidence_json"])["examples"][0]["file"], "/work/alpha/big.py")

    def test_read_after_edit_is_not_a_repeat(self):
        self.requests(2000)
        for i in range(40):
            self.tool(2 * i, "Read", "/work/alpha/big.py", 400_000)
            self.tool(2 * i + 1, "Edit", "/work/alpha/big.py", 10)
        self.assertNotIn("waste:repeated-reads:alpha", self.run_waste())

    def test_big_results_found_per_tool(self):
        self.requests(2000)
        for i in range(40):
            self.tool(i, "Bash", "", 200_000)
        f = self.run_waste()["waste:big-results:Bash"]
        self.assertIn("head", f["fix"])

    def test_small_results_ignored(self):
        self.requests(2000)
        for i in range(40):
            self.tool(i, "Bash", "", 2_000)
        self.assertEqual(self.run_waste(), {})

    def test_cache_reused_for_unchanged_file(self):
        self.requests(50)
        self.tool(0, "Read", "/a", 100)
        self.run_waste()
        from modelmgr.optimizers import waste
        cache = waste._load_cache()
        self.assertEqual(len(cache["files"]), 1)

    def test_subagent_heavy(self):
        from modelmgr import registry
        for i in range(60):
            cost = registry.estimate_cost("claude-opus-5-5", 1000, 20_000, 0, 0, 0)
            with self.conn:
                self.conn.execute(
                    """INSERT INTO requests (request_key, session_id, agent_id, ts, model, input_tokens, output_tokens,
                       cache_write_5m, cache_write_1h, cache_read, est_cost_usd) VALUES (?,?,?,?,?,?,?,0,0,0,?)""",
                    (f"a{i}", "s1", "ag1", self.at(i), "claude-opus-5-5", 1000, 20_000, cost))
        self.assertIn("waste:subagent-heavy", self.run_waste())


if __name__ == "__main__":
    unittest.main()

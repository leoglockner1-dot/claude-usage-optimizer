"""Fill a throwaway database with made-up usage, for screenshots and trying the dashboard.

    MM_HOME=/tmp/uo-demo MM_CLAUDE_HOME=/tmp/uo-demo/claude PYTHONPATH=src python3 scripts/demo_data.py
    MM_HOME=/tmp/uo-demo MM_CLAUDE_HOME=/tmp/uo-demo/claude model-manager dashboard --no-ingest --port 8766

Never point MM_HOME at your real data folder: the script deletes the demo database first.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

from modelmgr import db, optimizers, paths, queries, registry

PROJECTS = {"webshop": "/home/demo/code/webshop", "api-server": "/home/demo/code/api-server",
            "docs-site": "/home/demo/code/docs-site", "mobile-app": "/home/demo/code/mobile-app"}
MODELS = [("claude-opus-5-5", 0.55), ("claude-sonnet-5-5", 0.35), ("claude-haiku-5-5", 0.10)]
CATEGORIES = ["edit", "debug", "explain", "refactor", "test", "docs"]


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> int:
    home = paths.home()
    if home.resolve() == (paths.Path.home() / ".model-manager").resolve():
        print("Set MM_HOME to a demo folder first.", file=sys.stderr)
        return 1
    home.mkdir(parents=True, exist_ok=True)
    paths.claude_home().mkdir(parents=True, exist_ok=True)
    if paths.db_path().exists():
        paths.db_path().unlink()
    rnd = random.Random(7)
    conn = db.open_db()
    now = datetime.now(timezone.utc)
    n_session = 0
    for day in range(29, -1, -1):
        for _ in range(rnd.randint(2, 5)):
            n_session += 1
            sid = f"demo-{n_session:04d}"
            name = rnd.choice(list(PROJECTS))
            start = now - timedelta(days=day, hours=rnd.randint(1, 10))
            model = rnd.choices([m for m, _ in MODELS], [w for _, w in MODELS])[0]
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                         (sid, PROJECTS[name], name, "cli", iso(start), None, "2.1.325", model, "demo"))
            ctx = rnd.randint(18_000, 30_000)
            t = start
            long_session = name == "webshop" and rnd.random() < 0.6
            for task_n in range(rnd.randint(1, 4)):
                tid = f"{sid}-t{task_n}"
                cat = rnd.choice(CATEGORIES)
                conn.execute("INSERT INTO tasks (task_id, session_id, started_at, closed_at, prompt_excerpt, category, "
                             "complexity, outcome, outcome_source, turns, escalations, retries, cwd) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (tid, sid, iso(t), None, f"demo {cat} task", cat, rnd.randint(1, 4),
                              rnd.choice(["verified_success", "unverified_success", "verified_success", "failure"]),
                              "tests", 0, 0, 0, PROJECTS[name]))
                for req in range(rnd.randint(8, 40 if long_session else 18)):
                    gap = rnd.choice([20, 40, 60, 90]) if rnd.random() > 0.04 else rnd.randint(400, 2400)
                    t += timedelta(seconds=gap)
                    grow = rnd.randint(1_500, 9_000 if long_session else 4_000)
                    cold = gap > 300
                    write = ctx if cold else grow
                    read = 0 if cold else ctx
                    out = rnd.randint(300, 2_600)
                    think = int(out * rnd.uniform(0.2, 0.6))
                    ctx += grow
                    cost = registry.estimate_cost(model, rnd.randint(2, 40), out, write, 0, read)
                    conn.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (f"{tid}-r{req}", tid, sid, f"{tid}-turn{req // 3}", None, iso(t), model, "medium",
                                  rnd.randint(2, 40), out, think, write, 0, read, cost, "demo", "demo"))
    conn.commit()
    optimizers.run_all(conn)
    # a health trend for the sparkline, ending at today's real score
    today = queries.health(conn, queries.overview(conn, 30).get("cost_usd")).get("score") or 70
    for day in range(29, 0, -1):
        score = max(30, min(95, today - day // 2 + rnd.randint(-3, 3)))
        conn.execute("INSERT OR REPLACE INTO health_history VALUES (?,?,?,?)",
                     ((now - timedelta(days=day)).date().isoformat(), score, None, None))
    conn.commit()
    queries.write_summary(conn)
    print(f"demo data in {home}: {n_session} sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())

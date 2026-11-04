"""model-manager command line interface."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from . import __version__, backfill, config, db, ingest, paths, policy, registry


# ---------- formatting ----------

def table(rows: list[list], header: list[str]) -> str:
    cells = [[("—" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(header)]
    def fmt(r):
        return "  ".join(c.rjust(w) if _num(c) else c.ljust(w) for c, w in zip(r, widths))
    lines = [fmt(header), "  ".join("─" * w for w in widths)]
    lines += [fmt(r) for r in cells]
    return "\n".join(lines)


def _num(s: str) -> bool:
    return s.replace(".", "", 1).replace(",", "").replace("$", "").replace("%", "").lstrip("-").isdigit()


def tok(n) -> str | None:
    if n is None:
        return None
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{int(n)}"


def usd(x) -> str | None:
    return None if x is None else f"${x:,.2f}"


def since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# ---------- commands ----------

def cmd_init(a) -> int:
    paths.ensure()
    if not paths.config_path().exists():
        config.save(config.load())
        print(f"config   {paths.config_path()} (mode: observe)")
    conn = db.open_db()
    if policy.active() is None:
        f = policy.activate(policy.prior_policy(), conn)
        print(f"policy   {f} (v0, prior heuristics)")
    print(f"database {paths.db_path()}")
    print(f"spool    {paths.spool_dir()}")
    return 0


def _refresh(conn) -> dict:
    return ingest.run(conn)


def cmd_status(a) -> int:
    conn = db.open_db()
    _refresh(conn)
    cfg = config.load()
    pol = policy.active() or {}
    disabled = paths.disabled_flag().exists()
    mode = "off (kill switch)" if disabled else cfg["mode"]
    last = conn.execute("SELECT MAX(ts) FROM requests WHERE source='mod'").fetchone()[0]
    tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    spool = len(list(paths.spool_dir().glob("*.json"))) if paths.spool_dir().exists() else 0
    print(f"AI Model Manager {__version__}")
    print(f"  mode            {mode}")
    print(f"  policy          v{pol.get('version', '?')} ({pol.get('source', 'missing')})")
    print(f"  auto models     {', '.join(cfg['auto_models'])}  · ceiling {cfg['ceiling']}")
    print(f"  learning        {cfg['learning']}")
    print(f"  tracked tasks   {tasks}")
    print(f"  last mod event  {last or 'none yet – is the mod loaded? run: model-manager doctor'}")
    print(f"  spool backlog   {spool} file(s)")
    return 0


def cmd_stats(a) -> int:
    conn = db.open_db()
    _refresh(conn)
    lo = since(a.days)
    rows = conn.execute(
        """SELECT model, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o,
                  SUM(COALESCE(cache_write_5m,0)+COALESCE(cache_write_1h,0)) w, SUM(cache_read) r,
                  SUM(est_cost_usd) c
           FROM requests WHERE ts >= ? GROUP BY model ORDER BY c DESC""", (lo,)).fetchall()
    print(f"Measured token usage, last {a.days} day(s) (all sessions, transcripts + mod)\n")
    print(table([[r["model"], r["n"], tok(r["i"]), tok(r["o"]), tok(r["w"]), tok(r["r"]), usd(r["c"])] for r in rows],
                ["model", "requests", "input", "output", "cache write", "cache read", "API-equiv. est."]))
    total = sum((r["c"] or 0) for r in rows)
    print(f"\nAPI-equivalent estimate: {usd(total)}  — list-price estimate, NOT a charge (subscription).")
    q = conn.execute("SELECT quota_5h, quota_7d, started_at FROM turns WHERE quota_5h IS NOT NULL "
                     "ORDER BY started_at DESC LIMIT 1").fetchone()
    if q:
        print(f"Subscription quota (last seen {q['started_at']}): 5h {q['quota_5h']:.0f}% · 7d {q['quota_7d']:.0f}%")
    else:
        print("Subscription quota: not measured yet (comes from the mod).")
    t = conn.execute(
        """SELECT COUNT(*) n, SUM(outcome='verified_success') vs, SUM(outcome='unverified_success') us,
                  SUM(outcome='failure') f, SUM(escalations) e FROM tasks WHERE started_at >= ?""", (lo,)).fetchone()
    if t["n"]:
        print(f"\nTasks: {t['n']} · verified ok {t['vs'] or 0} · unverified ok {t['us'] or 0} · "
              f"failed {t['f'] or 0} · escalations {t['e'] or 0}")
        c = conn.execute(
            """SELECT AVG(est_cost_usd) FROM v_task_cost
               WHERE outcome IN ('verified_success','unverified_success') AND task_id IN
               (SELECT task_id FROM tasks WHERE started_at >= ?)""", (lo,)).fetchone()[0]
        print(f"Avg API-equiv. cost per successful task: {usd(c)}")
    return 0


def cmd_models(a) -> int:
    conn = db.open_db()
    seen = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT model, COUNT(*), MAX(ts) FROM requests GROUP BY model")}
    rows = []
    for m in registry.MODELS:
        inp, w5, w1, rd, out, thr, mult = registry.PRICES[m.model_id]
        n, last = seen.get(m.model_id, (0, None))
        rows.append([m.tier, m.alias, m.model_id, f"{m.context_window // 1000}k", m.default_effort,
                     "yes" if m.auto else "escalation", f"{inp:g}/{rd:g}/{out:g}", n, (last or "")[:10]])
    print(table(rows, ["tier", "alias", "model id", "ctx", "effort", "auto", "$/MTok in/read/out", "requests",
                       "last used"]))
    other = [k for k in seen if k not in registry.BY_ID]
    if other:
        print("\nAlso seen in history: " + ", ".join(f"{k} ({seen[k][0]})" for k in other))
    print(f"\nPrices: {registry.PRICE_SOURCE}, {registry.PRICE_DATE}. Haiku 5.5 prompts >100K: 5x.")
    return 0


def cmd_history(a) -> int:
    conn = db.open_db()
    _refresh(conn)
    rows = conn.execute(
        """SELECT t.started_at, t.category, t.complexity, t.prompt_excerpt, t.outcome, t.turns, t.escalations,
                  (SELECT chosen_model || ':' || chosen_effort || CASE applied WHEN 1 THEN '' ELSE ' (obs)' END
                     FROM routes r WHERE r.task_id = t.task_id AND r.agent_id = '' ORDER BY ts DESC LIMIT 1) route,
                  v.est_cost_usd, v.output_tokens
           FROM tasks t LEFT JOIN v_task_cost v ON v.task_id = t.task_id
           ORDER BY t.started_at DESC LIMIT ?""", (a.n,)).fetchall()
    if not rows:
        print("No tasks recorded yet. Tasks are recorded by the mod from the first prompt after it loads.")
        return 0
    print(table([[r["started_at"][5:16].replace("T", " "), r["category"], r["complexity"],
                  (r["prompt_excerpt"] or "")[:44], r["route"], r["outcome"], r["turns"], r["escalations"],
                  tok(r["output_tokens"]), usd(r["est_cost_usd"])] for r in rows],
                ["when", "category", "cx", "task", "route", "outcome", "turns", "esc", "out tok", "est."]))
    return 0


def cmd_last(a) -> int:
    """Recent requests the mod saw, with the model the API actually answered with (live verification)."""
    conn = db.open_db()
    _refresh(conn)
    rows = conn.execute(
        """SELECT q.ts, q.agent_id, q.model, q.effort, q.output_tokens, q.est_cost_usd, r.chosen_model, r.applied,
                  t.category, t.prompt_excerpt
           FROM requests q LEFT JOIN routes r ON r.turn_id = q.turn_id AND r.agent_id = ''
           LEFT JOIN tasks t ON t.task_id = q.task_id
           WHERE q.source = 'mod' ORDER BY q.ts DESC LIMIT ?""", (a.n,)).fetchall()
    if not rows:
        print("No requests recorded by the mod yet.")
        return 0
    print(table([[r["ts"][11:19], "sub" if r["agent_id"] else "main", r["model"], r["effort"],
                  (r["chosen_model"] or "") + ("" if r["applied"] else " (obs)" if r["chosen_model"] else ""),
                  r["category"], (r["prompt_excerpt"] or "")[:36], tok(r["output_tokens"]), usd(r["est_cost_usd"])]
                 for r in rows],
                ["time", "who", "answered by", "effort", "routed", "category", "task", "out", "est."]))
    return 0


def cmd_insights(a) -> int:
    conn = db.open_db()
    _refresh(conn)
    try:
        from . import learn
    except ImportError:
        learn = None
    if learn is None:
        print("Learning engine not installed yet.")
        return 0
    return learn.print_insights(conn)


def cmd_learn(a) -> int:
    from . import learn
    conn = db.open_db()
    _refresh(conn)
    return learn.run(conn, dry_run=a.dry_run)


def cmd_config(a) -> int:
    if a.key and a.value is not None:
        cfg = config.set_key(a.key, a.value)
    else:
        cfg = config.load()
    if a.key and a.value is None:
        node = cfg
        for p in a.key.split("."):
            node = node[p]
        print(json.dumps(node, indent=2))
    else:
        print(json.dumps(cfg, indent=2))
    return 0


def cmd_mode(a) -> int:
    cfg = config.load()
    if a.mode:
        cfg["mode"] = a.mode
        config.save(cfg)
    print(f"mode: {cfg['mode']}")
    return 0


def cmd_off(a) -> int:
    paths.ensure()
    paths.disabled_flag().write_text(db.now_iso() + "\n")
    print("Model Manager disabled (kill switch). Sessions keep their own model; nothing is routed or recorded.")
    print("Re-enable: model-manager on")
    return 0


def cmd_on(a) -> int:
    if paths.disabled_flag().exists():
        paths.disabled_flag().unlink()
    print(f"Model Manager enabled. mode: {config.load()['mode']}")
    return 0


def cmd_ingest(a) -> int:
    print(json.dumps(ingest.run(min_age_s=0 if a.now else ingest.MIN_AGE_S)))
    return 0


def cmd_backfill(a) -> int:
    print(json.dumps(backfill.run(force=a.force)))
    return 0


def cmd_policies(a) -> int:
    act = (policy.active() or {}).get("version")
    rows = [[("* " if h["version"] == act else "  ") + str(h["version"]), h["created_at"], h["source"], h["changes"]]
            for h in policy.history()]
    print(table(rows, ["version", "created", "source", "changes"]))
    return 0


def cmd_rollback(a) -> int:
    conn = db.open_db()
    pol = policy.rollback(a.version, conn)
    print(f"Active policy is now v{pol['version']} ({pol.get('source')}).")
    return 0


def cmd_dashboard(a) -> int:
    from . import web
    return web.serve(port=a.port, open_browser=a.open, auto_ingest=not a.no_ingest)


def cmd_doctor(a) -> int:
    ok = True
    def check(label, cond, hint=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'✔' if cond else '✘'} {label}" + ("" if cond or not hint else f"  → {hint}"))
    print("model-manager doctor")
    check("data dir", paths.home().exists(), "model-manager init")
    check("config.json", paths.config_path().exists(), "model-manager init")
    check("policy.json", policy.active() is not None, "model-manager init")
    try:
        conn = db.open_db()
        conn.execute("SELECT 1").fetchone()
        check("database", True)
    except Exception as e:  # noqa: BLE001 - doctor reports anything
        check(f"database ({e})", False)
        conn = None
    check("kill switch off", not paths.disabled_flag().exists(), "model-manager on")
    settings = paths.home().parent / ".claude" / "settings.json"
    try:
        s = json.loads(settings.read_text())
        enabled = any(k.startswith("model-manager@") and v for k, v in (s.get("enabledPlugins") or {}).items())
        check("plugin enabled in ~/.claude/settings.json", enabled, "claude plugin install model-manager@ai-model-manager")
        check("disableAllHooks not set", not s.get("disableAllHooks"), "remove disableAllHooks")
    except (OSError, json.JSONDecodeError):
        check("~/.claude/settings.json readable", False)
    if conn is not None:
        last = conn.execute("SELECT MAX(ts) FROM routes").fetchone()[0]
        check(f"mod has written events (last route: {last or 'never'})", last is not None,
              "open a new Claude Code session (or /reload-plugins) and send a prompt")
    cli = shutil.which("claude")
    if cli:
        v = subprocess.run([cli, "--version"], capture_output=True, text=True).stdout.strip()
        print(f"  · terminal CLI: {v} (mods need ≥ 2.1.287)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="model-manager", description="AI Model Manager for Claude Code")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create data dir, config, database, initial policy").set_defaults(f=cmd_init)
    sub.add_parser("status", help="mode, policy, last activity").set_defaults(f=cmd_status)
    s = sub.add_parser("stats", help="tokens, estimated cost, quota, outcomes")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(f=cmd_stats)
    sub.add_parser("models", help="model registry and prices").set_defaults(f=cmd_models)
    s = sub.add_parser("history", help="recent tasks")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(f=cmd_history)
    s = sub.add_parser("last", help="recent requests with the model that actually answered")
    s.add_argument("-n", type=int, default=15)
    s.set_defaults(f=cmd_last)
    sub.add_parser("insights", help="what the system has learned").set_defaults(f=cmd_insights)
    s = sub.add_parser("learn", help="recompute statistics and compile a new policy")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(f=cmd_learn)
    s = sub.add_parser("config", help="show or set config (dotted keys, JSON values)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(f=cmd_config)
    s = sub.add_parser("mode", help="show or set the operating mode")
    s.add_argument("mode", nargs="?", choices=config.MODES)
    s.set_defaults(f=cmd_mode)
    sub.add_parser("off", help="kill switch: stop routing and recording").set_defaults(f=cmd_off)
    sub.add_parser("on", help="undo the kill switch").set_defaults(f=cmd_on)
    s = sub.add_parser("ingest", help="import the mod's spool into the database")
    s.add_argument("--now", action="store_true", help="also take files written in the last 2 s")
    s.set_defaults(f=cmd_ingest)
    s = sub.add_parser("backfill", help="import historic usage from Claude Code transcripts")
    s.add_argument("--force", action="store_true")
    s.set_defaults(f=cmd_backfill)
    sub.add_parser("policies", help="list policy versions").set_defaults(f=cmd_policies)
    s = sub.add_parser("rollback", help="activate an earlier policy version")
    s.add_argument("version", type=int)
    s.set_defaults(f=cmd_rollback)
    s = sub.add_parser("dashboard", help="local web dashboard on 127.0.0.1 (ingests every 30 s)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open it in the browser")
    s.add_argument("--no-ingest", action="store_true", help="serve the database as it is")
    s.set_defaults(f=cmd_dashboard)
    sub.add_parser("doctor", help="check installation").set_defaults(f=cmd_doctor)
    a = p.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())

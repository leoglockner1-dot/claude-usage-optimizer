"""Read-only aggregations for the dashboard, the summary file and the mod pane.

Every function takes an open connection and returns plain JSON-ready values. Dollar figures are
API-equivalent list-price estimates. None means 'not measured' and is never replaced by 0.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any

from . import config, paths, registry

SUMMARY_VERSION = 1
BASELINE_MODEL = "claude-opus-5-5"  # what every request would have used without routing

# context size buckets (input side tokens per request): label, low (inclusive), high (exclusive)
CONTEXT_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("<50K", 0, 50_000),
    ("50–100K", 50_000, 100_000),
    ("100–200K", 100_000, 200_000),
    ("200–400K", 200_000, 400_000),
    (">400K", 400_000, None),
)

_REQ_COLS = """r.model, r.ts, r.session_id, r.turn_id, r.input_tokens, r.output_tokens,
               r.cache_write_5m, r.cache_write_1h, r.cache_read, r.est_cost_usd"""


def since(days: int | None) -> str:
    """ISO cutoff for 'last N days'; None means all history."""
    if days is None:
        return ""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def context_tokens(r: sqlite3.Row | dict) -> int | None:
    """Input-side tokens of one request: what the model had to read."""
    if r["input_tokens"] is None:
        return None
    return r["input_tokens"] + (r["cache_write_5m"] or 0) + (r["cache_write_1h"] or 0) + (r["cache_read"] or 0)


def cost_by_kind(r: sqlite3.Row | dict, model: str | None = None) -> dict[str, float] | None:
    """Split one request's estimate into input / cache_write / cache_read / output, priced as `model`."""
    mid = registry.normalize_model(model or r["model"])
    if mid not in registry.PRICES or r["input_tokens"] is None or r["output_tokens"] is None:
        return None
    inp, w5, w1, rd, out, thr, mult = registry.PRICES[mid]
    factor = mult if thr is not None and (context_tokens(r) or 0) > thr else 1.0
    return {
        "input": r["input_tokens"] * inp * factor / 1e6,
        "cache_write": ((r["cache_write_5m"] or 0) * w5 + (r["cache_write_1h"] or 0) * w1) * factor / 1e6,
        "cache_read": (r["cache_read"] or 0) * rd * factor / 1e6,
        "output": r["output_tokens"] * out * factor / 1e6,
    }


def _requests(conn: sqlite3.Connection, days: int | None) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT {_REQ_COLS} FROM requests r WHERE r.ts >= ? ORDER BY r.ts", (since(days),)).fetchall()


def _routed_turns(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT DISTINCT turn_id FROM routes WHERE applied = 1 AND turn_id IS NOT NULL")}


def _median(xs: list[int]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def project_name(cwd: str | None, project: str | None = None) -> str:
    """Readable project label from a session's working directory."""
    if project:
        return project
    if not cwd:
        return "(unknown)"
    if "/scratch-workspaces/" in cwd:
        return "(scratch)"
    if cwd.startswith(("/private/tmp/", "/tmp/")):
        return "(temp)"
    return PurePath(cwd).name or cwd


# ---------- overview ----------

def overview(conn: sqlite3.Connection, days: int | None = 30) -> dict[str, Any]:
    rows = _requests(conn, days)
    routed = _routed_turns(conn)
    kinds = {"input": 0.0, "cache_write": 0.0, "cache_read": 0.0, "output": 0.0}
    tokens = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    cost = 0.0
    priced = unpriced = 0
    ctx: list[int] = []
    routed_actual = routed_baseline = 0.0
    routed_n = 0
    sessions: set[str] = set()
    for r in rows:
        if r["session_id"]:
            sessions.add(r["session_id"])
        c = context_tokens(r)
        if c is not None:
            ctx.append(c)
        tokens["input"] += r["input_tokens"] or 0
        tokens["cache_write"] += (r["cache_write_5m"] or 0) + (r["cache_write_1h"] or 0)
        tokens["cache_read"] += r["cache_read"] or 0
        tokens["output"] += r["output_tokens"] or 0
        split = cost_by_kind(r)
        if split is None or r["est_cost_usd"] is None:
            unpriced += 1
            continue
        priced += 1
        cost += r["est_cost_usd"]
        for k, v in split.items():
            kinds[k] += v
        if r["turn_id"] in routed:
            base = cost_by_kind(r, BASELINE_MODEL)
            if base is not None:
                routed_n += 1
                routed_actual += r["est_cost_usd"]
                routed_baseline += sum(base.values())
    first = rows[0]["ts"] if rows else None
    last = rows[-1]["ts"] if rows else None
    return {
        "days": days,
        "from": first,
        "to": last,
        "requests": len(rows),
        "sessions": len(sessions),
        "priced_requests": priced,
        "unpriced_requests": unpriced,
        "est_cost_usd": _r(cost) if priced else None,
        "cost_by_kind": {k: _r(v) for k, v in kinds.items()} if priced else None,
        "tokens": tokens,
        "cache_read_share": _r(kinds["cache_read"] / cost) if cost else None,
        "median_context": _median(ctx),
        "savings": {
            # only requests the router actually switched count; otherwise there is nothing to compare
            "routed_requests": routed_n,
            "est_cost_usd": _r(routed_actual) if routed_n else None,
            "baseline_cost_usd": _r(routed_baseline) if routed_n else None,
            "est_savings_usd": _r(routed_baseline - routed_actual) if routed_n else None,
            "baseline_model": BASELINE_MODEL,
        },
    }


def daily(conn: sqlite3.Connection, days: int | None = 30) -> list[dict[str, Any]]:
    """Cost and requests per UTC day, split by model."""
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """SELECT substr(ts, 1, 10) d, model, COUNT(*) n, SUM(est_cost_usd) c, COUNT(est_cost_usd) nc
           FROM requests WHERE ts >= ? GROUP BY d, model ORDER BY d""", (since(days),)):
        day = out.setdefault(row["d"], {"date": row["d"], "requests": 0, "est_cost_usd": None, "by_model": {}})
        day["requests"] += row["n"]
        if row["nc"]:
            day["by_model"][row["model"]] = _r(row["c"])
            day["est_cost_usd"] = _r((day["est_cost_usd"] or 0) + row["c"])
    return list(out.values())


def model_mix(conn: sqlite3.Connection, days: int | None = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT model, COUNT(*) n, SUM(output_tokens) o, SUM(est_cost_usd) c, COUNT(est_cost_usd) nc
           FROM requests WHERE ts >= ? GROUP BY model ORDER BY COALESCE(SUM(est_cost_usd), 0) DESC, n DESC""",
        (since(days),)).fetchall()
    total_n = sum(r["n"] for r in rows) or 1
    total_c = sum(r["c"] or 0 for r in rows)
    return [{
        "model": r["model"],
        "tier": registry.tier_of(r["model"]),
        "requests": r["n"],
        "request_share": _r(r["n"] / total_n),
        "output_tokens": r["o"],
        "est_cost_usd": _r(r["c"]) if r["nc"] else None,
        "cost_share": _r(r["c"] / total_c) if r["nc"] and total_c else None,
    } for r in rows]


def context_histogram(conn: sqlite3.Connection, days: int | None = 30) -> list[dict[str, Any]]:
    buckets = [{"label": lab, "lo": lo, "hi": hi, "requests": 0, "est_cost_usd": 0.0} for lab, lo, hi in CONTEXT_BUCKETS]
    not_measured = 0
    for r in _requests(conn, days):
        c = context_tokens(r)
        if c is None:
            not_measured += 1
            continue
        for b in buckets:
            if c >= b["lo"] and (b["hi"] is None or c < b["hi"]):
                b["requests"] += 1
                b["est_cost_usd"] += r["est_cost_usd"] or 0
                break
    total = sum(b["est_cost_usd"] for b in buckets)
    for b in buckets:
        b["est_cost_usd"] = _r(b["est_cost_usd"])
        b["cost_share"] = _r(b["est_cost_usd"] / total) if total else None
    if not_measured:
        buckets.append({"label": "not measured", "lo": None, "hi": None, "requests": not_measured,
                        "est_cost_usd": None, "cost_share": None})
    return buckets


def context_daily(conn: sqlite3.Connection, days: int | None = 30) -> list[dict[str, Any]]:
    """Context size per UTC day: median, 90th percentile, and the cost share of requests above 200K."""
    per: dict[str, dict[str, Any]] = {}
    for r in _requests(conn, days):
        c = context_tokens(r)
        if c is None:
            continue
        d = per.setdefault(r["ts"][:10], {"ctx": [], "cost": 0.0, "cost_big": 0.0})
        d["ctx"].append(c)
        d["cost"] += r["est_cost_usd"] or 0
        if c > 200_000:
            d["cost_big"] += r["est_cost_usd"] or 0
    out = []
    for date, d in sorted(per.items()):
        s = sorted(d["ctx"])
        out.append({"date": date, "requests": len(s), "median_context": _median(s),
                    "p90_context": s[min(len(s) - 1, int(len(s) * 0.9))],
                    "over_200k_cost_share": _r(d["cost_big"] / d["cost"]) if d["cost"] else None})
    return out

# ---------- projects and sessions ----------

def projects(conn: sqlite3.Connection, days: int | None = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT s.cwd, s.project, r.session_id, r.model, COUNT(*) n, SUM(r.est_cost_usd) c,
                  COUNT(r.est_cost_usd) nc, MAX(r.ts) last
           FROM requests r LEFT JOIN sessions s ON s.session_id = r.session_id
           WHERE r.ts >= ? GROUP BY r.session_id, r.model""", (since(days),)).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = project_name(r["cwd"], r["project"])
        p = out.setdefault(name, {"project": name, "cwd": r["cwd"], "sessions": set(), "requests": 0,
                                  "est_cost_usd": None, "models": {}, "last": None})
        if r["session_id"]:
            p["sessions"].add(r["session_id"])
        p["requests"] += r["n"]
        if r["nc"]:
            p["est_cost_usd"] = (p["est_cost_usd"] or 0) + r["c"]
        p["models"][r["model"]] = p["models"].get(r["model"], 0) + r["n"]
        p["last"] = max(p["last"] or "", r["last"] or "") or None
    result = []
    for p in out.values():
        result.append({
            "project": p["project"],
            "cwd": p["cwd"],
            "sessions": len(p["sessions"]),
            "requests": p["requests"],
            "est_cost_usd": _r(p["est_cost_usd"]),
            "main_model": max(p["models"], key=p["models"].get) if p["models"] else None,
            "last": p["last"],
        })
    return sorted(result, key=lambda p: p["est_cost_usd"] or 0, reverse=True)


def sessions(conn: sqlite3.Connection, days: int | None = 30, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.session_id, s.cwd, s.project, s.surface, MIN(r.ts) first, MAX(r.ts) last, COUNT(*) n,
                  SUM(r.est_cost_usd) c, COUNT(r.est_cost_usd) nc,
                  MAX(COALESCE(r.input_tokens,0) + COALESCE(r.cache_write_5m,0) + COALESCE(r.cache_write_1h,0)
                      + COALESCE(r.cache_read,0)) peak
           FROM requests r LEFT JOIN sessions s ON s.session_id = r.session_id
           WHERE r.ts >= ? GROUP BY r.session_id ORDER BY last DESC LIMIT ?""", (since(days), limit)).fetchall()
    return [{
        "session_id": r["session_id"],
        "project": project_name(r["cwd"], r["project"]),
        "surface": r["surface"],
        "first": r["first"],
        "last": r["last"],
        "requests": r["n"],
        "est_cost_usd": _r(r["c"]) if r["nc"] else None,
        "peak_context": r["peak"],
    } for r in rows]


def session_timeline(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT {_REQ_COLS}, r.effort, r.source FROM requests r WHERE r.session_id = ? ORDER BY r.ts",
                        (session_id,)).fetchall()
    return [{
        "ts": r["ts"],
        "model": r["model"],
        "effort": r["effort"],
        "context": context_tokens(r),
        "output_tokens": r["output_tokens"],
        "est_cost_usd": _r(r["est_cost_usd"], 6),
        "source": r["source"],
    } for r in rows]


# ---------- routing and quota ----------

def routes(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.ts, r.task_id, r.turn_id, r.agent_id, r.mode, r.policy_version, r.session_model,
                  r.chosen_model, r.chosen_effort, r.applied, r.reason,
                  t.category, t.complexity, t.risk, t.class_confidence, t.prompt_excerpt, t.user_override, t.outcome
           FROM routes r LEFT JOIN tasks t ON t.task_id = r.task_id
           ORDER BY r.ts DESC LIMIT ?""", (limit,)).fetchall()
    return [{**dict(r), "applied": bool(r["applied"]), "agent_id": r["agent_id"] or None} for r in rows]


def categories(conn: sqlite3.Connection, days: int | None = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT category, COUNT(*) n, AVG(class_confidence) conf, AVG(complexity) cx,
                  SUM(outcome IN ('verified_success','unverified_success')) ok, SUM(outcome = 'failure') bad
           FROM tasks WHERE started_at >= ? GROUP BY category ORDER BY n DESC""", (since(days),)).fetchall()
    return [{"category": r["category"], "tasks": r["n"], "avg_confidence": _r(r["conf"], 3),
             "avg_complexity": _r(r["cx"], 2), "succeeded": r["ok"] or 0, "failed": r["bad"] or 0} for r in rows]


def escalations(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM escalations ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["signals"] = json.loads(d["signals"]) if d["signals"] else None
        except json.JSONDecodeError:
            pass
        out.append(d)
    return out


def quota(conn: sqlite3.Connection, days: int | None = 7) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT started_at ts, quota_5h, quota_7d FROM turns
           WHERE started_at >= ? AND (quota_5h IS NOT NULL OR quota_7d IS NOT NULL) ORDER BY started_at""",
        (since(days),)).fetchall()
    return [dict(r) for r in rows]


def last_route(conn: sqlite3.Connection) -> dict[str, Any] | None:
    r = routes(conn, limit=1)
    return r[0] if r else None


# ---------- summary file for the mod pane ----------

def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    cfg = config.load()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week = overview(conn, 7)
    days = {d["date"]: d for d in daily(conn, 7)}
    spark = []
    for i in range(6, -1, -1):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        spark.append({"date": d, "est_cost_usd": days.get(d, {}).get("est_cost_usd"),
                      "requests": days.get(d, {}).get("requests", 0)})
    q = quota(conn, 7)
    lr = last_route(conn)
    return {
        "v": SUMMARY_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": cfg.get("mode"),
        "disabled": paths.disabled_flag().exists(),
        "today": {"date": today, "requests": days.get(today, {}).get("requests", 0),
                  "est_cost_usd": days.get(today, {}).get("est_cost_usd")},
        "week": {
            "requests": week["requests"],
            "est_cost_usd": week["est_cost_usd"],
            "cache_read_share": week["cache_read_share"],
            "median_context": week["median_context"],
            "savings": week["savings"],
            "model_mix": [{"model": m["model"], "request_share": m["request_share"], "cost_share": m["cost_share"]}
                          for m in model_mix(conn, 7)],
        },
        "daily": spark,
        "quota": q[-1] if q else None,
        "last_route": None if lr is None else {k: lr[k] for k in (
            "ts", "category", "complexity", "chosen_model", "chosen_effort", "applied", "reason", "mode")},
    }


def write_summary(conn: sqlite3.Connection) -> Any:
    """Write summary.json atomically (temp file + rename) so the mod never reads half a file."""
    data = summary(conn)
    target = paths.summary_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".summary-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target

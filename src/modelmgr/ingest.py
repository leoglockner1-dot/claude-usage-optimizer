"""Ingest the mod's spool files into SQLite.

The mod writes one JSON file per batch: {"v": 1, "events": [{"type": ..., ...}, ...]}.
$.fs.write is not atomic, so files that are very new or do not parse are left for the next run.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from . import db, paths, queries, registry

MIN_AGE_S = 2.0
KEEP_DONE_DAYS = 14


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, separators=(",", ":"))


def _usage_fields(u: dict | None) -> dict[str, int | None]:
    u = u or {}
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens")
    w1 = cc.get("ephemeral_1h_input_tokens")
    total_write = u.get("cache_creation_input_tokens")
    if w5 is None and w1 is None and total_write is not None:
        w5 = total_write  # no split reported: price as 5m writes (cheaper; conservative estimate)
    details = u.get("output_tokens_details") or {}
    return {
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "thinking_tokens": details.get("thinking_tokens"),
        "cache_write_5m": w5,
        "cache_write_1h": w1,
        "cache_read": u.get("cache_read_input_tokens"),
    }


def _scalar(v: Any) -> Any:
    if isinstance(v, dict):
        return v.get("version") or v.get("id") or json.dumps(v, separators=(",", ":"))
    if isinstance(v, list):
        return json.dumps(v, separators=(",", ":"))
    return v


def apply_event(conn: sqlite3.Connection, ev: dict[str, Any]) -> None:
    # nested values are kept only where a column expects JSON; anything else is flattened
    ev = {k: (v if k in ("usage", "flags", "candidates", "signals") else _scalar(v)) for k, v in ev.items()}
    t = ev.get("type")
    if t == "session":
        conn.execute(
            """INSERT INTO sessions(session_id, cwd, project, surface, started_at, cc_version, default_model, source)
               VALUES (?,?,?,?,?,?,?, 'mod')
               ON CONFLICT(session_id) DO UPDATE SET cwd=COALESCE(excluded.cwd, cwd),
                 cc_version=COALESCE(excluded.cc_version, cc_version),
                 default_model=COALESCE(excluded.default_model, default_model), source='mod'""",
            (ev.get("session_id"), ev.get("cwd"), ev.get("project"), ev.get("surface"), ev.get("ts"),
             ev.get("cc_version"), ev.get("model")),
        )
    elif t == "session_end":
        conn.execute("UPDATE sessions SET ended_at=? WHERE session_id=?", (ev.get("ts"), ev.get("session_id")))
    elif t == "task_open":
        conn.execute(
            """INSERT OR IGNORE INTO tasks(task_id, session_id, started_at, prompt_excerpt, prompt_hash, category,
               complexity, risk, flags, class_confidence, user_override, exploration, cwd, turns, escalations)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
            (ev["task_id"], ev.get("session_id"), ev.get("ts"), ev.get("prompt_excerpt"), ev.get("prompt_hash"),
             ev.get("category"), ev.get("complexity"), ev.get("risk"), _j(ev.get("flags")), ev.get("confidence"),
             ev.get("override"), 1 if ev.get("exploration") else 0, ev.get("cwd")),
        )
    elif t == "task_close":
        conn.execute(
            """UPDATE tasks SET closed_at=?, outcome=?, outcome_source=?, turns=COALESCE(?, turns),
               escalations=COALESCE(?, escalations), retries=COALESCE(?, retries) WHERE task_id=?""",
            (ev.get("ts"), ev.get("outcome"), ev.get("outcome_source"), ev.get("turns"), ev.get("escalations"),
             ev.get("retries"), ev.get("task_id")),
        )
    elif t == "route":
        conn.execute(
            """INSERT OR REPLACE INTO routes(task_id, turn_id, agent_id, ts, mode, policy_version, session_model,
               chosen_model, chosen_effort, applied, reason, candidates) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ev.get("task_id"), ev.get("turn_id"), ev.get("agent_id") or "", ev.get("ts"), ev.get("mode"),
             ev.get("policy_version"), ev.get("session_model"), ev.get("chosen_model"), ev.get("chosen_effort"),
             1 if ev.get("applied") else 0, ev.get("reason"), _j(ev.get("candidates"))),
        )
    elif t == "request":
        uf = _usage_fields(ev.get("usage"))
        model = ev.get("model") or (ev.get("usage") or {}).get("model")
        cost = registry.estimate_cost(model, uf["input_tokens"], uf["output_tokens"], uf["cache_write_5m"],
                                      uf["cache_write_1h"], uf["cache_read"])
        conn.execute(
            """INSERT OR REPLACE INTO requests(request_key, task_id, session_id, turn_id, agent_id, ts, model, effort,
               input_tokens, output_tokens, thinking_tokens, cache_write_5m, cache_write_1h, cache_read,
               est_cost_usd, price_version, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'mod')""",
            (ev["request_key"], ev.get("task_id"), ev.get("session_id"), ev.get("turn_id"), ev.get("agent_id"),
             ev.get("ts"), registry.normalize_model(model), ev.get("effort"), uf["input_tokens"],
             uf["output_tokens"], uf["thinking_tokens"], uf["cache_write_5m"], uf["cache_write_1h"],
             uf["cache_read"], cost, registry.PRICE_DATE if cost is not None else None),
        )
    elif t == "turn":
        conn.execute(
            """INSERT OR REPLACE INTO turns(turn_id, task_id, session_id, agent_id, started_at, duration_ms, aborted,
               tool_calls, tool_errors, tests_run, tests_failed, tests_passed, builds_failed, quota_5h, quota_7d)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ev["turn_id"], ev.get("task_id"), ev.get("session_id"), ev.get("agent_id"), ev.get("ts"),
             ev.get("duration_ms"), 1 if ev.get("aborted") else 0, ev.get("tool_calls"), ev.get("tool_errors"),
             ev.get("tests_run"), ev.get("tests_failed"), ev.get("tests_passed"), ev.get("builds_failed"),
             ev.get("quota_5h"), ev.get("quota_7d")),
        )
        if not ev.get("agent_id"):
            conn.execute("UPDATE tasks SET turns = COALESCE(turns,0) + 1 WHERE task_id=?", (ev.get("task_id"),))
    elif t == "escalation":
        conn.execute(
            "INSERT INTO escalations VALUES (?,?,?,?,?,?,?,?)",
            (ev.get("task_id"), ev.get("turn_id"), ev.get("ts"), ev.get("from_model"), ev.get("from_effort"),
             ev.get("to_model"), ev.get("to_effort"), _j(ev.get("signals"))),
        )
        conn.execute("UPDATE tasks SET escalations = COALESCE(escalations,0) + 1 WHERE task_id=?", (ev.get("task_id"),))
    elif t == "feedback":
        conn.execute("INSERT INTO feedback VALUES (?,?,?,?)",
                     (ev.get("task_id"), ev.get("ts"), ev.get("kind"), ev.get("note")))
    elif t == "spawn":
        conn.execute(
            "INSERT INTO spawns VALUES (?,?,?,?,?,?,?,?)",
            (ev.get("ts"), ev.get("session_id"), ev.get("task_id"), ev.get("agent_type"), ev.get("requested_model"),
             ev.get("chosen_model"), 1 if ev.get("applied") else 0, ev.get("reason")),
        )
    # unknown types are ignored on purpose: newer mods must not break older cores


def _spool_files() -> Iterable[Path]:
    d = paths.spool_dir()
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix == ".json")


def run(conn: sqlite3.Connection | None = None, min_age_s: float = MIN_AGE_S) -> dict[str, int]:
    paths.ensure()
    own = conn is None
    conn = conn or db.open_db()
    stats = {"files": 0, "events": 0, "skipped": 0, "bad": 0}
    now = time.time()
    for f in _spool_files():
        try:
            if now - f.stat().st_mtime < min_age_s:
                stats["skipped"] += 1
                continue
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            # a partly written file parses later; a file older than a day that still fails is quarantined
            if now - f.stat().st_mtime > 86400:
                f.rename(f.with_suffix(".bad"))
                stats["bad"] += 1
            else:
                stats["skipped"] += 1
            continue
        events = data.get("events", []) if isinstance(data, dict) else data
        with conn:
            for ev in events:
                if isinstance(ev, dict):
                    apply_event(conn, ev)
                    stats["events"] += 1
        shutil.move(str(f), paths.spool_done_dir() / f.name)
        stats["files"] += 1
    cutoff = now - KEEP_DONE_DAYS * 86400
    for f in paths.spool_done_dir().glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
    try:
        from . import learn  # once a day or after 10 newly closed tasks, only with learning = auto
        learn.maybe_auto(conn)
    except Exception:  # noqa: BLE001 - learning must never break ingest
        pass
    try:
        queries.write_summary(conn)
    except (OSError, sqlite3.Error):
        pass  # the summary is a convenience for the pane; ingest must never fail because of it
    if own:
        conn.close()
    return stats

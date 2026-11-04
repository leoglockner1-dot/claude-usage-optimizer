"""Import historic per-request token usage from Claude Code transcripts (~/.claude/projects).

Transcripts write one `assistant` line per content block, all carrying the same requestId and usage,
so requests are deduplicated by requestId (keeping the line with the most output tokens).
Requests from the time the mod started recording a session are skipped to avoid double counting.
Backfilled requests have no task, route or outcome: they feed usage statistics, not learning.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import db, paths, registry
from .ingest import _usage_fields


def _iter_transcripts(root: Path):
    if not root.exists():
        return
    yield from root.glob("*/*.jsonl")
    yield from root.glob("*/*/subagents/*.jsonl")


def _parse(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    reqs: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    agent_from_name = path.stem.removeprefix("agent-") if path.parent.name == "subagents" else None
    with path.open(errors="replace") as f:
        for line in f:
            if '"assistant"' not in line and '"user"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = e.get("sessionId")
            if sid and e.get("type") == "user" and sid not in sessions:
                sessions[sid] = {"cwd": e.get("cwd"), "version": e.get("version"),
                                 "entrypoint": e.get("entrypoint"), "ts": e.get("timestamp")}
            if e.get("type") != "assistant":
                continue
            msg = e.get("message") or {}
            usage = msg.get("usage")
            rid = e.get("requestId") or msg.get("id")
            if not usage or not rid or msg.get("model") in (None, "<synthetic>"):
                continue
            prev = reqs.get(rid)
            if prev and (prev["usage"].get("output_tokens") or 0) >= (usage.get("output_tokens") or 0):
                continue
            reqs[rid] = {"session_id": sid, "agent_id": e.get("agentId") or agent_from_name,
                         "ts": e.get("timestamp"), "model": msg.get("model"), "usage": usage}
    return reqs, sessions


def run(conn: sqlite3.Connection | None = None, root: Path | None = None, force: bool = False) -> dict[str, int]:
    own = conn is None
    conn = conn or db.open_db()
    root = root or paths.claude_projects_dir()
    # per session, the mod is authoritative from its first recorded request onwards
    mod_first = {r[0]: r[1] for r in conn.execute(
        "SELECT session_id, MIN(ts) FROM requests WHERE source='mod' GROUP BY session_id")}
    stats = {"files": 0, "unchanged": 0, "requests": 0}
    for path in _iter_transcripts(root):
        try:
            st = path.stat()
        except OSError:
            continue
        row = conn.execute("SELECT mtime, size FROM ingest_files WHERE path=?", (str(path),)).fetchone()
        if row and not force and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
            stats["unchanged"] += 1
            continue
        reqs, sessions = _parse(path)
        with conn:
            for sid, s in sessions.items():
                conn.execute(
                    """INSERT INTO sessions(session_id, cwd, surface, started_at, cc_version, source)
                       VALUES (?,?,?,?,?, 'transcript') ON CONFLICT(session_id) DO NOTHING""",
                    (sid, s["cwd"], s["entrypoint"], s["ts"], s["version"]),
                )
            for rid, r in reqs.items():
                first = mod_first.get(r["session_id"])
                if first is not None and (r["ts"] or "") >= first:
                    continue
                uf = _usage_fields(r["usage"])
                cost = registry.estimate_cost(r["model"], uf["input_tokens"], uf["output_tokens"],
                                              uf["cache_write_5m"], uf["cache_write_1h"], uf["cache_read"])
                conn.execute(
                    """INSERT OR REPLACE INTO requests(request_key, session_id, agent_id, ts, model,
                       input_tokens, output_tokens, thinking_tokens, cache_write_5m, cache_write_1h, cache_read,
                       est_cost_usd, price_version, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'transcript')""",
                    (rid, r["session_id"], r["agent_id"], r["ts"], registry.normalize_model(r["model"]),
                     uf["input_tokens"], uf["output_tokens"], uf["thinking_tokens"], uf["cache_write_5m"],
                     uf["cache_write_1h"], uf["cache_read"], cost, registry.PRICE_DATE if cost is not None else None),
                )
                stats["requests"] += 1
            conn.execute("INSERT OR REPLACE INTO ingest_files VALUES (?,?,?,?)",
                         (str(path), st.st_mtime, st.st_size, db.now_iso()))
        stats["files"] += 1
    if own:
        conn.close()
    return stats

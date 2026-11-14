"""Waste Radar analyzer: tool use that fills context without adding information.

The database holds no per-tool data, so this analyzer reads Claude Code's transcript files
(`~/.claude/projects/**/*.jsonl`, read-only, last 30 days). Extracted events are cached per file
in `<mm home>/optimizer_cache/waste.json`, keyed by mtime and size, so a run only parses changed files.

Rules:

- repeated-reads  (per project) the same file read again in a session although it was not edited in between.
- big-results     (per tool)    single tool results above BIG_TOKENS: they stay in context for every later request.
- subagent-heavy               subagent requests are a large share of cost.

A tool result stays in context and is re-read from cache on every later request. Cost of an avoidable result:
tokens x (cache write price + later requests x cache read price). The count of later requests comes from the
`requests` table. Compaction shortens that carry, so every saving has a 0.5 haircut. Tokens are chars / 4.
"""

from __future__ import annotations

import bisect
import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .. import paths, queries, registry
from .base import Finding, analyzer

DAYS = 30
CHARS_PER_TOKEN = 4
BIG_TOKENS = 10_000            # one result above this is "big"
TRIM_TO_TOKENS = 2_000         # what a head/limit would keep
MIN_STORE_CHARS = 8_000        # smaller results are not cached
HAIRCUT = 0.5
MIN_SAVING_USD = 5.0
MIN_REPEATS = 3
SUBAGENT_SHARE = 0.15
MIN_SUBAGENT_USD = 20.0
SUBAGENT_REDUCTION = 0.2
CACHE_VERSION = 1
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

BIG_FIX = {
    "Bash": "Limit command output: pipe through `head -50` or `tail -50`, use `grep -c` for counts, or keep the "
            "rtk hook active so long output is filtered.",
    "Read": "Read part of the file: pass `offset` and `limit`, or search with Grep first and read only the lines you need.",
    "Grep": "Narrow the search: use `output_mode: files_with_matches` or `head_limit`, and a tighter glob.",
}
BIG_FIX_DEFAULT = "Ask this tool for less per call: a narrower query, a page size, or specific fields."


def _cache_path() -> Path:
    return paths.home() / "optimizer_cache" / "waste.json"


def _load_cache() -> dict[str, Any]:
    try:
        c = json.loads(_cache_path().read_text(encoding="utf-8"))
        return c if c.get("version") == CACHE_VERSION else {"version": CACHE_VERSION, "files": {}}
    except (OSError, ValueError):
        return {"version": CACHE_VERSION, "files": {}}


def _save_cache(cache: dict[str, Any]) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass  # the cache is an optimisation only


def _text_len(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    return 0


def parse_transcript(path: Path) -> dict[str, Any]:
    """Events of one transcript: reads, edits, big results, subagent spawns. Keeps nothing else."""
    sid = agent = cwd = None
    uses: dict[str, tuple[str, str]] = {}
    out: dict[str, Any] = {"reads": [], "edits": [], "big": [], "spawns": 0}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"tool_use' not in line and '"tool_result"' not in line:
                if cwd is None and '"cwd"' in line:
                    cwd = _first(line, "cwd")
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            sid = sid or d.get("sessionId")
            agent = agent or d.get("agentId")
            cwd = cwd or d.get("cwd")
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            ts = d.get("timestamp") or ""
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    name, inp = b.get("name", ""), b.get("input") or {}
                    target = inp.get("file_path") or inp.get("notebook_path") or ""
                    uses[b.get("id", "")] = (name, target)
                    if name in EDIT_TOOLS and target:
                        out["edits"].append([ts, target])
                    elif name in ("Agent", "Task"):
                        out["spawns"] += 1
                elif b.get("type") == "tool_result":
                    name, target = uses.get(b.get("tool_use_id", ""), ("", ""))
                    n = _text_len(b.get("content"))
                    if name == "Read" and target and not b.get("is_error"):
                        out["reads"].append([ts, target, n])
                    if n >= MIN_STORE_CHARS and name:
                        out["big"].append([ts, name, n, target[-80:]])
    out.update(sid=sid, agent=agent, cwd=cwd)
    return out


def _first(line: str, key: str) -> str | None:
    try:
        return json.loads(line).get(key)
    except ValueError:
        return None


def _files() -> Iterator[Path]:
    cutoff = time.time() - DAYS * 86400
    root = paths.claude_projects_dir()
    if not root.exists():
        return
    for p in root.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime >= cutoff:
                yield p
        except OSError:
            continue


def scan() -> list[dict[str, Any]]:
    """Parsed transcripts of the last 30 days; unchanged files come from the cache."""
    cache = _load_cache()
    files: dict[str, Any] = cache["files"]
    seen: dict[str, Any] = {}
    changed = False
    for p in _files():
        try:
            st = p.stat()
        except OSError:
            continue
        key = str(p)
        old = files.get(key)
        if old and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
            seen[key] = old
            continue
        seen[key] = {"mtime": st.st_mtime, "size": st.st_size, "data": parse_transcript(p)}
        changed = True
    if changed or len(seen) != len(files):
        cache["files"] = seen
        _save_cache(cache)
    return [v["data"] for v in seen.values()]


# ---------- cost of carrying a result ----------

class _Carry:
    """Cost of keeping `tokens` in context from `ts` on, from the requests that followed in that session."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.by: dict[tuple[str, str], tuple[list[str], list[str | None]]] = defaultdict(lambda: ([], []))
        since = queries.since(DAYS)
        for r in conn.execute("SELECT session_id, agent_id, ts, model FROM requests WHERE ts >= ? ORDER BY ts", (since,)):
            tss, models = self.by[(r["session_id"] or "", r["agent_id"] or "")]
            tss.append(r["ts"])
            models.append(r["model"])

    def cost(self, sid: str | None, agent: str | None, ts: str, tokens: float) -> float | None:
        tss, models = self.by.get((sid or "", agent or ""), ([], []))
        i = bisect.bisect_left(tss, ts[:19])
        if i >= len(tss):
            return None
        price = registry.PRICES.get(registry.normalize_model(models[i]))
        if price is None:
            return None
        later = len(tss) - i
        return tokens * (price[1] + later * price[3]) / 1e6 * HAIRCUT


def _ts_ok(ts: str, since: str) -> bool:
    return bool(ts) and ts[:19] >= since[:19]


@analyzer("waste")
def waste_findings(conn: sqlite3.Connection) -> list[Finding]:
    data = scan()
    since = queries.since(DAYS)
    carry = _Carry(conn)
    found: list[Finding] = []
    found += _repeated_reads(data, carry, since)
    found += _big_results(data, carry, since)
    found += _subagents(conn, data)
    return found


def _repeated_reads(data: list[dict[str, Any]], carry: _Carry, since: str) -> list[Finding]:
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "cost": 0.0, "tokens": 0, "files": defaultdict(lambda: [0, 0.0])})
    for t in data:
        edits: dict[str, list[str]] = defaultdict(list)
        for ts, target in t["edits"]:
            edits[target].append(ts)
        last: dict[str, str] = {}
        project = queries.project_name(t["cwd"], None)
        for ts, target, chars in sorted(t["reads"]):
            prev = last.get(target)
            last[target] = ts
            if prev is None or not _ts_ok(ts, since):
                continue
            if any(prev <= e <= ts for e in edits.get(target, ())):
                continue  # the file changed in between: reading it again is needed
            tokens = chars / CHARS_PER_TOKEN
            c = carry.cost(t["sid"], t["agent"], ts, tokens)
            if c is None:
                continue
            p = per[project]
            p["n"] += 1
            p["cost"] += c
            p["tokens"] += int(tokens)
            f = p["files"][target]
            f[0] += 1
            f[1] += c
    out = []
    for name, p in per.items():
        if p["n"] < MIN_REPEATS or p["cost"] < MIN_SAVING_USD:
            continue
        top = sorted(p["files"].items(), key=lambda kv: -kv[1][1])[:5]
        out.append(Finding(
            tool="waste", rule="repeated-reads", scope=name, severity="high" if p["cost"] > 50 else "medium",
            confidence="medium", title=f"Files read again in {name}",
            evidence={"summary": f"{p['n']:,} repeat reads of unchanged files, about {p['tokens'] / 1e6:.1f}M tokens "
                                 "added to context",
                      "repeat_reads": p["n"], "tokens": p["tokens"],
                      "rule": "repeat = same file read again in a session with no edit in between; cost = tokens x "
                              "(write price + later requests x read price) x 0.5 (compaction shortens the carry)",
                      "examples": [{"file": f, "repeats": n, "usd": round(c, 2)} for f, (n, c) in top]},
            fix="Read a file once and work from it. Use `offset` and `limit` for parts, and add to CLAUDE.md: "
                "\"Do not re-read a file already read this session unless it changed.\"",
            est_saving_usd=round(p["cost"], 2), est_saving_tokens=p["tokens"]))
    return out


def _big_results(data: list[dict[str, Any]], carry: _Carry, since: str) -> list[Finding]:
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "cost": 0.0, "tokens": 0, "items": []})
    for t in data:
        for ts, tool, chars, label in t["big"]:
            tokens = chars / CHARS_PER_TOKEN
            if tokens < BIG_TOKENS or not _ts_ok(ts, since):
                continue
            excess = tokens - TRIM_TO_TOKENS
            c = carry.cost(t["sid"], t["agent"], ts, excess)
            if c is None:
                continue
            p = per[tool]
            p["n"] += 1
            p["cost"] += c
            p["tokens"] += int(excess)
            p["items"].append((c, ts, int(tokens), label))
    out = []
    for tool, p in per.items():
        if p["cost"] < MIN_SAVING_USD:
            continue
        top = sorted(p["items"], reverse=True)[:5]
        short = tool if len(tool) <= 40 else tool[:37] + "..."
        out.append(Finding(
            tool="waste", rule="big-results", scope=tool, severity="high" if p["cost"] > 50 else "medium",
            confidence="medium", title=f"Oversized {short} results",
            evidence={"summary": f"{p['n']:,} results above {BIG_TOKENS // 1000}K tokens, "
                                 f"{p['tokens'] / 1e6:.1f}M tokens beyond a {TRIM_TO_TOKENS // 1000}K trim",
                      "results": p["n"], "tokens": p["tokens"],
                      "rule": f"cost = tokens above {TRIM_TO_TOKENS // 1000}K x (write price + later requests x read "
                              "price) x 0.5",
                      "examples": [{"ts": ts, "tokens": tk, "target": lb, "usd": round(c, 2)} for c, ts, tk, lb in top]},
            fix=BIG_FIX.get(tool, BIG_FIX_DEFAULT),
            est_saving_usd=round(p["cost"], 2), est_saving_tokens=p["tokens"]))
    return out


def _subagents(conn: sqlite3.Connection, data: list[dict[str, Any]]) -> list[Finding]:
    since = queries.since(DAYS)
    row = conn.execute(
        """SELECT SUM(est_cost_usd) AS total,
                  SUM(CASE WHEN agent_id IS NOT NULL THEN est_cost_usd ELSE 0 END) AS sub,
                  COUNT(DISTINCT CASE WHEN agent_id IS NOT NULL THEN agent_id END) AS agents
           FROM requests WHERE ts >= ?""", (since,)).fetchone()
    total, sub = row["total"] or 0.0, row["sub"] or 0.0
    if not total or sub < MIN_SUBAGENT_USD or sub / total < SUBAGENT_SHARE:
        return []
    spawns = sum(t["spawns"] for t in data)
    return [Finding(
        tool="waste", rule="subagent-heavy", severity="medium", confidence="low",
        title="Subagents are a large share of cost",
        evidence={"summary": f"subagent requests are {sub / total:.0%} of cost (${sub:,.0f}) across {row['agents']:,} agents",
                  "subagent_cost_usd": round(sub, 2), "share": round(sub / total, 3), "agents": row["agents"],
                  "spawns_seen": spawns,
                  "rule": f"saving = subagent cost x {SUBAGENT_REDUCTION:g} (assumed cut from fewer, narrower spawns)",
                  "examples": []},
        fix="Use a subagent for broad searches only. Give it a narrow prompt and a result size limit; do small "
            "lookups inline. A cheaper model for subagents is covered by Model Manager routing.",
        est_saving_usd=round(sub * SUBAGENT_REDUCTION, 2))]

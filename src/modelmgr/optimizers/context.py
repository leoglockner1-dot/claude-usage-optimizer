"""Context & Cache analyzer: where context size and cache behaviour cost money.

Three rules, all read from the `requests` table of the last 30 days:

- long-context   (per project) requests whose context is above CTX_LIMIT. Every extra token is re-read
                 from cache on every following request, so the cost grows with each turn.
- low-cache-read (per project) a project whose cache-read share of input-side tokens is low.
- cache-gap      requests that rewrote most of the context after a pause longer than the cache TTL.

Savings are estimates per 30 days. They assume Leo changes the habit; nothing here edits a setting.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from .. import queries, registry
from .base import Finding, analyzer

DAYS = 30
CTX_LIMIT = 200_000            # context above this is "long"
COMPACT_HAIRCUT = 0.5          # compacting is not free: a summary has to be read and written once
MIN_SAVING_USD = 5.0           # smaller findings are noise
LOW_READ_SHARE = 0.90          # cache-read share of input-side tokens below this is flagged
MIN_INPUT_TOKENS = 5_000_000   # ignore projects with too little traffic to judge
TTL_5M, TTL_1H = 300, 3600     # cache lifetimes in seconds
MIN_GAP_MISSES = 5
MIN_GAP_SAVING_USD = 2.0


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.request_key, r.session_id, r.agent_id, r.ts, r.model, r.input_tokens, r.output_tokens,
                  r.cache_write_5m, r.cache_write_1h, r.cache_read, r.est_cost_usd, s.cwd, s.project
           FROM requests r LEFT JOIN sessions s ON s.session_id = r.session_id
           WHERE r.ts >= ? ORDER BY r.session_id, r.agent_id, r.ts""", (queries.since(DAYS),)).fetchall()


def _fmt_k(n: float) -> str:
    return f"{n / 1000:.0f}K"


@analyzer("context")
def context_findings(conn: sqlite3.Connection) -> list[Finding]:
    rows = _rows(conn)
    out: list[Finding] = []
    out += _long_context(rows)
    out += _low_cache_read(rows)
    out += _cache_gaps(rows)
    return out


def _long_context(rows: list[sqlite3.Row]) -> list[Finding]:
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "n": 0, "over": 0, "cost": 0.0, "cost_over": 0.0, "excess": 0.0, "excess_tokens": 0,
        "sessions": defaultdict(float)})
    for r in rows:
        ctx = queries.context_tokens(r)
        split = queries.cost_by_kind(r)
        if ctx is None or split is None or r["est_cost_usd"] is None:
            continue
        p = per[queries.project_name(r["cwd"], r["project"])]
        p["n"] += 1
        p["cost"] += r["est_cost_usd"]
        if ctx <= CTX_LIMIT:
            continue
        frac = (ctx - CTX_LIMIT) / ctx
        # input-side cost scales with context; output does not
        excess = (split["input"] + split["cache_read"] + split["cache_write"]) * frac
        p["over"] += 1
        p["cost_over"] += r["est_cost_usd"]
        p["excess"] += excess
        p["excess_tokens"] += ctx - CTX_LIMIT
        if r["session_id"]:
            p["sessions"][r["session_id"]] += excess
    found = []
    for name, p in per.items():
        saving = p["excess"] * COMPACT_HAIRCUT
        if p["over"] == 0 or saving < MIN_SAVING_USD:
            continue
        share = p["cost_over"] / p["cost"] if p["cost"] else 0.0
        sev = "high" if share > 0.4 else "medium" if share > 0.15 else "low"
        top = sorted(p["sessions"].items(), key=lambda kv: -kv[1])[:5]
        found.append(Finding(
            tool="context", rule="long-context", scope=name, severity=sev, confidence="medium",
            title=f"Long contexts in {name}",
            evidence={
                "summary": f"{p['over']:,} of {p['n']:,} requests above {_fmt_k(CTX_LIMIT)} context, "
                           f"{share:.0%} of this project's cost",
                "requests_over": p["over"], "requests": p["n"], "cost_share_over": round(share, 3),
                "cost_over_usd": round(p["cost_over"], 2), "tokens_over_limit": p["excess_tokens"],
                "limit": CTX_LIMIT,
                "rule": f"saving = input-side cost of tokens above {_fmt_k(CTX_LIMIT)} x {COMPACT_HAIRCUT:g} "
                        "(compacting is not free)",
                "examples": [{"session": s, "excess_usd": round(v, 2)} for s, v in top],
            },
            fix=f"Compact earlier in {name}: run /compact when a task is done and before context passes "
                f"{_fmt_k(CTX_LIMIT)}, or start a new session per task. Lowering `autoCompactWindow` in "
                "settings makes this automatic.",
            est_saving_usd=round(saving, 2), est_saving_tokens=int(p["excess_tokens"] * COMPACT_HAIRCUT)))
    return found


def _low_cache_read(rows: list[sqlite3.Row]) -> list[Finding]:
    per: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0, 0.0, 0.0])  # input, write, read, in_price, rd_price
    for r in rows:
        if r["input_tokens"] is None:
            continue
        mid = registry.normalize_model(r["model"])
        price = registry.PRICES.get(mid)
        if price is None:
            continue
        a = per[queries.project_name(r["cwd"], r["project"])]
        w = (r["cache_write_5m"] or 0) + (r["cache_write_1h"] or 0)
        a[0] += r["input_tokens"]
        a[1] += w
        a[2] += r["cache_read"] or 0
        # uncached input and cache writes cost more than reads; sum the premium per request
        a[3] += (r["input_tokens"] * (price[0] - price[3]) + (r["cache_write_5m"] or 0) * (price[1] - price[3])
                 + (r["cache_write_1h"] or 0) * (price[2] - price[3])) / 1e6
    found = []
    for name, (inp, write, read, premium, _) in per.items():
        total = inp + write + read
        if total < MIN_INPUT_TOKENS:
            continue
        share = read / total
        if share >= LOW_READ_SHARE:
            continue
        # moving the share up to the threshold turns that much of the premium into read price
        saving = premium * (LOW_READ_SHARE - share) / (1 - share) if share < 1 else 0.0
        if saving < MIN_SAVING_USD:
            continue
        found.append(Finding(
            tool="context", rule="low-cache-read", scope=name, severity="medium", confidence="low",
            title=f"Low cache reuse in {name}",
            evidence={"summary": f"cache read is {share:.0%} of input-side tokens, below {LOW_READ_SHARE:.0%}",
                      "cache_read_share": round(share, 3), "input_side_tokens": int(total),
                      "examples": []},
            fix="Keep the start of the conversation stable: avoid editing CLAUDE.md or switching MCP servers and "
                "models mid-session, since each change rewrites the cached prefix.",
            est_saving_usd=round(saving, 2)))
    return found


def _cache_gaps(rows: list[sqlite3.Row]) -> list[Finding]:
    misses, excess = [], 0.0
    prev = None
    for r in rows:
        key = (r["session_id"], r["agent_id"])
        if prev is not None and prev[0] == key:
            ttl = TTL_1H if prev[2] else TTL_5M
            gap = (_ts(r["ts"]) - _ts(prev[1])).total_seconds()
            ctx = queries.context_tokens(r) or 0
            w = (r["cache_write_5m"] or 0) + (r["cache_write_1h"] or 0)
            price = registry.PRICES.get(registry.normalize_model(r["model"]))
            if gap > ttl and ctx and w > 0.5 * ctx and price is not None:
                cost = (r["cache_write_5m"] or 0) * (price[1] - price[3]) + (r["cache_write_1h"] or 0) * (price[2] - price[3])
                excess += cost / 1e6
                misses.append((cost / 1e6, r["session_id"], r["ts"], int(gap)))
        prev = (key, r["ts"], (r["cache_write_1h"] or 0) > 0)
    if len(misses) < MIN_GAP_MISSES or excess < MIN_GAP_SAVING_USD:
        return []
    top = sorted(misses, reverse=True)[:5]
    return [Finding(
        tool="context", rule="cache-gap", severity="low", confidence="medium",
        title="Context rewritten after pauses",
        evidence={"summary": f"{len(misses)} requests rewrote most of the context after a pause longer than the cache lifetime",
                  "misses": len(misses), "excess_usd": round(excess, 2),
                  "rule": "excess = tokens written x (write price - read price); lifetime 5 min, or 1 h if the previous request wrote a 1 h cache",
                  "examples": [{"session": s, "ts": t, "gap_s": g, "excess_usd": round(c, 2)} for c, s, t, g in top]},
        fix="Finish a burst of work before a long break, or compact before stepping away: a pause longer than the cache "
            "lifetime makes the next request pay to write the whole context again.",
        est_saving_usd=round(excess * 0.5, 2))]

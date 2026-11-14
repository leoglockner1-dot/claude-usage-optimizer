"""Output Lab analyzer: output and thinking tokens, and tools that make replies shorter.

Rules, read from `requests` of the last 30 days:

- high-output     output tokens are a large share of cost: reply-length habits matter.
- thinking-heavy  thinking is a large share of output tokens: lower effort on routine work.
- recommend:<id>  one finding per catalog entry that is not active yet, only while a rule above fires.
                  Recommendations carry no saving figure: their effects overlap, so adding them up would lie.
                  When Leo marks one applied and 7 days have passed, the finding shows the measured change in
                  output tokens per request before and after.

Savings are estimates per 30 days with the assumption written in the evidence. Thinking tokens are
counted inside output tokens (checked: never larger than output on any request).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import config, paths, queries, registry
from .base import Finding, analyzer

DAYS = 30
MIN_OUTPUT_SHARE = 0.10         # output cost share of total cost that triggers high-output
HIGH_OUTPUT_SHARE = 0.25
STYLE_REDUCTION = 0.25          # assumed cut in output tokens from a terse-reply habit
MIN_THINK_SHARE = 0.25          # thinking share of output tokens that triggers thinking-heavy
EFFORT_REDUCTION = 0.30         # assumed cut in thinking tokens from lower effort
MIN_SAVING_USD = 5.0
MEASURE_DAYS = 7                # days on each side of "applied"
MIN_MEASURE_REQUESTS = 50
CATALOG = Path(__file__).with_name("catalog.json")


def catalog() -> list[dict[str, Any]]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


# ---------- detection: reads Claude Code's config, never writes ----------

def _settings() -> dict[str, Any]:
    try:
        return json.loads((paths.claude_home() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def detect(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> str:
    """'active', 'installed' (present but switched off) or 'missing'."""
    st = _settings() if settings is None else settings
    d = entry["detect"]
    home = paths.claude_home()
    if "plugin" in d:
        enabled = any(k.split("@")[0] == d["plugin"] and v for k, v in (st.get("enabledPlugins") or {}).items())
        if enabled:
            return "active"
        return "installed" if (home / "plugins" / "cache" / d["plugin"]).exists() else "missing"
    if "file_exists" in d:
        return "active" if any((home / n).exists() for n in d["file_exists"]) else "missing"
    if "which" in d:
        hooks = json.dumps(st.get("hooks") or {})
        on_path = shutil.which(d["which"]) is not None
        if on_path and d.get("hook_mentions", "") in hooks:
            return "active"
        return "installed" if on_path else "missing"
    if "setting_set" in d:
        return "active" if st.get(d["setting_set"]) else "missing"
    if "setting_in" in d or "mm_mode" in d:
        for key, vals in (d.get("setting_in") or {}).items():
            if st.get(key) in vals:
                return "active"
        if d.get("mm_mode") and config.load().get("mode") == d["mm_mode"]:
            return "active"
        return "missing"
    if "claude_md_matches" in d:
        try:
            text = (home / "CLAUDE.md").read_text(encoding="utf-8")
        except OSError:
            return "missing"
        return "active" if re.search(d["claude_md_matches"], text, re.I) else "missing"
    return "missing"


# ---------- data ----------

def _rows(conn: sqlite3.Connection, since: str, until: str | None = None) -> list[sqlite3.Row]:
    q = """SELECT session_id, ts, model, input_tokens, output_tokens, thinking_tokens,
                  cache_write_5m, cache_write_1h, cache_read, est_cost_usd
           FROM requests WHERE ts >= ?"""
    args: list[Any] = [since]
    if until:
        q += " AND ts < ?"
        args.append(until)
    return conn.execute(q, args).fetchall()


def _out_price(model: str | None) -> float | None:
    p = registry.PRICES.get(registry.normalize_model(model))
    return None if p is None else p[4]


@analyzer("output")
def output_findings(conn: sqlite3.Connection) -> list[Finding]:
    rows = _rows(conn, queries.since(DAYS))
    total = out_cost = think_cost = 0.0
    out_tok = think_tok = think_out_tok = priced = 0
    sessions: dict[str, int] = defaultdict(int)
    for r in rows:
        split = queries.cost_by_kind(r)
        if split is None or r["est_cost_usd"] is None:
            continue
        priced += 1
        total += r["est_cost_usd"]
        out_cost += split["output"]
        out_tok += r["output_tokens"] or 0
        if r["session_id"]:
            sessions[r["session_id"]] += r["output_tokens"] or 0
        if r["thinking_tokens"] is not None and r["output_tokens"]:
            think_tok += r["thinking_tokens"]
            think_out_tok += r["output_tokens"]
            think_cost += split["output"] * r["thinking_tokens"] / r["output_tokens"]
    if not priced or not total:
        return []
    found: list[Finding] = []
    share = out_cost / total
    top = [{"session": s, "output_tokens": n} for s, n in sorted(sessions.items(), key=lambda kv: -kv[1])[:5]]
    base_ev = {"output_tokens": out_tok, "output_cost_usd": round(out_cost, 2), "requests": priced,
               "avg_output_per_request": round(out_tok / priced)}
    if share >= MIN_OUTPUT_SHARE and out_cost * STYLE_REDUCTION >= MIN_SAVING_USD:
        found.append(Finding(
            tool="output", rule="high-output", severity="high" if share > HIGH_OUTPUT_SHARE else "medium",
            confidence="low",
            title="Output tokens are a large part of the cost",
            evidence={**base_ev, "output_cost_share": round(share, 3),
                      "summary": f"{out_tok / 1e6:.1f}M output tokens, {share:.0%} of cost, "
                                 f"{round(out_tok / priced):,} per request",
                      "rule": f"saving = output cost x {STYLE_REDUCTION:g} (assumed cut from a terse-reply habit)",
                      "examples": top},
            fix="Ask for short answers: answer first, no recap, no closing offer. A terse-reply plugin or output "
                "style does this for every session; see the recommendations below.",
            est_saving_usd=round(out_cost * STYLE_REDUCTION, 2),
            est_saving_tokens=int(out_tok * STYLE_REDUCTION)))
    t_share = think_tok / think_out_tok if think_out_tok else 0.0
    if think_out_tok and t_share >= MIN_THINK_SHARE and think_cost * EFFORT_REDUCTION >= MIN_SAVING_USD:
        found.append(Finding(
            tool="output", rule="thinking-heavy", severity="medium", confidence="low",
            title="Thinking is a large share of output",
            evidence={"summary": f"{think_tok / 1e6:.1f}M thinking tokens, {t_share:.0%} of output tokens "
                                 "where thinking was measured",
                      "thinking_tokens": think_tok, "thinking_share": round(t_share, 3),
                      "thinking_cost_usd": round(think_cost, 2),
                      "rule": f"saving = thinking cost x {EFFORT_REDUCTION:g} (assumed cut from lower effort); "
                              "thinking tokens are part of output tokens",
                      "examples": top},
            fix="Use a lower effort for routine work. Model Manager mode auto picks effort per task; "
                "keep high effort for hard debugging.",
            est_saving_usd=round(think_cost * EFFORT_REDUCTION, 2),
            est_saving_tokens=int(think_tok * EFFORT_REDUCTION)))
    found += _recommendations(conn, found, base_ev)
    return found


# ---------- recommendations ----------

def _status_of(conn: sqlite3.Connection, fid: str) -> tuple[str | None, str | None]:
    r = conn.execute("SELECT status, status_ts FROM findings WHERE id = ?", (fid,)).fetchone()
    return (r["status"], r["status_ts"]) if r else (None, None)


def measure(conn: sqlite3.Connection, applied_at: str) -> dict[str, Any] | None:
    """Output tokens per request in the 7 days before and after `applied_at`; None until both sides have data."""
    at = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
    if datetime.now(at.tzinfo) < at + timedelta(days=MEASURE_DAYS):
        return None
    fmt = lambda d: d.isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    before = _rows(conn, fmt(at - timedelta(days=MEASURE_DAYS)), fmt(at))
    after = _rows(conn, fmt(at), fmt(at + timedelta(days=MEASURE_DAYS)))
    if len(before) < MIN_MEASURE_REQUESTS or len(after) < MIN_MEASURE_REQUESTS:
        return None
    avg = lambda rs: sum(r["output_tokens"] or 0 for r in rs) / len(rs)  # noqa: E731
    b, a = avg(before), avg(after)
    return {"before_avg": round(b), "after_avg": round(a), "change": round((a - b) / b, 3) if b else None,
            "requests_before": len(before), "requests_after": len(after)}


def _recommendations(conn: sqlite3.Connection, rules: list[Finding], base_ev: dict[str, Any]) -> list[Finding]:
    st = _settings()
    entries = catalog()
    state = {e["id"]: detect(e, st) for e in entries}
    out = []
    for e in entries:
        fid = Finding(tool="output", rule="recommend", scope=e["id"], title="", evidence={"x": 1}, fix="").id
        status, status_ts = _status_of(conn, fid)
        measured = measure(conn, status_ts) if status == "applied" and status_ts else None
        covered = any(state.get(c) == "active" for c in e.get("covers", []))
        want = bool(rules) and state[e["id"]] != "active" and not covered
        auto = False
        if not want and status != "applied":
            # Leo had this open and the change is now in place: close it as applied
            if status in ("new", "seen") and state[e["id"]] == "active":
                auto = True
            else:
                continue
        verb = "Enable" if state[e["id"]] == "installed" else "Try"
        summary = f"reduces {e['reduces']}: {e['effect']}"
        ev: dict[str, Any] = {"summary": summary, "catalog_id": e["id"], "state": state[e["id"]],
                              "install": e["install"], "tradeoff": e["tradeoff"], "examples": [],
                              "output_tokens_per_request": base_ev["avg_output_per_request"]}
        if measured:
            ev["measured"] = measured
            ev["summary"] = (f"measured: {measured['before_avg']:,} to {measured['after_avg']:,} output tokens per "
                             f"request ({measured['change']:+.0%}) in the 7 days before and after you applied it")
        out.append(Finding(
            tool="output", rule="recommend", scope=e["id"], severity="low", confidence="medium",
            auto_applied=auto,
            title=f"{'Active' if auto else verb}: {e['name']}", evidence=ev,
            fix=f"{e['install']} Trade-off: {e['tradeoff']}"))
    return out

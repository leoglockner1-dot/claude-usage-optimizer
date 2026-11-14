"""Finding type, analyzer registry and the run that stores findings.

An analyzer is a function (conn) -> list[Finding], registered with @analyzer("tool"). It only reads.
run_all() calls every analyzer, upserts its findings by stable id and marks findings that were not
produced again as inactive. Leo's status (seen / applied / ignored) is kept across runs.

Savings are always estimates: a changed habit changes the tokens. A finding without evidence rows is
a bug, so run_all() drops it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import db

STATUSES = ("new", "seen", "applied", "ignored")
SEVERITIES = ("high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")

# the optimizer tools; Model Manager is a tool too but lives in its own pages
TOOLS: dict[str, dict[str, str]] = {
    "context": {"name": "Context & Cache", "about": "Context size, compact timing, cache efficiency"},
    "output": {"name": "Output Lab", "about": "Output and thinking tokens, reply length, tools that shorten replies"},
    "waste": {"name": "Waste Radar", "about": "Repeated reads, oversized tool results, subagent overuse"},
}


@dataclass
class Finding:
    tool: str
    rule: str
    title: str
    evidence: dict[str, Any]          # numbers plus "examples": a few request/session ids
    fix: str                          # ready-to-use text: CLAUDE.md line, setting, habit
    scope: str = ""                   # e.g. a project name; part of the stable id
    severity: str = "medium"
    confidence: str = "medium"
    est_saving_usd: float | None = None     # per 30 days, API-equivalent estimate
    est_saving_tokens: int | None = None    # per 30 days
    extra: dict[str, Any] = field(default_factory=dict)
    auto_applied: bool = False              # the change it recommends is already in place

    @property
    def id(self) -> str:
        return ":".join(x for x in (self.tool, self.rule, self.scope) if x)

    def check(self) -> None:
        if self.tool not in TOOLS:
            raise ValueError(f"unknown tool {self.tool}")
        if self.severity not in SEVERITIES or self.confidence not in CONFIDENCES:
            raise ValueError(f"{self.id}: bad severity or confidence")
        if not self.evidence:
            raise ValueError(f"{self.id}: a finding needs evidence")


Analyzer = Callable[[sqlite3.Connection], list[Finding]]
_REGISTRY: dict[str, list[Analyzer]] = {}


def analyzer(tool: str) -> Callable[[Analyzer], Analyzer]:
    if tool not in TOOLS:
        raise ValueError(f"unknown tool {tool}")

    def wrap(fn: Analyzer) -> Analyzer:
        _REGISTRY.setdefault(tool, []).append(fn)
        return fn
    return wrap


def registered() -> dict[str, list[Analyzer]]:
    return {t: list(fns) for t, fns in _REGISTRY.items()}


def _load_builtin() -> None:
    # analyzer modules register themselves on import; phase 1 ships none yet
    import importlib
    for name in ("context", "output", "waste"):
        try:
            importlib.import_module(f"{__package__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{__package__}.{name}":
                raise


def run_all(conn: sqlite3.Connection, analyzers: dict[str, list[Analyzer]] | None = None) -> dict[str, Any]:
    """Run analyzers per tool; a tool whose analyzer fails keeps its previous findings."""
    if analyzers is None:
        _load_builtin()
        analyzers = registered()
    now = db.now_iso()
    out: dict[str, Any] = {"ts": now, "tools": {}}
    for tool, fns in analyzers.items():
        found: dict[str, Finding] = {}
        error = None
        try:
            for fn in fns:
                for f in fn(conn):
                    f.check()
                    found[f.id] = f
        except Exception as exc:  # noqa: BLE001 - one broken analyzer must not stop the others
            error = f"{type(exc).__name__}: {exc}"
        with conn:
            if error is None:
                conn.execute("UPDATE findings SET active = 0 WHERE tool = ?", (tool,))
                for f in found.values():
                    _upsert(conn, f, now)
            conn.execute("INSERT INTO optimizer_runs VALUES (?,?,?,?)",
                         (now, tool, None if error else len(found), error))
        out["tools"][tool] = {"findings": len(found), "error": error}
    record_health(conn, now)
    return out


# a finding Leo ignored or applied comes back only when its estimated saving grew by this factor (and $)
REOPEN_FACTOR = 1.5
REOPEN_MIN_USD = 5.0


def record_health(conn: sqlite3.Connection, now: str) -> None:
    """Store today's Usage Health score for the Home sparkline (one row per day)."""
    from .. import queries  # late: queries imports this package
    spend = queries.overview(conn, 30)["est_cost_usd"]
    h = queries.health(conn, spend)
    if h["score"] is None:
        return
    with conn:
        conn.execute("INSERT OR REPLACE INTO health_history VALUES (?,?,?,?)",
                     (now[:10], h["score"], h["avoidable_usd"], h["spend_usd"]))


def _upsert(conn: sqlite3.Connection, f: Finding, now: str) -> None:
    prev = conn.execute("SELECT status, status_saving FROM findings WHERE id = ?", (f.id,)).fetchone()
    ev = json.dumps({**f.evidence, **({"extra": f.extra} if f.extra else {})}, separators=(",", ":"), default=str)
    conn.execute(
        """INSERT INTO findings (id, tool, rule, title, severity, confidence, evidence_json, fix,
                                 est_saving_usd, est_saving_tokens, status, active, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'new', 1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET title = excluded.title, severity = excluded.severity,
             confidence = excluded.confidence, evidence_json = excluded.evidence_json, fix = excluded.fix,
             est_saving_usd = excluded.est_saving_usd, est_saving_tokens = excluded.est_saving_tokens,
             active = 1, last_seen = excluded.last_seen""",
        (f.id, f.tool, f.rule, f.title, f.severity, f.confidence, ev, f.fix,
         f.est_saving_usd, f.est_saving_tokens, now, now),
    )
    if prev is None:
        status = "new"
    else:
        status = prev["status"]
    if f.auto_applied and status in ("new", "seen"):
        conn.execute("UPDATE findings SET status = 'applied', status_ts = ?, status_saving = NULL WHERE id = ?",
                     (now, f.id))
    elif status in ("ignored", "applied") and prev["status_saving"] is not None and f.est_saving_usd is not None:
        base = prev["status_saving"]
        if f.est_saving_usd >= base * REOPEN_FACTOR and f.est_saving_usd - base >= REOPEN_MIN_USD:
            conn.execute("UPDATE findings SET status = 'new', status_ts = ?, status_saving = NULL WHERE id = ?",
                         (now, f.id))


def set_status(conn: sqlite3.Connection, finding_id: str, status: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    with conn:
        cur = conn.execute("UPDATE findings SET status = ?, status_ts = ?, status_saving = est_saving_usd WHERE id = ?",
                           (status, db.now_iso(), finding_id))
    if cur.rowcount == 0:
        raise KeyError(finding_id)
    return {"id": finding_id, "status": status}

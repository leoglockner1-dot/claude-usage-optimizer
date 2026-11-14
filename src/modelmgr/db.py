"""SQLite performance database. NULL always means 'not measured', never zero."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import paths, registry

SCHEMA_VERSION = 2  # 2: findings table (Usage Optimizer)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS models (
  model_id TEXT PRIMARY KEY, alias TEXT, family TEXT, tier INTEGER,
  context_window INTEGER, effort_levels TEXT, available INTEGER DEFAULT 1,
  first_seen TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS prices (
  model_id TEXT, valid_from TEXT, source TEXT,
  input REAL, cache_write_5m REAL, cache_write_1h REAL, cache_read REAL, output REAL,
  long_ctx_threshold INTEGER, long_ctx_multiplier REAL,
  PRIMARY KEY (model_id, valid_from)
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, cwd TEXT, project TEXT, surface TEXT,
  started_at TEXT, ended_at TEXT, cc_version TEXT, default_model TEXT, source TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, session_id TEXT, started_at TEXT, closed_at TEXT,
  prompt_excerpt TEXT, prompt_hash TEXT,
  category TEXT, complexity INTEGER, risk TEXT, flags TEXT, class_confidence REAL,
  outcome TEXT, outcome_source TEXT,
  turns INTEGER, escalations INTEGER, retries INTEGER,
  user_override TEXT, exploration INTEGER DEFAULT 0, cwd TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_cat ON tasks(category, complexity);

CREATE TABLE IF NOT EXISTS routes (
  route_id INTEGER PRIMARY KEY, task_id TEXT, turn_id TEXT, agent_id TEXT, ts TEXT,
  mode TEXT, policy_version INTEGER, session_model TEXT,
  chosen_model TEXT, chosen_effort TEXT, applied INTEGER, reason TEXT, candidates TEXT,
  UNIQUE(turn_id, agent_id)
);
CREATE INDEX IF NOT EXISTS ix_routes_task ON routes(task_id);

CREATE TABLE IF NOT EXISTS requests (
  request_key TEXT PRIMARY KEY, task_id TEXT, session_id TEXT, turn_id TEXT, agent_id TEXT, ts TEXT,
  model TEXT, effort TEXT,
  input_tokens INTEGER, output_tokens INTEGER, thinking_tokens INTEGER,
  cache_write_5m INTEGER, cache_write_1h INTEGER, cache_read INTEGER,
  est_cost_usd REAL, price_version TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS ix_req_task ON requests(task_id);
CREATE INDEX IF NOT EXISTS ix_req_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS ix_req_ts ON requests(ts);

CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY, task_id TEXT, session_id TEXT, agent_id TEXT,
  started_at TEXT, duration_ms INTEGER, aborted INTEGER,
  tool_calls INTEGER, tool_errors INTEGER,
  tests_run INTEGER, tests_failed INTEGER, tests_passed INTEGER, builds_failed INTEGER,
  quota_5h REAL, quota_7d REAL
);
CREATE INDEX IF NOT EXISTS ix_turns_task ON turns(task_id);

CREATE TABLE IF NOT EXISTS escalations (
  task_id TEXT, turn_id TEXT, ts TEXT, from_model TEXT, from_effort TEXT,
  to_model TEXT, to_effort TEXT, signals TEXT
);

CREATE TABLE IF NOT EXISTS feedback (task_id TEXT, ts TEXT, kind TEXT, note TEXT);

-- Leo's verdict on a routing decision, set later in the dashboard. Input for the learning engine.
CREATE TABLE IF NOT EXISTS route_grades (
  route_id INTEGER PRIMARY KEY, ts TEXT, verdict TEXT, better TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS spawns (
  ts TEXT, session_id TEXT, task_id TEXT, agent_type TEXT,
  requested_model TEXT, chosen_model TEXT, applied INTEGER, reason TEXT
);

CREATE TABLE IF NOT EXISTS policies (
  version INTEGER PRIMARY KEY, created_at TEXT, active INTEGER, changes TEXT, file TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_runs (
  run_id TEXT, case_id TEXT, model TEXT, effort TEXT, ts TEXT,
  passed INTEGER, score REAL, input_tokens INTEGER, output_tokens INTEGER,
  cache_read INTEGER, cache_write INTEGER, est_cost_usd REAL, duration_ms INTEGER,
  retries INTEGER, notes TEXT
);

-- Usage Optimizer: one row per finding, keyed by a stable id (tool:rule[:scope]) so status survives reruns.
-- active = produced by the latest run; status is Leo's: new / seen / applied / ignored.
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, tool TEXT NOT NULL, rule TEXT NOT NULL, title TEXT, severity TEXT, confidence TEXT,
  evidence_json TEXT, fix TEXT, est_saving_usd REAL, est_saving_tokens INTEGER,
  status TEXT NOT NULL DEFAULT 'new', active INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT, last_seen TEXT, status_ts TEXT
);
CREATE INDEX IF NOT EXISTS ix_findings_tool ON findings(tool, active);

CREATE TABLE IF NOT EXISTS optimizer_runs (ts TEXT, tool TEXT, findings INTEGER, error TEXT);

CREATE TABLE IF NOT EXISTS ingest_files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, ts TEXT);

CREATE VIEW IF NOT EXISTS v_task_cost AS
  SELECT t.task_id, t.category, t.complexity, t.outcome,
         COUNT(r.request_key) AS requests,
         SUM(r.est_cost_usd) AS est_cost_usd,
         SUM(r.input_tokens + COALESCE(r.cache_write_5m,0) + COALESCE(r.cache_write_1h,0)
             + COALESCE(r.cache_read,0)) AS input_side_tokens,
         SUM(r.output_tokens) AS output_tokens
  FROM tasks t LEFT JOIN requests r ON r.task_id = t.task_id
  GROUP BY t.task_id;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] < SCHEMA_VERSION:
        # every change so far is additive (CREATE IF NOT EXISTS above); just record the new version
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    registry.seed(conn)
    conn.commit()


def open_db(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    init(conn)
    return conn


def backup(keep: int = 10) -> Path | None:
    src = paths.db_path()
    if not src.exists():
        return None
    paths.backups_dir().mkdir(parents=True, exist_ok=True)
    dst = paths.backups_dir() / f"mm-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    with connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    olds = sorted(paths.backups_dir().glob("mm-*.db"))
    for old in olds[:-keep]:
        old.unlink()
    return dst


def wipe_test_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


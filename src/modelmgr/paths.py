"""Filesystem locations. Everything lives under MM_HOME (default ~/.model-manager)."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("MM_HOME", Path.home() / ".model-manager"))


def db_path() -> Path:
    return home() / "mm.db"


def spool_dir() -> Path:
    return home() / "spool"


def spool_done_dir() -> Path:
    return home() / "spool-done"


def config_path() -> Path:
    return home() / "config.json"


def policy_path() -> Path:
    return home() / "policy.json"


def summary_path() -> Path:
    return home() / "summary.json"


def policies_dir() -> Path:
    return home() / "policies"


def backups_dir() -> Path:
    return home() / "backups"


def disabled_flag() -> Path:
    return home() / "DISABLED"


def claude_projects_dir() -> Path:
    return Path(os.environ.get("MM_CLAUDE_PROJECTS", Path.home() / ".claude" / "projects"))


def ensure() -> None:
    for d in (home(), spool_dir(), spool_done_dir(), policies_dir(), backups_dir()):
        d.mkdir(parents=True, exist_ok=True)

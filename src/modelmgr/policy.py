"""Routing policy: the table the mod reads (~/.model-manager/policy.json).

v0 is the prior table from the vault note 'Routing Logic'. Learned versions are compiled by
modelmgr.learn and written as policies/policy-vNNN.json; policy.json is the active copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config, db, paths, registry

BANDS = ("low", "mid", "high")  # complexity 1-2, 3, 4-5

# category -> (low, mid, high) as "model:effort"
PRIOR_TABLE: dict[str, tuple[str, str, str]] = {
    "edit": ("haiku:low", "sonnet:low", "sonnet:medium"),
    "docs": ("haiku:low", "sonnet:low", "sonnet:medium"),
    "luau": ("haiku:medium", "sonnet:medium", "opus:medium"),
    "roblox_ui": ("sonnet:low", "sonnet:medium", "opus:medium"),
    "code": ("sonnet:medium", "sonnet:high", "opus:high"),
    "debug": ("sonnet:medium", "opus:medium", "opus:high"),
    "arch": ("sonnet:high", "opus:high", "opus:xhigh"),
    "research": ("sonnet:low", "sonnet:medium", "opus:medium"),
    "game_design": ("sonnet:medium", "opus:medium", "opus:high"),
    "blender": ("sonnet:medium", "sonnet:high", "opus:high"),
    "refactor": ("sonnet:low", "sonnet:medium", "opus:medium"),
    "test": ("haiku:medium", "sonnet:medium", "sonnet:high"),
    "chat": ("haiku:low", "sonnet:low", "sonnet:medium"),
}

LADDER = [
    "haiku:low", "haiku:medium", "sonnet:medium", "sonnet:high",
    "opus:high", "opus:xhigh", "fable:high",
]

PRIOR_P = 0.80       # prior success probability of the table's choice
PRIOR_STRENGTH = 4   # virtual observations


def band(complexity: int) -> str:
    return "low" if complexity <= 2 else "mid" if complexity == 3 else "high"


def _split(cell: str) -> tuple[str, str]:
    m, e = cell.split(":")
    return m, e


def prior_policy() -> dict[str, Any]:
    table: dict[str, dict[str, dict[str, Any]]] = {}
    for cat, cells in PRIOR_TABLE.items():
        table[cat] = {}
        for b, cell in zip(BANDS, cells):
            m, e = _split(cell)
            table[cat][b] = {"model": m, "effort": e, "p": PRIOR_P, "n": 0, "source": "prior",
                             "reason": "starting heuristic (no measured data yet)"}
    prices = {}
    for m in registry.MODELS:
        inp, w5, w1, rd, out, thr, mult = registry.PRICES[m.model_id]
        prices[m.alias] = {"input": inp, "cache_write_5m": w5, "cache_write_1h": w1, "cache_read": rd,
                           "output": out, "long_ctx_threshold": thr, "long_ctx_multiplier": mult}
    return {
        "version": 0,
        "created_at": db.now_iso(),
        "source": "prior",
        "model_ids": dict(registry.ALIASES),
        "tiers": {m.alias: m.tier for m in registry.MODELS},
        "prices": prices,
        "ladder": [list(_split(c)) for c in LADDER],
        "table": table,
        "changes": [],
    }


def active() -> dict[str, Any] | None:
    try:
        return json.loads(paths.policy_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def activate(pol: dict[str, Any], conn=None) -> Path:
    """Write policies/policy-vNNN.json and atomically make it the active policy.json."""
    paths.ensure()
    f = paths.policies_dir() / f"policy-v{pol['version']:03d}.json"
    config.atomic_write_json(f, pol)
    config.atomic_write_json(paths.policy_path(), pol)
    if conn is not None:
        with conn:
            conn.execute("UPDATE policies SET active=0")
            conn.execute(
                "INSERT OR REPLACE INTO policies(version, created_at, active, changes, file) VALUES (?,?,1,?,?)",
                (pol["version"], pol["created_at"], json.dumps(pol.get("changes", [])), str(f)),
            )
    return f


def rollback(version: int, conn=None) -> dict[str, Any]:
    f = paths.policies_dir() / f"policy-v{version:03d}.json"
    pol = json.loads(f.read_text())
    config.atomic_write_json(paths.policy_path(), pol)
    if conn is not None:
        with conn:
            conn.execute("UPDATE policies SET active = (version = ?)", (version,))
    return pol


def history() -> list[dict[str, Any]]:
    out = []
    for f in sorted(paths.policies_dir().glob("policy-v*.json")):
        try:
            p = json.loads(f.read_text())
            out.append({"version": p["version"], "created_at": p["created_at"], "source": p.get("source"),
                        "changes": len(p.get("changes", []))})
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out

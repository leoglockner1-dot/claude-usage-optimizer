"""User configuration, read by both the core and the mod (~/.model-manager/config.json)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import paths

MODES = ("observe", "auto", "balanced", "quality", "cost", "manual", "off")

DEFAULTS: dict[str, Any] = {
    "mode": "observe",
    # aliases the router may choose automatically, cheapest first
    "auto_models": ["haiku", "sonnet", "opus"],
    # highest model reachable at all (escalation and overrides included)
    "ceiling": "fable",
    "manual_model": "sonnet",
    "manual_effort": "medium",
    "route_subagents": True,
    "haiku_main_max_context": 80000,
    "min_switch_saving": 0.15,
    "exploration": 0.05,
    "escalation": {"enabled": True, "threshold": 2.0, "max_auto": 2},
    "quota_guard": {"five_hour": 80, "seven_day": 90},
    "learning": "auto",  # auto | suggest-only | off
    "store_prompts": "excerpt",  # excerpt | hash
    "test_patterns": [
        r"\bpytest\b", r"\bnpm (run )?test\b", r"\bpnpm test\b", r"\byarn test\b", r"\bcargo test\b",
        r"\bgo test\b", r"\bjest\b", r"\bvitest\b", r"\bunittest\b", r"\bclaude plugin test\b",
        r"\btestez\b", r"\brun-in-roblox\b", r"\blune run .*test",
    ],
    "build_patterns": [
        r"\btsc\b", r"\bnpm run build\b", r"\bcargo build\b", r"\bluau-analyze\b", r"\bselene\b",
        r"\bstylua --check\b", r"\brojo build\b", r"\bmake\b", r"\bpython3? -m py_compile\b",
    ],
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or paths.config_path()
    try:
        user = json.loads(path.read_text())
    except FileNotFoundError:
        user = {}
    return _merge(DEFAULTS, user)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def save(cfg: dict[str, Any], path: Path | None = None) -> None:
    validate(cfg)
    atomic_write_json(path or paths.config_path(), cfg)


def validate(cfg: dict[str, Any]) -> None:
    if cfg.get("mode") not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {cfg.get('mode')!r}")
    if cfg.get("learning") not in ("auto", "suggest-only", "off"):
        raise ValueError("learning must be auto, suggest-only or off")


def set_key(dotted: str, raw: str) -> dict[str, Any]:
    """Set a config value by dotted key; raw is parsed as JSON when possible."""
    cfg = load()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    node = cfg
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    save(cfg)
    return cfg

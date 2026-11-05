"""User configuration, read by both the core and the mod (~/.model-manager/config.json)."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from . import paths, registry

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


LEARNING = ("auto", "suggest-only", "off")
STORE_PROMPTS = ("excerpt", "hash")

# keys the dashboard may change: dotted key -> (kind, rule). kind: enum, bool, int, num, models
EDITABLE: dict[str, tuple[str, Any]] = {
    "mode": ("enum", MODES),
    "auto_models": ("models", None),
    "ceiling": ("enum", None),  # a registry alias, checked in validate
    "manual_model": ("enum", None),
    "manual_effort": ("enum", None),
    "route_subagents": ("bool", None),
    "haiku_main_max_context": ("int", (0, 1_000_000)),
    "min_switch_saving": ("num", (0.0, 1.0)),
    "exploration": ("num", (0.0, 0.5)),
    "escalation.enabled": ("bool", None),
    "escalation.threshold": ("num", (0.5, 10.0)),
    "escalation.max_auto": ("int", (0, 5)),
    "quota_guard.five_hour": ("int", (0, 100)),
    "quota_guard.seven_day": ("int", (0, 100)),
    "learning": ("enum", LEARNING),
    "store_prompts": ("enum", STORE_PROMPTS),
}


def _get(cfg: dict, dotted: str) -> Any:
    node: Any = cfg
    for p in dotted.split("."):
        node = node.get(p) if isinstance(node, dict) else None
    return node


def validate(cfg: dict[str, Any]) -> None:
    """Rejects a config the router could not use. Shared by the CLI and the dashboard."""
    aliases = {m.alias: m for m in registry.MODELS}
    enums = {"ceiling": tuple(aliases), "manual_model": tuple(aliases), "manual_effort": registry.EFFORTS}
    for key, (kind, rule) in EDITABLE.items():
        v = _get(cfg, key)
        if kind == "enum":
            allowed = rule or enums[key]
            if v not in allowed:
                raise ValueError(f"{key} must be one of {', '.join(allowed)}, got {v!r}")
        elif kind == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"{key} must be true or false")
        elif kind in ("int", "num"):
            ok = isinstance(v, int) if kind == "int" else isinstance(v, (int, float))
            if not ok or isinstance(v, bool) or not rule[0] <= v <= rule[1]:
                what = "a whole number" if kind == "int" else "a number"
                raise ValueError(f"{key} must be {what} from {rule[0]} to {rule[1]}")
        elif kind == "models":
            if not isinstance(v, list) or not v or len(set(v)) != len(v):
                raise ValueError(f"{key} must list at least one model, each once")
            bad = [m for m in v if m not in aliases or not aliases[m].auto]
            if bad:
                auto = ", ".join(a for a, m in aliases.items() if m.auto)
                raise ValueError(f"{key}: {', '.join(map(str, bad))} cannot be routed automatically (allowed: {auto})")
    ceiling = aliases[cfg["ceiling"]].tier
    for m in cfg["auto_models"]:
        if aliases[m].tier > ceiling:
            raise ValueError(f"auto_models: {m} is above the ceiling {cfg['ceiling']}")
    if aliases[cfg["manual_model"]].tier > ceiling:
        raise ValueError(f"manual_model {cfg['manual_model']} is above the ceiling {cfg['ceiling']}")
    for key in ("test_patterns", "build_patterns"):
        pats = cfg.get(key)
        if not isinstance(pats, list) or not all(isinstance(x, str) for x in pats):
            raise ValueError(f"{key} must be a list of regular expressions")
        for x in pats:
            try:
                re.compile(x)
            except re.error as exc:
                raise ValueError(f"{key}: {x!r} is not a valid regular expression ({exc})") from None


def apply_patch(patch: Any) -> dict[str, Any]:
    """Applies {dotted key: value} from the dashboard, validates the result and saves it."""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("send a JSON object of settings to change")
    unknown = sorted(k for k in patch if k not in EDITABLE)
    if unknown:
        raise ValueError(f"not editable here: {', '.join(unknown)}")
    cfg = load()
    for dotted, value in patch.items():
        node = cfg
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    save(cfg)
    return cfg


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

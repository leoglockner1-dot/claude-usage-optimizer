"""Learning engine (Phase 5): per-cell success estimates and policy compilation.

Follows the vault note 'Architecture/Learning Engine'.

A cell is (category, band, model alias, effort). Evidence per cell:
- task outcomes (tasks.outcome), attributed to the model and effort that actually answered the
  task's main-agent requests (in observe mode that is the session model, not the router's pick);
- Leo's dashboard verdicts (route_grades), attributed to the router's pick for that turn, because
  the verdict judges the pick. Verdicts are strong signals and count as verified evidence.

Estimation is Beta-binomial with the v0 prior (policy.PRIOR_P, PRIOR_STRENGTH virtual tasks),
time decay (half-life 30 days), pooling for thin cells (n < 5) and the 10th percentile of the
posterior (lower bound of the 80 % credible interval) for decisions.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db, paths, policy, registry

# outcome -> (success weight, failure weight, verified)
OUTCOME_WEIGHTS: dict[str, tuple[float, float, bool]] = {
    "verified_success": (1.0, 0.0, True),
    "unverified_success": (0.5, 0.0, False),
    "failure": (0.0, 1.0, True),
}
VERDICT_WEIGHT = 2.0       # a verdict counts like two objective outcomes
HALF_LIFE_DAYS = 30.0
POOL_BELOW_N = 5           # cells with fewer observations borrow their prior from a pool
CREDIBLE = 0.80            # lower bound of the 80 % interval = 10th percentile
SUCCESS_TARGET = 0.70      # a candidate's lower bound must reach this to be routed to
MIN_N = 8                  # closed tasks (plus verdicts) a new choice needs
MIN_GAIN = 0.10            # expected-cost improvement a change needs
AUTO_EVERY_TASKS = 10
AUTO_EVERY = timedelta(days=1)

EFFORT_INDEX = {e: i for i, e in enumerate(registry.EFFORTS)}
TIER = {m.alias: m.tier for m in registry.MODELS}
OUT_PRICE = {m.alias: registry.PRICES[m.model_id][4] for m in registry.MODELS}


# ---------- Beta distribution (no scipy) ----------

def _betacf(a: float, b: float, x: float) -> float:
    tiny, eps = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    bt = math.exp(ln)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def beta_quantile(q: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def lower_bound(a: float, b: float) -> float:
    return beta_quantile((1.0 - CREDIBLE) / 2.0, a, b)


# ---------- cells ----------

def rank(model: str, effort: str) -> int:
    return TIER.get(model, 0) * 10 + EFFORT_INDEX.get(effort, 1)


def prior_p(category: str, band: str, model: str, effort: str) -> float:
    """v0 prior: the heuristic table's pick and anything stronger start at PRIOR_P; each ladder
    step below it costs 0.1 (floor 0.5). Unknown categories use the chat row."""
    cells = policy.PRIOR_TABLE.get(category) or policy.PRIOR_TABLE["chat"]
    pm, pe = cells[policy.BANDS.index(band)].split(":")
    gap = rank(pm, pe) - rank(model, effort)
    if gap <= 0:
        return policy.PRIOR_P
    steps = (gap // 10) * 2 + gap % 10  # a model tier counts like two effort steps
    return max(0.5, policy.PRIOR_P - 0.1 * steps)


@dataclass
class Cell:
    category: str
    band: str
    model: str
    effort: str
    s: float = 0.0           # decayed, weighted successes
    f: float = 0.0           # decayed, weighted failures
    n: int = 0               # closed tasks with a known outcome + verdicts (raw count)
    vs: float = 0.0          # verified-only successes (verified outcomes, failures, verdicts)
    vf: float = 0.0
    vn: int = 0
    verdicts: int = 0
    costs: list[float] = field(default_factory=list)
    # computed
    alpha: float = 0.0
    beta: float = 0.0
    mean: float = 0.0
    lb: float = 0.0
    pooled: str | None = None
    cost: float | None = None
    cost_source: str | None = None

    @property
    def key(self) -> str:
        return f"{self.model}:{self.effort}"

    def add(self, s: float, f: float, verified: bool, w: float = 1.0) -> None:
        self.s += s * w
        self.f += f * w
        self.n += 1
        if verified:
            self.vs += s * w
            self.vf += f * w
            self.vn += 1

    def verified_mean(self) -> float | None:
        """Verified-only rate under one shared prior, so two models are compared on data alone
        (the per-model prior would favour the stronger model by construction)."""
        if not self.vn:
            return None
        k = policy.PRIOR_STRENGTH
        return (policy.PRIOR_P * k + self.vs) / (k + self.vs + self.vf)

    def as_dict(self) -> dict[str, Any]:
        return {"category": self.category, "band": self.band, "model": self.model, "effort": self.effort,
                "n": self.n, "verified_n": self.vn, "verdicts": self.verdicts,
                "successes": round(self.s, 3), "failures": round(self.f, 3),
                "p": round(self.mean, 4), "lb": round(self.lb, 4), "pooled": self.pooled,
                "cost": None if self.cost is None else round(self.cost, 6), "cost_source": self.cost_source}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _decay(ts: str | None, now: datetime) -> float:
    d = _parse(ts)
    if d is None:
        return 1.0
    age = max(0.0, (now - d).total_seconds() / 86400)
    return 0.5 ** (age / HALF_LIFE_DAYS)


def _alias(model_id: str | None) -> str | None:
    m = registry.BY_ID.get(registry.normalize_model(model_id) or "")
    return m.alias if m else None  # older model generations are not learned from


def _split_cell(s: str | None) -> tuple[str, str] | None:
    if not s:
        return None
    m, _, e = s.partition(":")
    if m not in TIER:
        return None
    if not e:
        e = next(x.default_effort for x in registry.MODELS if x.alias == m)
    return (m, e) if e in EFFORT_INDEX else None


def collect(conn: sqlite3.Connection, now: datetime | None = None) -> dict[tuple, Cell]:
    now = now or datetime.now(timezone.utc)
    cells: dict[tuple, Cell] = {}

    def cell(cat, b, m, e) -> Cell:
        k = (cat, b, m, e)
        if k not in cells:
            cells[k] = Cell(cat, b, m, e)
        return cells[k]

    # model and effort that answered each task's main-agent turns (most requests wins)
    answered: dict[str, tuple[str, str, int]] = {}
    for r in conn.execute(
            """SELECT task_id, model, effort, COUNT(*) n FROM requests
               WHERE task_id IS NOT NULL AND COALESCE(agent_id, '') = '' GROUP BY task_id, model, effort"""):
        alias = _alias(r["model"])
        if alias is None:
            continue
        eff = r["effort"] if r["effort"] in EFFORT_INDEX else \
            next(x.default_effort for x in registry.MODELS if x.alias == alias)
        if r["task_id"] not in answered or r["n"] > answered[r["task_id"]][2]:
            answered[r["task_id"]] = (alias, eff, r["n"])

    for t in conn.execute(
            """SELECT t.task_id, t.category, t.complexity, t.outcome, t.closed_at, t.started_at, v.est_cost_usd
               FROM tasks t LEFT JOIN v_task_cost v ON v.task_id = t.task_id
               WHERE t.category IS NOT NULL AND t.complexity IS NOT NULL AND t.outcome IS NOT NULL"""):
        if t["outcome"] not in OUTCOME_WEIGHTS or t["task_id"] not in answered:
            continue
        m, e, _ = answered[t["task_id"]]
        c = cell(t["category"], policy.band(t["complexity"]), m, e)
        s, f, verified = OUTCOME_WEIGHTS[t["outcome"]]
        c.add(s, f, verified, _decay(t["closed_at"] or t["started_at"], now))
        if t["est_cost_usd"] is not None:
            c.costs.append(t["est_cost_usd"])

    for g in conn.execute(
            """SELECT g.verdict, g.better, g.ts, r.chosen_model, r.chosen_effort, t.category, t.complexity
               FROM route_grades g JOIN routes r ON r.route_id = g.route_id
               JOIN tasks t ON t.task_id = r.task_id
               WHERE t.category IS NOT NULL AND t.complexity IS NOT NULL"""):
        chosen = _split_cell(f"{g['chosen_model']}:{g['chosen_effort'] or ''}")
        if chosen is None or g["verdict"] in (None, "wrong_category"):
            continue  # a wrong category says nothing about the model
        b = policy.band(g["complexity"])
        w = VERDICT_WEIGHT * _decay(g["ts"], now)
        c = cell(g["category"], b, *chosen)
        better = _split_cell(g["better"])
        if g["verdict"] == "right":
            c.add(1.0, 0.0, True, w)
        elif g["verdict"] == "too_weak":
            c.add(0.0, 1.0, True, w)
        elif g["verdict"] == "too_strong":
            c.add(1.0, 0.0, True, w)  # it worked, only overkill
        c.verdicts += 1
        if better and g["verdict"] in ("too_weak", "too_strong"):
            bc = cell(g["category"], b, *better)
            bc.add(1.0, 0.0, True, w)
            bc.verdicts += 1
    return cells


def estimate(cells: dict[tuple, Cell]) -> None:
    """Posterior per cell. Thin cells take the pooled rate of the same model in the category,
    else across all categories, as their prior mean (strength stays PRIOR_STRENGTH)."""
    k = policy.PRIOR_STRENGTH
    pools: dict[tuple, list[float]] = {}
    for c in cells.values():
        for key in ((c.category, c.model), ("*", c.model)):
            p = pools.setdefault(key, [0.0, 0.0, 0, 0.0])  # s, f, n, sum of prior means
            p[0] += c.s
            p[1] += c.f
            p[2] += c.n
            p[3] += prior_p(c.category, c.band, c.model, c.effort)
    for c in cells.values():
        m0 = prior_p(c.category, c.band, c.model, c.effort)
        if c.n < POOL_BELOW_N:
            for key, label in (((c.category, c.model), f"{c.model} in {c.category}"), (("*", c.model), f"{c.model} overall")):
                s, f, n, _ = pools.get(key, (0, 0, 0, 0))
                if n >= POOL_BELOW_N:
                    m0 = (m0 * k + s) / (k + s + f)
                    c.pooled = label
                    break
        c.alpha = m0 * k + c.s
        c.beta = (1 - m0) * k + c.f
        c.mean = c.alpha / (c.alpha + c.beta)
        c.lb = lower_bound(c.alpha, c.beta)
        if c.costs:
            xs = sorted(c.costs)
            mid = len(xs) // 2
            c.cost = xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2
            c.cost_source = "measured"


def _virtual(cells: dict[tuple, Cell], cat: str, b: str, m: str, e: str) -> Cell:
    """A cell with no evidence (e.g. the current choice before it ever ran)."""
    key = (cat, b, m, e)
    if key not in cells:
        c = Cell(cat, b, m, e)
        tmp = {key: c, **{k: v for k, v in cells.items() if v.model == m}}
        estimate(tmp)
        c.costs = []
        cells[key] = c
    return cells[key]


def _scaled_cost(cells: dict[tuple, Cell], c: Cell) -> None:
    """Cost of a cell without measured tasks: the best-measured cell of the same category (same band
    first) scaled by output price. Rough: assumes the same token usage on every model."""
    if c.cost is not None:
        return
    pool = [x for x in cells.values() if x.category == c.category and x.cost is not None]
    if not pool:
        return
    ref = max(pool, key=lambda x: (x.band == c.band, len(x.costs)))
    c.cost = ref.cost * OUT_PRICE[c.model] / OUT_PRICE[ref.model]
    c.cost_source = f"scaled from {ref.key}"


def _floor(cfg: dict[str, Any], cat: str) -> tuple[str, str] | None:
    return _split_cell(cfg.get("floors", {}).get(cat))


def _below(m: str, e: str, fl: tuple[str, str]) -> bool:
    return TIER.get(m, 0) < TIER[fl[0]] or (m == fl[0] and EFFORT_INDEX.get(e, 0) < EFFORT_INDEX[fl[1]])


def _allowed(cfg: dict[str, Any], cat: str, m: str, e: str) -> bool:
    if m not in cfg.get("auto_models", []):
        return False
    ceiling = TIER.get(cfg.get("ceiling", "fable"), 4)
    if TIER[m] > ceiling:
        return False
    fl = _floor(cfg, cat)
    return not (fl and _below(m, e, fl))


def _ecost(c: Cell) -> float | None:
    return None if c.cost is None or c.mean <= 0 else c.cost / c.mean


def decide(cells: dict[tuple, Cell], pol: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One decision per (category, band) of the active policy: keep, change (passes every
    threshold) or suggestion (a better candidate exists but fails a threshold)."""
    out = []
    for cat, bands in sorted(pol.get("table", {}).items()):
        for b in policy.BANDS:
            cur_cell = bands.get(b)
            if not cur_cell:
                continue
            cm, ce = cur_cell["model"], cur_cell["effort"]
            fl = _floor(cfg, cat)
            # a floor is config, not evidence: it always wins
            if fl and _below(cm, ce, fl):
                out.append({"category": cat, "band": b, "current": f"{cm}:{ce}", "best": f"{fl[0]}:{fl[1]}",
                            "status": "change", "kind": "floor",
                            "reasons": [f"floor for {cat} is {fl[0]}:{fl[1]} (config)"], "evidence": {}})
                continue
            cur = _virtual(cells, cat, b, cm, ce)
            cands = [c for c in cells.values() if c.category == cat and c.band == b and c.n > 0
                     and _allowed(cfg, cat, c.model, c.effort)]
            for c in cands + [cur]:
                _scaled_cost(cells, c)
            ok = [c for c in cands if c.lb >= SUCCESS_TARGET and _ecost(c) is not None]
            dec: dict[str, Any] = {"category": cat, "band": b, "current": cur.key, "best": cur.key,
                                   "status": "keep", "kind": None, "reasons": [],
                                   "evidence": {"current": cur.as_dict()}}
            out.append(dec)
            if not ok:
                n_data = sum(c.n for c in cands)
                dec["reasons"].append("not enough evidence" if n_data < MIN_N else
                                      f"no candidate reaches a lower bound of {SUCCESS_TARGET:.0%}")
                continue
            best = min(ok, key=lambda c: (_ecost(c), rank(c.model, c.effort)))
            if best.key == cur.key:
                dec["reasons"].append("current choice is the cheapest that clears the bar")
                continue
            dec["best"] = best.key
            dec["evidence"]["best"] = best.as_dict()
            fails = []
            if best.n < MIN_N:
                fails.append(f"{best.key} has n={best.n}, needs ≥ {MIN_N}")
            ec_old, ec_new = _ecost(cur), _ecost(best)
            upgrade = rank(best.model, best.effort) > rank(cm, ce)
            measured_bad = cur.n >= MIN_N and cur.lb < SUCCESS_TARGET
            if upgrade:
                if not measured_bad:
                    # only a stronger model has data; that is no reason to pay more
                    dec["best"] = cur.key
                    dec["evidence"].pop("best")
                    dec["reasons"].append(f"only stronger models have enough data; {cur.key} is not measured "
                                          f"worse (n={cur.n})" + ("" if cur.n >= MIN_N else " – not enough evidence"))
                    continue
                dec["kind"] = "upgrade"
            else:
                dec["kind"] = "cheaper"
                if ec_old is None:
                    fails.append(f"cost of {cur.key} is not measured")
                else:
                    gain = (ec_old - ec_new) / ec_old
                    dec["evidence"]["cost_gain"] = round(gain, 4)
                    if gain < MIN_GAIN:
                        fails.append(f"expected-cost gain {gain:.0%} < {MIN_GAIN:.0%}")
                vo, vb = cur.verified_mean(), best.verified_mean()
                if vo is not None and vb is not None and vo > vb:
                    fails.append(f"{cur.key} is better on verified data ({vo:.2f} vs {vb:.2f})")
            dec["status"] = "suggestion" if fails else "change"
            dec["reasons"] = fails or [f"{best.key}: p={best.mean:.2f}, lower bound {best.lb:.2f}, n={best.n}"]
    return out


# ---------- compile, run, auto ----------

def next_version() -> int:
    vs = [h["version"] for h in policy.history()]
    act = (policy.active() or {}).get("version")
    if act is not None:
        vs.append(act)
    return max(vs, default=-1) + 1


def compile_policy(pol: dict[str, Any], cells: dict[tuple, Cell], decisions: list[dict[str, Any]]) -> dict[str, Any] | None:
    changes = [d for d in decisions if d["status"] == "change"]
    if not changes:
        return None
    new = json.loads(json.dumps(pol))
    new.update(version=next_version(), created_at=db.now_iso(), source="learned", changes=[])
    for d in changes:
        m, e = d["best"].split(":")
        c = cells.get((d["category"], d["band"], m, e))
        new["table"][d["category"]][d["band"]] = {
            "model": m, "effort": e, "p": round(c.mean, 4) if c else policy.PRIOR_P, "n": c.n if c else 0,
            "source": "floor" if d["kind"] == "floor" else "learned", "reason": "; ".join(d["reasons"])}
        new["changes"].append({"category": d["category"], "band": d["band"], "from": d["current"], "to": d["best"],
                               "kind": d["kind"], "why": "; ".join(d["reasons"]),
                               "confidence": round(c.lb, 4) if c else None,
                               "cost_gain": d["evidence"].get("cost_gain")})
    return new


def compute(conn: sqlite3.Connection, cfg: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    cfg = cfg or config.load()
    pol = policy.active() or policy.prior_policy()
    cells = collect(conn, now)
    estimate(cells)
    decisions = decide(cells, pol, cfg)
    closed = conn.execute("SELECT COUNT(*) FROM tasks WHERE outcome IS NOT NULL").fetchone()[0]
    verdicts = conn.execute("SELECT COUNT(*) FROM route_grades WHERE verdict IS NOT NULL").fetchone()[0]
    return {"ts": db.now_iso(), "learning": cfg.get("learning"), "policy_version": pol.get("version"),
            "closed_tasks": closed, "verdicts": verdicts, "cells": cells, "decisions": decisions, "policy": pol}


def _save_state(res: dict[str, Any], written: int | None) -> None:
    config.atomic_write_json(paths.learn_state_path(), {
        "last_run": res["ts"], "closed_tasks": res["closed_tasks"], "verdicts": res["verdicts"],
        "written_version": written,
        "changes": sum(d["status"] == "change" for d in res["decisions"]),
        "suggestions": sum(d["status"] == "suggestion" for d in res["decisions"])})


def learn(conn: sqlite3.Connection, apply: bool | None = None, dry_run: bool = False,
          cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute everything; activate a new policy when apply (default: config.learning == auto).
    Changes that are not activated are reported as suggestions."""
    cfg = cfg or config.load()
    res = compute(conn, cfg)
    if apply is None:
        apply = cfg.get("learning") == "auto"
    res["written"] = None
    if not apply or dry_run:
        for d in res["decisions"]:
            if d["status"] == "change":
                d["status"] = "suggestion"
                d["reasons"] = d["reasons"] + ["passes the thresholds; not activated (" +
                                               ("dry run" if dry_run else f"learning: {cfg.get('learning')}") + ")"]
    else:
        new = compile_policy(res["policy"], res["cells"], res["decisions"])
        if new is not None:
            policy.activate(new, conn)
            res["written"] = new["version"]
    if not dry_run:
        _save_state(res, res["written"])
    return res


def maybe_auto(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Called by ingest: learn once a day or after every 10 newly closed tasks, only in learning=auto."""
    cfg = config.load()
    if cfg.get("learning") != "auto":
        return None
    try:
        st = json.loads(paths.learn_state_path().read_text())
    except (OSError, ValueError):
        st = {}
    closed = conn.execute("SELECT COUNT(*) FROM tasks WHERE outcome IS NOT NULL").fetchone()[0]
    last = _parse(st.get("last_run"))
    due = (last is None or datetime.now(timezone.utc) - last >= AUTO_EVERY
           or closed - int(st.get("closed_tasks") or 0) >= AUTO_EVERY_TASKS)
    if not due:
        return None
    return learn(conn, apply=True, cfg=cfg)


# ---------- views shared by CLI and dashboard ----------

def to_json(res: dict[str, Any]) -> dict[str, Any]:
    cells = sorted(res["cells"].values(), key=lambda c: (c.category, policy.BANDS.index(c.band), rank(c.model, c.effort)))
    heat: dict[str, dict[str, dict[str, Any]]] = {}
    for c in cells:
        if c.n == 0:
            continue
        h = heat.setdefault(c.category, {}).setdefault(c.model, {"s": 0.0, "f": 0.0, "n": 0, "verdicts": 0})
        h["s"] += c.s
        h["f"] += c.f
        h["n"] += c.n
        h["verdicts"] += c.verdicts
    k = policy.PRIOR_STRENGTH
    for cat, row in heat.items():
        for m, h in row.items():
            a, b = policy.PRIOR_P * k + h["s"], (1 - policy.PRIOR_P) * k + h["f"]
            h.update(p=round(a / (a + b), 4), lb=round(lower_bound(a, b), 4))
            h["s"], h["f"] = round(h["s"], 3), round(h["f"], 3)
    return {
        "ts": res["ts"], "learning": res["learning"], "policy_version": res["policy_version"],
        "closed_tasks": res["closed_tasks"], "verdicts": res["verdicts"], "written": res.get("written"),
        "thresholds": {"min_n": MIN_N, "min_gain": MIN_GAIN, "target_lb": SUCCESS_TARGET, "credible": CREDIBLE,
                       "pool_below_n": POOL_BELOW_N, "half_life_days": HALF_LIFE_DAYS},
        "models": [m.alias for m in registry.MODELS],
        "heatmap": heat,
        "cells": [c.as_dict() for c in cells if c.n > 0],
        "decisions": [{k2: v for k2, v in d.items()} for d in res["decisions"]],
        "table": res["policy"].get("table", {}),
    }


def _thin(d: dict[str, Any]) -> bool:
    return any("not enough evidence" in r for r in d["reasons"])


def _fmt_dec(d: dict[str, Any]) -> str:
    ev = d["evidence"].get("best") or {}
    head = f"{d['category']}/{d['band']}: {d['current']} → {d['best']}"
    if ev:
        head += f"  (p={ev['p']:.2f}, lb={ev['lb']:.2f}, n={ev['n']}"
        if d["evidence"].get("cost_gain") is not None:
            head += f", cost gain {d['evidence']['cost_gain']:.0%}"
        head += ")"
    return head + "\n      " + "; ".join(d["reasons"])


def print_result(res: dict[str, Any], header: str) -> None:
    decs = res["decisions"]
    data_cells = [c for c in res["cells"].values() if c.n > 0]
    keep_ne = sum(1 for d in decs if d["status"] == "keep" and _thin(d))
    print(header)
    print(f"  evidence     {res['closed_tasks']} closed tasks, {res['verdicts']} verdicts, "
          f"{len(data_cells)} cells with data")
    print(f"  learning     {res['learning']}  · active policy v{res['policy_version']}")
    print(f"  decisions    {len(decs)} category bands: {keep_ne} not enough evidence, "
          f"{sum(d['status'] == 'keep' for d in decs) - keep_ne} kept on evidence, "
          f"{sum(d['status'] == 'change' for d in decs)} changed, "
          f"{sum(d['status'] == 'suggestion' for d in decs)} suggestions")
    for d in decs:
        if d["status"] == "change":
            print("  ✔ " + _fmt_dec(d))
    for d in decs:
        if d["status"] == "suggestion":
            print("  ? " + _fmt_dec(d))
    if res.get("written") is not None:
        print(f"  wrote policy v{res['written']:03d} and activated it (rollback: model-manager rollback "
              f"{res['policy_version']})")


def run(conn: sqlite3.Connection, dry_run: bool = False, suggest_only: bool = False) -> int:
    cfg = config.load()
    apply = False if suggest_only or cfg.get("learning") != "auto" else True
    res = learn(conn, apply=apply, dry_run=dry_run, cfg=cfg)
    print_result(res, "model-manager learn" + (" (dry run)" if dry_run else " (suggest only)" if not apply else ""))
    if cfg.get("learning") == "off":
        print("  learning is off: nothing is activated. Turn on: model-manager config learning suggest-only")
    return 0


def print_insights(conn: sqlite3.Connection) -> int:
    from .cli import table
    res = compute(conn)
    cells = sorted((c for c in res["cells"].values() if c.n > 0),
                   key=lambda c: (c.category, policy.BANDS.index(c.band), rank(c.model, c.effort)))
    print(f"What the learning engine sees ({res['closed_tasks']} closed tasks, {res['verdicts']} verdicts)\n")
    if cells:
        print(table([[c.category, c.band, c.key, c.n, c.vn, f"{c.mean:.2f}", f"{c.lb:.2f}",
                      "" if c.cost is None else f"${c.cost:.3f}", c.pooled or ""] for c in cells],
                    ["category", "band", "model:effort", "n", "verified", "p", "lb 80%", "median cost", "pooled from"]))
    else:
        print("No closed tasks with a known outcome yet.")
    sug = [d for d in res["decisions"] if d["status"] in ("suggestion", "change")]
    print("\nSuggestions" + ("" if sug else ": none."))
    for d in sug:
        print("  ? " + _fmt_dec(d))
    ne = sum(1 for d in res["decisions"] if d["status"] == "keep" and _thin(d))
    print(f"\n{ne} of {len(res['decisions'])} category bands have not enough evidence for any change "
          f"(a new choice needs n ≥ {MIN_N} and a lower bound ≥ {SUCCESS_TARGET:.0%}).")
    if res["verdicts"] == 0:
        print("Tip: judge routing decisions on the dashboard's Routing page; verdicts count double.")
    return 0

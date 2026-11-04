"""Model registry and versioned list prices (USD per million tokens).

Prices are API list prices used only for API-equivalent estimates. Source and date are stored
with every price row so historic estimates are never silently recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICE_SOURCE = "platform.claude.com/docs/en/about-claude/pricing"
PRICE_DATE = "2026-11-04"


@dataclass(frozen=True)
class Model:
    model_id: str
    alias: str
    family: str
    tier: int
    context_window: int
    effort_levels: tuple[str, ...]
    default_effort: str
    auto: bool  # may the router pick it without escalation/override


EFFORTS = ("low", "medium", "high", "xhigh", "max")

MODELS: tuple[Model, ...] = (
    Model("claude-haiku-5-5", "haiku", "haiku", 1, 1_000_000, EFFORTS, "medium", True),
    Model("claude-sonnet-5-5", "sonnet", "sonnet", 2, 1_000_000, EFFORTS, "medium", True),
    Model("claude-opus-5-5", "opus", "opus", 3, 1_000_000, EFFORTS, "medium", True),
    Model("claude-fable-5-1", "fable", "fable", 4, 1_000_000, EFFORTS, "high", False),
)

# model_id -> (input, cache_write_5m, cache_write_1h, cache_read, output, long_ctx_threshold, long_ctx_mult)
PRICES: dict[str, tuple[float, float, float, float, float, int | None, float | None]] = {
    "claude-fable-5-1": (10.0, 12.5, 20.0, 0.25, 50.0, None, None),
    "claude-fable-5": (10.0, 12.5, 20.0, 1.0, 50.0, None, None),
    "claude-opus-5-5": (4.0, 5.0, 8.0, 0.20, 20.0, None, None),
    "claude-opus-5": (5.0, 6.25, 10.0, 0.50, 25.0, None, None),
    "claude-opus-4-8": (5.0, 6.25, 10.0, 0.50, 25.0, None, None),
    "claude-opus-4-7": (5.0, 6.25, 10.0, 0.50, 25.0, None, None),
    "claude-opus-4-6": (5.0, 6.25, 10.0, 0.50, 25.0, None, None),
    "claude-sonnet-5-5": (2.0, 2.5, 4.0, 0.10, 10.0, None, None),
    "claude-sonnet-5": (2.0, 2.5, 4.0, 0.20, 10.0, None, None),
    "claude-sonnet-4-6": (3.0, 3.75, 6.0, 0.30, 15.0, None, None),
    "claude-sonnet-4-5": (3.0, 3.75, 6.0, 0.30, 15.0, None, None),
    # Haiku 5.5: prompts over 100K input tokens (incl. cache) pay 5x on every category
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50, 100_000, 5.0),
    "claude-haiku-4-5": (1.0, 1.25, 2.0, 0.10, 5.0, None, None),
}

ALIASES = {m.alias: m.model_id for m in MODELS}
BY_ID = {m.model_id: m for m in MODELS}


def normalize_model(model: str | None) -> str | None:
    """Map aliases and dated/suffixed IDs ('claude-sonnet-5-5[1m]', '...-20261001') to a price key."""
    if not model:
        return None
    m = model.strip()
    m = ALIASES.get(m, m)
    m = m.split("[")[0]
    if m in PRICES:
        return m
    for key in sorted(PRICES, key=len, reverse=True):
        if m.startswith(key):
            return key
    return m


def tier_of(model: str | None) -> int | None:
    mid = normalize_model(model)
    if mid in BY_ID:
        return BY_ID[mid].tier
    for fam, t in (("haiku", 1), ("sonnet", 2), ("opus", 3), ("fable", 4)):
        if mid and fam in mid:
            return t
    return None


def estimate_cost(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_write_5m: int | None = 0,
    cache_write_1h: int | None = 0,
    cache_read: int | None = 0,
) -> float | None:
    """API-equivalent cost in USD. None if the model has no known price or tokens are missing."""
    mid = normalize_model(model)
    if mid not in PRICES or input_tokens is None or output_tokens is None:
        return None
    inp, w5, w1, rd, out, thr, mult = PRICES[mid]
    cw5, cw1, cr = cache_write_5m or 0, cache_write_1h or 0, cache_read or 0
    factor = 1.0
    if thr is not None and (input_tokens + cw5 + cw1 + cr) > thr:
        factor = mult or 1.0
    usd = (input_tokens * inp + cw5 * w5 + cw1 * w1 + cr * rd + output_tokens * out) / 1_000_000
    return round(usd * factor, 8)


def seed(conn) -> None:
    for m in MODELS:
        conn.execute(
            """INSERT INTO models(model_id, alias, family, tier, context_window, effort_levels, available)
               VALUES (?,?,?,?,?,?,1)
               ON CONFLICT(model_id) DO UPDATE SET alias=excluded.alias, tier=excluded.tier,
                 context_window=excluded.context_window, effort_levels=excluded.effort_levels""",
            (m.model_id, m.alias, m.family, m.tier, m.context_window, ",".join(m.effort_levels)),
        )
    for mid, (inp, w5, w1, rd, out, thr, mult) in PRICES.items():
        conn.execute(
            """INSERT OR IGNORE INTO prices(model_id, valid_from, source, input, cache_write_5m,
               cache_write_1h, cache_read, output, long_ctx_threshold, long_ctx_multiplier)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (mid, PRICE_DATE, PRICE_SOURCE, inp, w5, w1, rd, out, thr, mult),
        )

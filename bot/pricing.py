"""Model pricing for cost attribution on LLM calls.

Stored as a Python dict so price changes are a 1-line PR and deploy with code.
If we want historical cost to stay accurate across a price change, we move this
to a `model_prices(model, input_per_mtok, output_per_mtok, effective_from)`
table later — until then, a price change is applied to all future rows and
past rows keep the cost they were stamped with at write time.

Prices are USD per million tokens, as (input, output). Sources:
  - Anthropic:        https://www.anthropic.com/pricing
  - OpenAI Platform:  https://openai.com/api/pricing
Update both when bumping a model in analyzer._MODEL or analyzer._PREMIUM_MODEL.
"""
from __future__ import annotations

# (input_per_mtok, output_per_mtok)
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "gpt-4o-mini": (0.15, 0.60),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute USD cost for a single call. Unknown models cost 0 (we still
    record tokens, so we can backfill cost later if needed)."""
    prices = PRICES_PER_MTOK.get(model)
    if not prices:
        return 0.0
    in_per, out_per = prices
    return (input_tokens * in_per + output_tokens * out_per) / 1_000_000

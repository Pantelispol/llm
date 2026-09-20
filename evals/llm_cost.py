from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.ports import LLMUsage

PRICING_AS_OF = "2026-09-20"
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
LONG_CONTEXT_THRESHOLD = 272_000


@dataclass(frozen=True)
class TokenRates:
    input: Decimal
    cached_input: Decimal
    cache_write: Decimal
    output: Decimal


@dataclass(frozen=True)
class ModelRates:
    short: TokenRates
    long: TokenRates


MODEL_RATES = {
    "gpt-5.6-luna": ModelRates(
        short=TokenRates(
            input=Decimal("0.20"),
            cached_input=Decimal("0.02"),
            cache_write=Decimal("0.25"),
            output=Decimal("1.20"),
        ),
        long=TokenRates(
            input=Decimal("0.40"),
            cached_input=Decimal("0.04"),
            cache_write=Decimal("0.50"),
            output=Decimal("1.80"),
        ),
    ),
    "gpt-5.6-terra": ModelRates(
        short=TokenRates(
            input=Decimal("2.00"),
            cached_input=Decimal("0.20"),
            cache_write=Decimal("2.50"),
            output=Decimal("12.00"),
        ),
        long=TokenRates(
            input=Decimal("4.00"),
            cached_input=Decimal("0.40"),
            cache_write=Decimal("5.00"),
            output=Decimal("18.00"),
        ),
    ),
}


def base_model_id(model_id: str) -> str:
    for candidate in MODEL_RATES:
        if model_id == candidate or model_id.startswith(f"{candidate}-"):
            return candidate
    raise ValueError(f"no price table for model: {model_id}")


def calculate_cost_usd(usage: LLMUsage) -> Decimal:
    rates_by_context = MODEL_RATES[base_model_id(usage.model_id)]
    rates = (
        rates_by_context.long
        if usage.input_tokens > LONG_CONTEXT_THRESHOLD
        else rates_by_context.short
    )
    cost_per_million = (
        Decimal(usage.ordinary_input_tokens) * rates.input
        + Decimal(usage.cached_input_tokens) * rates.cached_input
        + Decimal(usage.cache_write_tokens) * rates.cache_write
        + Decimal(usage.output_tokens) * rates.output
    )
    return cost_per_million / Decimal(1_000_000)

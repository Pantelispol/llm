from decimal import Decimal

from app.domain.ports import LLMUsage
from evals.llm_cost import calculate_cost_usd


def usage(
    *,
    model_id: str = "gpt-5.6-luna",
    input_tokens: int = 1000,
    cached_input_tokens: int = 100,
    cache_write_tokens: int = 200,
    output_tokens: int = 50,
    reasoning_tokens: int = 40,
) -> LLMUsage:
    return LLMUsage(
        model_id=model_id,
        reasoning_effort="low",
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        latency_ms=10,
    )


def test_reasoning_tokens_are_not_double_charged() -> None:
    # 700*0.20 + 100*0.02 + 200*0.25 + 50*1.20 = 252 per million.
    assert calculate_cost_usd(usage()) == Decimal("0.000252")


def test_long_context_uses_the_documented_terra_tier() -> None:
    # 300,000*4.00 + 1,000*18.00 = 1,218,000 per million.
    result = calculate_cost_usd(
        usage(
            model_id="gpt-5.6-terra",
            input_tokens=300_000,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1_000,
            reasoning_tokens=500,
        )
    )
    assert result == Decimal("1.218")

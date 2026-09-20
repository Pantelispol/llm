from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from app.chat_cli import SCENARIOS, run_scenario
from app.config import Settings
from app.domain.ports import LLMUsage
from app.llm.openai_provider import OpenAIProvider
from app.llm.record_replay import FixtureStore, LiveCallBudget, LLMFixture
from app.llm.redaction import install_redaction_filter
from app.orchestrator.pipeline import build_pipeline
from app.tools.catalog import CatalogRepository
from evals.llm_cost import PRICING_AS_OF, calculate_cost_usd

ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "llm"
CONVERSATION_FIXTURE_ROOT = FIXTURE_ROOT / "conversation"
SCENARIO_NOW = datetime(2026, 9, 21, 12, tzinfo=ZoneInfo("Europe/Athens"))
SCENARIO_MAX_LIVE_CALLS = 8
SCENARIO_MAX_OUTPUT_TOKENS = 1536
SYSTEM_PROMPT = (
    "Follow the current user instruction exactly. Use the catalog only as reference data. "
    "Do not add facts that the user did not request."
)
STRUCTURED_USER_PROMPT = "Return label 'checkpoint-6a' and count 1."
TEXT_USER_PROMPT = "Reply with exactly checkpoint-6a-ok."


class SmokeExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    count: int


@dataclass(frozen=True)
class ReportRow:
    call: str
    fixture: str
    usage: LLMUsage
    cost_usd: Decimal


async def exercise_provider(mode: str) -> int:
    settings = Settings(
        llm_mode=mode,
        llm_max_output_tokens=64,
        understand_model="gpt-5.6-luna",
        understand_reasoning_effort="none",
    )
    install_redaction_filter(settings.llm_api_key)
    store = FixtureStore(FIXTURE_ROOT)
    budget = LiveCallBudget(max_calls=3) if mode == "record" else None
    provider = OpenAIProvider.from_settings(
        settings,
        profile="understand",
        call_name="checkpoint_6a_smoke",
        prompt_version="provider.v1",
        catalog=CatalogRepository().catalog,
        fixture_store=store,
        live_call_budget=budget,
    )
    structured = await provider.structured(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=STRUCTURED_USER_PROMPT,
        output_type=SmokeExtraction,
    )
    if structured.output != SmokeExtraction(label="checkpoint-6a", count=1):
        raise RuntimeError("structured smoke response did not match the requested values")
    text = await provider.text(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=TEXT_USER_PROMPT,
    )
    if text.output.strip() != "checkpoint-6a-ok":
        raise RuntimeError("text smoke response did not match the requested value")
    return budget.calls_used if budget is not None else 0


def fixture_rows() -> list[ReportRow]:
    rows = []
    for path in sorted(FIXTURE_ROOT.glob("checkpoint-6a-smoke--*.json")):
        fixture = LLMFixture.model_validate_json(path.read_text(encoding="utf-8"))
        usage = fixture.response.usage
        rows.append(
            ReportRow(
                call=fixture.identity.output_format,
                fixture=path.name,
                usage=usage,
                cost_usd=calculate_cost_usd(usage),
            )
        )
    if not rows:
        raise RuntimeError("no checkpoint 6a LLM fixtures were found")
    return rows


def scenario_settings(mode: str) -> Settings:
    return Settings(
        llm_mode=mode,
        llm_max_output_tokens=SCENARIO_MAX_OUTPUT_TOKENS,
        weather_fixture="clear_day",
        rag_store="memory",
    )


async def exercise_scenario(mode: str, *, live_budget: int) -> int:
    """Run the assignment conversation so its usage lands in the cost total."""
    settings = scenario_settings(mode)
    budget = LiveCallBudget(live_budget) if mode == "record" else None
    clients: list[Any] = []

    def factory(configured: Settings, **names: str):
        pipeline = build_pipeline(configured, live_call_budget=budget, **names)
        for component in (pipeline.understander, pipeline.narrator):
            provider = component.provider
            if isinstance(provider, OpenAIProvider) and provider._client is not None:
                clients.append(provider._client)
        return pipeline

    try:
        transcript = await run_scenario(
            SCENARIOS["assignment"],
            now=SCENARIO_NOW,
            settings=settings,
            pipeline_factory=factory,
        )
    finally:
        for client in clients:
            await client.close()
    for index, (turn, result) in enumerate(transcript, start=1):
        if result.itinerary_version != index:
            raise RuntimeError(
                f"scenario turn {index} ({turn!r}) produced version "
                f"{result.itinerary_version}"
            )
    return budget.calls_used if budget is not None else 0


def scenario_rows() -> list[ReportRow]:
    rows = []
    for path in sorted(CONVERSATION_FIXTURE_ROOT.glob("conversation-turn*.json")):
        fixture = LLMFixture.model_validate_json(path.read_text(encoding="utf-8"))
        rows.append(
            ReportRow(
                call=fixture.identity.call_name,
                fixture=path.name,
                usage=fixture.response.usage,
                cost_usd=calculate_cost_usd(fixture.response.usage),
            )
        )
    return rows


def print_report(rows: list[ReportRow]) -> None:
    headers = [
        "call",
        "fixture",
        "model",
        "effort",
        "ordinary_in",
        "cached_in",
        "cache_write_in",
        "output",
        "reasoning",
        "latency_ms",
        "cost_usd",
    ]
    body = [
        [
            row.call,
            row.fixture,
            row.usage.model_id,
            row.usage.reasoning_effort,
            str(row.usage.ordinary_input_tokens),
            str(row.usage.cached_input_tokens),
            str(row.usage.cache_write_tokens),
            str(row.usage.output_tokens),
            str(row.usage.reasoning_tokens),
            f"{row.usage.latency_ms:.2f}",
            f"{row.cost_usd:.8f}",
        ]
        for row in rows
    ]
    total_usage = [
        "TOTAL",
        f"{len(rows)} fixtures",
        "-",
        "-",
        str(sum(row.usage.ordinary_input_tokens for row in rows)),
        str(sum(row.usage.cached_input_tokens for row in rows)),
        str(sum(row.usage.cache_write_tokens for row in rows)),
        str(sum(row.usage.output_tokens for row in rows)),
        str(sum(row.usage.reasoning_tokens for row in rows)),
        f"{sum(row.usage.latency_ms for row in rows):.2f}",
        f"{sum((row.cost_usd for row in rows), Decimal('0')):.8f}",
    ]
    all_rows = body + [total_usage]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in all_rows))
        for index in range(len(headers))
    ]

    def render(row: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    print(f"LLM cost report (prices as of {PRICING_AS_OF})")
    print(render(headers))
    print("-+-".join("-" * width for width in widths))
    for row in all_rows:
        print(render(row))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run checkpoint 6a LLM record/replay eval")
    parser.add_argument(
        "--record",
        action="store_true",
        help="make at most three live provider-smoke calls and write new fixtures",
    )
    parser.add_argument(
        "--record-scenario",
        action="store_true",
        help="record the three-turn assignment conversation fixtures",
    )
    parser.add_argument(
        "--scenario-budget",
        type=int,
        default=SCENARIO_MAX_LIVE_CALLS,
        choices=range(1, SCENARIO_MAX_LIVE_CALLS + 1),
    )
    args = parser.parse_args()
    calls_used = 0
    if args.record:
        print("Live preflight: max_output_tokens=64 on every request")
        print("Live preflight: SDK retries=0; structured validation retries<=1")
        print("Live preflight: 2 planned calls; hard budget=3 total calls")
        calls_used = asyncio.run(exercise_provider("record"))
    else:
        asyncio.run(exercise_provider("replay"))

    scenario_calls = 0
    if args.record_scenario:
        print(
            f"Live preflight: max_output_tokens={SCENARIO_MAX_OUTPUT_TOKENS}; "
            f"3 turns x 2 calls planned; hard budget={args.scenario_budget}"
        )
        scenario_calls = asyncio.run(
            exercise_scenario("record", live_budget=args.scenario_budget)
        )
    else:
        asyncio.run(exercise_scenario("replay", live_budget=SCENARIO_MAX_LIVE_CALLS))

    rows = fixture_rows() + scenario_rows()
    print_report(rows)
    if args.record:
        print(f"Live calls used (provider smoke): {calls_used}/3")
    if args.record_scenario:
        print(f"Live calls used (scenario): {scenario_calls}/{args.scenario_budget}")


if __name__ == "__main__":
    main()

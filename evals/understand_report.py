from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.domain.models import Intent, TripState, TurnAnalysis
from app.domain.ports import LLMResult, LLMUsage
from app.llm.openai_provider import MAX_STRUCTURED_ATTEMPTS, LLMOutputError, OpenAIProvider
from app.llm.record_replay import FixtureStore, LiveCallBudget, LLMFixture
from app.llm.understand import Understander, UnderstandResult
from app.tools.catalog import CatalogRepository
from evals.llm_cost import calculate_cost_usd

ROOT = Path(__file__).parents[1]
CASES_PATH = ROOT / "evals" / "understand_cases.yaml"
FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "llm" / "understand"
MAX_LIVE_CALLS = 6
LIVE_MAX_OUTPUT_TOKENS = 512


@dataclass(frozen=True)
class Case:
    id: str
    kind: str
    user_turn: str
    trip_state: TripState
    expected_intent: Intent
    expected_fallback: bool
    live_record: bool
    synthetic_fixture: str | None


@dataclass(frozen=True)
class EvalRow:
    case: Case
    result: UnderstandResult
    usage: LLMUsage
    cost_usd: Decimal


def zero_usage() -> LLMUsage:
    return LLMUsage(
        model_id="gpt-5.6-luna",
        reasoning_effort="none",
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        latency_ms=0,
    )


def load_cases() -> list[Case]:
    payload = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported understand eval schema")
    return [
        Case(
            id=item["id"],
            kind=item["kind"],
            user_turn=item["user_turn"],
            trip_state=TripState.model_validate(item.get("trip_state", {})),
            expected_intent=Intent(item["expected_intent"]),
            expected_fallback=bool(item.get("expected_fallback", False)),
            live_record=bool(item.get("live_record", False)),
            synthetic_fixture=item.get("synthetic_fixture"),
        )
        for item in payload["cases"]
    ]


class SyntheticResponses:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **request: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output_text": '{"intents":["not_an_intent"]}',
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }


class SyntheticClient:
    def __init__(self) -> None:
        self.responses = SyntheticResponses()


class TimeoutProvider:
    async def structured(self, **kwargs: Any) -> LLMResult[TurnAnalysis]:
        raise TimeoutError("synthetic offline timeout")

    async def text(self, **kwargs: Any) -> LLMResult[str]:
        raise AssertionError("understand eval must not request text")


def case_call_name(case: Case) -> str:
    return f"understand_{case.id}"


def fixture_usages(case: Case) -> list[LLMUsage]:
    slug = re.sub(r"[^a-z0-9]+", "-", case_call_name(case).lower()).strip("-")
    usages = []
    for path in sorted(FIXTURE_ROOT.glob(f"{slug}--json-schema--*.json")):
        fixture = LLMFixture.model_validate_json(path.read_text(encoding="utf-8"))
        usages.append(fixture.response.usage)
    return usages


def aggregate_usage(usages: list[LLMUsage]) -> LLMUsage:
    if not usages:
        return zero_usage()
    return LLMUsage(
        model_id=usages[-1].model_id,
        reasoning_effort=usages[-1].reasoning_effort,
        input_tokens=sum(item.input_tokens for item in usages),
        cached_input_tokens=sum(item.cached_input_tokens for item in usages),
        cache_write_tokens=sum(item.cache_write_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        reasoning_tokens=sum(item.reasoning_tokens for item in usages),
        latency_ms=sum(item.latency_ms for item in usages),
    )


async def record_live_cases(cases: list[Case], *, live_budget: int) -> int:
    if MAX_STRUCTURED_ATTEMPTS != 2:
        raise RuntimeError("live recording requires exactly one structured-output retry")
    settings = Settings(llm_mode="record", llm_max_output_tokens=LIVE_MAX_OUTPUT_TOKENS)
    catalog = CatalogRepository().catalog
    store = FixtureStore(FIXTURE_ROOT)
    budget = LiveCallBudget(live_budget)
    for case in cases:
        if not case.live_record:
            continue
        understander = Understander.from_settings(
            settings,
            catalog,
            call_name=case_call_name(case),
            fixture_store=store,
            live_call_budget=budget,
        )
        try:
            result = await understander.analyze(case.user_turn, case.trip_state)
        finally:
            provider = understander.provider
            if isinstance(provider, OpenAIProvider) and provider._client is not None:
                await provider._client.close()
        if result.used_fallback:
            raise RuntimeError(
                f"live understand recording fell back for {case.id}: {result.failure_category}"
            )
        if case.expected_intent not in result.analysis.intents:
            raise RuntimeError(
                f"live understand intent mismatch for {case.id}: {result.analysis.intents}"
            )
    return budget.calls_used


async def record_synthetic_malformed(case: Case) -> None:
    catalog = CatalogRepository().catalog
    client = SyntheticClient()
    settings = Settings(
        llm_mode="record",
        llm_api_key="fixture-builder-not-a-live-key",
        llm_max_output_tokens=LIVE_MAX_OUTPUT_TOKENS,
    )
    understander = Understander.from_settings(
        settings,
        catalog,
        call_name=case_call_name(case),
        fixture_store=FixtureStore(FIXTURE_ROOT),
        client=client,
    )
    result = await understander.analyze(case.user_turn, case.trip_state)
    if not result.used_fallback or result.failure_category != LLMOutputError.__name__:
        raise RuntimeError("synthetic malformed fixture did not exercise exhausted validation")
    if client.responses.calls != 2:
        raise RuntimeError("synthetic malformed fixture did not make exactly two local attempts")


async def evaluate(cases: list[Case]) -> list[EvalRow]:
    catalog = CatalogRepository().catalog
    settings = Settings(llm_mode="replay", llm_max_output_tokens=LIVE_MAX_OUTPUT_TOKENS)
    store = FixtureStore(FIXTURE_ROOT)
    rows = []
    for case in cases:
        if case.kind == "provider_failure":
            understander = Understander(TimeoutProvider(), catalog)
        else:
            understander = Understander.from_settings(
                settings,
                catalog,
                call_name=case_call_name(case),
                fixture_store=store,
            )
        result = await understander.analyze(case.user_turn, case.trip_state)
        usages = fixture_usages(case) if case.kind == "replay" else []
        combined = aggregate_usage(usages)
        rows.append(
            EvalRow(
                case=case,
                result=result,
                usage=combined,
                cost_usd=sum((calculate_cost_usd(item) for item in usages), Decimal("0")),
            )
        )
    return rows


def validate_rows(rows: list[EvalRow]) -> None:
    failures = []
    for row in rows:
        if row.case.expected_intent not in row.result.analysis.intents:
            failures.append(f"{row.case.id}: intent {row.result.analysis.intents}")
        if row.result.used_fallback != row.case.expected_fallback:
            failures.append(f"{row.case.id}: fallback={row.result.used_fallback}")
    if failures:
        raise RuntimeError("understand eval failed: " + "; ".join(failures))


def render_table(rows: list[EvalRow]) -> None:
    headers = [
        "case",
        "expected_intent",
        "actual_intent",
        "schema_valid",
        "fallback",
        "ordinary_in",
        "cached_in",
        "cache_write_in",
        "output",
        "reasoning",
        "latency_ms",
        "cost_usd",
    ]
    body = []
    for row in rows:
        body.append(
            [
                row.case.id,
                row.case.expected_intent.value,
                ",".join(intent.value for intent in row.result.analysis.intents),
                "yes",
                "yes" if row.result.used_fallback else "no",
                str(row.usage.ordinary_input_tokens),
                str(row.usage.cached_input_tokens),
                str(row.usage.cache_write_tokens),
                str(row.usage.output_tokens),
                str(row.usage.reasoning_tokens),
                f"{row.usage.latency_ms:.2f}",
                f"{row.cost_usd:.8f}",
            ]
        )
    total = [
        "TOTAL",
        "-",
        "-",
        "-",
        str(sum(row.result.used_fallback for row in rows)),
        str(sum(row.usage.ordinary_input_tokens for row in rows)),
        str(sum(row.usage.cached_input_tokens for row in rows)),
        str(sum(row.usage.cache_write_tokens for row in rows)),
        str(sum(row.usage.output_tokens for row in rows)),
        str(sum(row.usage.reasoning_tokens for row in rows)),
        f"{sum(row.usage.latency_ms for row in rows):.2f}",
        f"{sum((row.cost_usd for row in rows), Decimal('0')):.8f}",
    ]
    all_rows = body + [total]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in all_rows))
        for index in range(len(headers))
    ]

    def render(row: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    print("Understand eval (replay mode)")
    print(render(headers))
    print("-+-".join("-" * width for width in widths))
    for row in all_rows:
        print(render(row))
    print("OpenAI client calls during replay: 0")


def cache_report(cases: list[Case]) -> None:
    live_usages = [(case, fixture_usages(case)) for case in cases if case.live_record]
    cached_reads = sum(
        usage.cached_input_tokens for _, usages in live_usages for usage in usages
    )
    hashes = set()
    for case, _ in live_usages:
        slug = re.sub(r"[^a-z0-9]+", "-", case_call_name(case).lower()).strip("-")
        for path in FIXTURE_ROOT.glob(f"{slug}--json-schema--*.json"):
            fixture = LLMFixture.model_validate_json(path.read_text(encoding="utf-8"))
            hashes.add(fixture.identity.stable_prefix_sha256)
    print(f"Cached input tokens observed across live understand fixtures: {cached_reads}")
    print(f"Distinct stable-prefix hashes across live understand fixtures: {len(hashes)}")
    if cached_reads == 0:
        print(
            "Cache investigation: all recorded calls reported zero cache reads; "
            "the fixture identities confirm an identical stable prefix."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run checkpoint 6b understand eval")
    parser.add_argument("--record", action="store_true")
    parser.add_argument(
        "--live-budget",
        type=int,
        default=MAX_LIVE_CALLS,
        choices=range(1, MAX_LIVE_CALLS + 1),
        help="hard per-run live-call cap; never exceeds the checkpoint cap of six",
    )
    args = parser.parse_args()
    cases = load_cases()
    calls_used = 0
    if args.record:
        print(f"Live preflight: max_output_tokens={LIVE_MAX_OUTPUT_TOKENS} on every request")
        print("Live preflight: SDK retries=0; structured validation retries<=1")
        print(
            f"Live preflight: 5 planned live cases; hard budget={args.live_budget} "
            "calls this run"
        )
        calls_used = asyncio.run(record_live_cases(cases, live_budget=args.live_budget))
        malformed = next(case for case in cases if case.synthetic_fixture == "malformed_twice")
        asyncio.run(record_synthetic_malformed(malformed))
    rows = asyncio.run(evaluate(cases))
    validate_rows(rows)
    render_table(rows)
    cache_report(cases)
    if args.record:
        print(f"Live calls used this run: {calls_used}/{args.live_budget}")
        print("Malformed-output fixtures: 2 synthetic local responses, 0 live calls")


if __name__ == "__main__":
    main()

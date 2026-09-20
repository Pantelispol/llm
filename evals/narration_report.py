"""Checkpoint 6d narration eval.

Rebuilds every frozen narration case, re-validates its itinerary with the real
validator, materializes the evidence registry from the catalog, the
opening-hours engine, and the content corpus, then narrates each case with
Luna and with Terra. Replay is the default; recording is explicit and budgeted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml

from app.config import Settings
from app.domain.catalog import PoiCatalog
from app.domain.models import (
    Activity,
    ActivityKind,
    EvidenceItem,
    EvidenceKind,
    Itinerary,
    NarrationBundle,
    NarrationDisclosure,
    TimeWindow,
    TripState,
    ValidationResult,
)
from app.domain.ports import (
    LLMUsage,
    PlanningContext,
    TravelMatrixRequest,
    TravelMatrixResult,
)
from app.llm.narrate import NarrationResult, Narrator
from app.llm.openai_provider import OpenAIProvider
from app.llm.record_replay import FixtureStore, LiveCallBudget
from app.orchestrator.evidence import (
    admission_evidence,
    approximate_travel_evidence,
    hours_evidence,
    weather_unavailable_evidence,
)
from app.orchestrator.language import resolve_turn_language
from app.planning.rules import pace_factors_for
from app.planning.validator import DeterministicItineraryValidator
from app.rag.ingest import chunk_document, load_content_docs
from app.tools.catalog import CatalogRepository
from app.tools.opening_hours import OpeningHoursEngine
from app.tools.travel import PrecomputedTravelTimeProvider, build_matrix
from app.tools.weather import parse_open_meteo
from app.tools.weather_flags import WeatherThresholds, derive_weather_flags
from evals.llm_cost import calculate_cost_usd

ROOT = Path(__file__).parents[1]
CASES_PATH = ROOT / "evals" / "narration_cases.yaml"
FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "llm" / "narrate"
ATHENS = ZoneInfo("Europe/Athens")
MODEL_IDS = {"luna": "gpt-5.6-luna", "terra": "gpt-5.6-terra"}
#: Luna is the configured narration default; Terra is a paid comparison run.
DEFAULT_MODEL_KEY = "luna"
LIVE_MAX_OUTPUT_TOKENS = 1536
#: Five cases plus, in the worst case, one post-check retry each.
MAX_LIVE_CALLS_PER_MODEL = 10
NOW = datetime(2026, 9, 20, 12, tzinfo=ATHENS)


@dataclass(frozen=True)
class NarrationCase:
    id: str
    kind: str
    user_question: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class EvalRow:
    case: NarrationCase
    model_key: str
    result: NarrationResult
    cost_usd: Decimal


def load_cases(path: Path = CASES_PATH) -> tuple[date, list[NarrationCase]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported narration eval schema")
    cases = [
        NarrationCase(
            id=item["id"],
            kind=item["kind"],
            user_question=item["user_question"],
            payload=item,
        )
        for item in payload["cases"]
    ]
    return payload["plan_date"], cases


class CaseEnvironment:
    """Shared, deterministic tool wiring for every narration case."""

    def __init__(self, plan_date: date) -> None:
        self.plan_date = plan_date
        self.repository = CatalogRepository()
        self.catalog: PoiCatalog = self.repository.catalog
        self.hours = OpeningHoursEngine(self.repository)
        self.validator = DeterministicItineraryValidator()
        self.locations = {poi.id: poi.coordinates for poi in self.catalog.pois}
        self.precomputed = PrecomputedTravelTimeProvider().matrix(
            TravelMatrixRequest(locations=self.locations)
        )
        self.approximate = self._approximate_matrix()
        self.chunks = {
            chunk.chunk_id: chunk
            for document in load_content_docs()
            for chunk in chunk_document(document, self.repository.get(document.poi_id))
        }

    def _approximate_matrix(self) -> TravelMatrixResult:
        matrix_file = build_matrix(self.catalog)
        if not matrix_file.approximate:
            raise RuntimeError("expected the haversine fallback matrix to be approximate")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write(matrix_file.model_dump_json())
            path = Path(handle.name)
        try:
            return PrecomputedTravelTimeProvider(path).matrix(
                TravelMatrixRequest(locations=self.locations)
            )
        finally:
            path.unlink(missing_ok=True)

    def at(self, clock: str) -> datetime:
        hour, minute = (int(part) for part in clock.split(":"))
        return datetime.combine(self.plan_date, time(hour, minute), tzinfo=ATHENS)

    def context(self, case: NarrationCase, state: TripState) -> PlanningContext:
        fixture = case.payload.get("weather_fixture", "clear_day")
        unavailable = case.payload.get("weather_unavailable_reason")
        flags = []
        if fixture != "none":
            path = ROOT / "evals" / "fixtures" / "weather" / f"{fixture}.json"
            weather = parse_open_meteo(
                json.loads(path.read_text(encoding="utf-8")),
                fetched_at=NOW,
                source=f"fixture:{fixture}",
                is_fixture=True,
            )
            flags = derive_weather_flags(weather, state.party, WeatherThresholds()).hours
        matrix = (
            self.approximate
            if case.payload.get("travel") == "approximate"
            else self.precomputed
        )
        return PlanningContext(
            now=NOW,
            catalog=self.catalog,
            opening_hours=self.hours,
            candidates=[],
            hourly_weather_flags=flags,
            weather_unavailable_reason=unavailable,
            travel_matrix=matrix,
            pace_factors=pace_factors_for(state),
        )


def _itinerary(environment: CaseEnvironment, case: NarrationCase) -> Itinerary:
    pois = {poi.id: poi for poi in environment.catalog.pois}
    activities = []
    for item in case.payload["activities"]:
        start = environment.at(item["start"])
        minutes = int(item["visit_minutes"])
        activities.append(
            Activity(
                kind=ActivityKind(item.get("kind", "visit")),
                poi_id=item["poi_id"],
                start=start,
                end=start + timedelta(minutes=minutes),
                visit_minutes=minutes,
                travel_from_previous_minutes=int(item.get("travel_from_previous_minutes", 0)),
                buffer_before_minutes=int(item.get("buffer_before_minutes", 0)),
                exposure=pois[item["poi_id"]].exposure,
            )
        )
    window = case.payload["window"]
    return Itinerary(
        window=TimeWindow(
            start=environment.at(window["start"]),
            end=environment.at(window["end"]),
        ),
        activities=activities,
        total_travel_minutes=int(case.payload["total_travel_minutes"]),
        approximate_travel_times=case.payload.get("travel") == "approximate",
    )


def _evidence(environment: CaseEnvironment, case: NarrationCase) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for spec in case.payload.get("evidence", []):
        kind = spec["kind"]
        if kind == "rag":
            chunk = environment.chunks[spec["chunk_id"]]
            items.append(
                EvidenceItem(
                    evidence_id=chunk.chunk_id,
                    kind=EvidenceKind.RAG,
                    poi_id=chunk.poi_id,
                    text=chunk.text,
                    source=f"data/content/{chunk.poi_id}.md",
                    is_untrusted=chunk.is_untrusted,
                )
            )
        elif kind == "hours":
            item = hours_evidence(environment.hours, spec["poi_id"], environment.plan_date)
            if item is None:
                raise RuntimeError(f"{spec['poi_id']} has no hours on the frozen plan date")
            items.append(item)
        elif kind == "catalog" and spec.get("field") == "admission":
            item = admission_evidence(environment.repository, spec["poi_id"])
            if item is None:
                raise RuntimeError(f"{spec['poi_id']} has no catalog admission price")
            items.append(item)
        elif kind == "weather":
            items.append(
                weather_unavailable_evidence(
                    environment.plan_date, case.payload["weather_unavailable_reason"]
                )
            )
        elif kind == "travel":
            items.append(approximate_travel_evidence("haversine_fallback"))
        else:
            raise RuntimeError(f"unsupported evidence spec in case {case.id}: {spec}")
    return items


def build_bundle(environment: CaseEnvironment, case: NarrationCase) -> NarrationBundle:
    """Rebuild one frozen case, re-validating the plan before narration sees it."""
    plan: Itinerary | None = None
    validation: ValidationResult | None = None
    catalog_poi_ids = list(case.payload.get("catalog_poi_ids", []))
    if case.kind == "plan":
        window = case.payload["window"]
        state = TripState(
            time_window=TimeWindow(
                start=environment.at(window["start"]),
                end=environment.at(window["end"]),
            ),
            interests=list(case.payload.get("interests", [])),
        )
        plan = _itinerary(environment, case)
        validation = environment.validator.validate(
            plan, state, environment.context(case, state)
        )
        if not validation.is_valid:
            codes = [item.code.value for item in validation.violations]
            raise RuntimeError(f"frozen case {case.id} no longer validates: {codes}")
        catalog_poi_ids = [
            activity.poi_id for activity in plan.activities if activity.poi_id
        ]
    return NarrationBundle(
        request_id=f"narrate-{case.id}",
        language=resolve_turn_language(case.user_question),
        user_question=case.user_question,
        plan=plan,
        validation=validation,
        catalog_poi_ids=catalog_poi_ids,
        evidence=_evidence(environment, case),
        disclosures=[
            NarrationDisclosure(value) for value in case.payload.get("disclosures", [])
        ],
    )


def case_call_name(case: NarrationCase) -> str:
    return f"narrate_{case.id}"


def fixture_root(model_key: str) -> Path:
    return FIXTURE_ROOT / model_key


def _settings(model_key: str, mode: str) -> Settings:
    return Settings(
        llm_mode=mode,
        llm_max_output_tokens=LIVE_MAX_OUTPUT_TOKENS,
        narrate_model=MODEL_IDS[model_key],
        narrate_reasoning_effort="low",
    )


async def run_model(
    environment: CaseEnvironment,
    cases: list[NarrationCase],
    *,
    model_key: str,
    mode: Literal["replay", "record"],
    live_budget: int = MAX_LIVE_CALLS_PER_MODEL,
) -> tuple[list[EvalRow], int]:
    settings = _settings(model_key, mode)
    store = FixtureStore(fixture_root(model_key))
    budget = LiveCallBudget(live_budget) if mode == "record" else None
    rows: list[EvalRow] = []
    for case in cases:
        narrator = Narrator.from_settings(
            settings,
            environment.catalog,
            call_name=case_call_name(case),
            fixture_store=store,
            live_call_budget=budget,
        )
        bundle = build_bundle(environment, case)
        before = bundle.plan.model_dump_json() if bundle.plan else "null"
        try:
            result = await narrator.narrate(bundle)
        finally:
            provider = narrator.provider
            if isinstance(provider, OpenAIProvider) and provider._client is not None:
                await provider._client.close()
        after = bundle.plan.model_dump_json() if bundle.plan else "null"
        if before != after:
            raise RuntimeError(f"narration mutated the validated plan for {case.id}")
        rows.append(
            EvalRow(
                case=case,
                model_key=model_key,
                result=result,
                cost_usd=sum(
                    (calculate_cost_usd(usage) for usage in result.usages), Decimal("0")
                ),
            )
        )
    return rows, budget.calls_used if budget else 0


def _aggregate(usages: list[LLMUsage], field: str) -> int:
    return sum(getattr(usage, field) for usage in usages)


def render_table(model_key: str, rows: list[EvalRow]) -> None:
    headers = [
        "case",
        "grounded",
        "first_draft",
        "violations",
        "retry",
        "fallback",
        "ordinary_in",
        "cache_write_in",
        "cached_in",
        "output",
        "reasoning",
        "latency_ms",
        "cost_usd",
    ]
    body = []
    for row in rows:
        result = row.result
        first_codes = [item.code.value for item in result.first_draft_violations]
        retry_state = "-"
        if result.retried:
            retry_state = "failed" if result.used_fallback else "passed"
        body.append(
            [
                row.case.id,
                "no" if result.used_fallback else "yes",
                "pass" if result.first_draft_passed else "fail",
                ",".join(first_codes) or "-",
                retry_state,
                "template" if result.used_fallback else "-",
                str(_aggregate(result.usages, "ordinary_input_tokens")),
                str(_aggregate(result.usages, "cache_write_tokens")),
                str(_aggregate(result.usages, "cached_input_tokens")),
                str(_aggregate(result.usages, "output_tokens")),
                str(_aggregate(result.usages, "reasoning_tokens")),
                f"{sum(usage.latency_ms for usage in result.usages):.2f}",
                f"{row.cost_usd:.8f}",
            ]
        )
    passes = sum(row.result.first_draft_passed for row in rows)
    subtotal = [
        f"SUBTOTAL {model_key}",
        f"{sum(not row.result.used_fallback for row in rows)}/{len(rows)}",
        f"{passes}/{len(rows)}",
        "-",
        str(sum(row.result.retried for row in rows)),
        str(sum(row.result.used_fallback for row in rows)),
        str(sum(_aggregate(row.result.usages, "ordinary_input_tokens") for row in rows)),
        str(sum(_aggregate(row.result.usages, "cache_write_tokens") for row in rows)),
        str(sum(_aggregate(row.result.usages, "cached_input_tokens") for row in rows)),
        str(sum(_aggregate(row.result.usages, "output_tokens") for row in rows)),
        str(sum(_aggregate(row.result.usages, "reasoning_tokens") for row in rows)),
        f"{sum(usage.latency_ms for row in rows for usage in row.result.usages):.2f}",
        f"{sum((row.cost_usd for row in rows), Decimal('0')):.8f}",
    ]
    table = body + [subtotal]
    widths = [
        max(len(headers[index]), *(len(line[index]) for line in table))
        for index in range(len(headers))
    ]

    def render(line: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(line))

    print(f"Narration eval — {MODEL_IDS[model_key]} (reasoning effort low)")
    print(render(headers))
    print("-+-".join("-" * width for width in widths))
    for line in table:
        print(render(line))
    print(f"First-draft post-check pass rate: {passes}/{len(rows)}")


def print_answers(model_key: str, rows: list[EvalRow]) -> None:
    print(f"\nRendered answers — {MODEL_IDS[model_key]}")
    for row in rows:
        print(f"\n[{row.case.id}]")
        print(row.result.answer)


def missing_fixtures(model_key: str, cases: list[NarrationCase]) -> list[str]:
    root = fixture_root(model_key)
    missing = []
    for case in cases:
        slug = re.sub(r"[^a-z0-9]+", "-", case_call_name(case).lower()).strip("-")
        if not list(root.glob(f"{slug}--text--*.json")):
            missing.append(case.id)
    return missing


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the checkpoint 6d narration eval")
    parser.add_argument("--record", action="store_true")
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_IDS),
        action="append",
        help=(
            "model to evaluate; defaults to the configured narration model. Terra "
            "fixtures are an explicit opt-in re-recording."
        ),
    )
    parser.add_argument(
        "--live-budget",
        type=int,
        default=MAX_LIVE_CALLS_PER_MODEL,
        choices=range(1, MAX_LIVE_CALLS_PER_MODEL + 1),
        help="hard per-model live-call cap for this run",
    )
    parser.add_argument("--show-answers", action="store_true")
    args = parser.parse_args()

    plan_date, cases = load_cases()
    environment = CaseEnvironment(plan_date)
    model_keys = args.model or [DEFAULT_MODEL_KEY]
    mode: Literal["replay", "record"] = "record" if args.record else "replay"
    if args.record:
        print(f"Live preflight: max_output_tokens={LIVE_MAX_OUTPUT_TOKENS} on every request")
        print("Live preflight: SDK retries=0; narration post-check retries<=1")
        print(
            f"Live preflight: {len(cases)} cases x {len(model_keys)} model(s); "
            f"hard budget={args.live_budget} calls per model"
        )

    totals: Decimal = Decimal("0")
    for model_key in model_keys:
        rows, used = asyncio.run(
            run_model(
                environment,
                cases,
                model_key=model_key,
                mode=mode,
                live_budget=args.live_budget,
            )
        )
        render_table(model_key, rows)
        if args.show_answers:
            print_answers(model_key, rows)
        totals += sum((row.cost_usd for row in rows), Decimal("0"))
        if args.record:
            print(f"Live calls used for {model_key}: {used}/{args.live_budget}")
        print()
    print(f"COMBINED TOTAL COST across {len(model_keys)} model(s): {totals:.8f} USD")


if __name__ == "__main__":
    main()

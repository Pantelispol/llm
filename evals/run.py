"""Phase 7 end-to-end evaluation harness.

Runs the shipped pipeline over the frozen case set in `evals/cases.yaml` and
reports measured numbers. Nothing here scores an answer with a model: every
metric is computed by code from what the pipeline returned.

Model variance is sampled once, at record time. Repetition `k` of a case gets
its own narration fixture namespace, so three recordings hold three genuinely
different model samples and `make eval-full` replays all three. The pass rate
is therefore both stochastic in origin and byte-reproducible in CI.

Understanding runs at `effort=none` on a constrained extraction schema and is
recorded once per case; only narration is re-recorded per repetition, so an
extra repetition costs one live call rather than two.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.config import Settings
from app.domain.models import TripState, ViolationSeverity
from app.domain.ports import LLMUsage
from app.llm.openai_provider import OpenAIProvider
from app.llm.record_replay import FixtureStore, LiveCallBudget
from app.orchestrator.models import PipelineResult, ToolName
from app.orchestrator.pipeline import ConversationPipeline, build_pipeline
from app.safety.answer_postcheck import (
    CAPITALIZED_WORD_PATTERN,
    PROPER_NAME_ALLOWLIST,
    SENTENCE_SPLIT_PATTERN,
    catalog_name_phrases,
    normalize,
)
from app.tools.catalog import CatalogRepository
from app.tools.weather import parse_open_meteo
from app.tools.weather_flags import derive_weather_flags, thresholds_from_settings
from evals.llm_cost import PRICING_AS_OF, calculate_cost_usd

ROOT = Path(__file__).parents[1]
CASES_PATH = ROOT / "evals" / "cases.yaml"
FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "llm" / "phase7"
CONVERSATION_FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "llm" / "conversation"
ATHENS = ZoneInfo("Europe/Athens")
MAX_OUTPUT_TOKENS = 1536
DEFAULT_REPETITIONS = 3
NOT_RECORDED = "ReplayFixtureNotFound"
ALREADY_RECORDED = "FixtureAlreadyExists"
WEATHER_FIXTURE_DIR = ROOT / "evals" / "fixtures" / "weather"
_ONE_HOUR = timedelta(hours=1)

ENTITY_CODES = frozenset(
    {"POI_NOT_IN_CATALOG", "POI_NOT_IN_BUNDLE", "RAW_POI_NAME", "UNKNOWN_PROPER_NAME"}
)


# ----------------------------------------------------------------------
# case model
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    user_turn: str
    checks: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Case:
    id: str
    coverage: str
    kind: str
    turns: tuple[Turn, ...]
    now: datetime
    weather_fixture: str
    expected_tools: tuple[ToolName, ...]
    seed_case: str | None = None
    reuse_fixtures: str | None = None

    @property
    def reuses_conversation(self) -> bool:
        return self.reuse_fixtures == "conversation"


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported Phase 7 eval schema")
    defaults = payload.get("defaults", {})
    cases = []
    for item in payload["cases"]:
        if "turns" in item:
            turns = tuple(
                Turn(user_turn=turn["user_turn"], checks=turn.get("checks", {}))
                for turn in item["turns"]
            )
        else:
            turns = (Turn(user_turn=item["user_turn"], checks=item.get("checks", {})),)
        cases.append(
            Case(
                id=item["id"],
                coverage=item["coverage"],
                kind=item.get("kind", "single"),
                turns=turns,
                now=datetime.fromisoformat(item.get("now", defaults["now"])),
                weather_fixture=item.get(
                    "weather_fixture", defaults.get("weather_fixture", "clear_day")
                ),
                expected_tools=tuple(ToolName(name) for name in item["expected_tools"]),
                seed_case=item.get("seed_case"),
                reuse_fixtures=item.get("reuse_fixtures"),
            )
        )
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate case ids in evals/cases.yaml")
    return cases


class EvalRetriever:
    """The hybrid retriever, pinned to the deterministic hash encoder.

    The app defaults to the E5 sentence-transformer. The eval pins the hash
    encoder instead so `make eval-full` is byte-reproducible on a fresh clone
    with no model download: retrieved chunk ids feed the narration prompt, so a
    different encoder would change the fixture identities and break replay.
    Retrieval *quality* is reported separately from the Phase 5 gold set.
    """

    def __init__(self) -> None:
        self._retriever: Any = None

    async def search(self, query: str, *, limit: int = 5):
        if self._retriever is None:
            from app.rag.dense import HashEncoder
            from app.rag.ingest import ingest_corpus
            from app.rag.retriever import HybridRetriever

            self._retriever = HybridRetriever(ingest_corpus(), HashEncoder())
        return await self._retriever.search(query, limit=limit)


# ----------------------------------------------------------------------
# running
# ----------------------------------------------------------------------


@dataclass
class TurnRun:
    turn: Turn
    result: PipelineResult
    failures: list[str] = field(default_factory=list)


@dataclass
class RepetitionRun:
    repetition: int
    turns: list[TurnRun] = field(default_factory=list)
    recorded: bool = True

    @property
    def passed(self) -> bool:
        return self.recorded and not any(run.failures for run in self.turns)

    @property
    def failures(self) -> list[str]:
        return [failure for run in self.turns for failure in run.failures]


@dataclass
class CaseRun:
    case: Case
    repetitions: list[RepetitionRun] = field(default_factory=list)

    @property
    def attempted(self) -> list[RepetitionRun]:
        return [item for item in self.repetitions if item.recorded]

    @property
    def passes(self) -> int:
        return sum(1 for item in self.attempted if item.passed)


def case_settings(case: Case, mode: str) -> Settings:
    return Settings(
        llm_mode=mode,
        llm_max_output_tokens=MAX_OUTPUT_TOKENS,
        weather_fixture=case.weather_fixture,
        rag_store="memory",
    )


def call_names(case: Case, repetition: int, turn_index: int) -> tuple[str, str]:
    """Understanding is recorded once per case; narration once per repetition."""
    if case.reuses_conversation:
        return (
            f"conversation_turn{turn_index}_understand",
            f"conversation_turn{turn_index}_narrate",
        )
    return f"case_{case.id}_understand", f"case_{case.id}_rep{repetition}_narrate"


def _understand_recorded(case: Case) -> bool:
    """Understanding is recorded once per case, and never re-recorded.

    The provider refuses to overwrite a fixture, so asking for a second
    recording would degrade the turn rather than reuse the first one.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"case_{case.id}_understand".lower()).strip("-")
    return any(FIXTURE_ROOT.glob(f"{slug}--json-schema--*.json"))


def make_pipeline(
    case: Case,
    repetition: int,
    turn_index: int,
    *,
    understand_mode: str,
    narrate_mode: str,
    budget: LiveCallBudget | None,
    retriever: EvalRetriever,
    clients: list[Any],
) -> ConversationPipeline:
    understand_call, narrate_call = call_names(case, repetition, turn_index)
    root = CONVERSATION_FIXTURE_ROOT if case.reuses_conversation else FIXTURE_ROOT
    pipeline = build_pipeline(
        case_settings(case, understand_mode),
        narrate_settings=case_settings(case, narrate_mode),
        retriever=retriever,
        understand_call_name=understand_call,
        narrate_call_name=narrate_call,
        fixture_store=FixtureStore(root),
        live_call_budget=budget,
    )
    for component in (pipeline.understander, pipeline.narrator):
        provider = component.provider
        if isinstance(provider, OpenAIProvider) and provider._client is not None:
            clients.append(provider._client)
    return pipeline


async def _seed_state(
    case: Case,
    cases_by_id: dict[str, Case],
    retriever: EvalRetriever,
    clients: list[Any],
) -> TripState:
    """Replay a dependency case to reach the state this case edits.

    The dependency's fixtures are already recorded under its own call names, so
    seeding always replays and never spends a live call.
    """
    if case.seed_case is None:
        return TripState()
    seed = cases_by_id[case.seed_case]
    pipeline = make_pipeline(
        seed,
        1,
        1,
        understand_mode="replay",
        narrate_mode="replay",
        budget=None,
        retriever=retriever,
        clients=clients,
    )
    result = await pipeline.run_turn(seed.turns[0].user_turn, TripState(), now=seed.now)
    return result.trip_state


async def run_repetition(
    case: Case,
    repetition: int,
    cases_by_id: dict[str, Case],
    *,
    record_repetition: int | None,
    budget: LiveCallBudget | None,
    retriever: EvalRetriever,
    clients: list[Any],
) -> RepetitionRun:
    # Recording is one repetition per pass: a fixture is never overwritten, so a
    # second pass over an already-recorded repetition would fail rather than reuse it.
    live = record_repetition == repetition and not case.reuses_conversation
    understand_mode = "record" if (live and not _understand_recorded(case)) else "replay"
    narrate_mode = "record" if live else "replay"

    run = RepetitionRun(repetition=repetition)
    baseline = await _seed_state(case, cases_by_id, retriever, clients)
    state = baseline
    previous: PipelineResult | None = None
    for turn_index, turn in enumerate(case.turns, start=1):
        pipeline = make_pipeline(
            case,
            repetition,
            turn_index,
            understand_mode=understand_mode,
            narrate_mode=narrate_mode,
            budget=budget,
            retriever=retriever,
            clients=clients,
        )
        result = await pipeline.run_turn(turn.user_turn, state, now=case.now)
        categories = {result.failure_category, _understand_category(result)}
        if ALREADY_RECORDED in categories:
            # The provider refuses to overwrite a fixture, and the pipeline turns
            # that refusal into an ordinary provider failure — which would record
            # the *next* call against a degraded analysis. Stop instead.
            raise RuntimeError(
                f"{case.id} repetition {repetition}: a fixture for this call already "
                "exists; delete it or record a different repetition"
            )
        if NOT_RECORDED in categories:
            run.recorded = False
            return run
        state = result.trip_state
        failures = evaluate_checks(case, turn, result, previous, baseline)
        run.turns.append(TurnRun(turn=turn, result=result, failures=failures))
        previous = result
    return run


def _understand_category(result: PipelineResult) -> str | None:
    return result.understand_failure_category


async def run_case(
    case: Case,
    cases_by_id: dict[str, Case],
    *,
    repetitions: int,
    record_repetition: int | None,
    budget: LiveCallBudget | None,
    retriever: EvalRetriever,
    clients: list[Any],
) -> CaseRun:
    run = CaseRun(case=case)
    # A case that reuses Phase 6e fixtures has exactly one recorded sample;
    # replaying it three times would report a pass rate that is not one.
    effective = 1 if case.reuses_conversation else repetitions
    for repetition in range(1, effective + 1):
        run.repetitions.append(
            await run_repetition(
                case,
                repetition,
                cases_by_id,
                record_repetition=record_repetition,
                budget=budget,
                retriever=retriever,
                clients=clients,
            )
        )
    return run


# ----------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------

REPOSITORY = CatalogRepository()
CATALOG = REPOSITORY.catalog
POI_BY_ID = {poi.id: poi for poi in CATALOG.pois}


def _plan_poi_ids(result: PipelineResult) -> list[str]:
    itinerary = result.trip_state.itinerary
    if itinerary is None:
        return []
    return [
        activity.poi_id for activity in itinerary.activities if activity.poi_id is not None
    ]


def _flagged_hours(case: Case, result: PipelineResult, risk: str) -> set[datetime]:
    """Recompute weather flags straight from the case fixture file.

    Deliberately not read back from the pipeline: a check that reuses the value
    under test proves nothing.
    """
    payload = json.loads(
        (WEATHER_FIXTURE_DIR / f"{case.weather_fixture}.json").read_text(encoding="utf-8")
    )
    weather = parse_open_meteo(
        payload,
        fetched_at=case.now,
        source=f"fixture:{case.weather_fixture}",
        is_fixture=True,
    )
    report = derive_weather_flags(
        weather, result.trip_state.party, thresholds_from_settings(Settings())
    )
    attribute = {"rain": "rain_risk", "heat": "heat_risk", "storm": "storm"}[risk]
    return {hour.at for hour in report.hours if getattr(hour, attribute)}


def evaluate_checks(
    case: Case,
    turn: Turn,
    result: PipelineResult,
    previous: PipelineResult | None,
    baseline: TripState,
) -> list[str]:
    failures: list[str] = []
    failures.extend(_universal_checks(result))
    itinerary = result.trip_state.itinerary
    plan_ids = _plan_poi_ids(result)
    answer = result.answer

    for name, expected in turn.checks.items():
        if name == "language":
            if result.language.value != expected:
                failures.append(f"language={result.language.value} expected {expected}")
        elif name == "plan_committed":
            if result.plan_committed != expected:
                failures.append(f"plan_committed={result.plan_committed}")
        elif name == "itinerary_version":
            if result.itinerary_version != expected:
                failures.append(f"itinerary_version={result.itinerary_version}")
        elif name == "min_activities":
            count = len(itinerary.activities) if itinerary else 0
            if count < expected:
                failures.append(f"activities={count} < {expected}")
        elif name == "plan_date":
            actual = (
                itinerary.window.start.astimezone(ATHENS).date().isoformat()
                if itinerary
                else None
            )
            if actual != expected:
                failures.append(f"plan_date={actual}")
        elif name == "plan_excludes_categories":
            offenders = [
                poi_id for poi_id in plan_ids if POI_BY_ID[poi_id].category in expected
            ]
            if offenders:
                failures.append(f"excluded category planned: {offenders}")
        elif name == "state_excludes_categories":
            missing = set(expected) - set(result.trip_state.exclude_categories)
            if missing:
                failures.append(f"state missing exclude_categories {sorted(missing)}")
        elif name == "state_children_ages":
            if list(result.trip_state.party.children_ages) != list(expected):
                failures.append(
                    f"children_ages={list(result.trip_state.party.children_ages)}"
                )
        elif name == "includes_poi_ids":
            missing = [poi_id for poi_id in expected if poi_id not in plan_ids]
            if missing:
                failures.append(f"plan missing {missing}")
        elif name == "no_outdoor_activity_in_flagged_hours":
            failures.extend(_weather_exposure_failures(case, result, expected))
        elif name == "answer_contains":
            missing = [needle for needle in expected if needle not in answer]
            if missing:
                failures.append(f"answer missing {missing}")
        elif name == "answer_contains_any":
            if not any(needle in answer for needle in expected):
                failures.append(f"answer contains none of {expected}")
        elif name == "answer_excludes":
            present = [needle for needle in expected if needle in answer]
            if present:
                failures.append(f"answer contains forbidden {present}")
        elif name == "min_citations":
            if len(result.citations) < expected:
                failures.append(f"citations={len(result.citations)} < {expected}")
        elif name == "max_citations":
            if len(result.citations) > expected:
                failures.append(f"citations={len(result.citations)} > {expected}")
        elif name == "no_fallback":
            if result.used_fallback != (not expected):
                failures.append(f"used_fallback={result.used_fallback}")
        elif name == "narration_invoked":
            if result.narration_invoked != expected:
                failures.append(f"narration_invoked={result.narration_invoked}")
        elif name == "min_violations":
            if len(result.violations) < expected:
                failures.append(f"violations={len(result.violations)} < {expected}")
        elif name == "preserves_previous_poi_ids":
            before = set(_plan_poi_ids(previous)) if previous else set()
            kept = before & set(plan_ids)
            if len(kept) < expected:
                failures.append(f"preserved {len(kept)} POIs < {expected}")
        elif name == "position_poi_changed":
            failures.extend(_position_changed_failures(baseline, plan_ids, expected))
        elif name == "position_exposure":
            failures.extend(_position_exposure_failures(plan_ids, expected))
        elif name == "answer_uses_greek_poi_names":
            greek = [
                poi_id
                for poi_id in plan_ids
                if POI_BY_ID[poi_id].names.el
                and POI_BY_ID[poi_id].names.el in answer
            ]
            if expected and not greek:
                failures.append("answer uses no Greek POI name")
        else:
            raise RuntimeError(f"unknown check in evals/cases.yaml: {name}")
    return failures


def _position_changed_failures(
    baseline: TripState, plan_ids: list[str], position: int
) -> list[str]:
    before = (
        [
            activity.poi_id
            for activity in baseline.itinerary.activities
            if activity.poi_id is not None
        ]
        if baseline.itinerary
        else []
    )
    if len(before) < position or len(plan_ids) < position:
        return [f"position {position} missing before or after the edit"]
    if before[position - 1] == plan_ids[position - 1]:
        return [f"position {position} unchanged ({plan_ids[position - 1]})"]
    return []


def _position_exposure_failures(plan_ids: list[str], expected: dict[str, Any]) -> list[str]:
    position = expected["position"]
    if len(plan_ids) < position:
        return [f"no POI at position {position}"]
    poi = POI_BY_ID[plan_ids[position - 1]]
    if poi.exposure != expected["exposure"]:
        return [f"position {position} exposure={poi.exposure}"]
    return []


def _weather_exposure_failures(case: Case, result: PipelineResult, risk: str) -> list[str]:
    itinerary = result.trip_state.itinerary
    if itinerary is None:
        return ["no plan to check against the forecast"]
    flagged = _flagged_hours(case, result, risk)
    offenders = []
    for activity in itinerary.activities:
        if activity.poi_id is None or POI_BY_ID[activity.poi_id].exposure != "outdoor":
            continue
        for hour in flagged:
            if activity.start < hour + _ONE_HOUR and hour < activity.end:
                offenders.append(f"{activity.poi_id}@{activity.start:%H:%M}")
                break
    return [f"outdoor stop in {risk} hours: {offenders}"] if offenders else []


def _universal_checks(result: PipelineResult) -> list[str]:
    """Invariants asserted on every case, whatever the case file says."""
    failures = []
    unresolved = [
        citation.evidence_id
        for citation in result.citations
        if citation.evidence_id not in set(result.evidence_ids)
    ]
    if unresolved:
        failures.append(f"citation ids not in this request's registry: {unresolved}")
    if result.plan_committed:
        errors = [
            violation.code.value
            for violation in result.violations
            if violation.severity == ViolationSeverity.ERROR
        ]
        if errors:
            failures.append(f"committed plan carries ERROR violations: {errors}")
    hallucinated = delivered_entity_violations(result)
    if hallucinated:
        failures.append(f"entity not grounded in the catalog: {hallucinated}")
    return failures


def delivered_entity_violations(result: PipelineResult) -> list[str]:
    """Proper names in the delivered answer that the catalog does not license.

    Recomputed here rather than read back from the post-check: the post-check
    runs on the model's token draft, this runs on the rendered text a user sees.
    """
    allowed = set(result.allowed_poi_ids)
    answer = result.answer
    offenders = []

    normalized = f" {normalize(answer)} "
    for phrase, poi_id in catalog_name_phrases(CATALOG).items():
        if f" {phrase} " in normalized and poi_id not in allowed:
            offenders.append(poi_id)

    residual = answer
    for poi_id in allowed:
        poi = POI_BY_ID.get(poi_id)
        if poi is None:
            continue
        for name in (poi.names.en, poi.names.el, *poi.aliases):
            residual = residual.replace(name, " ")
    for sentence in SENTENCE_SPLIT_PATTERN.split(residual):
        for index, match in enumerate(CAPITALIZED_WORD_PATTERN.finditer(sentence)):
            word = match.group(0)
            if normalize(word) in PROPER_NAME_ALLOWLIST:
                continue
            if index == 0 and not re.search(r"\w", sentence[: match.start()]):
                continue  # Sentence-initial capitalization names nothing by itself.
            offenders.append(word)
    return sorted(set(offenders))


# ----------------------------------------------------------------------
# metrics
# ----------------------------------------------------------------------


@dataclass
class Metrics:
    tool_selection_hits: int = 0
    tool_selection_total: int = 0
    committed_plans: int = 0
    committed_plans_with_errors: int = 0
    narrations: int = 0
    first_draft_passes: int = 0
    fallbacks: int = 0
    first_draft_entity_hits: int = 0
    delivered_entity_hits: int = 0
    citations: int = 0
    citations_valid: int = 0
    usages: list[LLMUsage] = field(default_factory=list)

    def observe(self, case: Case, result: PipelineResult) -> None:
        self.tool_selection_total += 1
        if tuple(result.tools_called) == case.expected_tools:
            self.tool_selection_hits += 1
        if result.plan_committed:
            self.committed_plans += 1
            if any(
                violation.severity == ViolationSeverity.ERROR
                for violation in result.violations
            ):
                self.committed_plans_with_errors += 1
        if result.narration_invoked:
            self.narrations += 1
            if result.narration_first_draft_passed:
                self.first_draft_passes += 1
            if result.used_fallback:
                self.fallbacks += 1
            if ENTITY_CODES & set(result.first_draft_codes):
                self.first_draft_entity_hits += 1
            if delivered_entity_violations(result):
                self.delivered_entity_hits += 1
        registry = set(result.evidence_ids)
        for citation in result.citations:
            self.citations += 1
            if citation.evidence_id in registry:
                self.citations_valid += 1
        self.usages.extend(result.usages)


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a (0 observed)"
    return f"{numerator / denominator:.1%} ({numerator}/{denominator})"


def collect_metrics(runs: list[CaseRun]) -> Metrics:
    metrics = Metrics()
    for run in runs:
        for repetition in run.attempted:
            for turn_run in repetition.turns:
                metrics.observe(run.case, turn_run.result)
    return metrics


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render(row: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    print(render(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(render(row))


def case_rows(runs: list[CaseRun]) -> list[list[str]]:
    rows = []
    for run in runs:
        attempted = run.attempted
        tools = sorted(
            {
                ",".join(tool.value for tool in turn_run.result.tools_called) or "-"
                for repetition in attempted
                for turn_run in repetition.turns
            }
        )
        failures = sorted(
            {failure for repetition in run.repetitions for failure in repetition.failures}
        )
        if len(run.repetitions) != len(attempted):
            missing = len(run.repetitions) - len(attempted)
            failures.insert(0, f"{missing} repetition(s) not recorded")
        rows.append(
            [
                run.case.id,
                run.case.coverage,
                f"{run.passes}/{len(attempted)}" if attempted else "0/0",
                ";".join(tools) or "-",
                "; ".join(failures)[:110] or "-",
            ]
        )
    return rows


def print_case_table(runs: list[CaseRun]) -> None:
    print("\n## Phase 7 case results (replay; model variance sampled at record time)\n")
    render_table(
        ["case", "coverage", "pass rate", "tools called", "failures"], case_rows(runs)
    )


def print_metrics(metrics: Metrics, runs: list[CaseRun]) -> None:
    print("\n## Metrics\n")
    rows = [
        [
            "tool-selection accuracy",
            _rate(metrics.tool_selection_hits, metrics.tool_selection_total),
            "tools_called equals the case's expected list, in canonical order",
        ],
        [
            "validator violation rate",
            _rate(metrics.committed_plans_with_errors, metrics.committed_plans),
            "committed plans carrying an ERROR violation; must be 0",
        ],
        [
            "entity hallucination (first draft)",
            _rate(metrics.first_draft_entity_hits, metrics.narrations),
            "model drafts the post-check rejected for an ungrounded name",
        ],
        [
            "entity hallucination (delivered)",
            _rate(metrics.delivered_entity_hits, metrics.narrations),
            "0 by construction: names are substituted by the renderer",
        ],
        [
            "citation validity",
            _rate(metrics.citations_valid, metrics.citations),
            "0 invalid by construction: the post-check rejects unknown ids",
        ],
        [
            "post-check first-draft pass rate",
            _rate(metrics.first_draft_passes, metrics.narrations),
            "narrations accepted with no retry",
        ],
        [
            "fallback rate",
            _rate(metrics.fallbacks, metrics.narrations),
            "turns that fell back to the deterministic template",
        ],
    ]
    render_table(["metric", "value", "definition"], rows)

    total_cases = len(runs)
    fully_passing = sum(1 for run in runs if run.attempted and run.passes == len(run.attempted))
    print(
        f"\nCases fully passing every recorded repetition: {fully_passing}/{total_cases}"
    )


def print_usage(metrics: Metrics) -> None:
    usages = metrics.usages
    print(f"\n## Tokens, latency and cost (prices as of {PRICING_AS_OF})\n")
    if not usages:
        print("no recorded calls")
        return
    cost = sum((calculate_cost_usd(usage) for usage in usages), Decimal("0"))
    rows = [
        ["calls replayed", str(len(usages))],
        ["ordinary input tokens", str(sum(usage.ordinary_input_tokens for usage in usages))],
        ["cached input tokens", str(sum(usage.cached_input_tokens for usage in usages))],
        ["cache-write tokens", str(sum(usage.cache_write_tokens for usage in usages))],
        ["output tokens", str(sum(usage.output_tokens for usage in usages))],
        ["reasoning tokens", str(sum(usage.reasoning_tokens for usage in usages))],
        [
            "mean recorded latency (ms)",
            f"{sum(usage.latency_ms for usage in usages) / len(usages):.0f}",
        ],
        ["cost (usd)", f"{cost:.8f}"],
    ]
    render_table(["measure", "value"], rows)
    print("\nLatency is the value recorded at call time and replayed, not replay latency.")


def print_retrieval() -> None:
    from evals.rag_report import evaluate_bm25, load_gold

    print("\n## Retrieval (Phase 5 gold set, BM25 lexical baseline)\n")
    gold = load_gold()
    row = evaluate_bm25(gold=gold)
    render_table(
        ["metric", "value"],
        [
            ["cases", str(len(gold.cases))],
            ["recall@5", f"{row.recall_at_5:.3f}"],
            ["recall@5 (POI-deduped)", f"{row.poi_deduped_recall_at_5:.3f}"],
            ["MRR", f"{row.mrr:.3f}"],
        ],
    )


# ----------------------------------------------------------------------
# understand effort A/B
# ----------------------------------------------------------------------

EFFORT_FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "llm" / "understand"
EFFORT_LOW_FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "llm" / "understand_low"


@dataclass(frozen=True)
class EffortRow:
    case_id: str
    effort: str
    intents: str
    matched_expected: bool
    agrees_with_none: bool
    usage: LLMUsage | None
    disagreement: str = ""


async def _analyze(
    case: Any, effort: str, mode: str, budget: LiveCallBudget | None
) -> tuple[Any, LLMUsage | None]:
    from app.llm.understand import Understander
    from evals.understand_report import case_call_name

    root = EFFORT_FIXTURE_ROOT if effort == "none" else EFFORT_LOW_FIXTURE_ROOT
    settings = Settings(
        llm_mode=mode,
        llm_max_output_tokens=512,
        understand_reasoning_effort=effort,
    )
    understander = Understander.from_settings(
        settings,
        CATALOG,
        call_name=case_call_name(case),
        fixture_store=FixtureStore(root),
        live_call_budget=budget,
    )
    try:
        result = await understander.analyze(case.user_turn, case.trip_state)
    finally:
        provider = understander.provider
        if isinstance(provider, OpenAIProvider) and provider._client is not None:
            await provider._client.close()
    return result, result.usage


async def run_effort_ab(*, record: bool, budget: LiveCallBudget | None) -> list[EffortRow]:
    """Same understand cases, same prompt version, effort=none versus effort=low."""
    from evals.understand_report import load_cases as load_understand_cases

    rows: list[EffortRow] = []
    for case in load_understand_cases():
        if not case.live_record:
            continue
        baseline, baseline_usage = await _analyze(case, "none", "replay", None)
        baseline_intents = list(baseline.analysis.intents)
        rows.append(
            EffortRow(
                case_id=case.id,
                effort="none",
                intents=",".join(intent.value for intent in baseline_intents),
                matched_expected=case.expected_intent in baseline_intents,
                agrees_with_none=True,
                usage=baseline_usage,
            )
        )
        low, low_usage = await _analyze(
            case, "low", "record" if record else "replay", budget
        )
        if low.used_fallback and low.failure_category == NOT_RECORDED:
            continue
        low_intents = list(low.analysis.intents)
        rows.append(
            EffortRow(
                case_id=case.id,
                effort="low",
                intents=",".join(intent.value for intent in low_intents),
                matched_expected=case.expected_intent in low_intents,
                agrees_with_none=(
                    low_intents == baseline_intents
                    and low.analysis.constraint_updates == baseline.analysis.constraint_updates
                    and low.analysis.entities == baseline.analysis.entities
                ),
                usage=low_usage,
                disagreement=_describe_disagreement(baseline.analysis, low.analysis),
            )
        )
    return rows


IMPLAUSIBLE_YEAR = 2000


def _describe_disagreement(baseline: Any, low: Any) -> str:
    """Name the fields the two efforts extracted differently, and flag a date
    no traveler could have meant."""
    notes = []
    for field_name in ("intents", "entities", "constraint_updates"):
        if getattr(baseline, field_name) != getattr(low, field_name):
            notes.append(field_name)
    for label, analysis in (("none", baseline), ("low", low)):
        window = analysis.constraint_updates.time_window
        if window is not None and window.start.year < IMPLAUSIBLE_YEAR:
            notes.append(f"effort={label} produced the year {window.start.year}")
    return ", ".join(notes)


def print_effort_ab(rows: list[EffortRow]) -> None:
    print("\n## Understand step: reasoning effort none versus low\n")
    if not any(row.effort == "low" for row in rows):
        print("effort=low has not been recorded; run with --record-effort to measure it")
        return
    render_table(
        ["case", "effort", "intents", "intent ok", "agrees with none", "difference"],
        [
            [
                row.case_id,
                row.effort,
                row.intents,
                "yes" if row.matched_expected else "NO",
                "-" if row.effort == "none" else ("yes" if row.agrees_with_none else "NO"),
                row.disagreement or "-",
            ]
            for row in rows
        ],
    )
    for effort in ("none", "low"):
        subset = [row for row in rows if row.effort == effort]
        usages = [row.usage for row in subset if row.usage is not None]
        accuracy = _rate(sum(1 for row in subset if row.matched_expected), len(subset))
        cost = sum((calculate_cost_usd(usage) for usage in usages), Decimal("0"))
        print(
            f"effort={effort:<5} intent accuracy {accuracy}  "
            f"output+reasoning tokens "
            f"{sum(u.output_tokens + u.reasoning_tokens for u in usages)}  "
            f"cost {cost:.8f}"
        )
    agreement = [row for row in rows if row.effort == "low"]
    print(
        "\nfull-analysis agreement with effort=none: "
        f"{_rate(sum(1 for row in agreement if row.agrees_with_none), len(agreement))}"
    )


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------


async def run_all(
    cases: list[Case],
    *,
    repetitions: int,
    record_repetition: int | None = None,
    budget: LiveCallBudget | None = None,
) -> list[CaseRun]:
    retriever = EvalRetriever()
    clients: list[Any] = []
    cases_by_id = {case.id: case for case in cases}
    runs = []
    try:
        for case in cases:
            runs.append(
                await run_case(
                    case,
                    cases_by_id,
                    repetitions=repetitions,
                    record_repetition=record_repetition,
                    budget=budget,
                    retriever=retriever,
                    clients=clients,
                )
            )
    finally:
        for client in clients:
            await client.close()
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Phase 7 end-to-end eval")
    parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_REPETITIONS,
        choices=range(1, DEFAULT_REPETITIONS + 1),
        help="repetitions to replay or record per case",
    )
    parser.add_argument("--only", action="append", default=None, help="limit to a case id")
    parser.add_argument(
        "--record-repetition",
        type=int,
        default=None,
        choices=range(1, DEFAULT_REPETITIONS + 1),
        help="record exactly this repetition live; every other one replays",
    )
    parser.add_argument(
        "--record-effort",
        action="store_true",
        help="record the understand effort=low arm of the A/B",
    )
    parser.add_argument(
        "--live-budget",
        type=int,
        default=0,
        help="hard cap on live calls for this run; required with --record",
    )
    args = parser.parse_args()

    recording = args.record_repetition is not None
    if (recording or args.record_effort) and args.live_budget < 1:
        raise SystemExit("recording requires an explicit --live-budget")
    if recording and args.record_repetition > args.repetitions:
        raise SystemExit("--record-repetition is outside --repetitions")
    budget = LiveCallBudget(args.live_budget) if args.live_budget else None

    cases = load_cases()
    if args.only:
        cases = [case for case in cases if case.id in set(args.only)]
        if not cases:
            raise SystemExit("no case matched --only")

    if recording:
        per_case = 2 if args.record_repetition == 1 else 1
        planned = sum(0 if case.reuses_conversation else per_case for case in cases)
        print(f"Live preflight: max_output_tokens={MAX_OUTPUT_TOKENS} on every request")
        print("Live preflight: SDK retries=0; narration retries<=1")
        print(
            f"Live preflight: <={planned} planned calls across {len(cases)} cases, "
            f"repetition {args.record_repetition}; hard budget={args.live_budget}"
        )

    runs = asyncio.run(
        run_all(
            cases,
            repetitions=args.repetitions,
            record_repetition=args.record_repetition,
            budget=budget,
        )
    )
    metrics = collect_metrics(runs)

    print_case_table(runs)
    print_metrics(metrics, runs)
    print_retrieval()
    effort_rows = asyncio.run(
        run_effort_ab(record=args.record_effort, budget=budget)
    )
    print_effort_ab(effort_rows)
    print_usage(metrics)

    if budget is not None:
        print(f"\nLive calls used this run: {budget.calls_used}/{budget.max_calls}")
    failing = [run.case.id for run in runs if run.attempted and run.passes < len(run.attempted)]
    unrecorded = [run.case.id for run in runs if not run.attempted]
    if failing:
        print(f"\nFindings — cases with a failing repetition: {', '.join(failing)}")
    if unrecorded:
        print(f"Findings — cases with no recorded repetition: {', '.join(unrecorded)}")
    if not failing and not unrecorded:
        print("\nAll cases passed every recorded repetition.")


if __name__ == "__main__":
    main()

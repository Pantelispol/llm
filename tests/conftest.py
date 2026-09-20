from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.domain.models import (
    GeoPoint,
    ValidationResult,
    Violation,
    ViolationCode,
    ViolationSeverity,
)
from app.domain.ports import (
    HourlyWeatherFlags,
    LLMResult,
    LLMUsage,
    PaceFactors,
    PlanningContext,
    TravelMatrixRequest,
    TravelMatrixResult,
)
from app.llm.narrate import Narrator
from app.llm.understand import Understander
from app.orchestrator.pipeline import ConversationPipeline
from app.planning.planner import BeamSearchPlanner
from app.planning.repair import PlanRepairer
from app.tools.catalog import CatalogRepository
from app.tools.opening_hours import OpeningHoursEngine
from app.tools.travel import MatrixFile, PrecomputedTravelTimeProvider
from app.tools.weather import build_weather_provider

os.environ.setdefault("RAG_DENSE", "off")
os.environ.setdefault("RAG_STORE", "memory")
os.environ.setdefault("LLM_MODE", "replay")

ATHENS = ZoneInfo("Europe/Athens")
FIXED_MATRIX_PATH = Path(__file__).parent / "fixtures" / "walking_matrix_fixed.json"


@pytest.fixture(scope="session")
def planning_context_factory() -> Callable[..., PlanningContext]:
    repository = CatalogRepository()
    locations = {poi.id: poi.coordinates for poi in repository.catalog.pois}
    matrix = PrecomputedTravelTimeProvider().matrix(TravelMatrixRequest(locations=locations))
    engine = OpeningHoursEngine(repository)

    def build(
        *,
        now: datetime = datetime(2026, 9, 22, 8, tzinfo=ATHENS),
        weather_flags: list[HourlyWeatherFlags] | None = None,
        weather_unavailable_reason: str | None = None,
        pace_factors: PaceFactors | None = None,
        transition_buffer_minutes: int = 5,
        long_walk_with_child_minutes: int = 20,
        travel_matrix: TravelMatrixResult | None = None,
    ) -> PlanningContext:
        return PlanningContext(
            now=now,
            catalog=repository.catalog,
            opening_hours=engine,
            candidates=[],
            hourly_weather_flags=weather_flags or [],
            weather_unavailable_reason=weather_unavailable_reason,
            travel_matrix=travel_matrix or matrix,
            pace_factors=pace_factors or PaceFactors(),
            transition_buffer_minutes=transition_buffer_minutes,
            long_walk_with_child_minutes=long_walk_with_child_minutes,
        )

    return build


@pytest.fixture(scope="session")
def fixed_planning_context_factory() -> Callable[..., PlanningContext]:
    repository = CatalogRepository()
    engine = OpeningHoursEngine(repository)
    fixture = MatrixFile.model_validate_json(FIXED_MATRIX_PATH.read_text(encoding="utf-8"))
    fixed_matrix = PrecomputedTravelTimeProvider(FIXED_MATRIX_PATH).matrix(
        TravelMatrixRequest(
            locations={
                poi_id: GeoPoint(latitude=40.6400, longitude=22.9400)
                for poi_id in fixture.poi_ids
            }
        )
    )

    def build(
        *,
        now: datetime = datetime(2026, 9, 22, 8, tzinfo=ATHENS),
        weather_flags: list[HourlyWeatherFlags] | None = None,
        weather_unavailable_reason: str | None = None,
        pace_factors: PaceFactors | None = None,
        transition_buffer_minutes: int = 5,
        long_walk_with_child_minutes: int = 20,
    ) -> PlanningContext:
        return PlanningContext(
            now=now,
            catalog=repository.catalog,
            opening_hours=engine,
            candidates=[],
            hourly_weather_flags=weather_flags or [],
            weather_unavailable_reason=weather_unavailable_reason,
            travel_matrix=fixed_matrix,
            pace_factors=pace_factors or PaceFactors(),
            transition_buffer_minutes=transition_buffer_minutes,
            long_walk_with_child_minutes=long_walk_with_child_minutes,
        )

    return build


# ---------------------------------------------------------------------------
# Conversation-pipeline harness
#
# The stub model reads the prompt it was given and answers from it, so tests
# exercise the real understander, router, planner, validator, narrator, and
# post-check. Only the network boundary is replaced.
# ---------------------------------------------------------------------------

def prompt_section(prompt: str, name: str) -> str:
    opening = f"[{name}]\n"
    closing = f"\n[/{name}]"
    start = prompt.index(opening) + len(opening)
    return prompt[start : prompt.index(closing, start)]


def stub_usage(model_id: str, effort: str) -> LLMUsage:
    return LLMUsage(
        model_id=model_id,
        reasoning_effort=effort,
        input_tokens=100,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=20,
        reasoning_tokens=0,
        latency_ms=1.0,
    )


class ScriptedUnderstandProvider:
    """Returns a prepared TurnAnalysis per turn and records every prompt."""

    def __init__(self, analyses):
        self.analyses = list(analyses)
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    async def structured(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        self.system_prompts.append(kwargs["system_prompt"])
        if len(self.analyses) > 1:
            analysis = self.analyses.pop(0)
        elif self.analyses:
            analysis = self.analyses[0]
        else:
            raise AssertionError("no scripted TurnAnalysis remains for this turn")
        return LLMResult(output=analysis, usage=stub_usage("gpt-5.6-luna", "none"))

    async def text(self, **kwargs):
        raise AssertionError("understand must not request text output")


class GroundedNarrateProvider:
    """Writes a grounded draft from AVAILABLE_TOKENS and the validated plan."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    async def text(self, **kwargs):
        prompt = kwargs["user_prompt"]
        self.prompts.append(prompt)
        self.system_prompts.append(kwargs["system_prompt"])
        plan = json.loads(prompt_section(prompt, "VALIDATED_PLAN_JSON"))
        # AVAILABLE_TOKENS is the contract: it already restricts fact tokens to
        # operational evidence, so a faithful model reads it rather than guessing
        # from the OPERATIONAL_FACTS block.
        available = json.loads(prompt_section(prompt, "AVAILABLE_TOKENS"))
        lines: list[str] = []
        if plan is not None:
            for position, activity in enumerate(plan["activities"], start=1):
                poi_id = activity["poi_id"]
                label = f"{{{{poi:{poi_id}}}}}" if poi_id else "a rest stop"
                lines.append(
                    f"{position}. {{{{time:{position}:start}}}} to "
                    f"{{{{time:{position}:end}}}} at {label}."
                )
        for fact_token in available["fact"]:
            evidence_id = fact_token[len("{{fact:") : -len("}}")]
            lines.append(f"{fact_token} {{{{cite:{evidence_id}}}}}.")
        if not lines:
            lines.append("Here is what is on record for your question.")
        return LLMResult(output="\n".join(lines), usage=stub_usage("gpt-5.6-luna", "low"))

    async def structured(self, **kwargs):
        raise AssertionError("narration must not request structured output")


class RaisingProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def structured(self, **kwargs):
        self.calls += 1
        raise self.error

    async def text(self, **kwargs):
        self.calls += 1
        raise self.error


class StubRetriever:
    def __init__(self, hits=None) -> None:
        self.hits = list(hits or [])
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5):
        self.queries.append(query)
        return self.hits[:limit]


class RecordingPlanner:
    """Wraps the real planner so a test can assert it was never consulted."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def plan(self, *args, **kwargs):
        self.calls += 1
        return self.inner.plan(*args, **kwargs)

    def plan_for_order(self, *args, **kwargs):
        self.calls += 1
        return self.inner.plan_for_order(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class AlwaysInvalidValidator:
    def __init__(self, code=None) -> None:
        self.code = code or ViolationCode.CLOSED_DURING_VISIT
        self.calls = 0

    def validate(self, itinerary, state, context):
        self.calls += 1
        return ValidationResult(
            is_valid=False,
            violations=[
                Violation(
                    code=self.code,
                    message="synthetic validation failure",
                    severity=ViolationSeverity.ERROR,
                    activity_position=1,
                )
            ],
            checked_at=context.now,
        )


@pytest.fixture
def stub_pipeline():
    """Build a ConversationPipeline whose only stubs are the model and retriever."""

    def build(
        *,
        analyses,
        narrate_provider=None,
        understand_provider=None,
        retriever=None,
        validator=None,
        weather_fixture="clear_day",
        planner=None,
    ):
        settings = Settings(
            llm_mode="replay", weather_fixture=weather_fixture, rag_store="memory"
        )
        repository = CatalogRepository()
        understand = understand_provider or ScriptedUnderstandProvider(analyses)
        narrate = narrate_provider or GroundedNarrateProvider()
        base_planner = planner or BeamSearchPlanner()
        pipeline = ConversationPipeline(
            settings=settings,
            repository=repository,
            hours=OpeningHoursEngine(repository),
            travel=PrecomputedTravelTimeProvider(),
            weather=build_weather_provider(settings),
            understander=Understander(understand, repository.catalog),
            narrator=Narrator(narrate, repository.catalog),
            retriever=retriever if retriever is not None else StubRetriever(),
            planner=base_planner,
            repairer=PlanRepairer(getattr(base_planner, "inner", base_planner)),
            validator=validator,
        )
        pipeline.understand_provider = understand
        pipeline.narrate_provider = narrate
        return pipeline

    return build

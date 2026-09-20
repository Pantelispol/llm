from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.models import (
    ConstraintUpdates,
    ExtractedEntities,
    Intent,
    Party,
    TripState,
    TurnAnalysis,
)
from app.domain.ports import RetrievalHit
from app.llm.openai_provider import LLMTransportError
from app.orchestrator.language import resolve_turn_language
from app.orchestrator.models import (
    REQUIRED_STAGE_ORDER,
    PipelineStage,
    ToolName,
)
from app.orchestrator.temporal import extract_duration_hours, resolve_date
from app.planning.planner import BeamSearchPlanner
from tests.conftest import (
    AlwaysInvalidValidator,
    RaisingProvider,
    RecordingPlanner,
    StubRetriever,
)

ATHENS = ZoneInfo("Europe/Athens")
UTC = ZoneInfo("UTC")
NOW = datetime(2026, 9, 21, 12, tzinfo=ATHENS)
ASSIGNMENT_TURNS = ("five hours tomorrow", "no museum", "with my 10-year-old")
PLAN_TOOL_CHAIN = [
    ToolName.CATALOG,
    ToolName.WEATHER,
    ToolName.HOURS,
    ToolName.TRAVEL,
    ToolName.PLANNER,
    ToolName.VALIDATOR,
]


def assignment_analyses() -> list[TurnAnalysis]:
    """What the understand model is expected to extract for the three turns."""
    return [
        TurnAnalysis(
            intents=[Intent.CREATE_PLAN],
            entities=ExtractedEntities(requested_date_text="tomorrow"),
        ),
        TurnAnalysis(
            intents=[Intent.EDIT_PLAN],
            constraint_updates=ConstraintUpdates(add_exclude_categories=["museum"]),
        ),
        TurnAnalysis(
            intents=[Intent.EDIT_PLAN],
            constraint_updates=ConstraintUpdates(party=Party(adults=1, children_ages=[10])),
        ),
    ]


async def run_turns(pipeline, turns, *, now=NOW, state=None):
    results = []
    current = state or TripState()
    for turn in turns:
        result = await pipeline.run_turn(turn, current, now=now)
        current = result.trip_state
        results.append(result)
    return results


@pytest.mark.asyncio
async def test_pipeline_runs_stages_in_required_order(stub_pipeline):
    pipeline = stub_pipeline(analyses=assignment_analyses()[:1])
    [result] = await run_turns(pipeline, ASSIGNMENT_TURNS[:1])
    assert result.stages == list(REQUIRED_STAGE_ORDER)
    assert PipelineStage.POST_CHECK in result.stages


@pytest.mark.asyncio
async def test_pipeline_passes_trip_state_not_raw_transcript_between_turns(stub_pipeline):
    pipeline = stub_pipeline(analyses=assignment_analyses())
    await run_turns(pipeline, ASSIGNMENT_TURNS)

    understand_prompts = pipeline.understand_provider.prompts
    narrate_prompts = pipeline.narrate_provider.prompts
    assert len(understand_prompts) == 3

    for index in (1, 2):
        prompt = understand_prompts[index]
        assert ASSIGNMENT_TURNS[index] in prompt
        for earlier in ASSIGNMENT_TURNS[:index]:
            assert earlier not in prompt
    for index, prompt in enumerate(narrate_prompts):
        for earlier in ASSIGNMENT_TURNS[:index]:
            assert earlier not in prompt
    # No accumulating message list and no earlier assistant prose anywhere.
    assert not any("assistant" in prompt.lower() for prompt in understand_prompts)


@pytest.mark.asyncio
async def test_prompt_size_does_not_grow_with_turn_count(stub_pipeline):
    """Prompt size is bounded by TripState, not by how many turns have passed."""
    turns = [f"tell me about the rotunda number {index}" for index in range(6)]
    analyses = [TurnAnalysis(intents=[Intent.TOURISM_QA]) for _ in turns]
    pipeline = stub_pipeline(
        analyses=analyses,
        retriever=StubRetriever(
            [
                RetrievalHit(
                    chunk_id="rotunda#history",
                    poi_id="rotunda",
                    section="History",
                    text="A circular late Roman building later used as a church.",
                    source_url="data/content/rotunda.md",
                    score=1.0,
                )
            ]
        ),
    )
    await run_turns(pipeline, turns)

    overheads = {
        len(prompt) - len(turn)
        for prompt, turn in zip(pipeline.understand_provider.prompts, turns, strict=True)
    }
    assert len(overheads) == 1, overheads


@pytest.mark.asyncio
async def test_pipeline_executes_only_the_deterministic_route_tool_list(stub_pipeline):
    pipeline = stub_pipeline(analyses=assignment_analyses()[:1])
    [result] = await run_turns(pipeline, ASSIGNMENT_TURNS[:1])
    assert result.route is not None
    assert result.tools_called == result.route.tools
    assert result.tools_called == PLAN_TOOL_CHAIN
    assert ToolName.RETRIEVER not in result.tools_called


@pytest.mark.asyncio
async def test_pipeline_commits_valid_plan_and_increments_version_once(stub_pipeline):
    pipeline = stub_pipeline(analyses=assignment_analyses()[:1])
    [result] = await run_turns(pipeline, ASSIGNMENT_TURNS[:1])

    state = result.trip_state
    assert result.plan_committed is True
    assert state.itinerary_version == 1
    assert state.itinerary is not None and state.itinerary.activities
    assert result.itinerary_version == 1
    assert not [item for item in result.violations if item.severity == "error"]


@pytest.mark.asyncio
async def test_pipeline_keeps_original_state_when_plan_validation_fails(stub_pipeline):
    validator = AlwaysInvalidValidator()
    pipeline = stub_pipeline(analyses=assignment_analyses()[:1], validator=validator)
    original = TripState()
    result = await pipeline.run_turn(ASSIGNMENT_TURNS[0], original, now=NOW)

    assert validator.calls == 1
    assert result.plan_committed is False
    assert result.trip_state == original
    assert result.trip_state.itinerary is None
    assert result.itinerary_version == 0
    assert result.failure_category == "validation_failed"
    assert [item.code.value for item in result.violations] == ["CLOSED_DURING_VISIT"]
    # Narration never sees a plan that did not validate.
    assert result.stages == list(REQUIRED_STAGE_ORDER[: REQUIRED_STAGE_ORDER.index(
        PipelineStage.VALIDATE
    ) + 1])
    assert pipeline.narrate_provider.prompts == []


@pytest.mark.asyncio
async def test_pipeline_plan_followups_run_complete_mandatory_tool_chain(stub_pipeline):
    pipeline = stub_pipeline(analyses=assignment_analyses())
    results = await run_turns(pipeline, ASSIGNMENT_TURNS)
    for result in results:
        assert result.tools_called == PLAN_TOOL_CHAIN
        assert result.route is not None and result.route.touches_plan
    assert [result.itinerary_version for result in results] == [1, 2, 3]


@pytest.mark.asyncio
async def test_non_planning_turn_skips_planner_and_returns_checked_answer(stub_pipeline):
    planner = RecordingPlanner(BeamSearchPlanner())
    pipeline = stub_pipeline(
        analyses=[TurnAnalysis(intents=[Intent.TOURISM_QA])],
        planner=planner,
        retriever=StubRetriever(
            [
                RetrievalHit(
                    chunk_id="rotunda#history",
                    poi_id="rotunda",
                    section="History",
                    text="A circular late Roman building later used as a church.",
                    source_url="data/content/rotunda.md",
                    score=1.0,
                )
            ]
        ),
    )
    result = await pipeline.run_turn("tell me about the rotunda", TripState(), now=NOW)

    assert planner.calls == 0
    assert result.plan_committed is False
    assert result.itinerary_version == 0
    assert result.tools_called == [ToolName.CATALOG, ToolName.RETRIEVER]
    assert result.answer.strip()
    assert result.used_fallback is False


@pytest.mark.asyncio
async def test_pipeline_aggregates_usage_for_understand_and_narrate_calls(stub_pipeline):
    pipeline = stub_pipeline(analyses=assignment_analyses()[:1])
    [result] = await run_turns(pipeline, ASSIGNMENT_TURNS[:1])

    assert len(result.usages) == 2
    assert [usage.reasoning_effort for usage in result.usages] == ["none", "low"]
    assert all(usage.model_id == "gpt-5.6-luna" for usage in result.usages)


@pytest.mark.asyncio
async def test_tomorrow_is_resolved_from_injected_athens_now(stub_pipeline):
    """The instant is injected and normalized to Athens; nothing reads the clock."""
    pipeline = stub_pipeline(analyses=assignment_analyses()[:1])
    # 23:30 UTC on the 21st is already 02:30 Athens on the 22nd, so "tomorrow" is the 23rd.
    late_utc = datetime(2026, 9, 21, 23, 30, tzinfo=UTC)
    result = await pipeline.run_turn(ASSIGNMENT_TURNS[0], TripState(), now=late_utc)

    window = result.trip_state.time_window
    assert window is not None
    assert window.start.astimezone(ATHENS).date().isoformat() == "2026-09-23"
    assert window.end - window.start == timedelta(hours=5)
    assert window.start.astimezone(ATHENS).hour == 9

    # The model is never shown a resolved date; it only sees the literal turn.
    assert "2026-09-23" not in pipeline.understand_provider.prompts[0]

    athens_noon = await pipeline.run_turn(ASSIGNMENT_TURNS[0], TripState(), now=NOW)
    assert athens_noon.trip_state.time_window.start.date().isoformat() == "2026-09-22"


def test_relative_dates_and_durations_resolve_without_a_model():
    assert resolve_date("tomorrow", NOW).isoformat() == "2026-09-22"
    assert resolve_date("αύριο", NOW).isoformat() == "2026-09-22"
    assert resolve_date("next month", NOW) is None
    assert extract_duration_hours("I have five hours tomorrow") == 5
    assert extract_duration_hours("πέντε ώρες αύριο") == 5
    assert extract_duration_hours("no museum") is None


def test_turn_language_is_resolved_from_the_script_the_traveler_used():
    assert resolve_turn_language("five hours tomorrow").value == "en"
    assert resolve_turn_language("Πες μου για τη Ροτόντα").value == "el"
    assert resolve_turn_language("").value == "en"


@pytest.mark.asyncio
async def test_greek_turn_renders_greek_poi_names_and_english_turn_renders_english(
    stub_pipeline,
):
    english = stub_pipeline(analyses=assignment_analyses()[:1])
    english_result = await english.run_turn("five hours tomorrow", TripState(), now=NOW)

    greek = stub_pipeline(
        analyses=[
            TurnAnalysis(
                intents=[Intent.CREATE_PLAN],
                entities=ExtractedEntities(requested_date_text="αύριο"),
            )
        ]
    )
    greek_result = await greek.run_turn("πέντε ώρες αύριο", TripState(), now=NOW)

    assert english_result.language.value == "en"
    assert greek_result.language.value == "el"

    english_ids = [
        activity.poi_id for activity in english_result.trip_state.itinerary.activities
    ]
    greek_ids = [activity.poi_id for activity in greek_result.trip_state.itinerary.activities]
    assert english_ids == greek_ids, "same plan, so only the rendering language differs"

    catalog = {poi.id: poi for poi in english.catalog.pois}
    for poi_id in english_ids:
        if poi_id is None:
            continue
        assert catalog[poi_id].names.en in english_result.answer
        assert catalog[poi_id].names.el in greek_result.answer
        assert catalog[poi_id].names.el not in english_result.answer
    assert "Πηγές:" in greek_result.answer
    assert "Sources:" in english_result.answer


@pytest.mark.asyncio
async def test_understand_provider_failure_falls_back_without_raising(stub_pipeline):
    pipeline = stub_pipeline(
        analyses=[],
        understand_provider=RaisingProvider(LLMTransportError("synthetic failure")),
    )
    result = await pipeline.run_turn("five hours tomorrow", TripState(), now=NOW)

    assert result.understand_fallback is True
    assert result.answer.strip()


@pytest.mark.asyncio
async def test_narrate_provider_failure_falls_back_to_the_template(stub_pipeline):
    pipeline = stub_pipeline(
        analyses=assignment_analyses()[:1],
        narrate_provider=RaisingProvider(LLMTransportError("synthetic failure")),
    )
    result = await pipeline.run_turn("five hours tomorrow", TripState(), now=NOW)

    assert result.used_fallback is True
    assert result.failure_category == "LLMTransportError"
    assert result.plan_committed is True, "a provider failure must not discard a valid plan"


@pytest.mark.asyncio
async def test_each_turn_builds_its_own_grounded_narration_input(stub_pipeline):
    pipeline = stub_pipeline(analyses=assignment_analyses())
    await run_turns(pipeline, ASSIGNMENT_TURNS)
    prompts = pipeline.narrate_provider.prompts
    assert len(prompts) == 3
    assert len(set(prompts)) == 3, "each turn's narration input must differ"
    for prompt in prompts:
        assert "[VALIDATED_PLAN_JSON]" in prompt
        assert "[UNTRUSTED_RETRIEVED_TEXT]" in prompt


@pytest.mark.asyncio
async def test_a_plan_turn_without_a_time_window_asks_rather_than_raising(
    stub_pipeline,
) -> None:
    """Regression: the planner raises without a window; the turn must not 500.

    Found in Phase 7: the model classified "I love Byzantine history. What
    should I see?" as a planning turn, and `BeamSearchPlanner._effective_state`
    raised `ValueError` straight out of `run_turn`.
    """
    pipeline = stub_pipeline(analyses=[TurnAnalysis(intents=[Intent.CREATE_PLAN])])

    result = await pipeline.run_turn(
        "I love Byzantine history. What should I see?", TripState(), now=NOW
    )

    assert result.plan_committed is False
    assert result.itinerary_version == 0
    assert ToolName.PLANNER not in result.tools_called
    assert result.answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn", "intent"),
    [
        ("Is the Acropolis open tomorrow?", Intent.OPENING_HOURS_QUESTION),
        ("What will the weather be like tomorrow?", Intent.WEATHER_QUESTION),
        ("Is it safe to walk around Plaka tomorrow evening?", Intent.SAFETY),
    ],
)
async def test_question_mentioning_tomorrow_leaves_state_untouched(stub_pipeline, turn, intent):
    pipeline = stub_pipeline(analyses=[
            TurnAnalysis(
                intents=[intent],
                entities=ExtractedEntities(requested_date_text="tomorrow"),
            )
        ]
    )
    before = TripState()
    result = await pipeline.run_turn(turn, before, now=NOW)

    assert result.trip_state.model_dump_json() == before.model_dump_json()
    assert result.itinerary_version == 0
    assert result.plan_committed is False
    assert ToolName.PLANNER not in result.tools_called

from __future__ import annotations

import asyncio

import pytest

from app.chat_cli import SCENARIOS, run_scenario, turn_call_names
from app.llm.openai_provider import OpenAIProvider
from app.orchestrator.models import ToolName
from app.orchestrator.pipeline import build_pipeline
from app.tools.catalog import CatalogRepository
from evals.llm_report import SCENARIO_NOW, scenario_settings

PLAN_TOOL_CHAIN = [
    ToolName.CATALOG,
    ToolName.WEATHER,
    ToolName.HOURS,
    ToolName.TRAVEL,
    ToolName.PLANNER,
    ToolName.VALIDATOR,
]


@pytest.fixture(scope="module")
def transcript():
    """The real pipeline, replaying frozen LLM and weather inputs. No network."""
    built: list = []

    def factory(settings, **names: str):
        pipeline = build_pipeline(settings, **names)
        built.append(pipeline)
        return pipeline

    result = asyncio.run(
        run_scenario(
            SCENARIOS["assignment"],
            now=SCENARIO_NOW,
            settings=scenario_settings("replay"),
            pipeline_factory=factory,
        )
    )
    return result, built


def test_assignment_three_turn_cli_scenario_runs_end_to_end_offline(transcript):
    turns, pipelines = transcript
    assert [turn for turn, _ in turns] == list(SCENARIOS["assignment"])

    for pipeline in pipelines:
        for component in (pipeline.understander, pipeline.narrator):
            provider = component.provider
            assert isinstance(provider, OpenAIProvider)
            assert provider._client is None, "replay mode must construct no OpenAI client"

    for index, (_, result) in enumerate(turns, start=1):
        assert result.itinerary_version == index
        assert result.plan_committed is True
        assert result.trip_state.itinerary is not None
        assert result.tools_called == PLAN_TOOL_CHAIN
        assert result.answer.strip()
        assert not [item for item in result.violations if item.severity == "error"]
        assert len(result.usages) == 2


def test_assignment_scenario_preserves_no_museum_and_child_constraints(transcript):
    turns, _ = transcript
    (_, first), (_, second), (_, third) = turns
    pois = {poi.id: poi for poi in CatalogRepository().catalog.pois}

    # Turn one: a five-hour window on the deterministically resolved next day.
    window = first.trip_state.time_window
    assert window is not None
    assert window.start.date().isoformat() == "2026-09-22"
    assert (window.end - window.start).total_seconds() == 5 * 3600

    # Turn two preserves what turn one built and removes every museum.
    assert second.trip_state.exclude_categories == ["museum"]
    assert second.trip_state.time_window == window
    second_ids = [
        activity.poi_id for activity in second.trip_state.itinerary.activities if activity.poi_id
    ]
    assert second_ids, "the repaired plan must not be empty"
    assert all(pois[poi_id].category != "museum" for poi_id in second_ids)

    # Turn three adapts rather than regenerating: constraints and window survive.
    assert third.trip_state.party.children_ages == [10]
    assert third.trip_state.exclude_categories == ["museum"]
    assert third.trip_state.time_window == window
    third_ids = [
        activity.poi_id for activity in third.trip_state.itinerary.activities if activity.poi_id
    ]
    assert all(pois[poi_id].category != "museum" for poi_id in third_ids)
    assert set(third_ids) & set(second_ids), "turn three must adapt, not start over"


def test_each_turn_uses_its_own_fixture_namespace():
    """A fixture recorded for turn one cannot satisfy either follow-up."""
    names = [turn_call_names(index) for index in range(1, 4)]
    assert len({understand for understand, _ in names}) == 3
    assert len({narrate for _, narrate in names}) == 3


def test_scenario_turns_are_the_literal_assignment_turns():
    assert SCENARIOS["assignment"] == (
        "five hours tomorrow",
        "no museum",
        "with my 10-year-old",
    )

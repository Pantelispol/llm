from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.domain.models import (
    ExtractedEntities,
    Intent,
    TripState,
    TurnAnalysis,
    ViolationCode,
)
from app.orchestrator.models import PipelineStage, ToolName
from app.orchestrator.temporal import extract_clock_times, resolve_feasibility_window
from app.planning.feasibility import FeasibilityVerdict
from app.planning.planner import BeamSearchPlanner
from tests.conftest import RecordingPlanner

ATHENS = ZoneInfo("Europe/Athens")
NOW = datetime(2026, 9, 21, 12, tzinfo=ATHENS)
MUSEUM_TURN = (
    "Can I visit the Archaeological Museum tomorrow if I arrive at 16:45 and it closes at 17:00?"
)


def analysis(*poi_ids: str, date_text: str | None = "tomorrow") -> TurnAnalysis:
    return TurnAnalysis(
        intents=[Intent.FEASIBILITY_CHECK],
        entities=ExtractedEntities(mentioned_poi_ids=list(poi_ids), requested_date_text=date_text),
    )


def test_closing_time_claim_is_not_read_as_the_end_of_the_window():
    times = extract_clock_times(MUSEUM_TURN)
    assert [f"{t:%H:%M}" for t in times] == ["16:45"]
    window, _ = resolve_feasibility_window(MUSEUM_TURN, "tomorrow", NOW)
    assert window is not None
    assert window.start.hour == 16 and window.start.minute == 45
    assert window.start.date().isoformat() == "2026-09-22"


def test_explicit_range_and_meridiem_are_parsed():
    window, _ = resolve_feasibility_window("between 10:00 and 1:30 pm tomorrow", None, NOW)
    assert window is not None
    assert (window.start.hour, window.end.hour, window.end.minute) == (10, 13, 30)
    assert resolve_feasibility_window("Can I see the Rotunda?", None, NOW)[0] is None


@pytest.mark.asyncio
async def test_infeasible_hypothesis_reports_infeasible_and_commits_nothing(stub_pipeline):
    planner = RecordingPlanner(BeamSearchPlanner())
    pipeline = stub_pipeline(analyses=[analysis("archaeological_museum")], planner=planner)
    result = await pipeline.run_turn(MUSEUM_TURN, TripState(), now=NOW)

    assert result.feasibility is not None
    assert result.feasibility.verdict == FeasibilityVerdict.INFEASIBLE
    assert result.plan_committed is False
    assert result.itinerary_version == 0
    assert result.trip_state == TripState()
    assert result.narration_invoked is False
    assert result.answer.startswith("No")
    assert "16:40" in result.answer
    assert any(v.code == ViolationCode.CLOSED_DURING_VISIT for v in result.violations)
    assert PipelineStage.FEASIBILITY in result.stages
    assert PipelineStage.PLAN not in result.stages
    assert ToolName.FEASIBILITY in result.tools_called
    assert ToolName.PLANNER not in result.tools_called
    assert planner.calls == 0


@pytest.mark.asyncio
async def test_feasible_hypothesis_answers_yes_without_committing(stub_pipeline):
    turn = "Can I see the Rotunda and the Arch of Galerius tomorrow between 10:00 and 13:00?"
    pipeline = stub_pipeline(analyses=[analysis("rotunda", "arch_of_galerius")])
    result = await pipeline.run_turn(turn, TripState(), now=NOW)

    assert result.feasibility is not None
    assert result.feasibility.verdict == FeasibilityVerdict.FEASIBLE
    assert result.answer.startswith("Yes")
    assert result.plan_committed is False
    assert result.itinerary_version == 0
    assert result.violations == []


@pytest.mark.asyncio
async def test_missing_place_or_time_asks_instead_of_planning(stub_pipeline):
    pipeline = stub_pipeline(analyses=[analysis()])
    result = await pipeline.run_turn("Can this fit?", TripState(), now=NOW)
    assert result.needs_clarification is True
    assert result.plan_committed is False
    assert result.feasibility is None


@pytest.mark.asyncio
async def test_greek_infeasible_answer_is_natural_and_keeps_the_facts(stub_pipeline):
    turn = "Προλαβαίνω το Αρχαιολογικό Μουσείο αύριο αν φτάσω στις 16:45 και κλείνει στις 17:00;"
    pipeline = stub_pipeline(analyses=[analysis("archaeological_museum", date_text="αύριο")])
    result = await pipeline.run_turn(turn, TripState(), now=NOW)

    assert result.answer.startswith("Όχι, δεν προλαβαίνετε.")
    assert "η τελευταία είσοδος είναι στις 16:40" in result.answer
    assert "κλείνει στις 17:00" in result.answer
    assert "θα φτάνατε στις 16:45" in result.answer
    assert result.answer.endswith("Το πρόγραμμά σας παραμένει αμετάβλητο.")
    assert not any(ord(c) < 128 and c.isalpha() for c in result.answer.split("Πρόταση")[0])
    assert result.plan_committed is False
    assert result.itinerary_version == 0
    assert result.narration_invoked is False
    assert result.feasibility is not None
    assert result.feasibility.verdict == FeasibilityVerdict.INFEASIBLE


@pytest.mark.asyncio
async def test_greek_feasible_answer_states_the_verdict_and_finish_time(stub_pipeline):
    turn = "Προλαβαίνω τη Ροτόντα και την Αψίδα του Γαλερίου αύριο από 10:00 έως 13:00;"
    pipeline = stub_pipeline(analyses=[analysis("rotunda", "arch_of_galerius", date_text="αύριο")])
    result = await pipeline.run_turn(turn, TripState(), now=NOW)

    assert result.answer.startswith(
        "Ναι, προλαβαίνετε μέσα στον διαθέσιμο χρόνο."
    )
    assert "πριν από τις 13:00" in result.answer
    assert result.plan_committed is False
    assert result.violations == []

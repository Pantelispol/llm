"""Tests for the Phase 7 harness itself.

The harness reports numbers a reader will trust, so the ways it could lie
quietly — counting an unrecorded repetition as a pass, skipping a check name it
does not recognise, missing an ungrounded name in a delivered answer — are
pinned here. All of it runs offline.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.domain.models import AnswerLanguage, TripState
from app.llm.narration_tokens import Citation
from app.orchestrator.models import PipelineResult, ToolName
from evals.run import (
    CaseRun,
    RepetitionRun,
    Turn,
    TurnRun,
    _understand_recorded,
    call_names,
    collect_metrics,
    delivered_entity_violations,
    evaluate_checks,
    load_cases,
    run_all,
)

ATHENS = ZoneInfo("Europe/Athens")

REQUIRED_COVERAGE = {
    "rag_factual_citation",
    "personalised_recommendation",
    "plan_creation",
    "no_museum_exclusion",
    "three_turn_continuity",
    "weather_replan_rain",
    "heat_replan",
    "positional_edit",
    "feasibility_feasible",
    "feasibility_infeasible",
    "museum_last_entry",
    "public_holiday_closed",
    "unsupported_events",
    "safety_storm_seich_sou",
    "adversarial_invent_attraction",
    "greek_variant_plan",
    "greek_variant_qa",
}


def test_case_set_covers_every_required_behaviour() -> None:
    cases = load_cases()
    assert 18 <= len(cases) <= 20
    assert {case.id for case in cases} >= REQUIRED_COVERAGE


def test_every_case_freezes_now_and_never_reads_the_clock() -> None:
    for case in load_cases():
        assert case.now.tzinfo is not None
        assert case.now.utcoffset() is not None


def test_every_check_name_in_the_case_file_is_implemented() -> None:
    """An unknown check must raise, never be skipped into a false pass."""
    result = _result()
    for case in load_cases():
        for turn in case.turns:
            evaluate_checks(case, turn, result, None, TripState())

    unknown = Turn(user_turn="x", checks={"not_a_real_check": 1})
    with pytest.raises(RuntimeError, match="unknown check"):
        evaluate_checks(load_cases()[0], unknown, result, None, TripState())


def test_an_unrecorded_repetition_is_never_counted_as_a_pass() -> None:
    case = load_cases()[0]
    run = CaseRun(
        case=case,
        repetitions=[
            RepetitionRun(repetition=1, turns=[TurnRun(turn=case.turns[0], result=_result())]),
            RepetitionRun(repetition=2, recorded=False),
        ],
    )
    assert run.passes == 1
    assert len(run.attempted) == 1
    assert collect_metrics([run]).tool_selection_total == 1


def test_a_failing_repetition_lowers_the_pass_rate_rather_than_the_bar() -> None:
    case = load_cases()[0]
    run = CaseRun(
        case=case,
        repetitions=[
            RepetitionRun(repetition=1, turns=[TurnRun(turn=case.turns[0], result=_result())]),
            RepetitionRun(
                repetition=2,
                turns=[
                    TurnRun(turn=case.turns[0], result=_result(), failures=["plan_committed=False"])
                ],
            ),
        ],
    )
    assert run.passes == 1
    assert len(run.attempted) == 2


def test_a_citation_from_another_request_is_reported_unresolved() -> None:
    result = _result(
        citations=[Citation(label=1, evidence_id="chunk-from-another-request")],
        evidence_ids=["chunk-from-this-request"],
    )
    failures = evaluate_checks(load_cases()[0], Turn(user_turn="x"), result, None, TripState())
    assert any("not in this request's registry" in failure for failure in failures)


def test_delivered_answer_naming_a_poi_outside_the_bundle_is_a_finding() -> None:
    result = _result(
        answer="Start at the White Tower and then walk on.",
        allowed_poi_ids=["rotunda"],
    )
    assert "white_tower" in delivered_entity_violations(result)


def test_delivered_answer_naming_only_bundle_pois_is_clean() -> None:
    result = _result(
        answer="Start at the Rotunda and then walk on.",
        allowed_poi_ids=["rotunda"],
    )
    assert delivered_entity_violations(result) == []


def test_an_invented_proper_name_in_a_delivered_answer_is_a_finding() -> None:
    result = _result(
        answer="Visit the Rotunda, then the Hidden Crypt of Kastra.",
        allowed_poi_ids=["rotunda"],
    )
    assert delivered_entity_violations(result)


def test_the_assignment_conversation_case_passes_offline_from_recorded_fixtures() -> None:
    """The one case with recorded fixtures must run end to end with no client."""
    cases = [case for case in load_cases() if case.id == "three_turn_continuity"]
    runs = asyncio.run(run_all(cases, repetitions=3))

    assert len(runs) == 1
    run = runs[0]
    # One recorded sample exists, so exactly one repetition is attempted.
    assert len(run.attempted) == 1
    assert run.passes == 1
    versions = [turn.result.itinerary_version for turn in run.attempted[0].turns]
    assert versions == [1, 2, 3]


def _result(**overrides: object) -> PipelineResult:
    defaults: dict[str, object] = {
        "answer": "A grounded answer.",
        "trip_state": TripState(),
        "language": AnswerLanguage.EN,
        "tools_called": list(load_cases()[0].expected_tools),
        "narration_invoked": True,
        "narration_first_draft_passed": True,
    }
    defaults.update(overrides)
    return PipelineResult(**defaults)


def test_tool_selection_accuracy_counts_order_not_just_membership() -> None:
    case = load_cases()[2]  # plan_creation: six tools in canonical order
    shuffled = list(case.expected_tools)
    shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
    run = CaseRun(
        case=case,
        repetitions=[
            RepetitionRun(
                repetition=1,
                turns=[
                    TurnRun(
                        turn=case.turns[0],
                        result=_result(tools_called=[ToolName(tool) for tool in shuffled]),
                    )
                ],
            )
        ],
    )
    metrics = collect_metrics([run])
    assert metrics.tool_selection_hits == 0
    assert metrics.tool_selection_total == 1


def test_frozen_now_is_the_athens_instant_the_case_file_states() -> None:
    holiday = next(case for case in load_cases() if case.id == "public_holiday_closed")
    assert holiday.now == datetime(2026, 4, 12, 9, 0, tzinfo=ATHENS)


def test_an_extra_repetition_re_records_narration_only() -> None:
    """Repetition 2 must replay understanding, or it would cost a second call
    and collide with the fixture repetition 1 already wrote."""
    case = next(case for case in load_cases() if case.id == "plan_creation")

    first_understand, first_narrate = call_names(case, 1, 1)
    second_understand, second_narrate = call_names(case, 2, 1)

    assert first_understand == second_understand
    assert first_narrate != second_narrate
    assert "rep1" in first_narrate and "rep2" in second_narrate


def test_the_conversation_case_reuses_the_phase_6e_fixture_names() -> None:
    case = next(case for case in load_cases() if case.id == "three_turn_continuity")
    assert call_names(case, 1, 2) == (
        "conversation_turn2_understand",
        "conversation_turn2_narrate",
    )


def test_the_lazy_retriever_can_actually_be_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `LazyRetriever._build` imported a name that does not exist.

    No Phase 6 test routed the pipeline to retrieval — every conversation turn
    goes to the planner — so the wiring was never executed until Phase 7.
    """
    from app.orchestrator.pipeline import LazyRetriever

    monkeypatch.setenv("RAG_DENSE", "off")
    hits = asyncio.run(LazyRetriever("memory").search("White Tower history", limit=3))
    assert hits


def test_recording_over_an_existing_fixture_stops_instead_of_degrading() -> None:
    """The provider refuses to overwrite a fixture and the pipeline treats that
    refusal as an ordinary provider failure. Left alone, the turn falls back and
    the *next* call is recorded against a degraded analysis — which is how three
    unusable narration fixtures were produced while recording Phase 7."""
    source = (Path(__file__).parents[1] / "evals" / "run.py").read_text(encoding="utf-8")
    assert "ALREADY_RECORDED in categories" in source
    assert 'ALREADY_RECORDED = "FixtureAlreadyExists"' in source


def test_understanding_is_never_re_recorded_once_a_fixture_exists() -> None:
    """Every Phase 7 case already has its understand fixture, so a second
    recording pass must replay it. Recording it again collides with the stored
    fixture, and the pipeline turns that collision into a provider failure —
    which silently records the following narration against a fallback analysis."""
    for case in load_cases():
        if case.reuses_conversation:
            continue
        assert _understand_recorded(case), f"{case.id} has no recorded understand fixture"

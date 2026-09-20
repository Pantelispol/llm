from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.catalog import HeatExposure
from app.domain.models import (
    ActivityKind,
    ConstraintUpdates,
    Exposure,
    Party,
    ReplaceActivity,
    TimeWindow,
    TripState,
    ViolationSeverity,
)
from app.domain.ports import PlanningContext
from app.planning.candidates import DroppedReason, PlannerConfig, generate_candidates
from app.planning.planner import BeamSearchPlanner, PlanResult
from app.planning.repair import PlanRepairer
from app.planning.rules import pace_factors_for
from app.tools.weather import parse_open_meteo
from app.tools.weather_flags import WeatherThresholds, derive_weather_flags

ATHENS = ZoneInfo("Europe/Athens")
FIXTURE_DIR = Path(__file__).parents[1] / "evals" / "fixtures" / "weather"


def at(hour: int, minute: int = 0, *, month: int = 9, day: int = 22) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ATHENS)


def trip(
    start_hour: int = 9,
    end_hour: int = 14,
    *,
    interests: list[str] | None = None,
    party: Party | None = None,
) -> TripState:
    return TripState(
        time_window=TimeWindow(start=at(start_hour), end=at(end_hour)),
        interests=interests or ["history"],
        party=party or Party(),
    )


def weather_flags(name: str, state: TripState):
    payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    weather = parse_open_meteo(
        payload,
        fetched_at=at(8),
        source=f"fixture:{name}",
        is_fixture=True,
    )
    return derive_weather_flags(weather, state.party, WeatherThresholds()).hours


def context_for(
    factory: Callable[..., PlanningContext],
    state: TripState,
    scenario: str = "clear_day",
) -> PlanningContext:
    return factory(
        weather_flags=weather_flags(scenario, state),
        pace_factors=pace_factors_for(state),
    )


def visit_ids(plan: PlanResult) -> list[str]:
    return [
        activity.poi_id
        for activity in plan.itinerary.activities
        if activity.kind == ActivityKind.VISIT and activity.poi_id is not None
    ]


def state_with_plan(state: TripState, plan: PlanResult, version: int) -> TripState:
    return state.model_copy(update={"itinerary": plan.itinerary, "itinerary_version": version})


def assert_no_errors(plan: PlanResult) -> None:
    assert not [
        item for item in plan.validation.violations if item.severity == ViolationSeverity.ERROR
    ]


def test_candidate_generation_filters_operational_and_user_constraints(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = trip()
    state = state.model_copy(
        update={
            "exclude_categories": ["museum"],
            "visited": ["arch_of_galerius"],
        }
    )
    context = planning_context_factory(
        weather_unavailable_reason="timeout",
        pace_factors=pace_factors_for(state),
    )

    generated = generate_candidates(state, context, PlannerConfig())
    reasons = {item.poi_id: item.reason for item in generated.dropped}

    assert len(generated.candidates) <= 10
    assert reasons["white_tower"] == DroppedReason.EXCLUDED
    assert reasons["arch_of_galerius"] == DroppedReason.EXCLUDED
    assert reasons["rotunda"] == DroppedReason.HOURS_UNKNOWN
    assert reasons["seich_sou_forest"] == DroppedReason.WEATHER


def test_candidate_generation_filters_temporary_closure(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = TripState(
        time_window=TimeWindow(
            start=at(9, month=10, day=13),
            end=at(14, month=10, day=13),
        )
    )
    context = planning_context_factory(
        now=at(8, month=10, day=13),
        pace_factors=pace_factors_for(state),
    )

    generated = generate_candidates(state, context, PlannerConfig())

    white_tower = next(item for item in generated.dropped if item.poi_id == "white_tower")
    assert white_tower.reason == DroppedReason.CLOSED
    assert "temporary closure" in white_tower.detail


def test_five_hour_plan_has_meal_assumption_and_no_errors(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = trip()
    plan = BeamSearchPlanner().plan(state, context_for(planning_context_factory, state))

    assert_no_errors(plan)
    assert visit_ids(plan)
    assert any(activity.kind == ActivityKind.MEAL for activity in plan.itinerary.activities)
    assert plan.assumptions == ["Start location assumed to be Aristotelous Square."]


def test_three_turn_continuity_diffs(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    planner = BeamSearchPlanner()
    repairer = PlanRepairer(planner)
    initial_state = trip()
    clear_context = context_for(planning_context_factory, initial_state)
    first = planner.plan(initial_state, clear_context)
    first_visits = visit_ids(first)

    second = repairer.repair(
        first,
        state_with_plan(initial_state, first, 1),
        clear_context,
        constraint_updates=ConstraintUpdates(add_exclude_categories=["museum"]),
    )
    second_visits = visit_ids(second.plan)
    catalog = {poi.id: poi for poi in clear_context.catalog.pois}
    expected_kept = [poi_id for poi_id in first_visits if catalog[poi_id].category != "museum"]
    assert second.diff.kept == expected_kept
    assert all(catalog[poi_id].category != "museum" for poi_id in second_visits)
    assert all(item.reason == "excluded" for item in second.diff.removed)

    third = repairer.repair(
        second.plan,
        state_with_plan(second.state, second.plan, 2),
        clear_context,
        constraint_updates=ConstraintUpdates(party=Party(children_ages=[10])),
    )
    assert set(second_visits).issubset(third.diff.kept)
    assert any(item.startswith("break:") for item in third.diff.added)
    assert any(
        item.reason == DroppedReason.CHILD_UNSUITABLE for item in third.plan.dropped_candidates
    )
    assert_no_errors(third.plan)


def test_rain_after_16_reorders_or_swaps_outdoor_visits(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    planner = BeamSearchPlanner()
    repairer = PlanRepairer(planner)
    state = trip(start_hour=13, end_hour=18)
    clear_context = context_for(planning_context_factory, state)
    first = planner.plan(state, clear_context)
    rainy_context = context_for(planning_context_factory, state, "rain_after_16")

    repaired = repairer.repair(
        first,
        state_with_plan(state, first, 1),
        rainy_context,
        previous_context=clear_context,
    )
    exposed_after_16 = [
        activity
        for activity in repaired.plan.itinerary.activities
        if activity.end > at(16) and activity.exposure != Exposure.INDOOR
    ]

    assert exposed_after_16 == []
    assert repaired.diff.retimed or repaired.diff.added or repaired.diff.removed
    assert_no_errors(repaired.plan)


def test_heatwave_avoids_high_heat_exposure_outdoors_midday(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = trip(start_hour=9, end_hour=17)
    context = context_for(planning_context_factory, state, "heatwave_39")
    plan = BeamSearchPlanner().plan(state, context)
    catalog = {poi.id: poi for poi in context.catalog.pois}

    unsafe = [
        activity
        for activity in plan.itinerary.activities
        if activity.kind == ActivityKind.VISIT
        and activity.poi_id
        and activity.start < at(17)
        and activity.end > at(12)
        and catalog[activity.poi_id].exposure != Exposure.INDOOR
        and catalog[activity.poi_id].heat_exposure == HeatExposure.HIGH
    ]
    assert unsafe == []
    assert_no_errors(plan)


def test_window_shrink_keeps_highest_scored_visit(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = trip(start_hour=9, end_hour=13)
    context = context_for(planning_context_factory, state)
    planner = BeamSearchPlanner()
    first = planner.plan(state, context)
    visit_scores = {
        score.poi_id: score.total
        for score in first.score_breakdown
        if score.kind == ActivityKind.VISIT and score.poi_id
    }
    highest = max(visit_scores, key=lambda poi_id: (visit_scores[poi_id], poi_id))

    repaired = PlanRepairer(planner).repair(
        first,
        state_with_plan(state, first, 1),
        context,
        constraint_updates=ConstraintUpdates(time_window=TimeWindow(start=at(9), end=at(11))),
    )

    assert highest in visit_ids(repaired.plan)
    assert all(item.reason == "window_shrink_lowest_score" for item in repaired.diff.removed)
    assert_no_errors(repaired.plan)


def test_replace_second_visit_indoor_keeps_other_visits(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = trip()
    context = context_for(planning_context_factory, state)
    planner = BeamSearchPlanner()
    first = planner.plan_for_order(
        state,
        context,
        ["arch_of_galerius", "archaeological_museum"],
    )
    assert first is not None
    old_visits = visit_ids(first)
    assert len(old_visits) == 2

    repaired = PlanRepairer(planner).repair(
        first,
        state_with_plan(state, first, 1),
        context,
        operations=[ReplaceActivity(position=2, required_exposure=Exposure.INDOOR)],
    )
    new_visits = visit_ids(repaired.plan)
    catalog = {poi.id: poi for poi in context.catalog.pois}

    assert new_visits[0] == old_visits[0]
    assert new_visits[1] != old_visits[1]
    assert catalog[new_visits[1]].exposure == Exposure.INDOOR
    assert old_visits[1] in {item.poi_id for item in repaired.diff.removed}
    assert_no_errors(repaired.plan)


def test_same_input_twice_is_identical(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = trip()
    context = context_for(planning_context_factory, state)
    planner = BeamSearchPlanner()

    assert planner.plan(state, context) == planner.plan(state, context)


def test_seeded_random_states_always_validate(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    randomizer = random.Random(20260922)
    planner = BeamSearchPlanner()
    interest_pool = ["history", "architecture", "waterfront", "art", "food"]
    for _ in range(50):
        start_hour = randomizer.randint(8, 13)
        duration = randomizer.randint(3, 5)
        state = trip(
            start_hour=start_hour,
            end_hour=start_hour + duration,
            interests=randomizer.sample(interest_pool, k=randomizer.randint(0, 2)),
            party=(
                Party(children_ages=[randomizer.randint(6, 14)])
                if randomizer.random() < 0.35
                else Party()
            ),
        )
        if randomizer.random() < 0.25:
            state = state.model_copy(update={"exclude_categories": ["museum"]})
        context = context_for(planning_context_factory, state)
        assert_no_errors(planner.plan(state, context))


def test_five_hour_plan_completes_under_300_ms(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = trip()
    context = context_for(planning_context_factory, state)

    started = time.perf_counter()
    BeamSearchPlanner().plan(state, context)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms < 300

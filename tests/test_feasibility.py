from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from app.domain.models import LocationRef, Pace, Party, TimeWindow, TripState, ViolationCode
from app.domain.ports import HourlyWeatherFlags, PlanningContext
from app.planning.feasibility import (
    FeasibilityChecker,
    FeasibilityFixKind,
    FeasibilityVerdict,
)
from app.planning.rules import pace_factors_for

ATHENS = ZoneInfo("Europe/Athens")
CHECKER = FeasibilityChecker()


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 22, hour, minute, tzinfo=ATHENS)


def state_for(
    start: datetime,
    end: datetime,
    *,
    start_poi: str | None = None,
    party: Party | None = None,
    pace: Pace = Pace.NORMAL,
) -> TripState:
    return TripState(
        time_window=TimeWindow(start=start, end=end),
        start_location=(LocationRef(label=start_poi, poi_id=start_poi) if start_poi else None),
        party=party or Party(),
        pace=pace,
    )


def test_five_attractions_across_city_in_90_minutes_is_infeasible_with_numbers(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9), at(10, 30))
    result = CHECKER.check(
        [
            "arch_of_galerius",
            "aristotelous_square",
            "ladadika",
            "new_waterfront_umbrellas",
            "nea_paralia_parks",
        ],
        state,
        planning_context_factory(),
    )

    assert result.verdict == FeasibilityVerdict.INFEASIBLE
    assert result.lower_bound.visit_total_minutes == 115
    assert result.lower_bound.walking_total_minutes > 0
    assert result.lower_bound.buffer_total_minutes == 20
    assert result.lower_bound.margin_to_deadline_minutes is not None
    assert result.lower_bound.margin_to_deadline_minutes < 0
    assert len(result.lower_bound.stops) == 5
    earlier_fix = next(
        fix for fix in result.minimal_fixes if fix.kind == FeasibilityFixKind.START_EARLIER
    )
    assert earlier_fix.minutes == 226
    assert any(
        fix.kind == FeasibilityFixKind.TRAVEL_TIMES_UNAVAILABLE for fix in result.minimal_fixes
    )


def test_realistic_three_stop_plan_before_1700_is_feasible(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9), at(17), start_poi="arch_of_galerius")
    context = planning_context_factory(pace_factors=pace_factors_for(state))

    result = CHECKER.check(
        ["arch_of_galerius", "white_tower", "archaeological_museum"],
        state,
        context,
    )

    assert result.verdict == FeasibilityVerdict.FEASIBLE
    assert result.comfortable.feasible is True
    assert result.comfortable.finish_at is not None
    assert result.comfortable.finish_at <= at(17)
    assert result.comfortable.visit_total_minutes == 185
    assert result.comfortable.margin_to_deadline_minutes is not None
    assert result.comfortable.margin_to_deadline_minutes >= 0
    assert result.minimal_fixes == []


def test_tight_means_only_minimum_visit_durations_fit(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(10), at(10, 15))

    result = CHECKER.check(
        ["arch_of_galerius"],
        state,
        planning_context_factory(),
    )

    assert result.verdict == FeasibilityVerdict.TIGHT
    assert result.lower_bound.feasible is True
    assert result.comfortable.feasible is False
    assert result.lower_bound.visit_total_minutes == 10
    assert result.comfortable.visit_total_minutes == 20


def test_archaeological_museum_arrival_at_1630_requires_another_day(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(16, 30), at(17))

    result = CHECKER.check(
        ["archaeological_museum"],
        state,
        planning_context_factory(),
    )

    assert result.verdict == FeasibilityVerdict.INFEASIBLE
    assert "archaeological_museum" in result.lower_bound.closed_poi_ids
    stop = result.lower_bound.stops[0]
    assert stop.arrival == at(16, 30)
    assert stop.visit_start is not None
    assert stop.visit_start.date() > stop.arrival.date()
    assert any(
        fix.kind == FeasibilityFixKind.VISIT_ANOTHER_DAY and fix.poi_id == "archaeological_museum"
        for fix in result.minimal_fixes
    )


def test_weather_unavailable_warns_and_excludes_critical_poi(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9), at(13))

    result = CHECKER.check(
        ["aristotelous_square", "seich_sou_forest"],
        state,
        planning_context_factory(weather_unavailable_reason="timeout"),
    )

    assert result.verdict == FeasibilityVerdict.INFEASIBLE
    assert result.excluded_poi_ids == ["seich_sou_forest"]
    assert "seich_sou_forest" not in result.lower_bound.order
    assert any(notice.code == ViolationCode.WEATHER_UNAVAILABLE for notice in result.notices)
    assert any(
        fix.kind == FeasibilityFixKind.VISIT_ANOTHER_DAY and fix.poi_id == "seich_sou_forest"
        for fix in result.minimal_fixes
    )


def test_weather_unavailable_still_produces_safe_city_plan(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9), at(11))

    result = CHECKER.check(
        ["aristotelous_square"],
        state,
        planning_context_factory(weather_unavailable_reason="timeout"),
    )

    assert result.verdict == FeasibilityVerdict.FEASIBLE
    assert result.comfortable.order == ["aristotelous_square"]
    assert result.excluded_poi_ids == []
    assert any(notice.code == ViolationCode.WEATHER_UNAVAILABLE for notice in result.notices)


def test_best_order_avoids_storm_slot(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(16, 30), at(20))
    storm_flags = [
        HourlyWeatherFlags(at=at(hour), storm=True)
        for hour in (18, 19, 20)
    ]

    result = CHECKER.check(
        ["museum_of_byzantine_culture", "new_waterfront_umbrellas"],
        state,
        planning_context_factory(weather_flags=storm_flags),
    )

    assert result.verdict == FeasibilityVerdict.FEASIBLE
    assert result.comfortable.order[0] == "new_waterfront_umbrellas"
    assert all(not stop.issue_codes for stop in result.comfortable.stops)


def test_relaxed_child_pace_factors_apply_to_walks_and_visits() -> None:
    state = state_for(
        at(9),
        at(13),
        party=Party(children_ages=[10]),
        pace=Pace.RELAXED,
    )

    factors = pace_factors_for(state)

    assert factors.travel_time_multiplier == 1.25 * 1.3
    assert factors.visit_time_multiplier == 1.25 * 1.15


def test_same_inputs_produce_identical_tie_broken_result(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9), at(13))
    context = planning_context_factory()
    requested = ["aristotelous_square", "arch_of_galerius"]

    first = CHECKER.check(requested, state, context)
    second = CHECKER.check(list(reversed(requested)), state, context)

    assert first == second

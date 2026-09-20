from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from app.domain.models import ActivityKind, Exposure
from app.planning.candidates import DroppedReason
from app.planning.demo import DemoEnvironment, _state, run_scenario

CORE_HISTORY_POIS = {
    "rotunda",
    "hagios_demetrios",
    "hagia_sophia",
    "acheiropoietos",
    "arch_of_galerius",
    "roman_forum",
    "archaeological_museum",
    "museum_of_byzantine_culture",
    "white_tower",
}


def visit_ids(plan) -> set[str]:
    return {
        activity.poi_id
        for activity in plan.itinerary.activities
        if activity.kind == ActivityKind.VISIT and activity.poi_id is not None
    }


def assert_no_break_soon_after_meal(plan, minimum_minutes: int = 45) -> None:
    meals = [
        activity for activity in plan.itinerary.activities if activity.kind == ActivityKind.MEAL
    ]
    breaks = [
        activity for activity in plan.itinerary.activities if activity.kind == ActivityKind.BREAK
    ]
    for meal in meals:
        assert all(
            not meal.end <= rest.start < meal.end + timedelta(minutes=minimum_minutes)
            for rest in breaks
        )


def test_history_plan_uses_window_and_selects_core_history() -> None:
    plan, _diff, _fixture = run_scenario("five_hours_history")

    assert plan.window_utilization >= 0.80
    assert len(visit_ids(plan) & CORE_HISTORY_POIS) >= 2
    assert "aristotelous_square" not in visit_ids(plan)


def test_demo_tuesday_closes_roman_forum_with_honest_reason() -> None:
    plan, _diff, _fixture = run_scenario("five_hours_history")
    dropped = {item.poi_id: item for item in plan.dropped_candidates}

    assert dropped["roman_forum"].reason == DroppedReason.CLOSED


def test_rain_repair_moves_outdoors_early_and_keeps_indoor_late() -> None:
    plan, diff, _fixture = run_scenario("rain_after_16")
    risk_start = datetime.combine(
        plan.itinerary.window.start.date(),
        time(16),
        tzinfo=plan.itinerary.window.start.tzinfo,
    )

    assert not [
        activity
        for activity in plan.itinerary.activities
        if activity.exposure != Exposure.INDOOR and activity.end > risk_start
    ]
    assert any(
        activity.kind == ActivityKind.VISIT
        and activity.exposure == Exposure.INDOOR
        and activity.start >= risk_start
        for activity in plan.itinerary.activities
    )
    assert diff is not None
    assert all(item.poi_id == "arch_of_galerius" for item in diff.removed)


def test_lunch_and_child_break_timing() -> None:
    history, _diff, _fixture = run_scenario("five_hours_history")
    lunch = next(
        activity for activity in history.itinerary.activities if activity.kind == ActivityKind.MEAL
    )
    assert time(13) <= lunch.start.timetz().replace(tzinfo=None) <= time(15, 30)
    assert lunch.end.timetz().replace(tzinfo=None) <= time(15, 30)

    child_plan, _diff, _fixture = run_scenario("with_child_followup")
    assert_no_break_soon_after_meal(child_plan)
    assert all(
        activity.travel_from_previous_minutes <= 25 for activity in child_plan.itinerary.activities
    )


def test_drop_reason_contract_is_numerically_consistent() -> None:
    plan, _diff, _fixture = run_scenario("five_hours_history")
    for dropped in plan.dropped_candidates:
        assert dropped.reason not in {
            DroppedReason.LOW_INTEREST,
            DroppedReason.TOO_FAR,
        }
        if dropped.reason == DroppedReason.NOT_ENOUGH_TIME:
            numbers = [int(value) for value in re.findall(r"\d+", dropped.detail)]
            assert len(numbers) >= 2
            assert numbers[0] > numbers[1]
        elif dropped.reason == DroppedReason.LOWER_SCORE:
            assert "Score gap versus weakest selected activity" in dropped.detail
            gap = float(dropped.detail.split(": ", maxsplit=1)[1].split()[0])
            assert gap >= 0


def test_dinner_uses_an_open_approved_meal_area() -> None:
    environment = DemoEnvironment()
    trip = _state(18, 22)
    plan = environment.planner.plan(trip, environment.context(trip))
    dinner = next(
        activity for activity in plan.itinerary.activities if activity.kind == ActivityKind.MEAL
    )

    assert time(19, 30) <= dinner.start.timetz().replace(tzinfo=None) <= time(21, 30)
    assert dinner.end.timetz().replace(tzinfo=None) <= time(21, 30)
    assert dinner.poi_id in {"kapani_market", "modiano_market", "ladadika"}

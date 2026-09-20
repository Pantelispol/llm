from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.models import (
    ConstraintUpdates,
    Exposure,
    Party,
    ReplaceActivity,
    TimeWindow,
    TripState,
    ViolationSeverity,
)
from app.domain.ports import PlanningContext, TravelMatrixRequest
from app.planning.planner import BeamSearchPlanner, PlanResult
from app.planning.repair import PlanDiff, PlanRepairer
from app.planning.rules import pace_factors_for
from app.tools.catalog import CatalogRepository
from app.tools.opening_hours import OpeningHoursEngine
from app.tools.travel import PrecomputedTravelTimeProvider
from app.tools.weather import parse_open_meteo
from app.tools.weather_flags import WeatherThresholds, derive_weather_flags

ATHENS = ZoneInfo("Europe/Athens")
ROOT = Path(__file__).parents[2]
SCENARIOS = (
    "five_hours_history",
    "no_museum_followup",
    "with_child_followup",
    "rain_after_16",
    "heatwave",
    "shrink_to_two_hours",
    "replace_second_indoor",
)


def _at(hour: int) -> datetime:
    return datetime(2026, 9, 22, hour, tzinfo=ATHENS)


def _state(start: int, end: int) -> TripState:
    return TripState(
        time_window=TimeWindow(start=_at(start), end=_at(end)),
        interests=["history"],
    )


class DemoEnvironment:
    def __init__(self) -> None:
        self.repository = CatalogRepository()
        locations = {poi.id: poi.coordinates for poi in self.repository.catalog.pois}
        self.matrix = PrecomputedTravelTimeProvider().matrix(
            TravelMatrixRequest(locations=locations)
        )
        self.hours = OpeningHoursEngine(self.repository)
        self.planner = BeamSearchPlanner()
        self.repairer = PlanRepairer(self.planner)

    def context(self, state: TripState, fixture: str = "clear_day") -> PlanningContext:
        path = ROOT / "evals" / "fixtures" / "weather" / f"{fixture}.json"
        weather = parse_open_meteo(
            json.loads(path.read_text(encoding="utf-8")),
            fetched_at=datetime(2026, 9, 20, 12, tzinfo=ATHENS),
            source=f"fixture:{fixture}",
            is_fixture=True,
        )
        flags = derive_weather_flags(weather, state.party, WeatherThresholds()).hours
        return PlanningContext(
            now=datetime(2026, 9, 20, 12, tzinfo=ATHENS),
            catalog=self.repository.catalog,
            opening_hours=self.hours,
            candidates=[],
            hourly_weather_flags=flags,
            travel_matrix=self.matrix,
            pace_factors=pace_factors_for(state),
        )


def run_scenario(name: str) -> tuple[PlanResult, PlanDiff | None, str]:
    environment = DemoEnvironment()
    if name == "five_hours_history":
        state = _state(9, 14)
        return environment.planner.plan(state, environment.context(state)), None, "clear_day"

    if name in {"no_museum_followup", "with_child_followup"}:
        state = _state(9, 14)
        context = environment.context(state)
        first = environment.planner.plan(state, context)
        state = state.model_copy(update={"itinerary": first.itinerary, "itinerary_version": 1})
        second = environment.repairer.repair(
            first,
            state,
            context,
            constraint_updates=ConstraintUpdates(add_exclude_categories=["museum"]),
        )
        if name == "no_museum_followup":
            return second.plan, second.diff, "clear_day"
        child_state = second.state.model_copy(
            update={"itinerary": second.plan.itinerary, "itinerary_version": 2}
        )
        third = environment.repairer.repair(
            second.plan,
            child_state,
            context,
            constraint_updates=ConstraintUpdates(party=Party(children_ages=[10])),
        )
        return third.plan, third.diff, "clear_day"

    if name == "rain_after_16":
        state = _state(13, 18)
        clear = environment.context(state)
        first = environment.planner.plan(state, clear)
        planned_state = state.model_copy(
            update={"itinerary": first.itinerary, "itinerary_version": 1}
        )
        rainy = environment.context(state, "rain_after_16")
        repaired = environment.repairer.repair(
            first,
            planned_state,
            rainy,
            previous_context=clear,
        )
        return repaired.plan, repaired.diff, "rain_after_16"

    if name == "heatwave":
        state = _state(9, 17)
        return (
            environment.planner.plan(state, environment.context(state, "heatwave_39")),
            None,
            "heatwave_39",
        )

    if name == "shrink_to_two_hours":
        state = _state(9, 13)
        context = environment.context(state)
        first = environment.planner.plan(state, context)
        planned_state = state.model_copy(
            update={"itinerary": first.itinerary, "itinerary_version": 1}
        )
        repaired = environment.repairer.repair(
            first,
            planned_state,
            context,
            constraint_updates=ConstraintUpdates(time_window=TimeWindow(start=_at(9), end=_at(11))),
        )
        return repaired.plan, repaired.diff, "clear_day"

    if name == "replace_second_indoor":
        state = _state(9, 14)
        context = environment.context(state)
        first = environment.planner.plan_for_order(
            state,
            context,
            ["arch_of_galerius", "archaeological_museum"],
        )
        if first is None:
            raise RuntimeError("fixed demo seed plan did not fit")
        planned_state = state.model_copy(
            update={"itinerary": first.itinerary, "itinerary_version": 1}
        )
        repaired = environment.repairer.repair(
            first,
            planned_state,
            context,
            operations=[ReplaceActivity(position=2, required_exposure=Exposure.INDOOR)],
        )
        return repaired.plan, repaired.diff, "clear_day"

    raise ValueError(f"unknown demo scenario: {name}")


def _print_result(name: str, plan: PlanResult, diff: PlanDiff | None, fixture: str) -> None:
    print(f"Scenario: {name} (recorded weather fixture: {fixture})")
    print("Timetable")
    for position, activity in enumerate(plan.itinerary.activities, start=1):
        label = activity.poi_id or activity.area_label or activity.kind.value
        print(
            f"  {position}. {activity.start:%H:%M}-{activity.end:%H:%M} "
            f"{activity.kind.value}: {label}"
        )
    if diff is not None:
        print("Diff")
        print(f"  kept: {', '.join(diff.kept) or '-'}")
        print(
            "  removed: "
            + (", ".join(f"{item.poi_id} ({item.reason})" for item in diff.removed) or "-")
        )
        print(f"  added: {', '.join(diff.added) or '-'}")
        print(
            "  retimed: "
            + (
                ", ".join(
                    f"{item.key} {item.old_start:%H:%M}->{item.new_start:%H:%M}"
                    for item in diff.retimed
                )
                or "-"
            )
        )
    print("Dropped candidates")
    for item in plan.dropped_candidates:
        print(f"  {item.poi_id}: {item.reason.value} — {item.detail}")
    warnings = [
        item for item in plan.validation.violations if item.severity == ViolationSeverity.WARNING
    ]
    print("Warnings")
    for warning in warnings:
        print(f"  {warning.code.value}: {warning.message}")
    if not warnings:
        print("  -")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a frozen deterministic planner demo")
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    args = parser.parse_args()
    plan, diff, fixture = run_scenario(args.scenario)
    _print_result(args.scenario, plan, diff, fixture)


if __name__ == "__main__":
    main()

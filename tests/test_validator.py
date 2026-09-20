from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.models import (
    Activity,
    Exposure,
    Itinerary,
    LocationRef,
    Pace,
    Party,
    TimeWindow,
    TripState,
    ViolationCode,
    ViolationSeverity,
)
from app.domain.ports import HourlyWeatherFlags, PlanningContext
from app.planning.rules import pace_factors_for
from app.planning.validator import DeterministicItineraryValidator
from app.tools.weather import parse_open_meteo
from app.tools.weather_flags import WeatherThresholds, derive_weather_flags

ATHENS = ZoneInfo("Europe/Athens")
VALIDATOR = DeterministicItineraryValidator()


def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ATHENS)


def activity(
    poi_id: str,
    start: datetime,
    minutes: int,
    *,
    exposure: Exposure = Exposure.INDOOR,
    stored_travel: int = 0,
) -> Activity:
    return Activity(
        poi_id=poi_id,
        start=start,
        end=start + timedelta(minutes=minutes),
        visit_minutes=minutes,
        travel_from_previous_minutes=stored_travel,
        exposure=exposure,
    )


def state_for(
    start: datetime,
    end: datetime,
    *,
    start_poi: str | None = None,
    exclude_categories: list[str] | None = None,
    exclude_poi_ids: list[str] | None = None,
    party: Party | None = None,
    pace: Pace = Pace.NORMAL,
) -> TripState:
    return TripState(
        time_window=TimeWindow(start=start, end=end),
        start_location=(LocationRef(label=start_poi, poi_id=start_poi) if start_poi else None),
        exclude_categories=exclude_categories or [],
        exclude_poi_ids=exclude_poi_ids or [],
        party=party or Party(),
        pace=pace,
    )


def itinerary_for(state: TripState, activities: list[Activity]) -> Itinerary:
    assert state.time_window is not None
    return Itinerary(window=state.time_window, activities=activities)


def error_codes(result) -> set[ViolationCode]:
    return {item.code for item in result.violations if item.severity == ViolationSeverity.ERROR}


def warning_codes(result) -> set[ViolationCode]:
    return {item.code for item in result.violations if item.severity == ViolationSeverity.WARNING}


def test_good_itinerary_has_zero_errors(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 9), at(9, 22, 13))
    itinerary = itinerary_for(
        state,
        [
            activity("white_tower", at(9, 22, 9), 60),
            activity("archaeological_museum", at(9, 22, 10, 15), 60),
        ],
    )

    result = VALIDATOR.validate(itinerary, state, planning_context_factory())

    assert result.is_valid is True
    assert error_codes(result) == set()
    assert ViolationCode.APPROXIMATE_TRAVEL in warning_codes(result)


def test_overlap_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 9), at(9, 22, 13))
    itinerary = itinerary_for(
        state,
        [
            activity("white_tower", at(9, 22, 9), 60),
            activity("archaeological_museum", at(9, 22, 9, 50), 60),
        ],
    )
    assert ViolationCode.OVERLAP in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory())
    )


def test_matrix_overrides_wrong_stored_travel_time(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 9), at(9, 22, 13))
    itinerary = itinerary_for(
        state,
        [
            activity("white_tower", at(9, 22, 9), 60),
            activity(
                "archaeological_museum",
                at(9, 22, 10, 10),
                60,
                stored_travel=10,
            ),
        ],
    )

    result = VALIDATOR.validate(itinerary, state, planning_context_factory())

    assert ViolationCode.TRAVEL_GAP_TOO_SHORT in error_codes(result)
    violation = next(
        item for item in result.violations if item.code == ViolationCode.TRAVEL_GAP_TOO_SHORT
    )
    assert "9 min walking plus 5 min buffer" in violation.message


def test_catalog_and_pace_override_shortened_stored_visit(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(
        at(9, 22, 9),
        at(9, 22, 10),
        party=Party(children_ages=[10]),
        pace=Pace.RELAXED,
    )
    itinerary = itinerary_for(state, [activity("white_tower", at(9, 22, 9), 45)])

    result = VALIDATOR.validate(
        itinerary,
        state,
        planning_context_factory(pace_factors=pace_factors_for(state)),
    )

    assert ViolationCode.OUTSIDE_USER_WINDOW in error_codes(result)
    violation = next(
        item for item in result.violations if item.code == ViolationCode.OUTSIDE_USER_WINDOW
    )
    assert "09:00–10:27" in violation.message


def test_closed_during_visit_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 9), at(9, 22, 20))
    itinerary = itinerary_for(
        state,
        [activity("archaeological_museum", at(9, 22, 18), 60)],
    )
    assert ViolationCode.CLOSED_DURING_VISIT in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory())
    )


def test_after_last_entry_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 16), at(9, 22, 17))
    itinerary = itinerary_for(
        state,
        [activity("archaeological_museum", at(9, 22, 16, 45), 10)],
    )
    assert ViolationCode.AFTER_LAST_ENTRY in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory())
    )


def test_unknown_hours_are_not_treated_as_closed_or_open(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 9), at(9, 22, 12))
    itinerary = itinerary_for(state, [activity("rotunda", at(9, 22, 10), 30)])
    assert ViolationCode.HOURS_UNKNOWN_FOR_DATE in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory())
    )


def test_outside_user_window_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 9), at(9, 22, 12))
    itinerary = itinerary_for(
        state,
        [activity("aristotelous_square", at(9, 22, 8, 45), 30)],
    )
    assert ViolationCode.OUTSIDE_USER_WINDOW in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory())
    )


def test_excluded_category_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(
        at(9, 22, 9),
        at(9, 22, 12),
        exclude_categories=["museum"],
    )
    itinerary = itinerary_for(state, [activity("white_tower", at(9, 22, 9), 60)])
    assert ViolationCode.EXCLUDED_CATEGORY in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory())
    )


def test_excluded_poi_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(
        at(9, 22, 9),
        at(9, 22, 12),
        exclude_poi_ids=["white_tower"],
    )
    itinerary = itinerary_for(state, [activity("white_tower", at(9, 22, 9), 60)])
    assert ViolationCode.EXCLUDED_POI in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory())
    )


def test_storm_evening_blocks_outdoor_activity_at_1900(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    fixture = Path(__file__).parents[1] / "evals/fixtures/weather/storm_evening.json"
    weather = parse_open_meteo(
        json.loads(fixture.read_text(encoding="utf-8")),
        fetched_at=at(9, 22, 8),
        source="fixture:storm_evening",
        is_fixture=True,
    )
    flags = derive_weather_flags(weather, Party(), WeatherThresholds()).hours
    context = planning_context_factory(weather_flags=flags)
    state = state_for(at(9, 22, 18), at(9, 22, 21))
    itinerary = itinerary_for(
        state,
        [
            activity(
                "new_waterfront_umbrellas",
                at(9, 22, 19),
                30,
                exposure=Exposure.OUTDOOR,
            )
        ],
    )

    result = VALIDATOR.validate(itinerary, state, context)

    assert ViolationCode.STORM_OUTDOOR in error_codes(result)


def test_critical_poi_after_dark_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    flags = [HourlyWeatherFlags(at=at(9, 22, 20), after_dark=True)]
    state = state_for(at(9, 22, 19), at(9, 22, 22))
    itinerary = itinerary_for(
        state,
        [activity("seich_sou_forest", at(9, 22, 20), 60, exposure=Exposure.OUTDOOR)],
    )
    result = VALIDATOR.validate(
        itinerary,
        state,
        planning_context_factory(weather_flags=flags),
    )
    assert ViolationCode.CRITICAL_AFTER_DARK in error_codes(result)


def test_temporary_closure_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(10, 13, 9), at(10, 13, 13))
    itinerary = itinerary_for(state, [activity("white_tower", at(10, 13, 10), 60)])
    assert ViolationCode.TEMPORARY_CLOSURE in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory(now=at(10, 13, 8)))
    )


def test_unknown_poi_is_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 9), at(9, 22, 12))
    itinerary = itinerary_for(state, [activity("invented_place", at(9, 22, 10), 30)])
    assert ViolationCode.UNKNOWN_POI in error_codes(
        VALIDATOR.validate(itinerary, state, planning_context_factory())
    )


def test_weather_and_operational_warnings_are_reported(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(at(9, 22, 19), at(9, 22, 21))
    itinerary = itinerary_for(
        state,
        [
            activity(
                "white_tower",
                at(9, 22, 19, 31),
                60,
                exposure=Exposure.INDOOR,
            )
        ],
    )
    result = VALIDATOR.validate(
        itinerary,
        state,
        planning_context_factory(),
    )

    assert ViolationCode.CLOSES_SOON_AFTER_VISIT in warning_codes(result)
    close_warning = next(
        item for item in result.violations if item.code == ViolationCode.CLOSES_SOON_AFTER_VISIT
    )
    assert "please confirm" in close_warning.message


def test_missing_weather_warns_and_child_rules_use_pace_adjusted_walk(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    state = state_for(
        at(9, 22, 9),
        at(9, 22, 14),
        start_poi="white_tower",
        party=Party(children_ages=[10]),
    )
    itinerary = itinerary_for(
        state,
        [activity("seich_sou_forest", at(9, 22, 10, 30), 60, exposure=Exposure.OUTDOOR)],
    )
    context = planning_context_factory(
        weather_unavailable_reason="timeout",
        pace_factors=pace_factors_for(state),
    )

    result = VALIDATOR.validate(itinerary, state, context)

    warnings = warning_codes(result)
    assert ViolationCode.WEATHER_UNAVAILABLE in warnings
    assert ViolationCode.CHILD_UNSUITABLE in warnings
    assert ViolationCode.LONG_WALK_WITH_CHILD in warnings


def test_outdoor_weather_warnings_are_caught(
    planning_context_factory: Callable[..., PlanningContext],
) -> None:
    flags = [
        HourlyWeatherFlags(
            at=at(9, 22, 10),
            rain_risk=True,
            heat_risk=True,
            uv_high=True,
        )
    ]
    state = state_for(at(9, 22, 9), at(9, 22, 12))
    itinerary = itinerary_for(
        state,
        [
            activity(
                "aristotelous_square",
                at(9, 22, 10),
                30,
                exposure=Exposure.OUTDOOR,
            )
        ],
    )

    warnings = warning_codes(
        VALIDATOR.validate(
            itinerary,
            state,
            planning_context_factory(weather_flags=flags),
        )
    )

    assert ViolationCode.RAIN_OUTDOOR in warnings
    assert ViolationCode.HEAT_OUTDOOR in warnings
    assert ViolationCode.UV_HIGH_OUTDOOR in warnings

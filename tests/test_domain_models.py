from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.domain.models import (
    Activity,
    Exposure,
    Intent,
    Itinerary,
    RemoveActivity,
    TimeWindow,
    TripState,
    TurnAnalysis,
    ValidationResult,
    Violation,
)

ATHENS = ZoneInfo("Europe/Athens")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 19, hour, minute, tzinfo=ATHENS)


def test_time_window_requires_aware_increasing_datetimes() -> None:
    with pytest.raises(ValidationError):
        TimeWindow(start=datetime(2026, 9, 19, 10), end=datetime(2026, 9, 19, 12))

    with pytest.raises(ValidationError):
        TimeWindow(start=at(12), end=at(10))


def test_activity_duration_must_match_schedule() -> None:
    with pytest.raises(ValidationError, match="visit_minutes"):
        Activity(
            poi_id="example",
            start=at(10),
            end=at(11),
            visit_minutes=45,
            exposure=Exposure.INDOOR,
        )


def test_trip_state_requires_version_for_existing_itinerary() -> None:
    itinerary = Itinerary(window=TimeWindow(start=at(10), end=at(13)))

    with pytest.raises(ValidationError, match="itinerary_version"):
        TripState(itinerary=itinerary)

    state = TripState(itinerary=itinerary, itinerary_version=1)
    assert state.itinerary_version == 1


def test_turn_analysis_parses_discriminated_plan_edit() -> None:
    analysis = TurnAnalysis.model_validate(
        {
            "intents": ["edit_plan"],
            "plan_edits": [
                {
                    "op": "replace_activity",
                    "position": 2,
                    "required_tags": ["indoor"],
                    "indoor_only": True,
                }
            ],
        }
    )

    assert analysis.intents == [Intent.EDIT_PLAN]
    assert analysis.plan_edits[0].position == 2


def test_remove_activity_requires_exactly_one_target() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        RemoveActivity(position=2, poi_id="example")


def test_clarification_question_is_consistent() -> None:
    with pytest.raises(ValidationError, match="clarifying_question"):
        TurnAnalysis(intents=[Intent.CREATE_PLAN], needs_clarification=True)


def test_validation_result_cannot_claim_valid_with_errors() -> None:
    with pytest.raises(ValidationError, match="is_valid"):
        ValidationResult(
            is_valid=True,
            violations=[Violation(code="overlap", message="Activities overlap")],
            checked_at=at(10),
        )


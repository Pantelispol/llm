from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.catalog import ClosureRule, DateOverride
from app.domain.ports import OpeningHoursRequest
from app.tools.catalog import CatalogRepository
from app.tools.opening_hours import OpeningHoursEngine

ATHENS = ZoneInfo("Europe/Athens")


@pytest.fixture(scope="module")
def engine() -> OpeningHoursEngine:
    return OpeningHoursEngine(CatalogRepository())


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ATHENS)


def check(
    engine: OpeningHoursEngine,
    poi_id: str,
    start: datetime,
    end: datetime,
):
    return engine.check(OpeningHoursRequest(poi_id=poi_id, visit_start=start, visit_end=end))


def test_archaeological_museum_enforces_last_entry(engine: OpeningHoursEngine) -> None:
    result = check(
        engine,
        "archaeological_museum",
        at(2026, 9, 20, 16, 45),
        at(2026, 9, 20, 16, 55),
    )
    assert result.can_visit is False
    assert result.last_entry_at == at(2026, 9, 20, 16, 40)
    assert result.reason == "last entry is 16:40"


def test_archaeological_museum_incident_time_is_closed(engine: OpeningHoursEngine) -> None:
    result = check(
        engine,
        "archaeological_museum",
        at(2026, 9, 20, 18),
        at(2026, 9, 20, 18, 30),
    )
    assert result.can_visit is False
    assert result.reason == "closed at requested start time"


def test_archaeological_museum_winter_tuesday_is_closed(
    engine: OpeningHoursEngine,
) -> None:
    result = check(
        engine,
        "archaeological_museum",
        at(2026, 11, 3, 10),
        at(2026, 11, 3, 11),
    )
    assert result.can_visit is False
    assert result.source_rule == "seasonal"


def test_full_visit_must_end_before_close(engine: OpeningHoursEngine) -> None:
    result = check(
        engine,
        "archaeological_museum",
        at(2026, 9, 20, 16, 30),
        at(2026, 9, 20, 17, 15),
    )
    assert result.can_visit is False
    assert result.closes_at == at(2026, 9, 20, 17)
    assert result.reason == "visit would end after closing at 17:00"


def test_byzantine_museum_october_november_boundary(
    engine: OpeningHoursEngine,
) -> None:
    october = check(
        engine,
        "museum_of_byzantine_culture",
        at(2026, 10, 31, 19),
        at(2026, 10, 31, 19, 30),
    )
    november = check(
        engine,
        "museum_of_byzantine_culture",
        at(2026, 11, 1, 14, 45),
        at(2026, 11, 1, 15),
    )
    assert october.can_visit is True
    assert october.closes_at == at(2026, 10, 31, 20)
    assert november.can_visit is True
    assert november.closes_at == at(2026, 11, 1, 15, 30)


def test_white_tower_temporary_closure_has_highest_precedence(
    engine: OpeningHoursEngine,
) -> None:
    result = check(
        engine,
        "white_tower",
        at(2026, 10, 13, 10),
        at(2026, 10, 13, 11),
    )
    assert result.can_visit is False
    assert result.source_rule == "temporary_closure"
    assert result.reason == "closed: construction works"


def test_dst_switch_preserves_local_wall_clock_hours(engine: OpeningHoursEngine) -> None:
    before = check(
        engine,
        "white_tower",
        at(2026, 10, 24, 10),
        at(2026, 10, 24, 11),
    )
    after = check(
        engine,
        "white_tower",
        at(2026, 10, 25, 10),
        at(2026, 10, 25, 11),
    )
    assert before.opens_at is not None and before.opens_at.utcoffset() == timedelta(hours=3)
    assert after.opens_at is not None and after.opens_at.utcoffset() == timedelta(hours=2)
    assert after.opens_at.hour == 8


def test_october_28_is_open_and_free_not_globally_closed(
    engine: OpeningHoursEngine,
) -> None:
    result = check(
        engine,
        "archaeological_museum",
        at(2026, 10, 28, 10),
        at(2026, 10, 28, 11),
    )
    assert result.can_visit is True
    assert result.source_rule == "date_override"
    assert result.admission_eur == 0


@pytest.mark.parametrize(
    "poi_id",
    [
        "aristotelous_square",
        "ladadika",
        "new_waterfront_umbrellas",
        "nea_paralia_parks",
        "tsinari_ano_poli",
    ],
)
def test_public_spaces_are_open_across_midnight(engine: OpeningHoursEngine, poi_id: str) -> None:
    result = check(
        engine,
        poi_id,
        at(2026, 9, 20, 23, 30),
        at(2026, 9, 21, 0, 30),
    )
    assert result.can_visit is True
    assert result.source_rule == "seasonal"


def test_next_open_interval_skips_winter_tuesday(engine: OpeningHoursEngine) -> None:
    interval = engine.next_open_interval("archaeological_museum", at(2026, 11, 3, 10))
    assert interval is not None
    assert interval.opens_at == at(2026, 11, 4, 9)
    assert interval.closes_at == at(2026, 11, 4, 17)


def test_unknown_period_is_conservative(engine: OpeningHoursEngine) -> None:
    result = check(
        engine,
        "museum_of_byzantine_culture",
        at(2026, 4, 20, 10),
        at(2026, 4, 20, 11),
    )
    assert result.can_visit is False
    assert result.needs_verification is True
    assert result.reason == "opening hours unknown for this date"


def test_good_friday_uses_date_override_not_summer_hours(
    engine: OpeningHoursEngine,
) -> None:
    morning = check(
        engine,
        "archaeological_museum",
        at(2026, 4, 10, 10),
        at(2026, 4, 10, 11),
    )
    afternoon = check(
        engine,
        "archaeological_museum",
        at(2026, 4, 10, 13),
        at(2026, 4, 10, 14),
    )
    assert morning.can_visit is False
    assert afternoon.can_visit is True
    assert afternoon.source_rule == "date_override"


def test_temporary_closure_beats_date_override() -> None:
    repository = CatalogRepository()
    repository.get("white_tower").date_overrides.append(
        DateOverride(
            date=at(2026, 10, 13, 0).date(),
            expression="24/7",
            reason="test override",
        )
    )
    result = check(
        OpeningHoursEngine(repository),
        "white_tower",
        at(2026, 10, 13, 10),
        at(2026, 10, 13, 11),
    )
    assert result.source_rule == "temporary_closure"


def test_date_override_beats_holiday_closure() -> None:
    repository = CatalogRepository()
    repository.get("archaeological_museum").closed.append(
        ClosureRule(holiday_id="ohi_day", reason="test holiday closure")
    )
    result = check(
        OpeningHoursEngine(repository),
        "archaeological_museum",
        at(2026, 10, 28, 10),
        at(2026, 10, 28, 11),
    )
    assert result.can_visit is True
    assert result.source_rule == "date_override"


def test_per_poi_holiday_closure_beats_seasonal_hours(
    engine: OpeningHoursEngine,
) -> None:
    result = check(
        engine,
        "archaeological_museum",
        at(2026, 5, 1, 10),
        at(2026, 5, 1, 11),
    )
    assert result.can_visit is False
    assert result.source_rule == "holiday"
    assert result.reason == "closed: Labour Day"

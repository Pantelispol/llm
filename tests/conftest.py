from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.domain.ports import HourlyWeatherFlags, PaceFactors, PlanningContext, TravelMatrixRequest
from app.tools.catalog import CatalogRepository
from app.tools.opening_hours import OpeningHoursEngine
from app.tools.travel import PrecomputedTravelTimeProvider

ATHENS = ZoneInfo("Europe/Athens")


@pytest.fixture(scope="session")
def planning_context_factory() -> Callable[..., PlanningContext]:
    repository = CatalogRepository()
    locations = {poi.id: poi.coordinates for poi in repository.catalog.pois}
    matrix = PrecomputedTravelTimeProvider().matrix(TravelMatrixRequest(locations=locations))
    engine = OpeningHoursEngine(repository)

    def build(
        *,
        now: datetime = datetime(2026, 9, 22, 8, tzinfo=ATHENS),
        weather_flags: list[HourlyWeatherFlags] | None = None,
        weather_unavailable_reason: str | None = None,
        pace_factors: PaceFactors | None = None,
        transition_buffer_minutes: int = 5,
        long_walk_with_child_minutes: int = 20,
    ) -> PlanningContext:
        return PlanningContext(
            now=now,
            catalog=repository.catalog,
            opening_hours=engine,
            candidates=[],
            hourly_weather_flags=weather_flags or [],
            weather_unavailable_reason=weather_unavailable_reason,
            travel_matrix=matrix,
            pace_factors=pace_factors or PaceFactors(),
            transition_buffer_minutes=transition_buffer_minutes,
            long_walk_with_child_minutes=long_walk_with_child_minutes,
        )

    return build

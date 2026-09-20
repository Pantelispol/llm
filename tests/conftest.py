from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.domain.models import GeoPoint
from app.domain.ports import (
    HourlyWeatherFlags,
    PaceFactors,
    PlanningContext,
    TravelMatrixRequest,
    TravelMatrixResult,
)
from app.tools.catalog import CatalogRepository
from app.tools.opening_hours import OpeningHoursEngine
from app.tools.travel import MatrixFile, PrecomputedTravelTimeProvider

os.environ.setdefault("RAG_DENSE", "off")
os.environ.setdefault("RAG_STORE", "memory")

ATHENS = ZoneInfo("Europe/Athens")
FIXED_MATRIX_PATH = Path(__file__).parent / "fixtures" / "walking_matrix_fixed.json"


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
        travel_matrix: TravelMatrixResult | None = None,
    ) -> PlanningContext:
        return PlanningContext(
            now=now,
            catalog=repository.catalog,
            opening_hours=engine,
            candidates=[],
            hourly_weather_flags=weather_flags or [],
            weather_unavailable_reason=weather_unavailable_reason,
            travel_matrix=travel_matrix or matrix,
            pace_factors=pace_factors or PaceFactors(),
            transition_buffer_minutes=transition_buffer_minutes,
            long_walk_with_child_minutes=long_walk_with_child_minutes,
        )

    return build


@pytest.fixture(scope="session")
def fixed_planning_context_factory() -> Callable[..., PlanningContext]:
    repository = CatalogRepository()
    engine = OpeningHoursEngine(repository)
    fixture = MatrixFile.model_validate_json(FIXED_MATRIX_PATH.read_text(encoding="utf-8"))
    fixed_matrix = PrecomputedTravelTimeProvider(FIXED_MATRIX_PATH).matrix(
        TravelMatrixRequest(
            locations={
                poi_id: GeoPoint(latitude=40.6400, longitude=22.9400)
                for poi_id in fixture.poi_ids
            }
        )
    )

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
            travel_matrix=fixed_matrix,
            pace_factors=pace_factors or PaceFactors(),
            transition_buffer_minutes=transition_buffer_minutes,
            long_walk_with_child_minutes=long_walk_with_child_minutes,
        )

    return build

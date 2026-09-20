from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.domain.models import Exposure, GeoPoint
from app.domain.ports import (
    HourlyWeatherFlags,
    LLMResult,
    LLMUsage,
    PaceFactors,
    PlanningCandidate,
    PlanningContext,
    TravelMatrixRequest,
    TravelMatrixResult,
    WeatherRequest,
)
from app.tools.catalog import CatalogRepository
from app.tools.opening_hours import OpeningHoursEngine

ATHENS = ZoneInfo("Europe/Athens")


def test_weather_request_uses_aware_datetimes_without_adapter_parameters() -> None:
    request = WeatherRequest(
        location=GeoPoint(latitude=40.6401, longitude=22.9444),
        start=datetime(2026, 9, 19, 10, tzinfo=ATHENS),
        end=datetime(2026, 9, 19, 14, tzinfo=ATHENS),
    )
    assert "timezone" not in request.model_dump()


def test_weather_request_rejects_adapter_specific_timezone() -> None:
    with pytest.raises(ValidationError):
        WeatherRequest(
            location=GeoPoint(latitude=40.6401, longitude=22.9444),
            start=datetime(2026, 9, 19, 10, tzinfo=ATHENS),
            end=datetime(2026, 9, 19, 14, tzinfo=ATHENS),
            timezone="auto",
        )


def test_travel_matrix_requires_at_least_two_locations() -> None:
    with pytest.raises(ValidationError):
        TravelMatrixRequest(
            locations={"start": GeoPoint(latitude=40.6401, longitude=22.9444)},
        )


def test_planning_context_is_fully_typed() -> None:
    repository = CatalogRepository()
    context = PlanningContext(
        now=datetime(2026, 9, 19, 9, tzinfo=ATHENS),
        catalog=repository.catalog,
        opening_hours=OpeningHoursEngine(repository),
        candidates=[
            PlanningCandidate(
                poi_id="example",
                visit_minutes=60,
                exposure=Exposure.INDOOR,
            )
        ],
        hourly_weather_flags=[
            HourlyWeatherFlags(
                at=datetime(2026, 9, 19, 10, tzinfo=ATHENS),
                rain_risk=True,
            )
        ],
        travel_matrix=TravelMatrixResult(legs=[], source="fixture"),
        pace_factors=PaceFactors(travel_time_multiplier=1.25),
    )
    assert context.candidates[0].poi_id == "example"


def test_llm_result_carries_usage_for_text_and_structured_outputs() -> None:
    result = LLMResult[str](
        output="hello",
        usage=LLMUsage(
            model_id="test-model",
            reasoning_effort="none",
            input_tokens=10,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=2,
            reasoning_tokens=0,
            latency_ms=12.5,
        ),
    )
    assert result.usage.output_tokens == 2
    assert result.usage.ordinary_input_tokens == 10

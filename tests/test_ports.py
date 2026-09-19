from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.domain.models import GeoPoint
from app.domain.ports import TravelMatrixRequest, WeatherRequest

ATHENS = ZoneInfo("Europe/Athens")


def test_weather_request_defaults_to_provider_timezone_auto() -> None:
    request = WeatherRequest(
        location=GeoPoint(latitude=40.6401, longitude=22.9444),
        start=datetime(2026, 9, 19, 10, tzinfo=ATHENS),
        end=datetime(2026, 9, 19, 14, tzinfo=ATHENS),
    )
    assert request.timezone == "auto"


def test_weather_request_rejects_non_auto_timezone() -> None:
    with pytest.raises(ValidationError):
        WeatherRequest(
            location=GeoPoint(latitude=40.6401, longitude=22.9444),
            start=datetime(2026, 9, 19, 10, tzinfo=ATHENS),
            end=datetime(2026, 9, 19, 14, tzinfo=ATHENS),
            timezone="Europe/Athens",  # type: ignore[arg-type]
        )


def test_travel_matrix_requires_at_least_two_locations() -> None:
    with pytest.raises(ValidationError):
        TravelMatrixRequest(
            locations={"start": GeoPoint(latitude=40.6401, longitude=22.9444)},
            travel_date=datetime(2026, 9, 19).date(),
        )

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.domain.models import GeoPoint, Party
from app.domain.ports import DailyWeather, HourlyWeather, WeatherRequest, WeatherResult
from app.tools.weather_flags import WeatherThresholds, derive_weather_flags

ATHENS = ZoneInfo("Europe/Athens")
AT = datetime(2026, 9, 22, 12, tzinfo=ATHENS)
THRESHOLDS = WeatherThresholds()


def _weather(
    *,
    at: datetime = AT,
    probability: int = 0,
    code: int = 0,
    apparent: float = 20.0,
    uv: float = 0.0,
) -> WeatherResult:
    return WeatherResult(
        hours=[
            HourlyWeather(
                at=at,
                temperature_c=20,
                apparent_temperature_c=apparent,
                precipitation_probability=probability,
                precipitation_mm=0,
                weather_code=code,
                uv_index=uv,
                wind_speed_kmh=5,
                wind_gusts_kmh=10,
            )
        ],
        days=[
            DailyWeather(
                date=at.date(),
                sunrise=at.replace(hour=7, minute=15),
                sunset=at.replace(hour=19, minute=25),
            )
        ],
        fetched_at=AT,
        source="test",
    )


def _only_flag(weather: WeatherResult, party: Party | None = None):
    return derive_weather_flags(weather, party or Party(), THRESHOLDS).hours[0]


def test_rain_probability_boundary_is_inclusive() -> None:
    assert _only_flag(_weather(probability=50)).rain_risk is True
    assert _only_flag(_weather(probability=49)).rain_risk is False


@pytest.mark.parametrize("code", [61, 67, 80, 82])
def test_rain_weather_codes_are_flagged(code: int) -> None:
    assert _only_flag(_weather(code=code)).rain_risk is True


@pytest.mark.parametrize("code", [60, 68, 79, 83])
def test_adjacent_non_rain_codes_are_not_flagged(code: int) -> None:
    assert _only_flag(_weather(code=code)).rain_risk is False


@pytest.mark.parametrize("code", [95, 96, 99])
def test_storm_codes_are_flagged(code: int) -> None:
    assert _only_flag(_weather(code=code)).storm is True


def test_adult_heat_boundary_is_inclusive() -> None:
    assert _only_flag(_weather(apparent=35)).heat_risk is True
    assert _only_flag(_weather(apparent=34.9)).heat_risk is False


def test_children_lower_the_heat_threshold_to_32() -> None:
    weather = _weather(apparent=32)

    assert _only_flag(weather, Party(children_ages=[10])).heat_risk is True
    assert _only_flag(weather, Party()).heat_risk is False


def test_uv_boundary_is_inclusive() -> None:
    assert _only_flag(_weather(uv=8)).uv_high is True
    assert _only_flag(_weather(uv=7.9)).uv_high is False


def test_after_dark_uses_daily_sunset() -> None:
    assert _only_flag(_weather(at=AT.replace(hour=20))).after_dark is True
    assert _only_flag(_weather(at=AT.replace(hour=19))).after_dark is False


@pytest.mark.asyncio
async def test_rain_fixture_produces_compact_time_range() -> None:
    from app.tools.weather import FixtureProvider

    weather = await FixtureProvider("rain_after_16").forecast(
        WeatherRequest(
            location=GeoPoint(latitude=40.6401, longitude=22.9444),
            start=AT.replace(hour=9),
            end=AT.replace(hour=20),
        )
    )
    report = derive_weather_flags(weather, Party(), THRESHOLDS)

    assert "rain likely 16:00-19:00" in report.summary
    assert "no storm" in report.summary
    assert len(report.hours) == 24


def test_unavailable_weather_has_no_flags_and_explains_why() -> None:
    weather = WeatherResult(
        hours=[],
        fetched_at=AT,
        source="open_meteo",
        unavailable_reason="timeout",
    )

    report = derive_weather_flags(weather, Party(), THRESHOLDS)

    assert report.hours == []
    assert report.summary == "weather unavailable: timeout"

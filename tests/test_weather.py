from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.domain.models import GeoPoint
from app.domain.ports import WeatherRequest
from app.tools.weather import (
    FixtureProvider,
    OpenMeteoProvider,
    build_weather_provider,
    parse_open_meteo,
)

FIXTURE_DIR = Path(__file__).parents[1] / "evals" / "fixtures" / "weather"
ATHENS = ZoneInfo("Europe/Athens")
FIXED_NOW = datetime(2026, 9, 20, 12, tzinfo=ATHENS)


def _request(*, latitude: float = 40.6401, longitude: float = 22.9444) -> WeatherRequest:
    return WeatherRequest(
        location=GeoPoint(latitude=latitude, longitude=longitude),
        start=datetime(2026, 9, 22, 9, tzinfo=ATHENS),
        end=datetime(2026, 9, 22, 20, tzinfo=ATHENS),
    )


def _payload(name: str = "live_sample") -> dict[str, object]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_open_meteo_success_sends_adapter_owned_parameters() -> None:
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(200, json=_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenMeteoProvider(client=client, now=lambda: FIXED_NOW)
        result = await provider.forecast(_request())

    assert result.unavailable_reason is None
    assert result.source == "open_meteo"
    assert len(result.hours) == 24
    assert result.hours[0].at.tzinfo is not None
    assert result.hours[0].precipitation_mm >= 0
    assert result.hours[0].wind_gusts_kmh >= 0
    assert result.days[0].sunrise.hour == 7
    assert seen_request is not None
    assert seen_request.url.params["timezone"] == "Europe/Athens"
    assert set(seen_request.url.params["hourly"].split(",")) == {
        "temperature_2m",
        "apparent_temperature",
        "precipitation_probability",
        "precipitation",
        "weather_code",
        "uv_index",
        "wind_speed_10m",
        "wind_gusts_10m",
    }
    assert set(seen_request.url.params["daily"].split(",")) == {"sunrise", "sunset"}


@pytest.mark.asyncio
async def test_open_meteo_timeout_retries_once_and_degrades() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoProvider(client=client, now=lambda: FIXED_NOW).forecast(_request())

    assert calls == 2
    assert result.hours == []
    assert result.unavailable_reason == "timeout"


@pytest.mark.asyncio
async def test_open_meteo_5xx_retries_once_and_degrades() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoProvider(client=client, now=lambda: FIXED_NOW).forecast(_request())

    assert calls == 2
    assert result.unavailable_reason == "http_503"


@pytest.mark.asyncio
async def test_forecast_horizon_is_rejected_without_http_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_payload())

    request = WeatherRequest(
        location=GeoPoint(latitude=40.6401, longitude=22.9444),
        start=datetime(2026, 10, 6, 9, tzinfo=ATHENS),
        end=datetime(2026, 10, 6, 12, tzinfo=ATHENS),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoProvider(client=client, now=lambda: FIXED_NOW).forecast(request)

    assert calls == 0
    assert result.unavailable_reason == "forecast_horizon_exceeded"


@pytest.mark.asyncio
async def test_cache_uses_rounded_coordinates_and_date() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenMeteoProvider(
            client=client,
            now=lambda: FIXED_NOW,
            cache_clock=lambda: 100.0,
        )
        first = await provider.forecast(_request(latitude=40.64011))
        second = await provider.forecast(_request(latitude=40.64019))

    assert calls == 1
    assert second is first


@pytest.mark.parametrize(
    "fixture_path",
    sorted(FIXTURE_DIR.glob("*.json")),
    ids=lambda path: path.stem,
)
def test_every_fixture_uses_the_live_parser(fixture_path: Path) -> None:
    result = parse_open_meteo(
        json.loads(fixture_path.read_text(encoding="utf-8")),
        fetched_at=FIXED_NOW,
        source=f"test:{fixture_path.stem}",
        is_fixture=True,
    )

    assert len(result.hours) == 24
    assert len(result.days) == 1
    assert result.hours[0].at.date().isoformat() == "2026-09-22"


@pytest.mark.asyncio
async def test_fixture_provider_marks_source_for_disclosure() -> None:
    provider = build_weather_provider(Settings(weather_fixture="clear_day"))
    assert isinstance(provider, FixtureProvider)

    result = await provider.forecast(_request())

    assert result.is_fixture is True
    assert result.source == "fixture:clear_day"


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_WEATHER_TESTS") != "1",
    reason="set RUN_LIVE_WEATHER_TESTS=1 to call Open-Meteo",
)
@pytest.mark.asyncio
async def test_live_open_meteo_smoke() -> None:
    now = datetime.now(UTC)
    local_tomorrow = now.astimezone(ATHENS) + timedelta(days=1)
    request = WeatherRequest(
        location=GeoPoint(latitude=40.6401, longitude=22.9444),
        start=local_tomorrow.replace(hour=9, minute=0, second=0, microsecond=0),
        end=local_tomorrow.replace(hour=12, minute=0, second=0, microsecond=0),
    )
    provider = OpenMeteoProvider(now=lambda: now)

    result = await provider.forecast(request)

    assert result.unavailable_reason is None
    assert result.hours

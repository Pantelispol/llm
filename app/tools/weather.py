from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.domain.ports import (
    DailyWeather,
    HourlyWeather,
    WeatherProvider,
    WeatherRequest,
    WeatherResult,
)

ATHENS = ZoneInfo("Europe/Athens")
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation_probability",
    "precipitation",
    "weather_code",
    "uv_index",
    "wind_speed_10m",
    "wind_gusts_10m",
)
DAILY_FIELDS = ("sunrise", "sunset")
DEFAULT_FIXTURE_DIR = Path(__file__).parents[2] / "evals" / "fixtures" / "weather"


def _local_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ATHENS)
    return parsed.astimezone(ATHENS)


def parse_open_meteo(
    payload: dict[str, Any],
    *,
    fetched_at: datetime,
    source: str,
    is_fixture: bool = False,
) -> WeatherResult:
    hourly = payload["hourly"]
    hourly_columns = [hourly["time"], *(hourly[field] for field in HOURLY_FIELDS)]
    expected_hours = len(hourly_columns[0])
    if any(len(column) != expected_hours for column in hourly_columns):
        raise ValueError("Open-Meteo hourly arrays have different lengths")

    hours = [
        HourlyWeather(
            at=_local_datetime(at),
            temperature_c=temperature,
            apparent_temperature_c=apparent,
            precipitation_probability=precipitation_probability,
            precipitation_mm=precipitation,
            weather_code=weather_code,
            uv_index=uv_index,
            wind_speed_kmh=wind_speed,
            wind_gusts_kmh=wind_gusts,
        )
        for (
            at,
            temperature,
            apparent,
            precipitation_probability,
            precipitation,
            weather_code,
            uv_index,
            wind_speed,
            wind_gusts,
        ) in zip(*hourly_columns, strict=True)
    ]

    daily = payload["daily"]
    daily_columns = [daily["time"], *(daily[field] for field in DAILY_FIELDS)]
    expected_days = len(daily_columns[0])
    if any(len(column) != expected_days for column in daily_columns):
        raise ValueError("Open-Meteo daily arrays have different lengths")
    days = [
        DailyWeather(
            date=datetime.fromisoformat(day).date(),
            sunrise=_local_datetime(sunrise),
            sunset=_local_datetime(sunset),
        )
        for day, sunrise, sunset in zip(*daily_columns, strict=True)
    ]

    return WeatherResult(
        hours=hours,
        days=days,
        fetched_at=fetched_at,
        source=source,
        is_fixture=is_fixture,
    )


class OpenMeteoProvider:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: int = 1200,
        now: Callable[[], datetime] | None = None,
        cache_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client or httpx.AsyncClient()
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._cache_clock = cache_clock
        self._cache: dict[tuple[float, float, str, str], tuple[float, WeatherResult]] = {}

    async def forecast(self, request: WeatherRequest) -> WeatherResult:
        fetched_at = self._now()
        start_date = request.start.astimezone(ATHENS).date()
        end_date = request.end.astimezone(ATHENS).date()
        last_forecast_date = fetched_at.astimezone(ATHENS).date() + timedelta(days=15)
        if end_date > last_forecast_date:
            return self._unavailable(fetched_at, "forecast_horizon_exceeded")

        key = (
            round(request.location.latitude, 3),
            round(request.location.longitude, 3),
            start_date.isoformat(),
            end_date.isoformat(),
        )
        cached = self._cache.get(key)
        cache_now = self._cache_clock()
        if cached is not None and cache_now - cached[0] < self._cache_ttl_seconds:
            return cached[1]

        params = {
            "latitude": request.location.latitude,
            "longitude": request.location.longitude,
            "timezone": "Europe/Athens",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "hourly": ",".join(HOURLY_FIELDS),
            "daily": ",".join(DAILY_FIELDS),
        }
        failure_reason = "weather_service_unavailable"
        for _attempt in range(2):
            try:
                response = await self._client.get(
                    OPEN_METEO_URL,
                    params=params,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException:
                failure_reason = "timeout"
                continue
            except httpx.TransportError:
                failure_reason = "transport_error"
                continue

            if response.status_code >= 500:
                failure_reason = f"http_{response.status_code}"
                continue
            if response.is_error:
                return self._unavailable(fetched_at, f"http_{response.status_code}")

            try:
                result = parse_open_meteo(
                    response.json(),
                    fetched_at=fetched_at,
                    source="open_meteo",
                )
            except (KeyError, TypeError, ValueError):
                return self._unavailable(fetched_at, "invalid_response")

            self._cache[key] = (cache_now, result)
            return result

        return self._unavailable(fetched_at, failure_reason)

    @staticmethod
    def _unavailable(fetched_at: datetime, reason: str) -> WeatherResult:
        return WeatherResult(
            hours=[],
            days=[],
            fetched_at=fetched_at,
            source="open_meteo",
            unavailable_reason=reason,
        )


class FixtureProvider:
    def __init__(self, scenario: str, *, fixture_dir: Path = DEFAULT_FIXTURE_DIR) -> None:
        if not scenario or Path(scenario).name != scenario:
            raise ValueError("weather fixture must be a scenario name")
        self._scenario = scenario
        self._fixture_path = fixture_dir / f"{scenario}.json"

    async def forecast(self, request: WeatherRequest) -> WeatherResult:
        del request
        fetched_at = datetime.now(UTC)
        try:
            payload = json.loads(self._fixture_path.read_text(encoding="utf-8"))
            return parse_open_meteo(
                payload,
                fetched_at=fetched_at,
                source=f"fixture:{self._scenario}",
                is_fixture=True,
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return WeatherResult(
                hours=[],
                days=[],
                fetched_at=fetched_at,
                source=f"fixture:{self._scenario}",
                is_fixture=True,
                unavailable_reason="fixture_unavailable",
            )


def build_weather_provider(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> WeatherProvider:
    if settings.weather_fixture:
        return FixtureProvider(settings.weather_fixture)
    return OpenMeteoProvider(
        client=client,
        cache_ttl_seconds=settings.weather_cache_ttl_seconds,
    )

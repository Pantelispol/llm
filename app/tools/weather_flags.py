from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.domain.models import Party
from app.domain.ports import HourlyWeatherFlags, WeatherResult

RAIN_CODES = frozenset((*range(61, 68), *range(80, 83)))
STORM_CODES = frozenset((95, 96, 99))


@dataclass(frozen=True)
class WeatherThresholds:
    rain_probability: int = 50
    heat_apparent_c: float = 35.0
    heat_children_apparent_c: float = 32.0
    uv_high: float = 8.0


class WeatherFlagReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: list[HourlyWeatherFlags]
    summary: str


def thresholds_from_settings(settings: Settings) -> WeatherThresholds:
    return WeatherThresholds(
        rain_probability=settings.weather_rain_probability_threshold,
        heat_apparent_c=settings.weather_heat_apparent_c,
        heat_children_apparent_c=settings.weather_heat_children_apparent_c,
        uv_high=settings.weather_uv_high_threshold,
    )


def derive_weather_flags(
    weather: WeatherResult,
    party: Party,
    thresholds: WeatherThresholds,
) -> WeatherFlagReport:
    if weather.unavailable_reason:
        return WeatherFlagReport(
            hours=[],
            summary=f"weather unavailable: {weather.unavailable_reason}",
        )

    sunsets = {day.date: day.sunset for day in weather.days}
    heat_threshold = (
        thresholds.heat_children_apparent_c if party.children_ages else thresholds.heat_apparent_c
    )
    flags = [
        HourlyWeatherFlags(
            at=hour.at,
            rain_risk=(
                hour.precipitation_probability >= thresholds.rain_probability
                or hour.weather_code in RAIN_CODES
            ),
            heat_risk=hour.apparent_temperature_c >= heat_threshold,
            storm=hour.weather_code in STORM_CODES,
            uv_high=hour.uv_index >= thresholds.uv_high,
            after_dark=(hour.at >= sunsets[hour.at.date()] if hour.at.date() in sunsets else False),
        )
        for hour in weather.hours
    ]
    return WeatherFlagReport(hours=flags, summary=_summarize(weather, flags))


def _summarize(weather: WeatherResult, flags: list[HourlyWeatherFlags]) -> str:
    if not weather.hours:
        return "weather data contains no hourly forecast"

    parts = [
        _flag_summary(flags, "rain_risk", "rain likely", "no rain risk"),
        _flag_summary(flags, "storm", "storm risk", "no storm"),
    ]
    if any(hour.heat_risk for hour in flags):
        parts.append(_flag_summary(flags, "heat_risk", "heat risk", ""))
    if any(hour.uv_high for hour in flags):
        parts.append(_flag_summary(flags, "uv_high", "high UV", ""))
    maximum = max(hour.apparent_temperature_c for hour in weather.hours)
    parts.append(f"max feels-like {maximum:.0f}C")
    return "; ".join(parts)


def _flag_summary(
    flags: list[HourlyWeatherFlags],
    field: str,
    present_label: str,
    absent_label: str,
) -> str:
    flagged_times = [hour.at for hour in flags if getattr(hour, field)]
    if not flagged_times:
        return absent_label
    ranges = ", ".join(_format_range(start, end) for start, end in _ranges(flagged_times))
    return f"{present_label} {ranges}"


def _ranges(times: list[datetime]) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []
    start = previous = times[0]
    for current in times[1:]:
        if current - previous > timedelta(hours=1):
            ranges.append((start, previous + timedelta(hours=1)))
            start = current
        previous = current
    ranges.append((start, previous + timedelta(hours=1)))
    return ranges


def _format_range(start: datetime, end: datetime) -> str:
    return f"{start:%H:%M}-{end:%H:%M}"

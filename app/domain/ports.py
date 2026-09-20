from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol, TypeVar, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.domain.catalog import PoiCatalog
from app.domain.models import Exposure, GeoPoint, Itinerary, TripState, ValidationResult


class PortModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeatherRequest(PortModel):
    location: GeoPoint
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def valid_window(self) -> WeatherRequest:
        if self.end <= self.start:
            raise ValueError("weather request end must be after start")
        return self


class HourlyWeather(PortModel):
    at: AwareDatetime
    temperature_c: float
    apparent_temperature_c: float
    precipitation_probability: int = Field(ge=0, le=100)
    precipitation_mm: float = Field(ge=0)
    weather_code: int
    uv_index: float = Field(ge=0)
    wind_speed_kmh: float = Field(ge=0)
    wind_gusts_kmh: float = Field(ge=0)


class DailyWeather(PortModel):
    date: date
    sunrise: AwareDatetime
    sunset: AwareDatetime


class WeatherResult(PortModel):
    hours: list[HourlyWeather]
    days: list[DailyWeather] = Field(default_factory=list)
    fetched_at: AwareDatetime
    source: str
    is_fixture: bool = False
    unavailable_reason: str | None = None


class WeatherProvider(Protocol):
    async def forecast(self, request: WeatherRequest) -> WeatherResult: ...


class OpeningHoursRequest(PortModel):
    poi_id: str
    visit_start: AwareDatetime
    visit_end: AwareDatetime

    @model_validator(mode="after")
    def valid_visit(self) -> OpeningHoursRequest:
        if self.visit_end <= self.visit_start:
            raise ValueError("visit_end must be after visit_start")
        return self


class OpeningHoursResult(PortModel):
    can_visit: bool
    reason: str
    opens_at: AwareDatetime | None = None
    closes_at: AwareDatetime | None = None
    last_entry_at: AwareDatetime | None = None
    source_rule: str | None = None
    admission_eur: float | None = Field(default=None, ge=0)
    needs_verification: bool = False


class OpenInterval(PortModel):
    opens_at: AwareDatetime
    closes_at: AwareDatetime
    last_entry_at: AwareDatetime
    source_rule: str
    needs_verification: bool = False


@runtime_checkable
class OpeningHoursChecker(Protocol):
    def check(self, request: OpeningHoursRequest) -> OpeningHoursResult: ...

    def next_open_interval(
        self, poi_id: str, after: datetime, *, search_days: int = 370
    ) -> OpenInterval | None: ...


class TravelMatrixRequest(PortModel):
    locations: dict[str, GeoPoint] = Field(min_length=2)


class TravelLeg(PortModel):
    origin_id: str
    destination_id: str
    minutes: int = Field(ge=0)
    approximate: bool = False


class TravelMatrixResult(PortModel):
    legs: list[TravelLeg]
    source: str


class TravelTimeProvider(Protocol):
    def matrix(self, request: TravelMatrixRequest) -> TravelMatrixResult: ...


class PlanningCandidate(PortModel):
    poi_id: str
    visit_minutes: int = Field(gt=0)
    exposure: Exposure
    tags: list[str] = Field(default_factory=list)
    utility_score: float = 0.0


class HourlyWeatherFlags(PortModel):
    at: AwareDatetime
    rain_risk: bool = False
    heat_risk: bool = False
    storm: bool = False
    uv_high: bool = False
    after_dark: bool = False


class PaceFactors(PortModel):
    travel_time_multiplier: float = Field(default=1.0, gt=0)
    visit_time_multiplier: float = Field(default=1.0, gt=0)


class PlanningContext(PortModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    now: AwareDatetime
    catalog: PoiCatalog
    opening_hours: OpeningHoursChecker
    candidates: list[PlanningCandidate]
    hourly_weather_flags: list[HourlyWeatherFlags]
    weather_unavailable_reason: str | None = None
    travel_matrix: TravelMatrixResult
    pace_factors: PaceFactors
    transition_buffer_minutes: int = Field(default=5, ge=0)
    long_walk_with_child_minutes: int = Field(default=20, ge=1)


class RetrievalHit(PortModel):
    chunk_id: str
    poi_id: str
    section: str
    text: str
    source_url: str
    score: float
    bm25_score: float | None = None
    dense_score: float | None = None
    rrf_score: float | None = None
    is_untrusted: bool = False


class Retriever(Protocol):
    async def search(self, query: str, *, limit: int = 5) -> list[RetrievalHit]: ...


class Planner(Protocol):
    def create_or_repair(self, state: TripState, context: PlanningContext) -> Itinerary: ...


class ItineraryValidator(Protocol):
    def validate(
        self, itinerary: Itinerary, state: TripState, context: PlanningContext
    ) -> ValidationResult: ...


StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


class LLMUsage(PortModel):
    model_id: str
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"]
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def usage_breakdowns_fit_totals(self) -> LLMUsage:
        if self.cached_input_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError("cached and cache-write tokens cannot exceed input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens cannot exceed output tokens")
        return self

    @property
    def ordinary_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens - self.cache_write_tokens


class LLMResult[Output](PortModel):
    output: Output
    usage: LLMUsage


class LLMProvider(Protocol):
    async def structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_type: type[StructuredOutput],
    ) -> LLMResult[StructuredOutput]: ...

    async def text(self, *, system_prompt: str, user_prompt: str) -> LLMResult[str]: ...

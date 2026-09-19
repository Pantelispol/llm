from __future__ import annotations

from datetime import date
from typing import Any, Literal, Protocol, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.models import GeoPoint, Itinerary, TripState, ValidationResult


class PortModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeatherRequest(PortModel):
    location: GeoPoint
    start: AwareDatetime
    end: AwareDatetime
    timezone: Literal["auto"] = "auto"


class HourlyWeather(PortModel):
    at: AwareDatetime
    temperature_c: float
    apparent_temperature_c: float
    precipitation_probability: int = Field(ge=0, le=100)
    weather_code: int
    uv_index: float = Field(ge=0)
    wind_speed_kmh: float = Field(ge=0)


class WeatherResult(PortModel):
    hours: list[HourlyWeather]
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


class OpeningHoursResult(PortModel):
    can_visit: bool
    reason: str
    closes_at: AwareDatetime | None = None
    last_entry_at: AwareDatetime | None = None
    needs_verification: bool = False


class OpeningHoursChecker(Protocol):
    def check(self, request: OpeningHoursRequest) -> OpeningHoursResult: ...


class TravelMatrixRequest(PortModel):
    locations: dict[str, GeoPoint] = Field(min_length=2)
    travel_date: date


class TravelLeg(PortModel):
    origin_id: str
    destination_id: str
    minutes: int = Field(ge=0)
    approximate: bool = False


class TravelMatrixResult(PortModel):
    legs: list[TravelLeg]
    source: str


class TravelTimeProvider(Protocol):
    async def matrix(self, request: TravelMatrixRequest) -> TravelMatrixResult: ...


class RetrievalHit(PortModel):
    chunk_id: str
    poi_id: str
    text: str
    source_url: str
    score: float


class Retriever(Protocol):
    async def search(self, query: str, *, limit: int = 5) -> list[RetrievalHit]: ...


class Planner(Protocol):
    def create_or_repair(self, state: TripState, gathered: Any) -> Itinerary: ...


class ItineraryValidator(Protocol):
    def validate(
        self, itinerary: Itinerary, state: TripState, gathered: Any
    ) -> ValidationResult: ...


StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


class LLMProvider(Protocol):
    async def structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_type: type[StructuredOutput],
    ) -> StructuredOutput: ...

    async def text(self, *, system_prompt: str, user_prompt: str) -> str: ...

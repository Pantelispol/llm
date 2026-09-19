from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.models import Exposure, GeoPoint


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


def validate_month_day(value: str) -> str:
    try:
        datetime.strptime(f"2000-{value}", "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("month-day must use MM-DD") from error
    return value


class LocalizedNames(CatalogModel):
    en: str
    el: str


class VisitMinutes(CatalogModel):
    min: int = Field(gt=0)
    typical: int = Field(gt=0)
    max: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not self.min <= self.typical <= self.max:
            raise ValueError("visit minutes must satisfy min <= typical <= max")
        return self


class SeasonalHours(CatalogModel):
    valid_from: str
    valid_to: str
    expression: str

    @field_validator("valid_from", "valid_to")
    @classmethod
    def valid_month_day(cls, value: str) -> str:
        return validate_month_day(value)


class TemporaryClosure(CatalogModel):
    start: date = Field(alias="from")
    end: date = Field(alias="to")
    reason: str

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("temporary closure end must not precede start")
        return self


class DateOverride(CatalogModel):
    date: date
    expression: str
    reason: str
    admission_eur: float | None = Field(default=None, ge=0)


class ClosureRule(CatalogModel):
    annual_date: str | None = None
    holiday_id: str | None = None
    reason: str

    @field_validator("annual_date")
    @classmethod
    def valid_annual_date(cls, value: str | None) -> str | None:
        return validate_month_day(value) if value is not None else None

    @model_validator(mode="after")
    def exactly_one_selector(self) -> Self:
        if (self.annual_date is None) == (self.holiday_id is None):
            raise ValueError("closure rule needs exactly one date or holiday id")
        return self


class PriceEur(CatalogModel):
    standard: float = Field(ge=0)
    reduced: float | None = Field(default=None, ge=0)


class Source(CatalogModel):
    url: str
    kind: str
    verified_at: date | None = None


class Conflict(CatalogModel):
    field: str
    stale_value: str
    risk: str
    source: Source


class ChildFriendly(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HeatExposure(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SafetyTier(StrEnum):
    STANDARD = "standard"
    CAUTION = "caution"
    CRITICAL = "critical"


class Poi(CatalogModel):
    id: str
    names: LocalizedNames
    aliases: list[str]
    coordinates: GeoPoint
    category: str
    tags: list[str]
    exposure: Exposure
    visit_minutes: VisitMinutes
    opening_hours: list[SeasonalHours]
    last_entry_before_close_min: int | None = Field(default=None, ge=0)
    temporary_closures: list[TemporaryClosure] = Field(default_factory=list)
    date_overrides: list[DateOverride] = Field(default_factory=list)
    closed: list[ClosureRule] = Field(default_factory=list)
    child_friendly: ChildFriendly
    step_free: bool | None
    hilly: bool | None = None
    heat_exposure: HeatExposure
    price_eur: PriceEur | None
    safety_tier: SafetyTier
    notes: str | None
    conflicts: list[Conflict]
    source: Source
    field_sources: dict[str, Source] = Field(default_factory=dict)
    needs_verification: dict[str, bool]


class PoiCatalog(CatalogModel):
    schema_version: int
    city: str
    timezone: str
    verification_policy: str
    pois: list[Poi]

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [poi.id for poi in self.pois]
        if len(ids) != len(set(ids)):
            raise ValueError("POI ids must be unique")
        return self


class Holiday(CatalogModel):
    id: str
    date: date
    name_en: str
    name_el: str
    scope: str
    easter_dependent: bool = False
    needs_verification: bool


class HolidayCalendar(CatalogModel):
    schema_version: int
    country: str
    year: int
    timezone: str
    policy: str
    sources_to_verify: list[Source]
    holidays: list[Holiday]

    @model_validator(mode="after")
    def unique_holiday_ids_and_dates(self) -> Self:
        ids = [holiday.id for holiday in self.holidays]
        if len(ids) != len(set(ids)):
            raise ValueError("holiday ids must be unique")
        if any(holiday.date.year != self.year for holiday in self.holidays):
            raise ValueError("holiday dates must match calendar year")
        return self

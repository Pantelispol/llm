from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Intent(StrEnum):
    TOURISM_QA = "tourism_qa"
    RECOMMENDATION = "recommendation"
    CREATE_PLAN = "create_plan"
    EDIT_PLAN = "edit_plan"
    FEASIBILITY_CHECK = "feasibility_check"
    WEATHER_QUESTION = "weather_question"
    SAFETY = "safety"
    UNSUPPORTED_LIVE_INFO = "unsupported_live_info"
    OUT_OF_SCOPE = "out_of_scope"
    SMALL_TALK = "small_talk"


class Pace(StrEnum):
    RELAXED = "relaxed"
    NORMAL = "normal"
    BRISK = "brisk"


class Mobility(StrEnum):
    STANDARD = "standard"
    LIMITED_WALKING = "limited_walking"
    STEP_FREE = "step_free"


class Transport(StrEnum):
    WALK = "walk"
    PUBLIC_TRANSPORT = "public_transport"
    CAR = "car"


class Exposure(StrEnum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "mixed"


class ActivityKind(StrEnum):
    VISIT = "visit"
    MEAL = "meal"
    BREAK = "break"


class GeoPoint(DomainModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocationRef(DomainModel):
    label: str = Field(min_length=1, max_length=120)
    poi_id: str | None = None
    coordinates: GeoPoint | None = None


class TimeWindow(DomainModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def end_must_follow_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError("time window end must be after start")
        return self


class Party(DomainModel):
    adults: int = Field(default=1, ge=1, le=20)
    children_ages: list[int] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def children_must_be_minors(self) -> Self:
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children_ages must contain ages from 0 to 17")
        return self


class Activity(DomainModel):
    kind: ActivityKind = ActivityKind.VISIT
    poi_id: str | None = Field(default=None, min_length=1)
    area_label: str | None = Field(default=None, min_length=1, max_length=120)
    start: AwareDatetime
    end: AwareDatetime
    visit_minutes: int = Field(gt=0, le=480)
    travel_from_previous_minutes: int = Field(default=0, ge=0, le=480)
    buffer_before_minutes: int = Field(default=0, ge=0, le=120)
    exposure: Exposure
    citation_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def duration_must_match_schedule(self) -> Self:
        if self.kind == ActivityKind.VISIT and self.poi_id is None:
            raise ValueError("visit activities require poi_id")
        elapsed_seconds = (self.end - self.start).total_seconds()
        if elapsed_seconds <= 0:
            raise ValueError("activity end must be after start")
        if elapsed_seconds != self.visit_minutes * 60:
            raise ValueError("activity times must exactly match visit_minutes")
        return self


class Itinerary(DomainModel):
    window: TimeWindow
    activities: list[Activity] = Field(default_factory=list)
    total_travel_minutes: int = Field(default=0, ge=0)
    approximate_travel_times: bool = False


class TripState(DomainModel):
    time_window: TimeWindow | None = None
    start_location: LocationRef | None = None
    party: Party = Field(default_factory=Party)
    mobility: Mobility = Mobility.STANDARD
    transport: Transport = Transport.WALK
    pace: Pace = Pace.NORMAL
    interests: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    exclude_poi_ids: list[str] = Field(default_factory=list)
    visited: list[str] = Field(default_factory=list)
    itinerary: Itinerary | None = None
    itinerary_version: int = Field(default=0, ge=0)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def itinerary_and_version_are_consistent(self) -> Self:
        if self.itinerary is None and self.itinerary_version != 0:
            raise ValueError("itinerary_version must be 0 when there is no itinerary")
        if self.itinerary is not None and self.itinerary_version == 0:
            raise ValueError("itinerary_version must be positive when an itinerary exists")
        return self


class ConstraintUpdates(DomainModel):
    time_window: TimeWindow | None = None
    clear_time_window: bool = False
    start_location: LocationRef | None = None
    clear_start_location: bool = False
    party: Party | None = None
    mobility: Mobility | None = None
    transport: Transport | None = None
    pace: Pace | None = None
    add_interests: list[str] = Field(default_factory=list)
    remove_interests: list[str] = Field(default_factory=list)
    add_exclude_categories: list[str] = Field(default_factory=list)
    remove_exclude_categories: list[str] = Field(default_factory=list)
    add_exclude_poi_ids: list[str] = Field(default_factory=list)
    remove_exclude_poi_ids: list[str] = Field(default_factory=list)
    add_visited: list[str] = Field(default_factory=list)

    @field_validator(
        "add_interests",
        "remove_interests",
        "add_exclude_categories",
        "remove_exclude_categories",
        "add_exclude_poi_ids",
        "remove_exclude_poi_ids",
        "add_visited",
    )
    @classmethod
    def deduplicate_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def clear_and_set_are_mutually_exclusive(self) -> Self:
        if self.clear_time_window and self.time_window is not None:
            raise ValueError("cannot set and clear time_window in the same update")
        if self.clear_start_location and self.start_location is not None:
            raise ValueError("cannot set and clear start_location in the same update")
        return self


def _normalize_exposure_selector(value: object) -> object:
    if not isinstance(value, dict):
        return value

    normalized = value.copy()
    tags = normalized.get("required_tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        return normalized

    normalized_tags = list(dict.fromkeys(tag.strip().lower() for tag in tags if tag.strip()))
    exposure_values = {item.value for item in Exposure}
    exposure_tags = [tag for tag in normalized_tags if tag in exposure_values]
    if len(exposure_tags) > 1:
        raise ValueError("required_tags contain conflicting exposure values")

    if exposure_tags:
        tag_exposure = exposure_tags[0]
        explicit_exposure = normalized.get("required_exposure")
        if isinstance(explicit_exposure, Exposure):
            explicit_exposure = explicit_exposure.value
        if explicit_exposure is not None and explicit_exposure != tag_exposure:
            raise ValueError("required_exposure conflicts with exposure in required_tags")
        normalized["required_exposure"] = tag_exposure

    normalized["required_tags"] = [
        tag for tag in normalized_tags if tag not in exposure_values
    ]
    return normalized


class ReplaceActivity(DomainModel):
    op: Literal["replace_activity"] = "replace_activity"
    position: int = Field(ge=1)
    preferred_poi_id: str | None = None
    required_tags: list[str] = Field(default_factory=list)
    required_exposure: Exposure | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_exposure_tag(cls, value: object) -> object:
        return _normalize_exposure_selector(value)


class RemoveActivity(DomainModel):
    op: Literal["remove_activity"] = "remove_activity"
    position: int | None = Field(default=None, ge=1)
    poi_id: str | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> Self:
        if (self.position is None) == (self.poi_id is None):
            raise ValueError("remove_activity requires exactly one of position or poi_id")
        return self


class AddActivity(DomainModel):
    op: Literal["add_activity"] = "add_activity"
    preferred_poi_id: str | None = None
    required_tags: list[str] = Field(default_factory=list)
    required_exposure: Exposure | None = None
    after_position: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def normalize_exposure_tag(cls, value: object) -> object:
        return _normalize_exposure_selector(value)

    @model_validator(mode="after")
    def selector_is_required(self) -> Self:
        if (
            self.preferred_poi_id is None
            and not self.required_tags
            and self.required_exposure is None
        ):
            raise ValueError("add_activity requires a POI id, tag, or exposure")
        return self


class MoveActivity(DomainModel):
    op: Literal["move_activity"] = "move_activity"
    from_position: int = Field(ge=1)
    to_position: int = Field(ge=1)


PlanEditOperation = Annotated[
    ReplaceActivity | RemoveActivity | AddActivity | MoveActivity,
    Field(discriminator="op"),
]


class ExtractedEntities(DomainModel):
    mentioned_poi_ids: list[str] = Field(default_factory=list)
    unresolved_place_names: list[str] = Field(default_factory=list)
    requested_date_text: str | None = None

    @field_validator("mentioned_poi_ids", "unresolved_place_names")
    @classmethod
    def deduplicate_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class TurnAnalysis(DomainModel):
    intents: list[Intent] = Field(min_length=1)
    entities: ExtractedEntities = Field(default_factory=ExtractedEntities)
    constraint_updates: ConstraintUpdates = Field(default_factory=ConstraintUpdates)
    plan_edits: list[PlanEditOperation] = Field(default_factory=list)
    needs_clarification: bool = False
    clarifying_question: str | None = None

    @field_validator("intents")
    @classmethod
    def deduplicate_intents(cls, values: list[Intent]) -> list[Intent]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def clarification_fields_are_consistent(self) -> Self:
        if self.needs_clarification and not self.clarifying_question:
            raise ValueError("clarifying_question is required when clarification is needed")
        if not self.needs_clarification and self.clarifying_question:
            self.clarifying_question = None
        return self


class ViolationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ViolationCode(StrEnum):
    OVERLAP = "OVERLAP"
    TRAVEL_GAP_TOO_SHORT = "TRAVEL_GAP_TOO_SHORT"
    CLOSED_DURING_VISIT = "CLOSED_DURING_VISIT"
    AFTER_LAST_ENTRY = "AFTER_LAST_ENTRY"
    HOURS_UNKNOWN_FOR_DATE = "HOURS_UNKNOWN_FOR_DATE"
    OUTSIDE_USER_WINDOW = "OUTSIDE_USER_WINDOW"
    EXCLUDED_CATEGORY = "EXCLUDED_CATEGORY"
    EXCLUDED_POI = "EXCLUDED_POI"
    STORM_OUTDOOR = "STORM_OUTDOOR"
    CRITICAL_AFTER_DARK = "CRITICAL_AFTER_DARK"
    TEMPORARY_CLOSURE = "TEMPORARY_CLOSURE"
    UNKNOWN_POI = "UNKNOWN_POI"
    RAIN_OUTDOOR = "RAIN_OUTDOOR"
    HEAT_OUTDOOR = "HEAT_OUTDOOR"
    UV_HIGH_OUTDOOR = "UV_HIGH_OUTDOOR"
    APPROXIMATE_TRAVEL = "APPROXIMATE_TRAVEL"
    WEATHER_UNAVAILABLE = "WEATHER_UNAVAILABLE"
    CLOSES_SOON_AFTER_VISIT = "CLOSES_SOON_AFTER_VISIT"
    CHILD_UNSUITABLE = "CHILD_UNSUITABLE"
    LONG_WALK_WITH_CHILD = "LONG_WALK_WITH_CHILD"


class Violation(DomainModel):
    code: ViolationCode
    message: str = Field(min_length=1)
    severity: ViolationSeverity = ViolationSeverity.ERROR
    poi_id: str | None = None
    activity_position: int | None = Field(default=None, ge=1)


class ValidationResult(DomainModel):
    is_valid: bool
    violations: list[Violation] = Field(default_factory=list)
    checked_at: AwareDatetime
    validator_version: str = "v1"

    @model_validator(mode="after")
    def validity_matches_errors(self) -> Self:
        has_errors = any(item.severity == ViolationSeverity.ERROR for item in self.violations)
        if self.is_valid == has_errors:
            raise ValueError("is_valid must be false exactly when error violations exist")
        return self

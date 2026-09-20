from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from math import ceil

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.domain.catalog import ChildFriendly, Poi, SafetyTier
from app.domain.models import TripState
from app.domain.ports import OpeningHoursRequest, PlanningContext
from app.planning.rules import weather_during


class CandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DroppedReason(StrEnum):
    CLOSED = "CLOSED"
    HOURS_UNKNOWN = "HOURS_UNKNOWN"
    EXCLUDED = "EXCLUDED"
    WEATHER = "WEATHER"
    TOO_FAR = "TOO_FAR"
    NOT_ENOUGH_TIME = "NOT_ENOUGH_TIME"
    LOW_INTEREST = "LOW_INTEREST"
    LOWER_SCORE = "LOWER_SCORE"
    CHILD_UNSUITABLE = "CHILD_UNSUITABLE"


class DroppedCandidate(CandidateModel):
    poi_id: str
    reason: DroppedReason
    detail: str


class PlannerConfig(CandidateModel):
    top_k: int = Field(default=10, ge=1, le=25)
    beam_width: int = Field(default=30, ge=1, le=100)
    max_opening_wait_minutes: int = Field(default=20, ge=0, le=120)
    transition_buffer_minutes: int = Field(default=5, ge=0)
    meal_minutes: int = Field(default=45, gt=0)
    meal_window_minimum_minutes: int = Field(default=240, gt=0)
    child_break_minutes: int = Field(default=15, gt=0)
    child_break_interval_minutes: int = Field(default=90, gt=0)
    interest_weight: float = Field(default=4.0, ge=0)
    must_see_weight: float = Field(default=2.0, ge=0)
    weather_weight: float = Field(default=4.0, ge=0)
    child_weight: float = Field(default=4.0, ge=0)
    diversity_weight: float = Field(default=1.0, ge=0)
    travel_weight: float = Field(default=0.02, ge=0)
    perturbation_weight: float = Field(default=3.0, ge=0)
    utilization_weight: float = Field(default=12.0, ge=0)
    child_walk_soft_minutes: int = Field(default=15, ge=0)
    child_walk_hard_minutes: int = Field(default=25, ge=1)
    child_walk_penalty: float = Field(default=0.4, ge=0)
    child_hilly_penalty: float = Field(default=3.0, ge=0)
    minimum_break_after_meal_minutes: int = Field(default=45, ge=0)

    @classmethod
    def from_settings(cls, settings: Settings) -> PlannerConfig:
        return cls(
            top_k=settings.planner_top_k,
            beam_width=settings.planner_beam_width,
            interest_weight=settings.planner_interest_weight,
            must_see_weight=settings.planner_must_see_weight,
            weather_weight=settings.planner_weather_weight,
            child_weight=settings.planner_child_weight,
            diversity_weight=settings.planner_diversity_weight,
            travel_weight=settings.planner_travel_weight,
            perturbation_weight=settings.planner_perturbation_weight,
            utilization_weight=settings.planner_utilization_weight,
            child_walk_soft_minutes=settings.planner_child_walk_soft_minutes,
            child_walk_hard_minutes=settings.planner_child_walk_hard_minutes,
            child_walk_penalty=settings.planner_child_walk_penalty,
            child_hilly_penalty=settings.planner_child_hilly_penalty,
        )


class StaticScore(CandidateModel):
    interest_match: float = 0.0
    must_see: float = 0.0
    child_suitability: float = 0.0

    @property
    def total(self) -> float:
        return self.interest_match + self.must_see + self.child_suitability


class ScoredCandidate(CandidateModel):
    poi_id: str
    category: str
    score: StaticScore


class CandidateGeneration(CandidateModel):
    candidates: list[ScoredCandidate]
    dropped: list[DroppedCandidate]


def generate_candidates(
    state: TripState,
    context: PlanningContext,
    config: PlannerConfig,
) -> CandidateGeneration:
    if state.time_window is None:
        raise ValueError("planning requires a user time window")

    candidates: list[ScoredCandidate] = []
    dropped: list[DroppedCandidate] = []
    for poi in sorted(context.catalog.pois, key=lambda item: item.id):
        reason = _filter_reason(poi, state, context)
        if reason is not None:
            dropped.append(reason)
            continue
        candidates.append(
            ScoredCandidate(
                poi_id=poi.id,
                category=poi.category,
                score=_static_score(poi, state, config),
            )
        )

    ranked = sorted(candidates, key=lambda item: (-item.score.total, item.poi_id))
    for candidate in ranked[config.top_k :]:
        dropped.append(
            DroppedCandidate(
                poi_id=candidate.poi_id,
                reason=DroppedReason.LOWER_SCORE,
                detail="Candidate fell below the deterministic top-K cutoff.",
            )
        )
    return CandidateGeneration(
        candidates=ranked[: config.top_k],
        dropped=sorted(dropped, key=lambda item: item.poi_id),
    )


def _filter_reason(
    poi: Poi,
    state: TripState,
    context: PlanningContext,
) -> DroppedCandidate | None:
    assert state.time_window is not None
    if (
        poi.id in state.exclude_poi_ids
        or poi.category in state.exclude_categories
        or poi.id in state.visited
    ):
        return DroppedCandidate(
            poi_id=poi.id,
            reason=DroppedReason.EXCLUDED,
            detail="POI is excluded by category, id, or visited state.",
        )
    if state.party.children_ages and poi.child_friendly == ChildFriendly.LOW:
        return DroppedCandidate(
            poi_id=poi.id,
            reason=DroppedReason.CHILD_UNSUITABLE,
            detail="POI has low child suitability for this party.",
        )
    day = state.time_window.start.date()
    if any(closure.start <= day <= closure.end for closure in poi.temporary_closures):
        return DroppedCandidate(
            poi_id=poi.id,
            reason=DroppedReason.CLOSED,
            detail="POI has a temporary closure on the requested date.",
        )
    flags = weather_during(context, state.time_window.start, state.time_window.end)
    if poi.safety_tier == SafetyTier.CRITICAL and (
        context.weather_unavailable_reason or any(flag.storm or flag.after_dark for flag in flags)
    ):
        return DroppedCandidate(
            poi_id=poi.id,
            reason=DroppedReason.WEATHER,
            detail="Critical-safety POI is excluded without a safe weather window.",
        )

    probe_end = min(state.time_window.start + timedelta(minutes=1), state.time_window.end)
    probe = context.opening_hours.check(
        OpeningHoursRequest(
            poi_id=poi.id,
            visit_start=state.time_window.start,
            visit_end=probe_end,
        )
    )
    if probe.source_rule is None and probe.needs_verification:
        return DroppedCandidate(
            poi_id=poi.id,
            reason=DroppedReason.HOURS_UNKNOWN,
            detail="Opening hours are unknown for the requested date.",
        )
    if not _has_full_visit_interval(poi, state, context):
        return DroppedCandidate(
            poi_id=poi.id,
            reason=DroppedReason.CLOSED,
            detail="No complete visit fits an open interval inside the requested window.",
        )
    return None


def _has_full_visit_interval(
    poi: Poi,
    state: TripState,
    context: PlanningContext,
) -> bool:
    assert state.time_window is not None
    visit_minutes = ceil(poi.visit_minutes.typical * context.pace_factors.visit_time_multiplier)
    cursor = state.time_window.start
    for _attempt in range(4):
        interval = context.opening_hours.next_open_interval(
            poi.id,
            cursor,
            search_days=0,
        )
        if (
            interval is None
            or interval.opens_at.date() != state.time_window.start.date()
            or interval.opens_at >= state.time_window.end
        ):
            return False
        visit_start = max(cursor, interval.opens_at)
        visit_end = visit_start + timedelta(minutes=visit_minutes)
        if visit_end <= state.time_window.end:
            result = context.opening_hours.check(
                OpeningHoursRequest(
                    poi_id=poi.id,
                    visit_start=visit_start,
                    visit_end=visit_end,
                )
            )
            if result.can_visit:
                return True
        cursor = interval.closes_at + timedelta(minutes=1)
        if cursor >= state.time_window.end:
            return False
    return False


def _static_score(poi: Poi, state: TripState, config: PlannerConfig) -> StaticScore:
    interests = {value.lower() for value in state.interests}
    searchable = {poi.category.lower(), *(tag.lower() for tag in poi.tags)}
    matches = len(interests & searchable)
    interest_raw = float(matches)
    must_see_raw = 1.0 if {"landmark", "unesco"} & set(poi.tags) else 0.0
    child_score = 0.0
    if state.party.children_ages:
        child_score = config.child_weight * (
            1.0 if poi.child_friendly == ChildFriendly.HIGH else 0.5
        )
        if poi.hilly:
            child_score -= config.child_hilly_penalty
    return StaticScore(
        interest_match=interest_raw * config.interest_weight,
        must_see=must_see_raw * config.must_see_weight,
        child_suitability=child_score,
    )

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from pydantic import BaseModel, ConfigDict, Field

from app.domain.catalog import HeatExposure, Poi, SafetyTier
from app.domain.models import (
    Activity,
    ActivityKind,
    Exposure,
    Itinerary,
    LocationRef,
    TripState,
    ValidationResult,
    ViolationSeverity,
)
from app.domain.ports import OpeningHoursRequest, PlanningContext
from app.planning.candidates import (
    CandidateGeneration,
    DroppedCandidate,
    DroppedReason,
    PlannerConfig,
    ScoredCandidate,
    generate_candidates,
)
from app.planning.rules import adjusted_minutes, travel_leg, weather_during
from app.planning.validator import DeterministicItineraryValidator

DEFAULT_START_POI_ID = "aristotelous_square"
MEAL_AREA_IDS = ("kapani_market", "modiano_market", "ladadika")
ANO_POLI_IDS = {
    "ano_poli_walls",
    "heptapyrgio",
    "tsinari_ano_poli",
    "vlatadon_monastery",
}


@dataclass(frozen=True)
class MealPeriod:
    key: str
    opens_at: datetime
    closes_at: datetime
    target: datetime


class PlannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivityScore(PlannerModel):
    activity_position: int = Field(ge=1)
    poi_id: str | None
    kind: ActivityKind
    interest_match: float = 0.0
    must_see: float = 0.0
    weather_penalty: float = 0.0
    child_suitability: float = 0.0
    diversity_penalty: float = 0.0
    travel_cost: float = 0.0
    perturbation_penalty: float = 0.0
    total: float = 0.0


class PlanResult(PlannerModel):
    itinerary: Itinerary
    score_breakdown: list[ActivityScore]
    dropped_candidates: list[DroppedCandidate]
    assumptions: list[str]
    validation: ValidationResult
    total_score: float
    window_utilization: float = Field(ge=0, le=1)
    utilization_score: float = Field(ge=0)


@dataclass(frozen=True)
class BeamState:
    sequence: tuple[str, ...]
    activities: tuple[Activity, ...]
    scores: tuple[ActivityScore, ...]
    current_time: datetime
    current_location: str
    used_pois: frozenset[str]
    accumulated_score: float
    total_travel_minutes: int
    approximate_travel: bool
    meals_added: frozenset[str]
    last_rest_at: datetime
    last_visit_category: str | None


class BeamSearchPlanner:
    def __init__(
        self,
        config: PlannerConfig | None = None,
        validator: DeterministicItineraryValidator | None = None,
    ) -> None:
        self.config = config or PlannerConfig()
        self.validator = validator or DeterministicItineraryValidator()

    def plan(
        self,
        state: TripState,
        context: PlanningContext,
        *,
        preferred_order: list[str] | None = None,
        required_poi_ids: set[str] | None = None,
    ) -> PlanResult:
        effective_state, assumptions = self._effective_state(state)
        generation = generate_candidates(effective_state, context, self.config)
        all_generation = generate_candidates(
            effective_state,
            context,
            self.config.model_copy(update={"top_k": min(25, len(context.catalog.pois))}),
        )
        required = required_poi_ids or set()
        candidates_by_id = {candidate.poi_id: candidate for candidate in generation.candidates}
        for candidate in all_generation.candidates:
            if candidate.poi_id in required:
                candidates_by_id.setdefault(candidate.poi_id, candidate)
        search_candidates = sorted(
            candidates_by_id.values(),
            key=lambda candidate: (-candidate.score.total, candidate.poi_id),
        )
        poi_by_id = {poi.id: poi for poi in context.catalog.pois}
        initial = self._initial_state(effective_state)
        beam = [initial]
        explored = [initial]
        failure_reasons: dict[str, DroppedReason] = {}

        for _depth in range(len(search_candidates)):
            expansions: list[BeamState] = []
            for partial in beam:
                for candidate in search_candidates:
                    if candidate.poi_id in partial.used_pois:
                        continue
                    expanded, reason = self._expand(
                        partial,
                        candidate,
                        poi_by_id,
                        effective_state,
                        context,
                    )
                    if expanded is None:
                        failure_reasons.setdefault(candidate.poi_id, reason)
                        continue
                    expansions.append(expanded)
            if not expansions:
                break
            beam = sorted(
                expansions,
                key=lambda item: self._beam_key(item, effective_state, preferred_order),
            )[: self.config.beam_width]
            explored.extend(beam)

        valid_results = [
            result
            for partial in explored
            if partial.sequence
            and required <= set(partial.sequence)
            and (
                result := self._finalize(
                    partial,
                    effective_state,
                    context,
                    generation,
                    failure_reasons,
                    assumptions,
                    preferred_order,
                )
            )
            is not None
        ]
        if valid_results:
            best = min(
                valid_results,
                key=lambda result: (
                    -result.total_score,
                    tuple(
                        activity.poi_id or activity.area_label or ""
                        for activity in result.itinerary.activities
                    ),
                ),
            )
            return self._with_honest_drops(best, effective_state, context)
        return self._empty_result(
            effective_state,
            context,
            generation,
            assumptions,
        )

    def create_or_repair(
        self,
        state: TripState,
        context: PlanningContext,
    ) -> Itinerary:
        """Satisfy the domain planner port while keeping rich results available via plan()."""
        return self.plan(state, context).itinerary

    def plan_for_order(
        self,
        state: TripState,
        context: PlanningContext,
        poi_ids: list[str],
        *,
        assumptions: list[str] | None = None,
    ) -> PlanResult | None:
        effective_state, default_assumptions = self._effective_state(state)
        generation = generate_candidates(
            effective_state,
            context,
            self.config.model_copy(update={"top_k": min(25, len(context.catalog.pois))}),
        )
        candidate_by_id = {candidate.poi_id: candidate for candidate in generation.candidates}
        poi_by_id = {poi.id: poi for poi in context.catalog.pois}
        partial = self._initial_state(effective_state)
        for poi_id in poi_ids:
            candidate = candidate_by_id.get(poi_id)
            if candidate is None:
                return None
            partial, _reason = self._expand(
                partial,
                candidate,
                poi_by_id,
                effective_state,
                context,
            )
            if partial is None:
                return None
        result = self._finalize(
            partial,
            effective_state,
            context,
            generation,
            {},
            assumptions or default_assumptions,
            poi_ids,
        )
        return (
            self._with_honest_drops(result, effective_state, context)
            if result is not None
            else None
        )

    def _expand(
        self,
        partial: BeamState,
        candidate: ScoredCandidate,
        pois: dict[str, Poi],
        state: TripState,
        context: PlanningContext,
    ) -> tuple[BeamState | None, DroppedReason]:
        poi = pois[candidate.poi_id]
        if (
            state.start_location is not None
            and candidate.poi_id == state.start_location.poi_id
            and candidate.score.total <= 0
        ):
            return None, DroppedReason.LOWER_SCORE
        prepared = self._insert_due_break(partial, state)
        if prepared is None:
            return None, DroppedReason.NOT_ENOUGH_TIME

        projected_end = self._projected_visit_end(prepared, poi, context)
        meal_period = self._meal_due(prepared, state, projected_end)
        if meal_period is not None:
            prepared = self._insert_meal(
                prepared,
                state,
                context,
                pois,
                meal_period,
                avoid_poi_id=poi.id,
            )
            if prepared is None:
                return None, DroppedReason.NOT_ENOUGH_TIME

        leg = travel_leg(context, prepared.current_location, poi.id)
        walking = adjusted_minutes(
            leg.minutes,
            context.pace_factors.travel_time_multiplier,
        )
        if state.party.children_ages and walking > self.config.child_walk_hard_minutes:
            return None, DroppedReason.LOWER_SCORE
        buffer_minutes = context.transition_buffer_minutes if walking else 0
        arrival = prepared.current_time + timedelta(minutes=walking + buffer_minutes)
        assert state.time_window is not None
        if arrival >= state.time_window.end:
            return None, DroppedReason.NOT_ENOUGH_TIME

        interval = context.opening_hours.next_open_interval(poi.id, arrival, search_days=0)
        if interval is None or interval.opens_at.date() != arrival.date():
            return None, DroppedReason.CLOSED
        visit_start = max(arrival, interval.opens_at)
        wait_minutes = int((visit_start - arrival).total_seconds() // 60)
        if wait_minutes > self.config.max_opening_wait_minutes:
            return None, DroppedReason.CLOSED
        visit_minutes = adjusted_minutes(
            poi.visit_minutes.typical,
            context.pace_factors.visit_time_multiplier,
        )
        visit_end = visit_start + timedelta(minutes=visit_minutes)
        if visit_end > state.time_window.end:
            return None, DroppedReason.NOT_ENOUGH_TIME
        hours = context.opening_hours.check(
            OpeningHoursRequest(
                poi_id=poi.id,
                visit_start=visit_start,
                visit_end=visit_end,
            )
        )
        if not hours.can_visit:
            return None, DroppedReason.CLOSED

        weather_penalty, weather_blocked = self._weather_score(
            poi,
            visit_start,
            visit_end,
            context,
        )
        if weather_blocked:
            return None, DroppedReason.WEATHER
        diversity_penalty = (
            -self.config.diversity_weight if prepared.last_visit_category == poi.category else 0.0
        )
        travel_cost = self._travel_cost(walking, state)
        score = ActivityScore(
            activity_position=len(prepared.activities) + 1,
            poi_id=poi.id,
            kind=ActivityKind.VISIT,
            interest_match=candidate.score.interest_match,
            must_see=candidate.score.must_see,
            weather_penalty=weather_penalty,
            child_suitability=candidate.score.child_suitability,
            diversity_penalty=diversity_penalty,
            travel_cost=travel_cost,
            total=(candidate.score.total + weather_penalty + diversity_penalty + travel_cost),
        )
        activity = Activity(
            kind=ActivityKind.VISIT,
            poi_id=poi.id,
            start=visit_start,
            end=visit_end,
            visit_minutes=visit_minutes,
            travel_from_previous_minutes=walking,
            buffer_before_minutes=buffer_minutes,
            exposure=poi.exposure,
        )
        return (
            replace(
                prepared,
                sequence=(*prepared.sequence, poi.id),
                activities=(*prepared.activities, activity),
                scores=(*prepared.scores, score),
                current_time=visit_end,
                current_location=poi.id,
                used_pois=prepared.used_pois | {poi.id},
                accumulated_score=prepared.accumulated_score + score.total,
                total_travel_minutes=prepared.total_travel_minutes + walking,
                approximate_travel=prepared.approximate_travel or leg.approximate,
                last_visit_category=poi.category,
            ),
            DroppedReason.LOWER_SCORE,
        )

    def _insert_due_break(self, partial: BeamState, state: TripState) -> BeamState | None:
        if not state.party.children_ages:
            return partial
        elapsed = int((partial.current_time - partial.last_rest_at).total_seconds() // 60)
        break_after = max(
            self.config.child_break_interval_minutes,
            self.config.minimum_break_after_meal_minutes,
        )
        if elapsed < break_after:
            return partial
        assert state.time_window is not None
        end = partial.current_time + timedelta(minutes=self.config.child_break_minutes)
        if end > state.time_window.end:
            return None
        activity = Activity(
            kind=ActivityKind.BREAK,
            area_label=f"Indoor rest near {partial.current_location}",
            start=partial.current_time,
            end=end,
            visit_minutes=self.config.child_break_minutes,
            exposure=Exposure.INDOOR,
        )
        score = ActivityScore(
            activity_position=len(partial.activities) + 1,
            poi_id=None,
            kind=ActivityKind.BREAK,
        )
        return replace(
            partial,
            activities=(*partial.activities, activity),
            scores=(*partial.scores, score),
            current_time=end,
            last_rest_at=end,
        )

    def _insert_meal(
        self,
        partial: BeamState,
        state: TripState,
        context: PlanningContext,
        pois: dict[str, Poi],
        period: MealPeriod,
        *,
        avoid_poi_id: str | None = None,
    ) -> BeamState | None:
        assert state.time_window is not None
        allowed_ids = list(MEAL_AREA_IDS)
        if partial.current_location in ANO_POLI_IDS:
            allowed_ids.append("tsinari_ano_poli")
        meal_areas = [
            pois[poi_id]
            for poi_id in allowed_ids
            if poi_id in pois
            and poi_id not in partial.used_pois
            and poi_id != avoid_poi_id
            and poi_id not in state.exclude_poi_ids
            and pois[poi_id].category not in state.exclude_categories
        ]
        ranked = sorted(
            meal_areas,
            key=lambda poi: (
                poi.hilly is True,
                travel_leg(context, partial.current_location, poi.id).minutes,
                MEAL_AREA_IDS.index(poi.id) if poi.id in MEAL_AREA_IDS else 99,
                poi.id,
            ),
        )
        for area in ranked:
            leg = travel_leg(context, partial.current_location, area.id)
            walking = adjusted_minutes(
                leg.minutes,
                context.pace_factors.travel_time_multiplier,
            )
            if state.party.children_ages and walking > self.config.child_walk_hard_minutes:
                continue
            buffer_minutes = context.transition_buffer_minutes if walking else 0
            arrival = partial.current_time + timedelta(minutes=walking + buffer_minutes)
            meal_start = max(arrival, period.opens_at, period.target)
            meal_end = meal_start + timedelta(minutes=self.config.meal_minutes)
            if meal_end > min(state.time_window.end, period.closes_at):
                continue
            if area.exposure != Exposure.INDOOR and any(
                flag.rain_risk for flag in weather_during(context, meal_start, meal_end)
            ):
                continue
            hours = context.opening_hours.check(
                OpeningHoursRequest(
                    poi_id=area.id,
                    visit_start=meal_start,
                    visit_end=meal_end,
                )
            )
            if not hours.can_visit:
                continue
            travel_cost = self._travel_cost(walking, state)
            activity = Activity(
                kind=ActivityKind.MEAL,
                poi_id=area.id,
                area_label=area.names.en,
                start=meal_start,
                end=meal_end,
                visit_minutes=self.config.meal_minutes,
                travel_from_previous_minutes=walking,
                buffer_before_minutes=buffer_minutes,
                exposure=area.exposure,
            )
            score = ActivityScore(
                activity_position=len(partial.activities) + 1,
                poi_id=area.id,
                kind=ActivityKind.MEAL,
                travel_cost=travel_cost,
                total=travel_cost,
            )
            return replace(
                partial,
                activities=(*partial.activities, activity),
                scores=(*partial.scores, score),
                current_time=meal_end,
                current_location=area.id,
                used_pois=partial.used_pois | {area.id},
                accumulated_score=partial.accumulated_score + travel_cost,
                total_travel_minutes=partial.total_travel_minutes + walking,
                approximate_travel=partial.approximate_travel or leg.approximate,
                meals_added=partial.meals_added | {period.key},
                last_rest_at=meal_end,
            )
        return None

    def _finalize(
        self,
        partial: BeamState,
        state: TripState,
        context: PlanningContext,
        generation: CandidateGeneration,
        failure_reasons: dict[str, DroppedReason],
        assumptions: list[str],
        preferred_order: list[str] | None,
    ) -> PlanResult | None:
        assert state.time_window is not None
        pois = {poi.id: poi for poi in context.catalog.pois}
        finalized = partial
        for period in self._meal_periods(state):
            if period.key not in finalized.meals_added:
                finalized = self._insert_meal(finalized, state, context, pois, period)
                if finalized is None:
                    return None
        if state.party.children_ages:
            elapsed = int((finalized.current_time - finalized.last_rest_at).total_seconds() // 60)
            break_after = max(
                self.config.child_break_interval_minutes,
                self.config.minimum_break_after_meal_minutes,
            )
            if elapsed >= break_after:
                finalized = self._append_break(finalized, state)
                if finalized is None:
                    return None
        itinerary = Itinerary(
            window=state.time_window,
            activities=list(finalized.activities),
            total_travel_minutes=finalized.total_travel_minutes,
            approximate_travel_times=finalized.approximate_travel,
        )
        validation = self.validator.validate(itinerary, state, context)
        if any(item.severity == ViolationSeverity.ERROR for item in validation.violations):
            return None

        perturbation = self._perturbation_penalty(finalized.sequence, preferred_order)
        scores = self._with_positions_and_perturbation(
            finalized.scores,
            preferred_order,
        )
        selected = set(finalized.sequence)
        dropped = list(generation.dropped)
        for candidate in generation.candidates:
            if candidate.poi_id not in selected:
                reason = failure_reasons.get(candidate.poi_id, DroppedReason.LOWER_SCORE)
                dropped.append(
                    DroppedCandidate(
                        poi_id=candidate.poi_id,
                        reason=reason,
                        detail="Candidate was not selected by the bounded search.",
                    )
                )
        utilization = self._window_utilization(itinerary)
        utilization_score = utilization * self.config.utilization_weight
        return PlanResult(
            itinerary=itinerary,
            score_breakdown=scores,
            dropped_candidates=self._deduplicate_dropped(dropped),
            assumptions=assumptions,
            validation=validation,
            total_score=finalized.accumulated_score - perturbation + utilization_score,
            window_utilization=utilization,
            utilization_score=utilization_score,
        )

    def _append_break(self, partial: BeamState, state: TripState) -> BeamState | None:
        assert state.time_window is not None
        end = partial.current_time + timedelta(minutes=self.config.child_break_minutes)
        if end > state.time_window.end:
            return None
        activity = Activity(
            kind=ActivityKind.BREAK,
            area_label=f"Indoor rest near {partial.current_location}",
            start=partial.current_time,
            end=end,
            visit_minutes=self.config.child_break_minutes,
            exposure=Exposure.INDOOR,
        )
        score = ActivityScore(
            activity_position=len(partial.activities) + 1,
            poi_id=None,
            kind=ActivityKind.BREAK,
        )
        return replace(
            partial,
            activities=(*partial.activities, activity),
            scores=(*partial.scores, score),
            current_time=end,
            last_rest_at=end,
        )

    def _empty_result(
        self,
        state: TripState,
        context: PlanningContext,
        generation: CandidateGeneration,
        assumptions: list[str],
    ) -> PlanResult:
        assert state.time_window is not None
        itinerary = Itinerary(window=state.time_window)
        validation = self.validator.validate(itinerary, state, context)
        return PlanResult(
            itinerary=itinerary,
            score_breakdown=[],
            dropped_candidates=generation.dropped,
            assumptions=assumptions,
            validation=validation,
            total_score=0.0,
            window_utilization=0.0,
            utilization_score=0.0,
        )

    def _effective_state(self, state: TripState) -> tuple[TripState, list[str]]:
        if state.time_window is None:
            raise ValueError("planning requires a user time window")
        assumptions = list(state.assumptions)
        if state.start_location and state.start_location.poi_id:
            return state, assumptions
        assumption = "Start location assumed to be Aristotelous Square."
        if assumption not in assumptions:
            assumptions.append(assumption)
        return (
            state.model_copy(
                update={
                    "start_location": LocationRef(
                        label="Aristotelous Square",
                        poi_id=DEFAULT_START_POI_ID,
                    )
                }
            ),
            assumptions,
        )

    @staticmethod
    def _initial_state(state: TripState) -> BeamState:
        assert state.time_window is not None
        assert state.start_location is not None and state.start_location.poi_id is not None
        return BeamState(
            sequence=(),
            activities=(),
            scores=(),
            current_time=state.time_window.start,
            current_location=state.start_location.poi_id,
            used_pois=frozenset(),
            accumulated_score=0.0,
            total_travel_minutes=0,
            approximate_travel=False,
            meals_added=frozenset(),
            last_rest_at=state.time_window.start,
            last_visit_category=None,
        )

    def _projected_visit_end(
        self,
        partial: BeamState,
        poi: Poi,
        context: PlanningContext,
    ) -> datetime:
        leg = travel_leg(context, partial.current_location, poi.id)
        walking = adjusted_minutes(
            leg.minutes,
            context.pace_factors.travel_time_multiplier,
        )
        buffer_minutes = context.transition_buffer_minutes if walking else 0
        visit_minutes = adjusted_minutes(
            poi.visit_minutes.typical,
            context.pace_factors.visit_time_multiplier,
        )
        return partial.current_time + timedelta(minutes=walking + buffer_minutes + visit_minutes)

    def _meal_due(
        self,
        partial: BeamState,
        state: TripState,
        projected_end: datetime,
    ) -> MealPeriod | None:
        for period in self._meal_periods(state):
            if period.key in partial.meals_added:
                continue
            latest_start = period.closes_at - timedelta(minutes=self.config.meal_minutes)
            if partial.current_time >= period.target or projected_end > latest_start:
                return period
        return None

    def _meal_periods(self, state: TripState) -> list[MealPeriod]:
        assert state.time_window is not None
        duration = int((state.time_window.end - state.time_window.start).total_seconds() // 60)
        if duration < self.config.meal_window_minimum_minutes:
            return []
        midpoint = state.time_window.start + (state.time_window.end - state.time_window.start) / 2
        periods: list[MealPeriod] = []
        for key, starts, ends in (
            ("lunch", time(13), time(15, 30)),
            ("dinner", time(19, 30), time(21, 30)),
        ):
            opens_at = max(
                state.time_window.start,
                datetime.combine(midpoint.date(), starts, tzinfo=midpoint.tzinfo),
            )
            closes_at = min(
                state.time_window.end,
                datetime.combine(midpoint.date(), ends, tzinfo=midpoint.tzinfo),
            )
            latest_start = closes_at - timedelta(minutes=self.config.meal_minutes)
            if opens_at > latest_start:
                continue
            periods.append(
                MealPeriod(
                    key=key,
                    opens_at=opens_at,
                    closes_at=closes_at,
                    target=min(max(midpoint, opens_at), latest_start),
                )
            )
        return periods

    def _weather_score(
        self,
        poi: Poi,
        start: datetime,
        end: datetime,
        context: PlanningContext,
    ) -> tuple[float, bool]:
        flags = weather_during(context, start, end)
        exposed = poi.exposure != Exposure.INDOOR
        if exposed and any(flag.rain_risk or flag.storm for flag in flags):
            return 0.0, True
        if poi.safety_tier == SafetyTier.CRITICAL and any(flag.after_dark for flag in flags):
            return 0.0, True
        if (
            exposed
            and poi.heat_exposure == HeatExposure.HIGH
            and any(flag.heat_risk for flag in flags)
        ):
            return 0.0, True
        penalty = 0.0
        if exposed and any(flag.heat_risk for flag in flags):
            penalty -= self.config.weather_weight * 0.75
        if exposed and any(flag.uv_high for flag in flags):
            penalty -= self.config.weather_weight * 0.5
        return penalty, False

    def _beam_key(
        self,
        beam_state: BeamState,
        trip_state: TripState,
        preferred_order: list[str] | None,
    ) -> tuple[float, tuple[str, ...]]:
        penalty = self._perturbation_penalty(beam_state.sequence, preferred_order)
        utilization = self._elapsed_utilization(beam_state.current_time, trip_state)
        objective = (
            beam_state.accumulated_score - penalty + utilization * self.config.utilization_weight
        )
        return (-objective, beam_state.sequence)

    def _travel_cost(self, walking: int, state: TripState) -> float:
        cost = -walking * self.config.travel_weight
        if state.party.children_ages and walking > self.config.child_walk_soft_minutes:
            cost -= (walking - self.config.child_walk_soft_minutes) * self.config.child_walk_penalty
        return cost

    @staticmethod
    def _elapsed_utilization(current_time: datetime, state: TripState) -> float:
        assert state.time_window is not None
        total = (state.time_window.end - state.time_window.start).total_seconds()
        elapsed = (current_time - state.time_window.start).total_seconds()
        return max(0.0, min(1.0, elapsed / total))

    @staticmethod
    def _window_utilization(itinerary: Itinerary) -> float:
        if not itinerary.activities:
            return 0.0
        total = (itinerary.window.end - itinerary.window.start).total_seconds()
        final_end = max(activity.end for activity in itinerary.activities)
        elapsed = (final_end - itinerary.window.start).total_seconds()
        return max(0.0, min(1.0, elapsed / total))

    def _with_honest_drops(
        self,
        plan: PlanResult,
        state: TripState,
        context: PlanningContext,
    ) -> PlanResult:
        expanded_config = self.config.model_copy(
            update={"top_k": min(25, len(context.catalog.pois))}
        )
        generation = generate_candidates(state, context, expanded_config)
        operational = {
            item.poi_id: item
            for item in generation.dropped
            if item.reason
            not in {
                DroppedReason.LOWER_SCORE,
                DroppedReason.LOW_INTEREST,
                DroppedReason.NOT_ENOUGH_TIME,
                DroppedReason.TOO_FAR,
            }
        }
        selected = {
            activity.poi_id
            for activity in plan.itinerary.activities
            if activity.kind == ActivityKind.VISIT and activity.poi_id is not None
        }
        candidate_by_id = {candidate.poi_id: candidate for candidate in generation.candidates}
        selected_scores = [
            candidate_by_id[poi_id]
            for poi_id in selected
            if poi_id in candidate_by_id
        ]
        weakest = min(
            selected_scores,
            key=lambda candidate: (candidate.score.total, candidate.poi_id),
            default=None,
        )
        dropped = list(operational.values())
        for candidate in generation.candidates:
            if candidate.poi_id in selected:
                continue
            if self._capacity_proves_no_insertion(plan.itinerary, candidate, state, context):
                window_minutes = int(
                    (plan.itinerary.window.end - plan.itinerary.window.start).total_seconds() // 60
                )
                required_minutes = self._insertion_lower_bound_minutes(
                    plan.itinerary,
                    candidate,
                    state,
                    context,
                )
                dropped.append(
                    DroppedCandidate(
                        poi_id=candidate.poi_id,
                        reason=DroppedReason.NOT_ENOUGH_TIME,
                        detail=(
                            f"Insertion needs at least {required_minutes} occupied minutes "
                            f"inside the {window_minutes}-minute window."
                        ),
                    )
                )
                continue
            weakest_id = weakest.poi_id if weakest else "none"
            gap = max(0.0, (weakest.score.total if weakest else 0.0) - candidate.score.total)
            dropped.append(
                DroppedCandidate(
                    poi_id=candidate.poi_id,
                    reason=DroppedReason.LOWER_SCORE,
                    detail=(
                        f"Score gap versus weakest selected activity {weakest_id}: {gap:+.2f} "
                        "before route costs."
                    ),
                )
            )
        return plan.model_copy(update={"dropped_candidates": self._deduplicate_dropped(dropped)})

    def _capacity_proves_no_insertion(
        self,
        itinerary: Itinerary,
        candidate: ScoredCandidate,
        state: TripState,
        context: PlanningContext,
    ) -> bool:
        required = self._insertion_lower_bound_minutes(itinerary, candidate, state, context)
        available = int((itinerary.window.end - itinerary.window.start).total_seconds() // 60)
        return required > available

    def _insertion_lower_bound_minutes(
        self,
        itinerary: Itinerary,
        candidate: ScoredCandidate,
        state: TripState,
        context: PlanningContext,
    ) -> int:
        poi = next(item for item in context.catalog.pois if item.id == candidate.poi_id)
        activity_minutes = sum(activity.visit_minutes for activity in itinerary.activities)
        visit = adjusted_minutes(
            poi.visit_minutes.typical,
            context.pace_factors.visit_time_multiplier,
        )
        locations = [
            activity.poi_id for activity in itinerary.activities if activity.poi_id is not None
        ]
        route_minutes = min(
            self._route_minutes(
                [*locations[:position], poi.id, *locations[position:]],
                state,
                context,
            )
            for position in range(len(locations) + 1)
        )
        return activity_minutes + visit + route_minutes

    @staticmethod
    def _route_minutes(
        locations: list[str],
        state: TripState,
        context: PlanningContext,
    ) -> int:
        if state.start_location is None or state.start_location.poi_id is None:
            return 0
        total = 0
        origin = state.start_location.poi_id
        for destination in locations:
            walking = adjusted_minutes(
                travel_leg(context, origin, destination).minutes,
                context.pace_factors.travel_time_multiplier,
            )
            total += walking
            if walking:
                total += context.transition_buffer_minutes
            origin = destination
        return total

    def _perturbation_penalty(
        self,
        sequence: tuple[str, ...],
        preferred_order: list[str] | None,
    ) -> float:
        if not preferred_order:
            return 0.0
        original = {poi_id: index for index, poi_id in enumerate(preferred_order)}
        removed = len(set(preferred_order) - set(sequence))
        kept = [poi_id for poi_id in sequence if poi_id in original]
        reordered = sum(1 for index, poi_id in enumerate(kept) if original[poi_id] != index)
        return (removed + reordered) * self.config.perturbation_weight

    def _with_positions_and_perturbation(
        self,
        scores: tuple[ActivityScore, ...],
        preferred_order: list[str] | None,
    ) -> list[ActivityScore]:
        original = {poi_id: index for index, poi_id in enumerate(preferred_order or [])}
        visit_index = 0
        result: list[ActivityScore] = []
        for position, score in enumerate(scores, start=1):
            perturbation = 0.0
            if score.kind == ActivityKind.VISIT:
                if score.poi_id in original and original[score.poi_id] != visit_index:
                    perturbation = -self.config.perturbation_weight
                visit_index += 1
            result.append(
                score.model_copy(
                    update={
                        "activity_position": position,
                        "perturbation_penalty": perturbation,
                        "total": score.total + perturbation,
                    }
                )
            )
        return result

    @staticmethod
    def _deduplicate_dropped(items: list[DroppedCandidate]) -> list[DroppedCandidate]:
        by_id: dict[str, DroppedCandidate] = {}
        for item in items:
            by_id.setdefault(item.poi_id, item)
        return [by_id[poi_id] for poi_id in sorted(by_id)]

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from itertools import permutations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.catalog import Poi, SafetyTier
from app.domain.models import (
    Exposure,
    TimeWindow,
    TripState,
    Violation,
    ViolationCode,
    ViolationSeverity,
)
from app.domain.ports import PlanningContext
from app.planning.rules import adjusted_minutes, travel_leg, weather_during


class FeasibilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeasibilityVerdict(StrEnum):
    FEASIBLE = "feasible"
    TIGHT = "tight"
    INFEASIBLE = "infeasible"


class FeasibilityStop(FeasibilityModel):
    position: int = Field(ge=1)
    poi_id: str
    arrival: AwareDatetime
    visit_start: AwareDatetime | None = None
    departure: AwareDatetime | None = None
    walking_from_previous_minutes: int = Field(ge=0)
    waiting_minutes: int = Field(ge=0)
    visit_minutes: int = Field(gt=0)
    issue_codes: list[ViolationCode] = Field(default_factory=list)


class FeasibilityEstimate(FeasibilityModel):
    order: list[str]
    stops: list[FeasibilityStop]
    visit_total_minutes: int = Field(ge=0)
    walking_total_minutes: int = Field(ge=0)
    waiting_total_minutes: int = Field(ge=0)
    buffer_total_minutes: int = Field(ge=0)
    finish_at: AwareDatetime | None
    margin_to_deadline_minutes: int | None
    feasible: bool
    closed_poi_ids: list[str] = Field(default_factory=list)


class FeasibilityFixKind(StrEnum):
    DROP_POI = "drop_poi"
    START_EARLIER = "start_earlier"
    VISIT_ANOTHER_DAY = "visit_another_day"
    TRAVEL_TIMES_UNAVAILABLE = "travel_times_unavailable"


class FeasibilityFix(FeasibilityModel):
    kind: FeasibilityFixKind
    message: str
    poi_id: str | None = None
    minutes: int | None = Field(default=None, gt=0)


class FeasibilityResult(FeasibilityModel):
    verdict: FeasibilityVerdict
    lower_bound: FeasibilityEstimate
    comfortable: FeasibilityEstimate
    notices: list[Violation] = Field(default_factory=list)
    excluded_poi_ids: list[str] = Field(default_factory=list)
    minimal_fixes: list[FeasibilityFix] = Field(default_factory=list)


class FeasibilityChecker:
    def check(
        self,
        poi_ids: list[str],
        state: TripState,
        context: PlanningContext,
    ) -> FeasibilityResult:
        requested = sorted(set(poi_ids))
        if not requested:
            raise ValueError("feasibility check requires at least one POI")
        if len(requested) > 6:
            raise ValueError("feasibility check supports at most 6 POIs")
        if state.time_window is None:
            raise ValueError("feasibility check requires a user time window")

        pois = {poi.id: poi for poi in context.catalog.pois}
        notices: list[Violation] = []
        excluded: list[str] = []
        safe_ids: list[str] = []

        if context.weather_unavailable_reason:
            notices.append(
                Violation(
                    code=ViolationCode.WEATHER_UNAVAILABLE,
                    severity=ViolationSeverity.WARNING,
                    message=(
                        f"Weather is unavailable for {state.time_window.start:%Y-%m-%d %H:%M}–"
                        f"{state.time_window.end:%H:%M}: {context.weather_unavailable_reason}."
                    ),
                    poi_id=requested[0],
                    activity_position=1,
                )
            )

        for position, poi_id in enumerate(requested, start=1):
            poi = pois.get(poi_id)
            if poi is None:
                excluded.append(poi_id)
                notices.append(
                    self._notice(
                        ViolationCode.UNKNOWN_POI,
                        ViolationSeverity.ERROR,
                        f"POI '{poi_id}' is not in the catalog.",
                        poi_id,
                        position,
                    )
                )
                continue
            if poi.id in state.exclude_poi_ids:
                excluded.append(poi.id)
                notices.append(
                    self._notice(
                        ViolationCode.EXCLUDED_POI,
                        ViolationSeverity.ERROR,
                        f"{poi.names.en} is explicitly excluded.",
                        poi.id,
                        position,
                    )
                )
                continue
            if poi.category in state.exclude_categories:
                excluded.append(poi.id)
                notices.append(
                    self._notice(
                        ViolationCode.EXCLUDED_CATEGORY,
                        ViolationSeverity.ERROR,
                        f"{poi.names.en} has excluded category '{poi.category}'.",
                        poi.id,
                        position,
                    )
                )
                continue
            if context.weather_unavailable_reason and poi.safety_tier == SafetyTier.CRITICAL:
                excluded.append(poi.id)
                continue
            safe_ids.append(poi.id)

        lower = self._best_estimate(safe_ids, pois, state, context, comfortable=False)
        comfortable = self._best_estimate(safe_ids, pois, state, context, comfortable=True)

        if excluded:
            verdict = FeasibilityVerdict.INFEASIBLE
        elif comfortable.feasible:
            verdict = FeasibilityVerdict.FEASIBLE
        elif lower.feasible:
            verdict = FeasibilityVerdict.TIGHT
        else:
            verdict = FeasibilityVerdict.INFEASIBLE

        fixes = (
            self._minimal_fixes(
                safe_ids,
                excluded,
                pois,
                state,
                context,
                comfortable,
            )
            if verdict != FeasibilityVerdict.FEASIBLE
            else []
        )
        return FeasibilityResult(
            verdict=verdict,
            lower_bound=lower,
            comfortable=comfortable,
            notices=notices,
            excluded_poi_ids=excluded,
            minimal_fixes=fixes,
        )

    def _best_estimate(
        self,
        poi_ids: list[str],
        pois: dict[str, Poi],
        state: TripState,
        context: PlanningContext,
        *,
        comfortable: bool,
    ) -> FeasibilityEstimate:
        if not poi_ids:
            assert state.time_window is not None
            return FeasibilityEstimate(
                order=[],
                stops=[],
                visit_total_minutes=0,
                walking_total_minutes=0,
                waiting_total_minutes=0,
                buffer_total_minutes=0,
                finish_at=state.time_window.start,
                margin_to_deadline_minutes=int(
                    (state.time_window.end - state.time_window.start).total_seconds() // 60
                ),
                feasible=True,
            )

        estimates = [
            self._estimate_order(order, pois, state, context, comfortable=comfortable)
            for order in permutations(sorted(poi_ids))
        ]
        return min(
            estimates,
            key=lambda item: (
                any(stop.issue_codes for stop in item.stops),
                item.finish_at is None,
                item.finish_at or datetime.max.replace(tzinfo=state.time_window.start.tzinfo),
                tuple(item.order),
            ),
        )

    def _estimate_order(
        self,
        order: tuple[str, ...],
        pois: dict[str, Poi],
        state: TripState,
        context: PlanningContext,
        *,
        comfortable: bool,
    ) -> FeasibilityEstimate:
        assert state.time_window is not None
        current = state.time_window.start
        origin_id = state.start_location.poi_id if state.start_location else None
        stops: list[FeasibilityStop] = []
        walking_total = waiting_total = buffer_total = visit_total = 0
        closed_poi_ids: list[str] = []

        for position, poi_id in enumerate(order, start=1):
            poi = pois[poi_id]
            walking = 0
            if origin_id is not None:
                leg = travel_leg(context, origin_id, poi_id)
                walking = adjusted_minutes(
                    leg.minutes,
                    context.pace_factors.travel_time_multiplier,
                )
                buffer = context.transition_buffer_minutes if origin_id != poi_id else 0
                current += timedelta(minutes=walking + buffer)
                buffer_total += buffer
            arrival = current
            visit_base = poi.visit_minutes.typical if comfortable else poi.visit_minutes.min
            visit_minutes = adjusted_minutes(
                visit_base,
                context.pace_factors.visit_time_multiplier,
            )
            slot = self._next_fitting_slot(poi_id, arrival, visit_minutes, context)
            if slot is None:
                stops.append(
                    FeasibilityStop(
                        position=position,
                        poi_id=poi_id,
                        arrival=arrival,
                        walking_from_previous_minutes=walking,
                        waiting_minutes=0,
                        visit_minutes=visit_minutes,
                        issue_codes=[ViolationCode.HOURS_UNKNOWN_FOR_DATE],
                    )
                )
                return self._unfinished_estimate(
                    order,
                    stops,
                    visit_total,
                    walking_total + walking,
                    waiting_total,
                    buffer_total,
                    closed_poi_ids,
                )

            visit_start, departure = slot
            waiting = int((visit_start - arrival).total_seconds() // 60)
            issue_codes = self._weather_errors(poi, visit_start, departure, context)
            if visit_start.date() > arrival.date():
                closed_poi_ids.append(poi_id)
            stops.append(
                FeasibilityStop(
                    position=position,
                    poi_id=poi_id,
                    arrival=arrival,
                    visit_start=visit_start,
                    departure=departure,
                    walking_from_previous_minutes=walking,
                    waiting_minutes=waiting,
                    visit_minutes=visit_minutes,
                    issue_codes=issue_codes,
                )
            )
            walking_total += walking
            waiting_total += waiting
            visit_total += visit_minutes
            current = departure
            origin_id = poi_id

        margin = int((state.time_window.end - current).total_seconds() // 60)
        has_errors = any(stop.issue_codes for stop in stops)
        return FeasibilityEstimate(
            order=list(order),
            stops=stops,
            visit_total_minutes=visit_total,
            walking_total_minutes=walking_total,
            waiting_total_minutes=waiting_total,
            buffer_total_minutes=buffer_total,
            finish_at=current,
            margin_to_deadline_minutes=margin,
            feasible=margin >= 0 and not has_errors,
            closed_poi_ids=sorted(set(closed_poi_ids)),
        )

    @staticmethod
    def _next_fitting_slot(
        poi_id: str,
        arrival: datetime,
        visit_minutes: int,
        context: PlanningContext,
    ) -> tuple[datetime, datetime] | None:
        search_after = arrival
        for _ in range(370):
            interval = context.opening_hours.next_open_interval(
                poi_id,
                search_after,
                search_days=370,
            )
            if interval is None:
                return None
            visit_start = max(arrival, interval.opens_at)
            departure = visit_start + timedelta(minutes=visit_minutes)
            if visit_start <= interval.last_entry_at and departure <= interval.closes_at:
                return visit_start, departure
            search_after = interval.closes_at + timedelta(microseconds=1)
        return None

    @staticmethod
    def _weather_errors(
        poi: Poi,
        start: datetime,
        end: datetime,
        context: PlanningContext,
    ) -> list[ViolationCode]:
        flags = weather_during(context, start, end)
        errors: list[ViolationCode] = []
        if poi.exposure != Exposure.INDOOR and any(item.storm for item in flags):
            errors.append(ViolationCode.STORM_OUTDOOR)
        if poi.safety_tier == SafetyTier.CRITICAL and any(item.after_dark for item in flags):
            errors.append(ViolationCode.CRITICAL_AFTER_DARK)
        return errors

    @staticmethod
    def _unfinished_estimate(
        order: tuple[str, ...],
        stops: list[FeasibilityStop],
        visit_total: int,
        walking_total: int,
        waiting_total: int,
        buffer_total: int,
        closed_poi_ids: list[str],
    ) -> FeasibilityEstimate:
        return FeasibilityEstimate(
            order=list(order),
            stops=stops,
            visit_total_minutes=visit_total,
            walking_total_minutes=walking_total,
            waiting_total_minutes=waiting_total,
            buffer_total_minutes=buffer_total,
            finish_at=None,
            margin_to_deadline_minutes=None,
            feasible=False,
            closed_poi_ids=closed_poi_ids,
        )

    def _minimal_fixes(
        self,
        safe_ids: list[str],
        excluded: list[str],
        pois: dict[str, Poi],
        state: TripState,
        context: PlanningContext,
        comfortable: FeasibilityEstimate,
    ) -> list[FeasibilityFix]:
        fixes: list[FeasibilityFix] = []
        for poi_id in excluded:
            poi = pois.get(poi_id)
            if (
                poi is not None
                and context.weather_unavailable_reason
                and poi.safety_tier == SafetyTier.CRITICAL
            ):
                fixes.append(
                    FeasibilityFix(
                        kind=FeasibilityFixKind.VISIT_ANOTHER_DAY,
                        poi_id=poi_id,
                        message=(
                            f"Move {poi.names.en} to a day with an available safe forecast; "
                            "critical-safety POIs are excluded when weather is unavailable."
                        ),
                    )
                )

        for poi_id in comfortable.closed_poi_ids:
            fixes.append(
                FeasibilityFix(
                    kind=FeasibilityFixKind.VISIT_ANOTHER_DAY,
                    poi_id=poi_id,
                    message=(
                        f"Visit {pois[poi_id].names.en} on another day; the first fitting open "
                        "slot falls after the requested date."
                    ),
                )
            )

        if not excluded and len(safe_ids) > 1:
            drop_options: list[tuple[int, str]] = []
            for dropped in sorted(safe_ids):
                subset = [poi_id for poi_id in safe_ids if poi_id != dropped]
                estimate = self._best_estimate(
                    subset,
                    pois,
                    state,
                    context,
                    comfortable=True,
                )
                if estimate.feasible:
                    drop_options.append((estimate.margin_to_deadline_minutes or 0, dropped))
            if drop_options:
                _margin, dropped = min(drop_options, key=lambda item: (-item[0], item[1]))
                fixes.append(
                    FeasibilityFix(
                        kind=FeasibilityFixKind.DROP_POI,
                        poi_id=dropped,
                        message=(
                            f"Drop {pois[dropped].names.en} to make the comfortable estimate fit."
                        ),
                    )
                )

        if (
            comfortable.margin_to_deadline_minutes is not None
            and comfortable.margin_to_deadline_minutes < 0
            and not any(stop.issue_codes for stop in comfortable.stops)
        ):
            start_fix = self._find_earlier_start(safe_ids, pois, state, context, comfortable)
            if start_fix is not None:
                fixes.append(start_fix)

        if not comfortable.feasible and comfortable.walking_total_minutes > 0:
            fixes.append(
                FeasibilityFix(
                    kind=FeasibilityFixKind.TRAVEL_TIMES_UNAVAILABLE,
                    message=(
                        "Taxi and public-transport travel times are not available; no faster "
                        "transport estimate is claimed."
                    ),
                )
            )
        return fixes

    def _find_earlier_start(
        self,
        poi_ids: list[str],
        pois: dict[str, Poi],
        state: TripState,
        context: PlanningContext,
        estimate: FeasibilityEstimate,
    ) -> FeasibilityFix | None:
        assert state.time_window is not None
        assert estimate.margin_to_deadline_minutes is not None
        first_candidate = max(1, -estimate.margin_to_deadline_minutes)
        for minutes in range(first_candidate, 361):
            earlier_state = state.model_copy(
                update={
                    "time_window": TimeWindow(
                        start=state.time_window.start - timedelta(minutes=minutes),
                        end=state.time_window.end,
                    )
                }
            )
            revised = self._best_estimate(
                poi_ids,
                pois,
                earlier_state,
                context,
                comfortable=True,
            )
            if revised.feasible:
                return FeasibilityFix(
                    kind=FeasibilityFixKind.START_EARLIER,
                    minutes=minutes,
                    message=(
                        f"Start {minutes} min earlier to keep all stops at comfortable durations."
                    ),
                )
        return None

    @staticmethod
    def _notice(
        code: ViolationCode,
        severity: ViolationSeverity,
        message: str,
        poi_id: str,
        position: int,
    ) -> Violation:
        return Violation(
            code=code,
            severity=severity,
            message=message,
            poi_id=poi_id,
            activity_position=position,
        )

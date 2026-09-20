from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.catalog import ChildFriendly, Poi, SafetyTier
from app.domain.models import (
    Activity,
    ActivityKind,
    Exposure,
    Itinerary,
    TripState,
    ValidationResult,
    Violation,
    ViolationCode,
    ViolationSeverity,
)
from app.domain.ports import OpeningHoursRequest, OpeningHoursResult, PlanningContext
from app.planning.rules import adjusted_minutes, travel_leg, weather_during


class DeterministicItineraryValidator:
    def validate(
        self,
        itinerary: Itinerary,
        state: TripState,
        context: PlanningContext,
    ) -> ValidationResult:
        violations: list[Violation] = []
        pois = {poi.id: poi for poi in context.catalog.pois}
        window = state.time_window or itinerary.window

        if context.weather_unavailable_reason and itinerary.activities:
            first = itinerary.activities[0]
            violations.append(
                self._violation(
                    ViolationCode.WEATHER_UNAVAILABLE,
                    ViolationSeverity.WARNING,
                    (
                        f"Weather is unavailable for {window.start:%Y-%m-%d %H:%M}–"
                        f"{window.end:%H:%M}: {context.weather_unavailable_reason}."
                    ),
                    first.poi_id,
                    1,
                )
            )

        previous_activity: Activity | None = None
        previous_location = state.start_location.poi_id if state.start_location else None
        previous_end = window.start

        for position, activity in enumerate(itinerary.activities, start=1):
            poi = pois.get(activity.poi_id) if activity.poi_id else None
            effective_end = activity.end
            if poi is not None and activity.kind == ActivityKind.VISIT:
                visit_minutes = adjusted_minutes(
                    poi.visit_minutes.typical,
                    context.pace_factors.visit_time_multiplier,
                )
                effective_end = activity.start + timedelta(minutes=visit_minutes)

            if previous_activity is not None and activity.start < previous_end:
                violations.append(
                    self._violation(
                        ViolationCode.OVERLAP,
                        ViolationSeverity.ERROR,
                        (
                            f"Activity {position} starts at {activity.start:%H:%M} before "
                            f"activity {position - 1} recomputes to end at "
                            f"{previous_end:%H:%M}."
                        ),
                        activity.poi_id,
                        position,
                    )
                )

            if activity.start < window.start or effective_end > window.end:
                violations.append(
                    self._violation(
                        ViolationCode.OUTSIDE_USER_WINDOW,
                        ViolationSeverity.ERROR,
                        (
                            f"Visit {activity.start:%H:%M}–{effective_end:%H:%M} is outside "
                            f"the user window {window.start:%H:%M}–{window.end:%H:%M}."
                        ),
                        activity.poi_id,
                        position,
                    )
                )

            if activity.poi_id and poi is None:
                violations.append(
                    self._violation(
                        ViolationCode.UNKNOWN_POI,
                        ViolationSeverity.ERROR,
                        (
                            f"POI '{activity.poi_id}' scheduled at "
                            f"{activity.start:%H:%M}–{activity.end:%H:%M} is not in the catalog."
                        ),
                        activity.poi_id,
                        position,
                    )
                )

            if poi is not None and previous_location is not None:
                self._check_travel(
                    violations,
                    context,
                    state,
                    previous_location,
                    poi.id,
                    previous_end,
                    activity,
                    position,
                )

            if poi is not None:
                self._check_constraints(
                    violations,
                    state,
                    poi,
                    activity,
                    effective_end,
                    position,
                )
                if activity.kind == ActivityKind.VISIT:
                    hours = context.opening_hours.check(
                        OpeningHoursRequest(
                            poi_id=poi.id,
                            visit_start=activity.start,
                            visit_end=effective_end,
                        )
                    )
                    self._check_hours(
                        violations,
                        hours,
                        poi.id,
                        activity,
                        effective_end,
                        position,
                    )
                self._check_weather(
                    violations,
                    context,
                    poi,
                    activity,
                    effective_end,
                    position,
                )
                self._check_child_rules(
                    violations,
                    state,
                    poi,
                    activity,
                    effective_end,
                    position,
                )
                previous_location = poi.id
            elif activity.poi_id is None:
                self._check_unlocated_weather(violations, context, activity, position)

            previous_activity = activity
            previous_end = effective_end

        return ValidationResult(
            is_valid=not any(item.severity == ViolationSeverity.ERROR for item in violations),
            violations=violations,
            checked_at=context.now,
        )

    def _check_travel(
        self,
        violations: list[Violation],
        context: PlanningContext,
        state: TripState,
        origin_id: str,
        destination_id: str,
        previous_end: datetime,
        activity: Activity,
        position: int,
    ) -> None:
        leg = travel_leg(context, origin_id, destination_id)
        walking_minutes = adjusted_minutes(
            leg.minutes,
            context.pace_factors.travel_time_multiplier,
        )
        buffer_minutes = context.transition_buffer_minutes if origin_id != destination_id else 0
        required_gap = walking_minutes + buffer_minutes
        available_gap = int((activity.start - previous_end).total_seconds() // 60)
        if available_gap < required_gap:
            violations.append(
                self._violation(
                    ViolationCode.TRAVEL_GAP_TOO_SHORT,
                    ViolationSeverity.ERROR,
                    (
                        f"Only {available_gap} min is scheduled before {activity.start:%H:%M}; "
                        f"the matrix requires {walking_minutes} min walking plus "
                        f"{buffer_minutes} min buffer ({required_gap} min)."
                    ),
                    destination_id,
                    position,
                )
            )
        if leg.approximate and origin_id != destination_id:
            violations.append(
                self._violation(
                    ViolationCode.APPROXIMATE_TRAVEL,
                    ViolationSeverity.WARNING,
                    (
                        f"The {walking_minutes} min walk arriving by {activity.start:%H:%M} "
                        "uses an approximate matrix leg."
                    ),
                    destination_id,
                    position,
                )
            )
        if state.party.children_ages and walking_minutes >= context.long_walk_with_child_minutes:
            violations.append(
                self._violation(
                    ViolationCode.LONG_WALK_WITH_CHILD,
                    ViolationSeverity.WARNING,
                    (
                        f"The walk arriving at {activity.start:%H:%M} is {walking_minutes} min, "
                        "at or above the "
                        f"{context.long_walk_with_child_minutes} min child threshold."
                    ),
                    destination_id,
                    position,
                )
            )

    def _check_constraints(
        self,
        violations: list[Violation],
        state: TripState,
        poi: Poi,
        activity: Activity,
        effective_end: datetime,
        position: int,
    ) -> None:
        if poi.category in state.exclude_categories:
            violations.append(
                self._violation(
                    ViolationCode.EXCLUDED_CATEGORY,
                    ViolationSeverity.ERROR,
                    (
                        f"{poi.names.en} at {activity.start:%H:%M}–{effective_end:%H:%M} has "
                        f"excluded category '{poi.category}'."
                    ),
                    poi.id,
                    position,
                )
            )
        if poi.id in state.exclude_poi_ids:
            violations.append(
                self._violation(
                    ViolationCode.EXCLUDED_POI,
                    ViolationSeverity.ERROR,
                    (
                        f"{poi.names.en} at {activity.start:%H:%M}–{effective_end:%H:%M} "
                        "is explicitly excluded."
                    ),
                    poi.id,
                    position,
                )
            )

    def _check_hours(
        self,
        violations: list[Violation],
        result: OpeningHoursResult,
        poi_id: str,
        activity: Activity,
        effective_end: datetime,
        position: int,
    ) -> None:
        if not result.can_visit:
            code = ViolationCode.CLOSED_DURING_VISIT
            if result.source_rule == "temporary_closure":
                code = ViolationCode.TEMPORARY_CLOSURE
            elif result.source_rule is None and result.needs_verification:
                code = ViolationCode.HOURS_UNKNOWN_FOR_DATE
            elif result.reason.startswith("last entry"):
                code = ViolationCode.AFTER_LAST_ENTRY
            violations.append(
                self._violation(
                    code,
                    ViolationSeverity.ERROR,
                    (
                        f"Visit {activity.start:%Y-%m-%d %H:%M}–{effective_end:%H:%M} "
                        f"cannot proceed: {result.reason}."
                    ),
                    poi_id,
                    position,
                )
            )
            return

        if result.closes_at is not None:
            margin = int((result.closes_at - effective_end).total_seconds() // 60)
            if margin < 30:
                violations.append(
                    self._violation(
                        ViolationCode.CLOSES_SOON_AFTER_VISIT,
                        ViolationSeverity.WARNING,
                        (
                            f"The visit ends at {effective_end:%H:%M}, only {margin} min before "
                            f"closing at {result.closes_at:%H:%M}; please confirm."
                        ),
                        poi_id,
                        position,
                    )
                )

    def _check_weather(
        self,
        violations: list[Violation],
        context: PlanningContext,
        poi: Poi,
        activity: Activity,
        effective_end: datetime,
        position: int,
    ) -> None:
        if context.weather_unavailable_reason:
            return
        flags = weather_during(context, activity.start, effective_end)
        exposed = poi.exposure != Exposure.INDOOR
        if exposed and any(item.storm for item in flags):
            self._append_weather(
                violations,
                ViolationCode.STORM_OUTDOOR,
                ViolationSeverity.ERROR,
                "Storm conditions overlap the outdoor visit",
                poi.id,
                activity,
                effective_end,
                position,
            )
        if poi.safety_tier == SafetyTier.CRITICAL and any(item.after_dark for item in flags):
            self._append_weather(
                violations,
                ViolationCode.CRITICAL_AFTER_DARK,
                ViolationSeverity.ERROR,
                "A critical-safety POI is scheduled after dark",
                poi.id,
                activity,
                effective_end,
                position,
            )
        for field, code, label in (
            ("rain_risk", ViolationCode.RAIN_OUTDOOR, "Rain risk overlaps the outdoor visit"),
            ("heat_risk", ViolationCode.HEAT_OUTDOOR, "Heat risk overlaps the outdoor visit"),
            ("uv_high", ViolationCode.UV_HIGH_OUTDOOR, "High UV overlaps the outdoor visit"),
        ):
            if exposed and any(getattr(item, field) for item in flags):
                self._append_weather(
                    violations,
                    code,
                    ViolationSeverity.WARNING,
                    label,
                    poi.id,
                    activity,
                    effective_end,
                    position,
                )

    def _check_unlocated_weather(
        self,
        violations: list[Violation],
        context: PlanningContext,
        activity: Activity,
        position: int,
    ) -> None:
        if context.weather_unavailable_reason or activity.exposure == Exposure.INDOOR:
            return
        flags = weather_during(context, activity.start, activity.end)
        for field, code, label, severity in (
            (
                "storm",
                ViolationCode.STORM_OUTDOOR,
                "Storm conditions overlap",
                ViolationSeverity.ERROR,
            ),
            (
                "rain_risk",
                ViolationCode.RAIN_OUTDOOR,
                "Rain risk overlaps",
                ViolationSeverity.WARNING,
            ),
            (
                "heat_risk",
                ViolationCode.HEAT_OUTDOOR,
                "Heat risk overlaps",
                ViolationSeverity.WARNING,
            ),
            (
                "uv_high",
                ViolationCode.UV_HIGH_OUTDOOR,
                "High UV overlaps",
                ViolationSeverity.WARNING,
            ),
        ):
            if any(getattr(item, field) for item in flags):
                self._append_weather(
                    violations,
                    code,
                    severity,
                    f"{label} this outdoor activity",
                    None,
                    activity,
                    activity.end,
                    position,
                )

    def _check_child_rules(
        self,
        violations: list[Violation],
        state: TripState,
        poi: Poi,
        activity: Activity,
        effective_end: datetime,
        position: int,
    ) -> None:
        if state.party.children_ages and poi.child_friendly == ChildFriendly.LOW:
            violations.append(
                self._violation(
                    ViolationCode.CHILD_UNSUITABLE,
                    ViolationSeverity.WARNING,
                    (
                        f"{poi.names.en} is rated low for children during the "
                        f"{activity.start:%H:%M}–{effective_end:%H:%M} visit."
                    ),
                    poi.id,
                    position,
                )
            )

    @staticmethod
    def _append_weather(
        violations: list[Violation],
        code: ViolationCode,
        severity: ViolationSeverity,
        label: str,
        poi_id: str | None,
        activity: Activity,
        effective_end: datetime,
        position: int,
    ) -> None:
        violations.append(
            DeterministicItineraryValidator._violation(
                code,
                severity,
                f"{label} at {activity.start:%Y-%m-%d %H:%M}–{effective_end:%H:%M}.",
                poi_id,
                position,
            )
        )

    @staticmethod
    def _violation(
        code: ViolationCode,
        severity: ViolationSeverity,
        message: str,
        poi_id: str | None,
        position: int | None,
    ) -> Violation:
        return Violation(
            code=code,
            severity=severity,
            message=message,
            poi_id=poi_id,
            activity_position=position,
        )

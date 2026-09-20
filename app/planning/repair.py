from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.catalog import ChildFriendly, HeatExposure, Poi
from app.domain.models import (
    Activity,
    ActivityKind,
    AddActivity,
    ConstraintUpdates,
    Exposure,
    MoveActivity,
    PlanEditOperation,
    RemoveActivity,
    ReplaceActivity,
    TripState,
)
from app.domain.ports import PlanningContext
from app.planning.candidates import PlannerConfig, generate_candidates
from app.planning.planner import BeamSearchPlanner, PlanResult
from app.planning.rules import pace_factors_for, weather_during


class RepairModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemovedActivity(RepairModel):
    poi_id: str
    reason: str


class RetimedActivity(RepairModel):
    key: str
    old_start: AwareDatetime
    new_start: AwareDatetime


class PlanDiff(RepairModel):
    kept: list[str] = Field(default_factory=list)
    removed: list[RemovedActivity] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)
    retimed: list[RetimedActivity] = Field(default_factory=list)


class RepairResult(RepairModel):
    plan: PlanResult
    diff: PlanDiff
    state: TripState
    used_full_replan: bool = False


class PlanRepairer:
    def __init__(
        self,
        planner: BeamSearchPlanner | None = None,
        config: PlannerConfig | None = None,
    ) -> None:
        self.planner = planner or BeamSearchPlanner(config=config)

    def repair(
        self,
        previous: PlanResult,
        state: TripState,
        context: PlanningContext,
        *,
        operations: list[PlanEditOperation] | None = None,
        constraint_updates: ConstraintUpdates | None = None,
        previous_context: PlanningContext | None = None,
    ) -> RepairResult:
        updated_state = apply_constraint_updates(state, constraint_updates or ConstraintUpdates())
        updated_context = context.model_copy(
            update={"pace_factors": pace_factors_for(updated_state)}
        )
        old_order = self._visit_order(previous)
        target_order = list(old_order)
        removal_reasons: dict[str, str] = {}

        for operation in operations or []:
            target_order = self._apply_operation(
                target_order,
                operation,
                updated_state,
                updated_context,
                removal_reasons,
            )

        pois = {poi.id: poi for poi in updated_context.catalog.pois}
        filtered: list[str] = []
        for poi_id in target_order:
            poi = pois.get(poi_id)
            if poi is None:
                removal_reasons[poi_id] = "unknown_poi"
            elif (
                poi_id in updated_state.exclude_poi_ids
                or poi.category in updated_state.exclude_categories
            ):
                removal_reasons[poi_id] = "excluded"
            elif updated_state.party.children_ages and poi.child_friendly == ChildFriendly.LOW:
                removal_reasons[poi_id] = "child_unsuitable"
            else:
                filtered.append(poi_id)
        target_order = filtered

        local = self.planner.plan_for_order(
            updated_state,
            updated_context,
            target_order,
            assumptions=previous.assumptions,
        )
        if local is not None and self._has_undesirable_weather(local, updated_context, pois):
            reordered = sorted(
                enumerate(target_order),
                key=lambda item: (
                    pois[item[1]].exposure == Exposure.INDOOR,
                    item[0],
                ),
            )
            reordered_ids = [poi_id for _index, poi_id in reordered]
            if reordered_ids != target_order:
                weather_local = self.planner.plan_for_order(
                    updated_state,
                    updated_context,
                    reordered_ids,
                    assumptions=previous.assumptions,
                )
                if weather_local is not None:
                    local = weather_local
                    target_order = reordered_ids

        if local is None and self._window_shrank(state, updated_state):
            local, target_order = self._drop_low_scores_until_fit(
                previous,
                target_order,
                updated_state,
                updated_context,
                removal_reasons,
            )

        used_full_replan = local is None or self._has_undesirable_weather(
            local, updated_context, pois
        )
        user_removed = {
            poi_id
            for poi_id, reason in removal_reasons.items()
            if reason in {"replaced_by_user_edit", "removed_by_user_edit"}
        }
        replan_state = updated_state.model_copy(
            update={
                "exclude_poi_ids": list(
                    dict.fromkeys([*updated_state.exclude_poi_ids, *sorted(user_removed)])
                )
            }
        )
        plan = (
            self.planner.plan(
                replan_state,
                updated_context,
                preferred_order=target_order,
            )
            if used_full_replan
            else local
        )
        assert plan is not None
        for poi_id in set(old_order) - set(self._visit_order(plan)):
            removal_reasons.setdefault(
                poi_id,
                "weather"
                if previous_context is not None
                and self._weather_changed(previous_context, updated_context)
                else "replanned",
            )
        diff = self._diff(previous, plan, removal_reasons)
        return RepairResult(
            plan=plan,
            diff=diff,
            state=updated_state,
            used_full_replan=used_full_replan,
        )

    def _apply_operation(
        self,
        order: list[str],
        operation: PlanEditOperation,
        state: TripState,
        context: PlanningContext,
        removal_reasons: dict[str, str],
    ) -> list[str]:
        result = list(order)
        if isinstance(operation, ReplaceActivity):
            index = operation.position - 1
            if not 0 <= index < len(result):
                return result
            replacement = self._select_candidate(
                state,
                context,
                result,
                preferred_poi_id=operation.preferred_poi_id,
                required_tags=operation.required_tags,
                required_exposure=operation.required_exposure,
            )
            if replacement:
                removal_reasons[result[index]] = "replaced_by_user_edit"
                result[index] = replacement
        elif isinstance(operation, RemoveActivity):
            if operation.position is not None and operation.position <= len(result):
                removed = result.pop(operation.position - 1)
                removal_reasons[removed] = "removed_by_user_edit"
            elif operation.poi_id in result:
                result.remove(operation.poi_id)
                removal_reasons[operation.poi_id] = "removed_by_user_edit"
        elif isinstance(operation, AddActivity):
            addition = self._select_candidate(
                state,
                context,
                result,
                preferred_poi_id=operation.preferred_poi_id,
                required_tags=operation.required_tags,
                required_exposure=operation.required_exposure,
            )
            if addition:
                index = (
                    len(result) if operation.after_position is None else operation.after_position
                )
                result.insert(min(index, len(result)), addition)
        elif isinstance(operation, MoveActivity):
            source = operation.from_position - 1
            target = operation.to_position - 1
            if 0 <= source < len(result) and 0 <= target < len(result):
                result.insert(target, result.pop(source))
        return result

    def _select_candidate(
        self,
        state: TripState,
        context: PlanningContext,
        existing: list[str],
        *,
        preferred_poi_id: str | None,
        required_tags: list[str],
        required_exposure: Exposure | None,
    ) -> str | None:
        generation = generate_candidates(state, context, self.planner.config)
        candidates = generation.candidates
        pois = {poi.id: poi for poi in context.catalog.pois}
        if preferred_poi_id:
            candidates = sorted(
                candidates,
                key=lambda item: (item.poi_id != preferred_poi_id, -item.score.total, item.poi_id),
            )
        for candidate in candidates:
            poi = pois[candidate.poi_id]
            if candidate.poi_id in existing:
                continue
            if required_tags and not set(required_tags).issubset(poi.tags):
                continue
            if required_exposure is not None and poi.exposure != required_exposure:
                continue
            return candidate.poi_id
        return None

    def _drop_low_scores_until_fit(
        self,
        previous: PlanResult,
        order: list[str],
        state: TripState,
        context: PlanningContext,
        removal_reasons: dict[str, str],
    ) -> tuple[PlanResult | None, list[str]]:
        scores = {
            item.poi_id: item.total
            for item in previous.score_breakdown
            if item.kind == ActivityKind.VISIT and item.poi_id
        }
        remaining = list(order)
        while remaining:
            candidate = self.planner.plan_for_order(
                state,
                context,
                remaining,
                assumptions=previous.assumptions,
            )
            if candidate is not None:
                return candidate, remaining
            dropped = min(remaining, key=lambda poi_id: (scores.get(poi_id, 0.0), poi_id))
            remaining.remove(dropped)
            removal_reasons[dropped] = "window_shrink_lowest_score"
        return None, remaining

    @staticmethod
    def _has_undesirable_weather(
        plan: PlanResult,
        context: PlanningContext,
        pois: dict[str, Poi],
    ) -> bool:
        for activity in plan.itinerary.activities:
            if activity.kind != ActivityKind.VISIT or activity.poi_id is None:
                continue
            poi = pois[activity.poi_id]
            flags = weather_during(context, activity.start, activity.end)
            if poi.exposure != Exposure.INDOOR and any(flag.rain_risk for flag in flags):
                return True
            if (
                poi.exposure != Exposure.INDOOR
                and poi.heat_exposure == HeatExposure.HIGH
                and any(flag.heat_risk for flag in flags)
            ):
                return True
        return False

    @staticmethod
    def _window_shrank(old: TripState, new: TripState) -> bool:
        if old.time_window is None or new.time_window is None:
            return False
        return (new.time_window.end - new.time_window.start) < (
            old.time_window.end - old.time_window.start
        )

    @staticmethod
    def _weather_changed(old: PlanningContext, new: PlanningContext) -> bool:
        return (
            old.hourly_weather_flags != new.hourly_weather_flags
            or old.weather_unavailable_reason != new.weather_unavailable_reason
        )

    @staticmethod
    def _visit_order(plan: PlanResult) -> list[str]:
        return [
            activity.poi_id
            for activity in plan.itinerary.activities
            if activity.kind == ActivityKind.VISIT and activity.poi_id is not None
        ]

    def _diff(
        self,
        previous: PlanResult,
        current: PlanResult,
        removal_reasons: dict[str, str],
    ) -> PlanDiff:
        old_visits = self._visit_order(previous)
        new_visits = self._visit_order(current)
        old_by_key = {
            _activity_key(activity): activity for activity in previous.itinerary.activities
        }
        new_by_key = {
            _activity_key(activity): activity for activity in current.itinerary.activities
        }
        kept = [poi_id for poi_id in new_visits if poi_id in old_visits]
        removed = [
            RemovedActivity(
                poi_id=poi_id,
                reason=removal_reasons.get(poi_id, "replanned"),
            )
            for poi_id in old_visits
            if poi_id not in new_visits
        ]
        added = [key for key in new_by_key if key not in old_by_key]
        retimed = [
            RetimedActivity(
                key=key,
                old_start=old_by_key[key].start,
                new_start=new_by_key[key].start,
            )
            for key in new_by_key.keys() & old_by_key.keys()
            if new_by_key[key].start != old_by_key[key].start
        ]
        return PlanDiff(
            kept=kept,
            removed=removed,
            added=sorted(added),
            retimed=sorted(retimed, key=lambda item: item.key),
        )


def apply_constraint_updates(state: TripState, updates: ConstraintUpdates) -> TripState:
    values = state.model_dump()
    if updates.clear_time_window:
        values["time_window"] = None
    elif updates.time_window is not None:
        values["time_window"] = updates.time_window
    if updates.clear_start_location:
        values["start_location"] = None
    elif updates.start_location is not None:
        values["start_location"] = updates.start_location
    for field in ("party", "mobility", "transport", "pace"):
        value = getattr(updates, field)
        if value is not None:
            values[field] = value
    for field, additions, removals in (
        ("interests", updates.add_interests, updates.remove_interests),
        (
            "exclude_categories",
            updates.add_exclude_categories,
            updates.remove_exclude_categories,
        ),
        (
            "exclude_poi_ids",
            updates.add_exclude_poi_ids,
            updates.remove_exclude_poi_ids,
        ),
    ):
        current = [item for item in values[field] if item not in removals]
        values[field] = list(dict.fromkeys([*current, *additions]))
    values["visited"] = list(dict.fromkeys([*values["visited"], *updates.add_visited]))
    return TripState.model_validate(values)


def _activity_key(activity: Activity) -> str:
    if activity.kind == ActivityKind.VISIT:
        return activity.poi_id or "visit"
    if activity.kind == ActivityKind.MEAL:
        return f"meal:{activity.poi_id or activity.area_label or 'area'}"
    return f"break:{activity.area_label or 'rest'}"

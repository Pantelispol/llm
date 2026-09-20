from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Protocol

from app.domain.models import ConstraintUpdates, Intent, TripState, TurnAnalysis, ValidationResult
from app.orchestrator.models import (
    CANONICAL_TOOL_ORDER,
    RouteDecision,
    RouteExecution,
    RouteReason,
    RouteReasonCode,
    RouteStopReason,
    ToolName,
)

PLAN_INTENTS = frozenset(
    {Intent.CREATE_PLAN, Intent.EDIT_PLAN, Intent.FEASIBILITY_CHECK}
)
PLAN_TOOLS = frozenset(
    {
        ToolName.CATALOG,
        ToolName.WEATHER,
        ToolName.HOURS,
        ToolName.TRAVEL,
        ToolName.PLANNER,
        ToolName.VALIDATOR,
    }
)

INTENT_TOOL_POLICY: dict[Intent, tuple[ToolName, ...]] = {
    Intent.TOURISM_QA: (ToolName.CATALOG, ToolName.RETRIEVER),
    Intent.RECOMMENDATION: (ToolName.CATALOG, ToolName.RETRIEVER),
    Intent.CREATE_PLAN: (
        ToolName.CATALOG,
        ToolName.WEATHER,
        ToolName.HOURS,
        ToolName.TRAVEL,
        ToolName.PLANNER,
        ToolName.VALIDATOR,
    ),
    Intent.EDIT_PLAN: (
        ToolName.CATALOG,
        ToolName.WEATHER,
        ToolName.HOURS,
        ToolName.TRAVEL,
        ToolName.PLANNER,
        ToolName.VALIDATOR,
    ),
    Intent.FEASIBILITY_CHECK: (
        ToolName.CATALOG,
        ToolName.WEATHER,
        ToolName.HOURS,
        ToolName.TRAVEL,
        ToolName.PLANNER,
        ToolName.VALIDATOR,
    ),
    Intent.WEATHER_QUESTION: (ToolName.WEATHER,),
    Intent.OPENING_HOURS_QUESTION: (ToolName.CATALOG, ToolName.HOURS),
    Intent.PRICE_QUESTION: (ToolName.CATALOG,),
    Intent.SAFETY: (ToolName.CATALOG, ToolName.WEATHER),
    Intent.UNSUPPORTED_LIVE_INFO: (),
    Intent.OUT_OF_SCOPE: (),
    Intent.SMALL_TALK: (),
}

OPENING_HOURS_TERMS = (
    "opening hours",
    "open",
    "opens",
    "close",
    "closes",
    "ωραριο",
    "ωρες λειτουργιας",
    "ανοιχτα",
    "ανοιγει",
    "κλεινει",
)
PRICE_TERMS = (
    "admission",
    "ticket",
    "tickets",
    "cost",
    "price",
    "τιμη",
    "κοστος",
    "κοστιζει",
    "εισιτηριο",
)
PLAN_TEXT_TERMS = (
    "plan",
    "itinerary",
    "schedule",
    "change the plan",
    "change my plan",
    "replace",
    "remove",
    "add",
    "reschedule",
    "no museum",
    "fit into",
    "feasible",
    "feasibility",
    "προγραμμα",
    "δρομολογιο",
    "αλλαξε",
    "αντικαταστησε",
    "βγαλε",
    "προσθεσε",
    "προλαβαινω",
)


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[\w]+", without_marks, flags=re.UNICODE))


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    padded = f" {_normalized(text)} "
    return any(f" {_normalized(term)} " in padded for term in terms)


def _has_constraint_updates(updates: ConstraintUpdates) -> bool:
    return bool(updates.model_dump(exclude_defaults=True, exclude_none=True))


def touches_plan(user_turn: str, analysis: TurnAnalysis) -> bool:
    return bool(
        PLAN_INTENTS.intersection(analysis.intents)
        or analysis.plan_edits
        or _has_constraint_updates(analysis.constraint_updates)
        or _contains_any(user_turn, PLAN_TEXT_TERMS)
    )


def _effective_intents(
    user_turn: str, analysis: TurnAnalysis
) -> tuple[list[Intent], list[RouteReason]]:
    effective = list(analysis.intents)
    reasons: list[RouteReason] = []

    if _contains_any(user_turn, OPENING_HOURS_TERMS):
        effective = [
            intent
            for intent in effective
            if intent not in {Intent.TOURISM_QA, Intent.RECOMMENDATION}
        ]
        if Intent.OPENING_HOURS_QUESTION not in effective:
            effective.append(Intent.OPENING_HOURS_QUESTION)
        reasons.append(
            RouteReason(
                code=RouteReasonCode.SOURCE_BOUNDARY,
                detail="opening-hours language routes to catalog and hours, never retrieval",
                intent=Intent.OPENING_HOURS_QUESTION,
            )
        )

    if _contains_any(user_turn, PRICE_TERMS):
        effective = [
            intent
            for intent in effective
            if intent not in {Intent.TOURISM_QA, Intent.RECOMMENDATION}
        ]
        if Intent.PRICE_QUESTION not in effective:
            effective.append(Intent.PRICE_QUESTION)
        reasons.append(
            RouteReason(
                code=RouteReasonCode.SOURCE_BOUNDARY,
                detail="price language routes to the operational catalog, never retrieval",
                intent=Intent.PRICE_QUESTION,
            )
        )

    return list(dict.fromkeys(effective)), reasons


def route_turn(user_turn: str, analysis: TurnAnalysis, trip_state: TripState) -> RouteDecision:
    del trip_state  # State changes are represented by the validated analysis deltas.
    effective_intents, reasons = _effective_intents(user_turn, analysis)
    selected: set[ToolName] = set()
    for intent in effective_intents:
        selected.update(INTENT_TOOL_POLICY[intent])
        reasons.append(
            RouteReason(
                code=RouteReasonCode.INTENT_POLICY,
                detail=f"mandatory tools for {intent.value}",
                intent=intent,
            )
        )

    plan_touch = touches_plan(user_turn, analysis)
    if plan_touch:
        selected.update(PLAN_TOOLS)
        reasons.append(
            RouteReason(
                code=RouteReasonCode.PLAN_SAFETY_OVERRIDE,
                detail=(
                    "every plan touch requires catalog, weather, hours, travel, "
                    "planner, validator"
                ),
            )
        )

    ordered_tools = [tool for tool in CANONICAL_TOOL_ORDER if tool in selected]
    return RouteDecision(
        effective_intents=effective_intents,
        tools=ordered_tools,
        reasons=reasons,
        touches_plan=plan_touch,
    )


class ToolInvoker(Protocol):
    def invoke(self, tool: ToolName, prior_results: Mapping[ToolName, object]) -> object: ...


NarrationSink = Callable[[Mapping[ToolName, object]], object]


def execute_route(
    decision: RouteDecision,
    invoker: ToolInvoker,
    *,
    narrator: NarrationSink | None = None,
) -> RouteExecution:
    """Enforce tool order and prevent narration before successful validation."""
    results: dict[ToolName, object] = {}
    called: list[ToolName] = []

    for tool in decision.tools:
        if tool == ToolName.VALIDATOR and results.get(ToolName.PLANNER) is None:
            return RouteExecution(
                called_tools=called,
                narration_permitted=False,
                narration_invoked=False,
                stop_reason=RouteStopReason.PLANNER_OUTPUT_MISSING,
            )
        result = invoker.invoke(tool, results.copy())
        called.append(tool)
        results[tool] = result
        if tool == ToolName.PLANNER and result is None:
            return RouteExecution(
                called_tools=called,
                narration_permitted=False,
                narration_invoked=False,
                stop_reason=RouteStopReason.PLANNER_OUTPUT_MISSING,
            )
        if tool == ToolName.VALIDATOR:
            if not isinstance(result, ValidationResult):
                raise TypeError("validator tool must return ValidationResult")
            if not result.is_valid:
                return RouteExecution(
                    called_tools=called,
                    narration_permitted=False,
                    narration_invoked=False,
                    stop_reason=RouteStopReason.VALIDATION_FAILED,
                )

    narration_permitted = ToolName.VALIDATOR not in decision.tools or bool(
        isinstance(results.get(ToolName.VALIDATOR), ValidationResult)
        and results[ToolName.VALIDATOR].is_valid
    )
    narration_invoked = False
    if narrator is not None and narration_permitted:
        narrator(results.copy())
        narration_invoked = True
    return RouteExecution(
        called_tools=called,
        narration_permitted=narration_permitted,
        narration_invoked=narration_invoked,
    )

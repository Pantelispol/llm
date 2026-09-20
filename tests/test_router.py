from __future__ import annotations

import inspect
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError

from app.domain.models import (
    Activity,
    ConstraintUpdates,
    Exposure,
    Intent,
    Itinerary,
    TimeWindow,
    TripState,
    TurnAnalysis,
    ValidationResult,
    Violation,
    ViolationCode,
)
from app.llm.openai_provider import OpenAIProvider
from app.orchestrator.models import RouteStopReason, ToolName
from app.orchestrator.router import INTENT_TOOL_POLICY, execute_route, route_turn

ATHENS = ZoneInfo("Europe/Athens")
GOLD_PATH = Path(__file__).parents[1] / "evals" / "rag_gold.yaml"
ROUTER_CASES_PATH = Path(__file__).parents[1] / "evals" / "router_cases.yaml"
PLAN_CHAIN = [
    ToolName.CATALOG,
    ToolName.WEATHER,
    ToolName.HOURS,
    ToolName.TRAVEL,
    ToolName.PLANNER,
    ToolName.VALIDATOR,
]
EXPECTED_TOOL_MATRIX = {
    Intent.TOURISM_QA: [ToolName.CATALOG, ToolName.RETRIEVER],
    Intent.RECOMMENDATION: [ToolName.CATALOG, ToolName.RETRIEVER],
    Intent.CREATE_PLAN: PLAN_CHAIN,
    Intent.EDIT_PLAN: PLAN_CHAIN,
    Intent.FEASIBILITY_CHECK: PLAN_CHAIN,
    Intent.WEATHER_QUESTION: [ToolName.WEATHER],
    Intent.OPENING_HOURS_QUESTION: [ToolName.CATALOG, ToolName.HOURS],
    Intent.PRICE_QUESTION: [ToolName.CATALOG],
    Intent.SAFETY: [ToolName.CATALOG, ToolName.WEATHER],
    Intent.UNSUPPORTED_LIVE_INFO: [],
    Intent.OUT_OF_SCOPE: [],
    Intent.SMALL_TALK: [],
}
ROUTER_CASES = yaml.safe_load(ROUTER_CASES_PATH.read_text(encoding="utf-8"))
YAML_TOOL_MATRIX = {
    Intent(case["intent"]): [ToolName(tool) for tool in case["expected_tools"]]
    for case in ROUTER_CASES["intent_cases"]
}


def at(hour: int) -> datetime:
    return datetime(2026, 9, 22, hour, tzinfo=ATHENS)


def itinerary() -> Itinerary:
    return Itinerary(
        window=TimeWindow(start=at(9), end=at(12)),
        activities=[
            Activity(
                poi_id="white_tower",
                start=at(9),
                end=at(10),
                visit_minutes=60,
                exposure=Exposure.INDOOR,
            )
        ],
    )


def valid_result() -> ValidationResult:
    return ValidationResult(is_valid=True, checked_at=at(8))


def invalid_result() -> ValidationResult:
    return ValidationResult(
        is_valid=False,
        violations=[
            Violation(
                code=ViolationCode.CLOSED_DURING_VISIT,
                message="The site is closed during the planned visit.",
                poi_id="white_tower",
                activity_position=1,
            )
        ],
        checked_at=at(8),
    )


class SpyInvoker:
    def __init__(
        self,
        *,
        validation: ValidationResult | None = None,
        planner_output_missing: bool = False,
    ) -> None:
        self.calls: list[ToolName] = []
        self.prior_results: list[dict[ToolName, object]] = []
        self.validation = validation or valid_result()
        self.planner_output_missing = planner_output_missing

    def invoke(self, tool: ToolName, prior_results: dict[ToolName, object]) -> object:
        self.calls.append(tool)
        self.prior_results.append(prior_results)
        if tool == ToolName.PLANNER:
            return None if self.planner_output_missing else itinerary()
        if tool == ToolName.VALIDATOR:
            assert isinstance(prior_results[ToolName.PLANNER], Itinerary)
            return self.validation
        return {"tool": tool.value}


class NarratorSpy:
    def __init__(self) -> None:
        self.calls: list[dict[ToolName, object]] = []

    def __call__(self, results: dict[ToolName, object]) -> str:
        self.calls.append(results)
        return "narrated"


@pytest.mark.parametrize("intent", list(Intent))
def test_intent_tool_matrix(intent: Intent) -> None:
    assert set(EXPECTED_TOOL_MATRIX) == set(Intent)
    assert set(YAML_TOOL_MATRIX) == set(Intent)
    assert set(INTENT_TOOL_POLICY) == set(Intent)
    decision = route_turn(
        "Neutral request text.",
        TurnAnalysis(intents=[intent]),
        TripState(),
    )

    assert YAML_TOOL_MATRIX[intent] == EXPECTED_TOOL_MATRIX[intent]
    assert decision.tools == YAML_TOOL_MATRIX[intent]


def test_mixed_intents_union_tools_in_canonical_order() -> None:
    decision = route_turn(
        "Tell me about Roman history and the weather.",
        TurnAnalysis(intents=[Intent.WEATHER_QUESTION, Intent.TOURISM_QA]),
        TripState(),
    )

    assert decision.tools == [ToolName.CATALOG, ToolName.RETRIEVER, ToolName.WEATHER]
    assert len(decision.tools) == len(set(decision.tools))


@pytest.mark.parametrize(
    ("turn", "analysis"),
    [
        ("Help with this request.", TurnAnalysis(intents=[Intent.CREATE_PLAN])),
        (
            "Help with this request.",
            TurnAnalysis(
                intents=[Intent.TOURISM_QA],
                plan_edits=[
                    {
                        "op": "replace_activity",
                        "position": 1,
                        "required_exposure": "outdoor",
                    }
                ],
            ),
        ),
        (
            "Help with this request.",
            TurnAnalysis(
                intents=[Intent.TOURISM_QA],
                constraint_updates=ConstraintUpdates(add_exclude_categories=["museum"]),
            ),
        ),
        ("Άλλαξε το πρόγραμμά μου.", TurnAnalysis(intents=[Intent.TOURISM_QA])),
    ],
)
def test_every_plan_touch_forces_complete_planning_chain(
    turn: str, analysis: TurnAnalysis
) -> None:
    decision = route_turn(turn, analysis, TripState())

    assert decision.touches_plan is True
    assert [tool for tool in decision.tools if tool in PLAN_CHAIN] == PLAN_CHAIN


def test_plan_chain_runs_when_analysis_misclassifies_the_turn() -> None:
    decision = route_turn(
        "Please change my plan and replace the second stop.",
        TurnAnalysis(intents=[Intent.TOURISM_QA]),
        TripState(),
    )
    tools = SpyInvoker()
    narrator = NarratorSpy()

    execution = execute_route(decision, tools, narrator=narrator)

    assert [tool for tool in tools.calls if tool in PLAN_CHAIN] == PLAN_CHAIN
    assert Counter(tools.calls) == Counter({tool: 1 for tool in decision.tools})
    assert execution.called_tools == decision.tools
    assert execution.narration_invoked is True
    assert len(narrator.calls) == 1


def test_opening_hours_question_uses_catalog_and_never_rag() -> None:
    decision = route_turn(
        "When does the Rotunda open?",
        TurnAnalysis(intents=[Intent.TOURISM_QA]),
        TripState(),
    )
    tools = SpyInvoker()

    execute_route(decision, tools)

    assert tools.calls == [ToolName.CATALOG, ToolName.HOURS]
    assert tools.calls.count(ToolName.RETRIEVER) == 0


def test_price_question_uses_catalog_and_never_rag() -> None:
    decision = route_turn(
        "How much does admission to the Archaeological Museum cost?",
        TurnAnalysis(intents=[Intent.TOURISM_QA]),
        TripState(),
    )
    tools = SpyInvoker()

    execute_route(decision, tools)

    assert tools.calls == [ToolName.CATALOG]
    assert tools.calls.count(ToolName.RETRIEVER) == 0


def test_phase_5c_opening_hours_gold_query_routes_to_catalog() -> None:
    gold = yaml.safe_load(GOLD_PATH.read_text(encoding="utf-8"))
    case = next(item for item in gold["cases"] if item["id"] == "abstain_opening_hours")
    assert case["query"] == "What are the White Tower opening hours?"
    decision = route_turn(
        case["query"],
        TurnAnalysis(intents=[Intent.TOURISM_QA]),
        TripState(),
    )
    tools = SpyInvoker()

    execute_route(decision, tools)

    assert tools.calls == [ToolName.CATALOG, ToolName.HOURS]
    assert tools.calls.count(ToolName.RETRIEVER) == 0


def test_router_never_calls_llm_or_accepts_model_tool_names() -> None:
    assert not inspect.iscoroutinefunction(route_turn)
    with patch.object(
        OpenAIProvider,
        "__init__",
        side_effect=AssertionError("router constructed an LLM provider"),
    ) as constructor:
        decision = route_turn(
            "Tell me about the Rotunda.",
            TurnAnalysis(intents=[Intent.TOURISM_QA]),
            TripState(),
        )
    constructor.assert_not_called()
    assert decision.tools == [ToolName.CATALOG, ToolName.RETRIEVER]

    with pytest.raises(ValidationError, match="tools"):
        TurnAnalysis.model_validate(
            {"intents": ["tourism_qa"], "tools": ["weather", "planner"]}
        )


def test_validator_failure_blocks_narration() -> None:
    decision = route_turn(
        "Create a plan for tomorrow.",
        TurnAnalysis(intents=[Intent.CREATE_PLAN]),
        TripState(),
    )
    tools = SpyInvoker(validation=invalid_result())
    narrator = NarratorSpy()

    execution = execute_route(decision, tools, narrator=narrator)

    assert tools.calls == PLAN_CHAIN
    assert len(narrator.calls) == 0
    assert execution.narration_permitted is False
    assert execution.narration_invoked is False
    assert execution.stop_reason == RouteStopReason.VALIDATION_FAILED


def test_missing_planner_output_blocks_validation_and_narration() -> None:
    decision = route_turn(
        "Create a plan for tomorrow.",
        TurnAnalysis(intents=[Intent.CREATE_PLAN]),
        TripState(),
    )
    tools = SpyInvoker(planner_output_missing=True)
    narrator = NarratorSpy()

    execution = execute_route(decision, tools, narrator=narrator)

    assert tools.calls == PLAN_CHAIN[:-1]
    assert len(narrator.calls) == 0
    assert execution.stop_reason == RouteStopReason.PLANNER_OUTPUT_MISSING


def test_same_input_produces_same_route_decision() -> None:
    analysis = TurnAnalysis(
        intents=[Intent.TOURISM_QA, Intent.WEATHER_QUESTION],
        entities={"mentioned_poi_ids": ["white_tower"]},
    )
    state = TripState(interests=["history"])

    first = route_turn("Tell me about the White Tower and the weather.", analysis, state)
    second = route_turn("Tell me about the White Tower and the weather.", analysis, state)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()

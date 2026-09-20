from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.domain.models import Intent, TripState, TurnAnalysis, ValidationResult, Violation
from app.orchestrator.models import ToolName
from app.orchestrator.router import execute_route, route_turn

ROOT = Path(__file__).parents[1]
CASES_PATH = ROOT / "evals" / "router_cases.yaml"
ATHENS = ZoneInfo("Europe/Athens")


class SpyInvoker:
    def __init__(self, *, validator_valid: bool = True) -> None:
        self.calls: list[ToolName] = []
        self.validator_valid = validator_valid

    def invoke(self, tool: ToolName, prior_results: dict[ToolName, object]) -> object:
        self.calls.append(tool)
        if tool == ToolName.PLANNER:
            return {"validated": False}
        if tool == ToolName.VALIDATOR:
            violations = (
                []
                if self.validator_valid
                else [Violation(code="CLOSED_DURING_VISIT", message="closed")]
            )
            return ValidationResult(
                is_valid=self.validator_valid,
                violations=violations,
                checked_at=datetime(2026, 9, 20, 12, tzinfo=ATHENS),
            )
        return {"tool": tool.value}


class NarratorSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, results: dict[ToolName, object]) -> str:
        self.calls += 1
        return "narrated"


def load_cases() -> dict[str, Any]:
    payload = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported router eval schema")
    return payload


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render(row: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    print(render(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(render(row))


def intent_table(payload: dict[str, Any]) -> None:
    cases = payload["intent_cases"]
    configured = {Intent(case["intent"]) for case in cases}
    if configured != set(Intent):
        missing = sorted(intent.value for intent in set(Intent) - configured)
        extra = sorted(intent.value for intent in configured - set(Intent))
        raise RuntimeError(
            f"router intent cases are not exhaustive; missing={missing}, extra={extra}"
        )

    rows = []
    for case in cases:
        intent = Intent(case["intent"])
        expected = [ToolName(tool) for tool in case["expected_tools"]]
        decision = route_turn(
            case["user_turn"], TurnAnalysis(intents=[intent]), TripState()
        )
        if decision.tools != expected:
            raise RuntimeError(
                f"router mismatch for {intent.value}: expected={expected}, actual={decision.tools}"
            )
        rows.append(
            [
                intent.value,
                ",".join(tool.value for tool in expected) or "none",
                ",".join(tool.value for tool in decision.tools) or "none",
                "yes" if decision.touches_plan else "no",
            ]
        )
    print("Router intent/tool matrix")
    render_table(["intent", "expected_tools", "actual_tools", "touches_plan"], rows)


def plan_spy_table(payload: dict[str, Any]) -> None:
    rows = []
    for case in payload["plan_spy_cases"]:
        analysis = TurnAnalysis.model_validate(case["analysis"])
        decision = route_turn(case["user_turn"], analysis, TripState())
        invoker = SpyInvoker(validator_valid=case["validator_valid"])
        narrator = NarratorSpy()
        execution = execute_route(decision, invoker, narrator=narrator)
        counts = Counter(invoker.calls)
        if any(count != 1 for count in counts.values()):
            raise RuntimeError(f"duplicate tool call in {case['id']}")
        rows.append(
            [
                case["id"],
                ",".join(tool.value for tool in decision.tools),
                ",".join(tool.value for tool in invoker.calls),
                ",".join(f"{tool.value}:{counts[tool]}" for tool in invoker.calls),
                str(narrator.calls),
                execution.stop_reason.value if execution.stop_reason else "none",
            ]
        )
    print("\nPlan-safety spy runs")
    render_table(
        ["case", "route", "observed_calls", "call_counts", "narrator_calls", "stop"],
        rows,
    )


def source_boundary_table(payload: dict[str, Any]) -> None:
    rows = []
    for case in payload["source_boundary_cases"]:
        decision = route_turn(
            case["user_turn"],
            TurnAnalysis(intents=[Intent(case["analysis_intent"])]),
            TripState(),
        )
        invoker = SpyInvoker()
        execute_route(decision, invoker)
        expected = [ToolName(tool) for tool in case["expected_calls"]]
        if invoker.calls != expected:
            raise RuntimeError(f"source-boundary call mismatch for {case['id']}")
        rows.append(
            [
                case["id"],
                ",".join(tool.value for tool in invoker.calls),
                str(invoker.calls.count(ToolName.RETRIEVER)),
            ]
        )
    print("\nSource-boundary spy runs")
    render_table(["case", "observed_calls", "retriever_calls"], rows)


def main() -> None:
    payload = load_cases()
    intent_table(payload)
    plan_spy_table(payload)
    source_boundary_table(payload)
    if "app.llm.openai_provider" in sys.modules:
        raise RuntimeError("router evaluation imported the OpenAI provider")
    print("\nLLM clients constructed: 0")


if __name__ == "__main__":
    main()

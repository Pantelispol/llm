"""Minimal conversation CLI.

This is not a second planner demo: every turn runs the same
understand → route → gather → plan → validate → narrate → post-check pipeline
the HTTP endpoint runs, feeding each returned `TripState` into the next call.
It exists to prove the architecture end to end, not to be a user interface.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings
from app.domain.models import TripState
from app.orchestrator.models import PipelineResult
from app.orchestrator.pipeline import ConversationPipeline, build_pipeline

ATHENS = ZoneInfo("Europe/Athens")

SCENARIOS: dict[str, tuple[str, ...]] = {
    "assignment": (
        "five hours tomorrow",
        "no museum",
        "with my 10-year-old",
    ),
}


def turn_call_names(index: int) -> tuple[str, str]:
    """Each turn gets its own fixture namespace, so turn one cannot replay turn two."""
    return f"conversation_turn{index}_understand", f"conversation_turn{index}_narrate"


async def run_scenario(
    turns: tuple[str, ...],
    *,
    now: datetime,
    settings: Settings | None = None,
    pipeline_factory: Callable[..., ConversationPipeline] = build_pipeline,
) -> list[tuple[str, PipelineResult]]:
    """Feed each returned TripState into the next turn; nothing else crosses turns."""
    resolved = settings or get_settings()
    state = TripState()
    transcript: list[tuple[str, PipelineResult]] = []
    for index, turn in enumerate(turns, start=1):
        understand_call, narrate_call = turn_call_names(index)
        pipeline = pipeline_factory(
            resolved,
            understand_call_name=understand_call,
            narrate_call_name=narrate_call,
        )
        result = await pipeline.run_turn(turn, state, now=now)
        state = result.trip_state
        transcript.append((turn, result))
    return transcript


def print_turn(index: int, turn: str, result: PipelineResult) -> None:
    print(f"\n{'=' * 72}")
    print(f"Turn {index} — you: {turn}")
    print(f"{'=' * 72}")
    print(result.answer)
    print("\n-- state --")
    state = result.trip_state
    window = state.time_window
    print(f"  itinerary_version : {result.itinerary_version}")
    print(f"  language          : {result.language.value}")
    if window is not None:
        print(
            f"  time_window       : {window.start:%Y-%m-%d %H:%M} to "
            f"{window.end:%H:%M} {window.start.tzname()}"
        )
    else:
        print("  time_window       : -")
    print(
        f"  party             : {state.party.adults} adult(s), "
        f"children {state.party.children_ages}"
    )
    print(f"  interests         : {state.interests or '-'}")
    print(f"  exclude_categories: {state.exclude_categories or '-'}")
    print(f"  exclude_poi_ids   : {state.exclude_poi_ids or '-'}")
    if state.itinerary:
        print("  plan              :")
        for position, activity in enumerate(state.itinerary.activities, start=1):
            label = activity.poi_id or activity.area_label or activity.kind.value
            print(
                f"    {position}. {activity.start:%H:%M}-{activity.end:%H:%M} "
                f"{activity.kind.value}: {label}"
            )
        print(f"  total_travel      : {state.itinerary.total_travel_minutes} min")
    print("\n-- turn trace --")
    print(f"  stages            : {' -> '.join(stage.value for stage in result.stages)}")
    print(f"  tools             : {', '.join(tool.value for tool in result.tools_called) or '-'}")
    print(f"  plan_committed    : {result.plan_committed}")
    print(f"  used_fallback     : {result.used_fallback} ({result.failure_category or 'none'})")
    print(f"  postcheck_codes   : {result.postcheck_codes or '-'}")
    print(f"  violations        : {[item.code.value for item in result.violations] or '-'}")
    print(f"  disclosures       : {[item.value for item in result.disclosures] or '-'}")
    usage_line = ", ".join(
        f"{usage.model_id}/{usage.reasoning_effort} in={usage.input_tokens} "
        f"cached={usage.cached_input_tokens} out={usage.output_tokens}"
        for usage in result.usages
    )
    print(f"  usage             : {usage_line or '-'}")
    for assumption in result.assumptions:
        print(f"  assumption        : {assumption}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a scripted conversation scenario")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=None,
        help="ISO 8601 instant injected as 'now'; defaults to the current Athens time",
    )
    args = parser.parse_args()
    now = args.now or datetime.now(ATHENS)
    if now.tzinfo is None:
        raise SystemExit("--now must include a timezone offset")

    print(f"Scenario: {args.scenario}")
    print(f"Injected now: {now.isoformat()}")
    transcript = asyncio.run(run_scenario(SCENARIOS[args.scenario], now=now))
    for index, (turn, result) in enumerate(transcript, start=1):
        print_turn(index, turn, result)


if __name__ == "__main__":
    main()

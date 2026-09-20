from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.domain.models import Intent, TripState, TurnAnalysis
from app.domain.ports import LLMResult, LLMUsage
from app.llm.openai_provider import (
    LLMOutputError,
    LLMResponseError,
    OpenAIProvider,
    strict_json_schema,
)
from app.llm.prompting import build_stable_prefix
from app.llm.record_replay import (
    FixtureStore,
    LLMFixture,
    RecordedResponse,
)
from app.llm.understand import (
    FALLBACK_QUESTION,
    PROMPT_VERSION,
    Understander,
    canonical_dynamic_input,
)
from app.tools.catalog import CatalogRepository


def usage() -> LLMUsage:
    return LLMUsage(
        model_id="gpt-5.6-luna",
        reasoning_effort="none",
        input_tokens=100,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=20,
        reasoning_tokens=0,
        latency_ms=4.0,
    )


class StaticProvider:
    def __init__(self, output: TurnAnalysis) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    async def structured(self, **kwargs: Any) -> LLMResult[TurnAnalysis]:
        self.calls.append(kwargs)
        return LLMResult(output=self.output, usage=usage())

    async def text(self, **kwargs: Any) -> LLMResult[str]:
        raise AssertionError("understand must not request text output")


class RaisingProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def structured(self, **kwargs: Any) -> LLMResult[TurnAnalysis]:
        raise self.error

    async def text(self, **kwargs: Any) -> LLMResult[str]:
        raise AssertionError("understand must not request text output")


class FakeResponses:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output_text": self.outputs.pop(0),
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }


class FakeClient:
    def __init__(self, outputs: list[str]) -> None:
        self.responses = FakeResponses(outputs)


@pytest.fixture
def catalog():
    return CatalogRepository().catalog


@pytest.mark.asyncio
async def test_understand_sends_user_turn_and_trip_state_after_stable_prefix(catalog) -> None:
    provider = StaticProvider(TurnAnalysis(intents=[Intent.EDIT_PLAN]))
    understander = Understander(provider, catalog)
    state = TripState(interests=["roman history"], itinerary_version=0)

    await understander.analyze("Replace the second stop.", state)

    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["system_prompt"] == understander.system_prompt
    payload = json.loads(
        call["user_prompt"].removeprefix("[TURN_INPUT]\n").removesuffix(
            "\n[/TURN_INPUT]"
        )
    )
    assert payload["user_turn"] == "Replace the second stop."
    assert payload["trip_state"] == state.model_dump(mode="json")
    assert call["output_type"] is TurnAnalysis


@pytest.mark.asyncio
async def test_understand_uses_luna_with_none_effort(catalog) -> None:
    output = TurnAnalysis(intents=[Intent.TOURISM_QA]).model_dump_json()
    client = FakeClient([output])
    settings = Settings(
        llm_mode="live",
        llm_api_key="test-key",
        llm_max_output_tokens=321,
        understand_model="gpt-5.6-luna",
        understand_reasoning_effort="none",
    )
    understander = Understander.from_settings(settings, catalog, client=client)

    await understander.analyze("Tell me about the Rotunda.", TripState())

    request = client.responses.requests[0]
    assert request["model"] == "gpt-5.6-luna"
    assert request["reasoning"] == {"effort": "none"}
    assert request["max_output_tokens"] == 321


@pytest.mark.asyncio
async def test_understand_returns_valid_turn_analysis(catalog) -> None:
    expected = TurnAnalysis(
        intents=[Intent.CREATE_PLAN],
        entities={"requested_date_text": "tomorrow"},
        needs_clarification=True,
        clarifying_question="What time would you like to start?",
    )
    result = await Understander(StaticProvider(expected), catalog).analyze(
        "Plan tomorrow.", TripState()
    )

    assert result.analysis == expected
    assert result.used_fallback is False
    assert result.failure_category is None
    assert result.usage == usage()


def test_turn_analysis_schema_uses_supported_any_of_for_plan_edits() -> None:
    schema_text = json.dumps(strict_json_schema(TurnAnalysis))

    assert '"anyOf"' in schema_text
    assert '"oneOf"' not in schema_text
    assert '"discriminator"' not in schema_text


@pytest.mark.asyncio
async def test_understand_resolves_only_catalog_poi_ids(catalog) -> None:
    model_output = TurnAnalysis(
        intents=[Intent.TOURISM_QA],
        entities={
            "mentioned_poi_ids": ["rotunda", "invented_id"],
            "unresolved_place_names": ["Kamara", "Atlantis Palace"],
        },
        constraint_updates={"add_exclude_poi_ids": ["white_tower", "invented_id"]},
    )

    result = await Understander(StaticProvider(model_output), catalog).analyze(
        "Tell me about Kamara and Atlantis Palace.", TripState()
    )

    assert result.analysis.entities.mentioned_poi_ids == ["rotunda", "arch_of_galerius"]
    assert result.analysis.entities.unresolved_place_names == ["Atlantis Palace"]
    assert result.analysis.constraint_updates.add_exclude_poi_ids == ["white_tower"]


@pytest.mark.asyncio
async def test_understand_invalid_output_retries_once_then_uses_fallback(catalog) -> None:
    client = FakeClient(["{}", "{}"])
    provider = OpenAIProvider(
        model_id="gpt-5.6-luna",
        reasoning_effort="none",
        api_key="test-key",
        max_output_tokens=128,
        mode="live",
        call_name="invalid_understand",
        prompt_version=PROMPT_VERSION,
        catalog=catalog,
        client=client,
    )

    result = await Understander(provider, catalog).analyze(
        "What are the White Tower opening hours?", TripState()
    )

    assert len(client.responses.requests) == 2
    assert "[VALIDATION_FEEDBACK]" in client.responses.requests[1]["input"][1]["content"]
    assert result.analysis.intents == [Intent.OPENING_HOURS_QUESTION]
    assert result.used_fallback is True
    assert result.failure_category == "LLMOutputError"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_name", "error"),
    [
        ("timeout", TimeoutError("timed out")),
        ("refusal", LLMResponseError("refused")),
        ("exhausted_retries", LLMOutputError("invalid twice")),
    ],
)
async def test_understand_provider_failure_never_escapes_to_orchestrator(
    catalog, failure_name: str, error: Exception
) -> None:
    result = await Understander(RaisingProvider(error), catalog).analyze(
        "What is the weather?", TripState()
    )

    assert failure_name
    assert result.analysis.intents == [Intent.WEATHER_QUESTION]
    assert result.used_fallback is True
    assert result.failure_category == type(error).__name__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn", "state", "expected"),
    [
        ("When does the Rotunda open?", TripState(), Intent.OPENING_HOURS_QUESTION),
        ("Πόσο κοστίζει το εισιτήριο;", TripState(), Intent.PRICE_QUESTION),
        ("Create a plan for tomorrow.", TripState(), Intent.CREATE_PLAN),
        ("Remove the second stop.", TripState(), Intent.EDIT_PLAN),
        ("Can these visits fit?", TripState(), Intent.FEASIBILITY_CHECK),
        ("Τι καιρό θα κάνει;", TripState(), Intent.WEATHER_QUESTION),
    ],
)
async def test_fallback_preserves_plan_and_catalog_safety_routes(
    catalog, turn: str, state: TripState, expected: Intent
) -> None:
    result = await Understander(
        RaisingProvider(TimeoutError("provider unavailable")), catalog
    ).analyze(turn, state)

    assert result.analysis.intents == [expected]
    assert result.analysis.plan_edits == []
    assert result.used_fallback is True


@pytest.mark.asyncio
async def test_fallback_unknown_place_never_fabricates_poi_id(catalog) -> None:
    result = await Understander(
        RaisingProvider(LLMOutputError("invalid twice")), catalog
    ).analyze("Tell me about Atlantis Palace.", TripState())

    assert result.analysis.entities.mentioned_poi_ids == []
    assert result.analysis.entities.unresolved_place_names == ["Atlantis Palace"]
    assert result.analysis.intents == [Intent.OUT_OF_SCOPE]
    assert result.analysis.needs_clarification is True
    assert result.analysis.clarifying_question == FALLBACK_QUESTION


@pytest.mark.asyncio
async def test_understand_replay_is_deterministic_offline(catalog, tmp_path: Path) -> None:
    store = FixtureStore(tmp_path)
    turn = "Tell me about the Rotunda."
    state = TripState()
    output = TurnAnalysis(
        intents=[Intent.TOURISM_QA],
        entities={"mentioned_poi_ids": ["rotunda"]},
    )
    provider = OpenAIProvider(
        model_id="gpt-5.6-luna",
        reasoning_effort="none",
        api_key="",
        max_output_tokens=128,
        mode="replay",
        call_name="deterministic_understand",
        prompt_version=PROMPT_VERSION,
        catalog=catalog,
        fixture_store=store,
    )
    understander = Understander(provider, catalog)
    dynamic_input = canonical_dynamic_input(turn, state)
    identity = provider._identity(
        stable_prefix=build_stable_prefix(understander.system_prompt, catalog),
        dynamic_input=dynamic_input,
        output_format="json_schema",
        schema_name_value="TurnAnalysis",
        schema=strict_json_schema(TurnAnalysis),
    )
    store.write(
        LLMFixture(
            identity=identity,
            response=RecordedResponse(
                status="completed",
                model_id="gpt-5.6-luna",
                output_text=output.model_dump_json(),
                usage=usage(),
            ),
            parsed_output=output.model_dump(mode="json"),
        )
    )

    first = await understander.analyze(turn, state)
    second = await understander.analyze(turn, state)

    assert first == second
    assert first.analysis == output
    assert provider._client is None

from __future__ import annotations

from io import StringIO
from logging import Formatter, Logger, StreamHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from app.config import Settings
from app.llm.openai_provider import (
    LLMOutputError,
    LLMResponseError,
    OpenAIProvider,
    strict_json_schema,
)
from app.llm.record_replay import FixtureStore
from app.llm.redaction import SecretRedactionFilter
from app.tools.catalog import CatalogRepository


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    count: int


class Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class OptionalContainer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nested: Nested | None = None
    tags: list[str] = []


def response(
    output_text: str,
    *,
    status: str = "completed",
    output: list[Any] | None = None,
    input_tokens: int = 100,
    cached_tokens: int = 10,
    cache_write_tokens: int = 20,
    output_tokens: int = 12,
    reasoning_tokens: int = 4,
) -> SimpleNamespace:
    return SimpleNamespace(
        model="gpt-5.6-luna",
        status=status,
        output_text=output_text,
        output=output or [],
        incomplete_details=(
            SimpleNamespace(reason="max_output_tokens") if status == "incomplete" else None
        ),
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            input_tokens_details=SimpleNamespace(
                cached_tokens=cached_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
            output_tokens=output_tokens,
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
    )


class FakeResponses:
    def __init__(self, queued: list[SimpleNamespace]) -> None:
        self.queued = list(queued)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> SimpleNamespace:
        self.calls.append(request)
        return self.queued.pop(0)


class FakeClient:
    def __init__(self, queued: list[SimpleNamespace]) -> None:
        self.responses = FakeResponses(queued)


def provider(
    tmp_path: Path,
    queued: list[SimpleNamespace],
    *,
    mode: str = "live",
    max_output_tokens: int = 64,
) -> tuple[OpenAIProvider, FakeClient]:
    client = FakeClient(queued)
    adapter = OpenAIProvider(
        model_id="gpt-5.6-luna",
        reasoning_effort="none",
        api_key=SecretStr("llm-secret"),
        max_output_tokens=max_output_tokens,
        mode=mode,  # type: ignore[arg-type]
        call_name="test_call",
        prompt_version="test.v1",
        catalog=CatalogRepository().catalog,
        fixture_store=FixtureStore(tmp_path),
        client=client,
    )
    return adapter, client


@pytest.mark.asyncio
async def test_structured_request_uses_responses_json_schema(tmp_path: Path) -> None:
    adapter, client = provider(tmp_path, [response('{"label":"ok","count":1}')])

    result = await adapter.structured(
        system_prompt="Stable instructions",
        user_prompt="Dynamic input",
        output_type=Extraction,
    )

    request = client.responses.calls[0]
    assert result.output == Extraction(label="ok", count=1)
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["store"] is False
    assert request["service_tier"] == "default"


def test_structured_schema_requires_fields_and_forbids_additional_properties() -> None:
    schema = strict_json_schema(OptionalContainer)

    assert schema["required"] == ["nested", "tags"]
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Nested"]["required"] == ["value"]
    assert schema["$defs"]["Nested"]["additionalProperties"] is False
    assert "null" in schema["properties"]["nested"]["anyOf"][1]["type"]


@pytest.mark.asyncio
async def test_every_request_sets_reasoning_effort_explicitly(tmp_path: Path) -> None:
    adapter, client = provider(
        tmp_path,
        [response('{"label":"ok","count":1}'), response("hello")],
    )

    await adapter.structured(
        system_prompt="Stable instructions",
        user_prompt="Dynamic input",
        output_type=Extraction,
    )
    await adapter.text(system_prompt="Stable instructions", user_prompt="Dynamic input")

    assert [call["reasoning"] for call in client.responses.calls] == [
        {"effort": "none"},
        {"effort": "none"},
    ]
    assert [call["max_output_tokens"] for call in client.responses.calls] == [64, 64]


@pytest.mark.asyncio
async def test_stable_prefix_precedes_dynamic_input_and_has_cache_breakpoint(
    tmp_path: Path,
) -> None:
    adapter, client = provider(tmp_path, [response("hello")])

    await adapter.text(system_prompt="Stable instructions", user_prompt="Dynamic input")

    request = client.responses.calls[0]
    developer, user = request["input"]
    assert developer["role"] == "developer"
    assert developer["content"][0]["text"].startswith("Stable instructions")
    assert "[CATALOG_SUMMARY]" in developer["content"][0]["text"]
    assert developer["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert user == {"role": "user", "content": "Dynamic input"}
    assert request["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}


@pytest.mark.asyncio
async def test_usage_captures_model_effort_token_breakdown_and_latency(tmp_path: Path) -> None:
    adapter, _client = provider(
        tmp_path,
        [
            response(
                "hello",
                input_tokens=120,
                cached_tokens=40,
                cache_write_tokens=20,
                output_tokens=30,
                reasoning_tokens=8,
            )
        ],
    )

    result = await adapter.text(system_prompt="Stable", user_prompt="Dynamic")

    assert result.usage.model_id == "gpt-5.6-luna"
    assert result.usage.reasoning_effort == "none"
    assert result.usage.input_tokens == 120
    assert result.usage.ordinary_input_tokens == 60
    assert result.usage.cached_input_tokens == 40
    assert result.usage.cache_write_tokens == 20
    assert result.usage.output_tokens == 30
    assert result.usage.reasoning_tokens == 8
    assert result.usage.latency_ms >= 0


@pytest.mark.asyncio
async def test_invalid_structured_output_retries_once_with_validation_error(
    tmp_path: Path,
) -> None:
    adapter, client = provider(
        tmp_path,
        [response('{"label":"missing count"}'), response('{"label":"ok","count":1}')],
    )

    result = await adapter.structured(
        system_prompt="Stable",
        user_prompt="Dynamic",
        output_type=Extraction,
    )

    assert result.output.count == 1
    assert len(client.responses.calls) == 2
    first, second = client.responses.calls
    assert first["input"][0] == second["input"][0]
    assert "[VALIDATION_FEEDBACK]" in second["input"][1]["content"]


@pytest.mark.asyncio
async def test_invalid_structured_output_never_retries_more_than_once(tmp_path: Path) -> None:
    adapter, client = provider(
        tmp_path,
        [response("{}"), response("{}"), response('{"label":"unused","count":3}')],
    )

    with pytest.raises(LLMOutputError, match="after one retry"):
        await adapter.structured(
            system_prompt="Stable",
            user_prompt="Dynamic",
            output_type=Extraction,
        )

    assert len(client.responses.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refusal", "incomplete"])
async def test_refusal_or_incomplete_response_is_not_accepted_as_output(
    tmp_path: Path,
    kind: str,
) -> None:
    if kind == "refusal":
        item = SimpleNamespace(
            content=[SimpleNamespace(type="refusal", refusal="not allowed")]
        )
        queued = [response("", output=[item])]
    else:
        queued = [response("partial", status="incomplete")]
    adapter, client = provider(tmp_path, queued)

    with pytest.raises(LLMResponseError):
        await adapter.text(system_prompt="Stable", user_prompt="Dynamic")

    assert len(client.responses.calls) == 1


def test_client_uses_llm_api_key_and_ignores_ambient_openai_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-key")
    captured: dict[str, Any] = {}

    def client_factory(**kwargs: Any) -> FakeClient:
        captured.update(kwargs)
        return FakeClient([])

    settings = Settings(
        _env_file=None,
        llm_mode="live",
        llm_api_key=SecretStr("configured-llm-key"),
    )
    OpenAIProvider.from_settings(
        settings,
        profile="understand",
        call_name="key_test",
        prompt_version="test.v1",
        catalog=CatalogRepository().catalog,
        fixture_store=FixtureStore(tmp_path),
        client_factory=client_factory,
    )

    assert captured == {"api_key": "configured-llm-key", "max_retries": 0}
    assert captured["api_key"] != "ambient-openai-key"


@pytest.mark.parametrize("mode", ["live", "record"])
def test_live_and_record_modes_require_llm_api_key(mode: str) -> None:
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        Settings(_env_file=None, llm_mode=mode, llm_api_key="")


def test_default_settings_use_replay_without_a_key() -> None:
    settings = Settings(_env_file=None, llm_mode="replay", llm_api_key="")
    assert settings.llm_mode == "replay"


def test_logs_redact_llm_api_key_and_bearer_token() -> None:
    secret = "configured-llm-secret"
    stream = StringIO()
    handler = StreamHandler(stream)
    handler.setFormatter(Formatter("%(message)s %(credential)s"))
    redactor = SecretRedactionFilter(SecretStr(secret))
    handler.addFilter(redactor)
    logger = Logger("redaction-test")
    logger.addHandler(handler)

    try:
        raise RuntimeError(f"failure contained {secret}")
    except RuntimeError:
        logger.exception(
            "key=%s Bearer ambient-token",
            secret,
            extra={"credential": secret},
        )

    rendered = stream.getvalue()
    assert secret not in rendered
    assert "ambient-token" not in rendered
    assert rendered.count("[REDACTED]") >= 3

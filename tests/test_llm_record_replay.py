from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from app.llm.openai_provider import OpenAIProvider
from app.llm.record_replay import FixtureStore, ReplayFixtureNotFound
from app.tools.catalog import CatalogRepository


class ReplayOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class FakeResponses:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **_request: Any) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            model="gpt-5.6-luna",
            status="completed",
            output_text='{"value":"recorded"}',
            output=[],
            incomplete_details=None,
            usage=SimpleNamespace(
                input_tokens=50,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=0,
                    cache_write_tokens=50,
                ),
                output_tokens=6,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


def make_provider(
    tmp_path: Path,
    *,
    mode: str,
    client: Any | None = None,
    key: str = "fixture-secret",
    client_factory: Any = None,
) -> OpenAIProvider:
    kwargs: dict[str, Any] = {}
    if client_factory is not None:
        kwargs["client_factory"] = client_factory
    return OpenAIProvider(
        model_id="gpt-5.6-luna",
        reasoning_effort="none",
        api_key=SecretStr(key),
        max_output_tokens=64,
        mode=mode,  # type: ignore[arg-type]
        call_name="replay_test",
        prompt_version="test.v1",
        catalog=CatalogRepository().catalog,
        fixture_store=FixtureStore(tmp_path),
        client=client,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_replay_mode_never_calls_openai(tmp_path: Path) -> None:
    live_client = FakeClient()
    recorder = make_provider(tmp_path, mode="record", client=live_client)
    recorded = await recorder.structured(
        system_prompt="Stable",
        user_prompt="Dynamic",
        output_type=ReplayOutput,
    )
    assert live_client.responses.calls == 1

    def fail_factory(**_kwargs: Any) -> None:
        raise AssertionError("replay attempted to construct an OpenAI client")

    replay = make_provider(tmp_path, mode="replay", client_factory=fail_factory)
    result = await replay.structured(
        system_prompt="Stable",
        user_prompt="Dynamic",
        output_type=ReplayOutput,
    )

    assert result == recorded


@pytest.mark.asyncio
async def test_missing_replay_fixture_does_not_fall_through_to_live(tmp_path: Path) -> None:
    def fail_factory(**_kwargs: Any) -> None:
        raise AssertionError("missing fixture attempted to construct a client")

    replay = make_provider(tmp_path, mode="replay", client_factory=fail_factory)

    with pytest.raises(ReplayFixtureNotFound, match="will not call OpenAI"):
        await replay.structured(
            system_prompt="Stable",
            user_prompt="unrecorded dynamic input",
            output_type=ReplayOutput,
        )


@pytest.mark.asyncio
async def test_recorded_fixture_contains_no_api_key_or_authorization_header(
    tmp_path: Path,
) -> None:
    key = "fixture-secret-value"
    recorder = make_provider(tmp_path, mode="record", client=FakeClient(), key=key)

    await recorder.structured(
        system_prompt="Stable",
        user_prompt="Dynamic",
        output_type=ReplayOutput,
    )

    fixture_text = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert key not in fixture_text
    assert "authorization" not in fixture_text.lower()
    assert "bearer " not in fixture_text.lower()

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, SecretStr, ValidationError

from app.config import LLMMode, ReasoningEffort, Settings
from app.domain.catalog import PoiCatalog
from app.domain.ports import LLMResult, LLMUsage, StructuredOutput
from app.llm.prompting import build_stable_prefix
from app.llm.record_replay import (
    FixtureAlreadyExists,
    FixtureIdentity,
    FixtureStore,
    LiveCallBudget,
    LLMFixture,
    RecordedResponse,
    sha256_json,
    sha256_text,
)
from app.llm.redaction import install_redaction_filter

SUPPORTED_MODELS = {"gpt-5.6-luna", "gpt-5.6-terra"}
SUPPORTED_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
MAX_STRUCTURED_ATTEMPTS = 2
DEFAULT_FIXTURE_ROOT = Path(__file__).parents[2] / "evals" / "fixtures" / "llm"


class LLMProviderError(RuntimeError):
    pass


class LLMTransportError(LLMProviderError):
    pass


class LLMResponseError(LLMProviderError):
    pass


class LLMOutputError(LLMProviderError):
    pass


def strict_json_schema(output_type: type[BaseModel]) -> dict[str, Any]:
    schema = output_type.model_json_schema(mode="serialization")

    def normalize(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("discriminator", None)
            if "oneOf" in node:
                node["anyOf"] = node.pop("oneOf")
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                normalize(value)
        elif isinstance(node, list):
            for value in node:
                normalize(value)

    normalize(schema)
    return schema


def schema_name(output_type: type[BaseModel]) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", output_type.__name__).strip("_")
    return (value or "structured_output")[:64]


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _refusal_text(response: Any) -> str | None:
    for item in _attribute(response, "output", []) or []:
        for content in _attribute(item, "content", []) or []:
            if _attribute(content, "type") == "refusal":
                refusal = _attribute(content, "refusal", "Model refused the request")
                return str(refusal)
    return None


def _validation_summary(error: ValidationError) -> str:
    parts = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "output"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)[:1000]


class OpenAIProvider:
    """Responses API adapter with explicit effort, bounded retries, and replay."""

    def __init__(
        self,
        *,
        model_id: str,
        reasoning_effort: ReasoningEffort,
        api_key: SecretStr | str,
        max_output_tokens: int,
        mode: LLMMode,
        call_name: str,
        prompt_version: str,
        catalog: PoiCatalog,
        fixture_store: FixtureStore | None = None,
        client_factory: Callable[..., Any] = AsyncOpenAI,
        client: Any | None = None,
        live_call_budget: LiveCallBudget | None = None,
    ) -> None:
        if model_id not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported OpenAI model: {model_id}")
        if reasoning_effort not in SUPPORTED_EFFORTS:
            raise ValueError(f"unsupported reasoning effort: {reasoning_effort}")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        key_value = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if mode in {"live", "record"} and not key_value:
            raise ValueError("LLM_API_KEY is required when LLM_MODE is live or record")

        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.mode = mode
        self.call_name = call_name
        self.prompt_version = prompt_version
        self.catalog = catalog
        self.fixture_store = fixture_store or FixtureStore(DEFAULT_FIXTURE_ROOT)
        self.live_call_budget = live_call_budget
        self.last_fixture_path: Path | None = None
        install_redaction_filter(api_key)

        self._client = None
        if mode in {"live", "record"}:
            self._client = client or client_factory(api_key=key_value, max_retries=0)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        profile: Literal["understand", "narrate"],
        call_name: str,
        prompt_version: str,
        catalog: PoiCatalog,
        fixture_store: FixtureStore | None = None,
        client_factory: Callable[..., Any] = AsyncOpenAI,
        client: Any | None = None,
        live_call_budget: LiveCallBudget | None = None,
    ) -> OpenAIProvider:
        if profile == "understand":
            model_id = settings.understand_model
            effort = settings.understand_reasoning_effort
        else:
            model_id = settings.narrate_model
            effort = settings.narrate_reasoning_effort
        return cls(
            model_id=model_id,
            reasoning_effort=effort,
            api_key=settings.llm_api_key,
            max_output_tokens=settings.llm_max_output_tokens,
            mode=settings.llm_mode,
            call_name=call_name,
            prompt_version=prompt_version,
            catalog=catalog,
            fixture_store=fixture_store,
            client_factory=client_factory,
            client=client,
            live_call_budget=live_call_budget,
        )

    async def structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_type: type[StructuredOutput],
    ) -> LLMResult[StructuredOutput]:
        stable_prefix = build_stable_prefix(system_prompt, self.catalog)
        schema = strict_json_schema(output_type)
        output_schema_name = schema_name(output_type)
        dynamic_input = user_prompt
        last_error = "invalid structured output"

        for attempt in range(MAX_STRUCTURED_ATTEMPTS):
            identity = self._identity(
                stable_prefix=stable_prefix,
                dynamic_input=dynamic_input,
                output_format="json_schema",
                schema_name_value=output_schema_name,
                schema=schema,
            )
            request = self._request(
                stable_prefix=stable_prefix,
                dynamic_input=dynamic_input,
                text_format={
                    "type": "json_schema",
                    "name": output_schema_name,
                    "strict": True,
                    "schema": schema,
                },
            )
            response = await self._exchange(identity, request)
            try:
                self._ensure_usable(response)
                parsed = output_type.model_validate_json(response.output_text)
            except ValidationError as error:
                last_error = _validation_summary(error)
                self._record(identity, response, parsed_output=None)
                if attempt + 1 == MAX_STRUCTURED_ATTEMPTS:
                    raise LLMOutputError(
                        f"structured output remained invalid after one retry: {last_error}"
                    ) from None
                dynamic_input = (
                    f"{user_prompt}\n\n"
                    "[VALIDATION_FEEDBACK]\n"
                    f"Previous output was invalid: {last_error}\n"
                    "Return corrected JSON matching the schema.\n"
                    "[/VALIDATION_FEEDBACK]"
                )
                continue
            except LLMResponseError:
                self._record(identity, response, parsed_output=None)
                raise

            self._record(identity, response, parsed_output=parsed.model_dump(mode="json"))
            return LLMResult(output=parsed, usage=response.usage)

        raise LLMOutputError(last_error)

    async def text(self, *, system_prompt: str, user_prompt: str) -> LLMResult[str]:
        stable_prefix = build_stable_prefix(system_prompt, self.catalog)
        identity = self._identity(
            stable_prefix=stable_prefix,
            dynamic_input=user_prompt,
            output_format="text",
        )
        request = self._request(
            stable_prefix=stable_prefix,
            dynamic_input=user_prompt,
            text_format={"type": "text"},
        )
        response = await self._exchange(identity, request)
        try:
            self._ensure_usable(response)
        except LLMResponseError:
            self._record(identity, response, parsed_output=None)
            raise
        self._record(identity, response, parsed_output=response.output_text)
        return LLMResult(output=response.output_text, usage=response.usage)

    def _request(
        self,
        *,
        stable_prefix: str,
        dynamic_input: str,
        text_format: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "reasoning": {"effort": self.reasoning_effort},
            "input": [
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": stable_prefix,
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ],
                },
                {"role": "user", "content": dynamic_input},
            ],
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
            "text": {"format": text_format},
            "max_output_tokens": self.max_output_tokens,
            "service_tier": "default",
            "store": False,
        }

    def _identity(
        self,
        *,
        stable_prefix: str,
        dynamic_input: str,
        output_format: str,
        schema_name_value: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> FixtureIdentity:
        return FixtureIdentity(
            call_name=self.call_name,
            prompt_version=self.prompt_version,
            model_id=self.model_id,
            reasoning_effort=self.reasoning_effort,
            output_format=output_format,
            schema_name=schema_name_value,
            schema_sha256=sha256_json(schema) if schema is not None else None,
            stable_prefix_sha256=sha256_text(stable_prefix),
            dynamic_input_sha256=sha256_text(dynamic_input),
        )

    async def _exchange(
        self,
        identity: FixtureIdentity,
        request: dict[str, Any],
    ) -> RecordedResponse:
        if self.mode == "replay":
            fixture, path = self.fixture_store.load(identity)
            self.last_fixture_path = path
            return fixture.response

        if self.mode == "record":
            path = self.fixture_store.path_for(identity)
            if path.exists():
                raise FixtureAlreadyExists(
                    f"refusing to make a live call that would overwrite {path.name}"
                )

        if self._client is None:
            raise LLMTransportError("OpenAI client is unavailable outside replay mode")
        if self.live_call_budget is not None:
            self.live_call_budget.consume()

        started = perf_counter()
        try:
            raw_response = await self._client.responses.create(**request)
        except Exception as error:
            error_name = type(error).__name__
            raise LLMTransportError(f"OpenAI Responses request failed ({error_name})") from None
        latency_ms = (perf_counter() - started) * 1000
        return self._to_recorded_response(raw_response, latency_ms)

    def _to_recorded_response(self, response: Any, latency_ms: float) -> RecordedResponse:
        usage = _attribute(response, "usage")
        if usage is None:
            raise LLMResponseError("OpenAI response did not include usage")
        input_details = _attribute(usage, "input_tokens_details")
        output_details = _attribute(usage, "output_tokens_details")
        model_id = str(_attribute(response, "model", self.model_id))
        mapped_usage = LLMUsage(
            model_id=model_id,
            reasoning_effort=self.reasoning_effort,
            input_tokens=int(_attribute(usage, "input_tokens", 0)),
            cached_input_tokens=int(_attribute(input_details, "cached_tokens", 0) or 0),
            cache_write_tokens=int(_attribute(input_details, "cache_write_tokens", 0) or 0),
            output_tokens=int(_attribute(usage, "output_tokens", 0)),
            reasoning_tokens=int(_attribute(output_details, "reasoning_tokens", 0) or 0),
            latency_ms=latency_ms,
        )
        incomplete = _attribute(response, "incomplete_details")
        return RecordedResponse(
            status=str(_attribute(response, "status", "completed")),
            model_id=model_id,
            output_text=str(_attribute(response, "output_text", "") or ""),
            refusal=_refusal_text(response),
            incomplete_reason=(
                str(_attribute(incomplete, "reason")) if incomplete is not None else None
            ),
            usage=mapped_usage,
        )

    def _ensure_usable(self, response: RecordedResponse) -> None:
        if response.refusal:
            raise LLMResponseError("OpenAI response was a refusal")
        if response.status != "completed":
            detail = response.incomplete_reason or response.status
            raise LLMResponseError(f"OpenAI response was not completed ({detail})")
        if not response.output_text.strip():
            raise LLMResponseError("OpenAI response did not contain output text")

    def _record(
        self,
        identity: FixtureIdentity,
        response: RecordedResponse,
        *,
        parsed_output: Any,
    ) -> None:
        if self.mode != "record":
            return
        fixture = LLMFixture(
            identity=identity,
            response=response,
            parsed_output=parsed_output,
        )
        self.last_fixture_path = self.fixture_store.write(fixture)

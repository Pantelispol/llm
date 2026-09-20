from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain.ports import LLMUsage


class FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FixtureIdentity(FixtureModel):
    call_name: str
    prompt_version: str
    model_id: str
    reasoning_effort: str
    output_format: str
    schema_name: str | None = None
    schema_sha256: str | None = None
    stable_prefix_sha256: str
    dynamic_input_sha256: str

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class RecordedResponse(FixtureModel):
    status: str
    model_id: str
    output_text: str
    refusal: str | None = None
    incomplete_reason: str | None = None
    usage: LLMUsage


class LLMFixture(FixtureModel):
    identity: FixtureIdentity
    response: RecordedResponse
    parsed_output: Any = None


class ReplayFixtureError(RuntimeError):
    pass


class ReplayFixtureNotFound(ReplayFixtureError):
    pass


class FixtureAlreadyExists(ReplayFixtureError):
    pass


class LiveCallBudgetExceeded(RuntimeError):
    pass


class LiveCallBudget:
    def __init__(self, max_calls: int) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.max_calls = max_calls
        self.calls_used = 0

    def consume(self) -> None:
        if self.calls_used >= self.max_calls:
            raise LiveCallBudgetExceeded(
                f"live call budget exhausted ({self.calls_used}/{self.max_calls})"
            )
        self.calls_used += 1


class FixtureStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, identity: FixtureIdentity) -> Path:
        call_slug = re.sub(r"[^a-z0-9]+", "-", identity.call_name.lower()).strip("-")
        format_slug = re.sub(r"[^a-z0-9]+", "-", identity.output_format.lower()).strip("-")
        return self.root / f"{call_slug}--{format_slug}--{identity.digest[:16]}.json"

    def load(self, identity: FixtureIdentity) -> tuple[LLMFixture, Path]:
        path = self.path_for(identity)
        if not path.is_file():
            raise ReplayFixtureNotFound(
                f"missing LLM replay fixture: {path.name}; replay mode will not call OpenAI"
            )
        fixture = LLMFixture.model_validate_json(path.read_text(encoding="utf-8"))
        if fixture.identity != identity:
            raise ReplayFixtureError(f"LLM replay fixture identity mismatch: {path.name}")
        return fixture, path

    def write(self, fixture: LLMFixture) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(fixture.identity)
        if path.exists():
            raise FixtureAlreadyExists(
                f"refusing to overwrite recorded LLM fixture: {path.name}"
            )
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            fixture.model_dump_json(indent=2, exclude_none=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sha256_json(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()

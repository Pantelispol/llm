"""HTTP surface.

There is deliberately no UI, no streaming, and no server-side session store.
`/chat` is stateless: the client sends back the `trip_state` it last received,
which is the same contract the CLI uses, so both exercise one pipeline.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.domain.models import (
    AnswerLanguage,
    NarrationDisclosure,
    TripState,
    Violation,
)
from app.domain.ports import LLMUsage
from app.llm.narration_tokens import Citation
from app.orchestrator.models import ToolName
from app.orchestrator.pipeline import ConversationPipeline, build_pipeline

ATHENS = ZoneInfo("Europe/Athens")

app = FastAPI(title="Thessaloniki Tourist Assistant", version="0.1.0")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    trip_state: TripState = Field(default_factory=TripState)


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    trip_state: TripState
    language: AnswerLanguage
    itinerary_version: int
    plan_committed: bool
    tools_called: list[ToolName] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    disclosures: list[NarrationDisclosure] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    used_fallback: bool = False
    understand_fallback: bool = False
    failure_category: str | None = None
    postcheck_codes: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    assumptions: list[str] = Field(default_factory=list)
    usages: list[LLMUsage] = Field(default_factory=list)


@lru_cache
def _cached_pipeline() -> ConversationPipeline:
    return build_pipeline(get_settings())


def get_pipeline(settings: Annotated[Settings, Depends(get_settings)]) -> ConversationPipeline:
    del settings  # Settings are read once when the cached pipeline is built.
    return _cached_pipeline()


def get_now() -> datetime:
    """`now` is injected by the server, never taken from the model."""
    return datetime.now(ATHENS)


PipelineDependency = Annotated[ConversationPipeline, Depends(get_pipeline)]
NowDependency = Annotated[datetime, Depends(get_now)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
async def chat(
    request: ChatRequest,
    pipeline: PipelineDependency,
    now: NowDependency,
) -> ChatResponse:
    result = await pipeline.run_turn(request.message, request.trip_state, now=now)
    return ChatResponse(
        answer=result.answer,
        trip_state=result.trip_state,
        language=result.language,
        itinerary_version=result.itinerary_version,
        plan_committed=result.plan_committed,
        tools_called=result.tools_called,
        violations=result.violations,
        disclosures=result.disclosures,
        citations=result.citations,
        used_fallback=result.used_fallback,
        understand_fallback=result.understand_fallback,
        failure_category=result.failure_category,
        postcheck_codes=result.postcheck_codes,
        needs_clarification=result.needs_clarification,
        assumptions=result.assumptions,
        usages=result.usages,
    )

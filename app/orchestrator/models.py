from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.domain.models import (
    AnswerLanguage,
    EvidenceItem,
    Intent,
    NarrationDisclosure,
    TripState,
    Violation,
)
from app.domain.ports import LLMUsage, PlanningContext, RetrievalHit
from app.llm.narration_tokens import Citation
from app.planning.feasibility import FeasibilityResult


class OrchestratorModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolName(StrEnum):
    CATALOG = "catalog"
    RETRIEVER = "retriever"
    WEATHER = "weather"
    HOURS = "hours"
    TRAVEL = "travel"
    PLANNER = "planner"
    VALIDATOR = "validator"
    FEASIBILITY = "feasibility"


CANONICAL_TOOL_ORDER = (
    ToolName.CATALOG,
    ToolName.RETRIEVER,
    ToolName.WEATHER,
    ToolName.HOURS,
    ToolName.TRAVEL,
    ToolName.PLANNER,
    ToolName.VALIDATOR,
    ToolName.FEASIBILITY,
)


class RouteReasonCode(StrEnum):
    INTENT_POLICY = "intent_policy"
    SOURCE_BOUNDARY = "source_boundary"
    PLAN_SAFETY_OVERRIDE = "plan_safety_override"


class RouteReason(OrchestratorModel):
    code: RouteReasonCode
    detail: str = Field(min_length=1)
    intent: Intent | None = None


class RouteDecision(OrchestratorModel):
    effective_intents: list[Intent] = Field(min_length=1)
    tools: list[ToolName]
    reasons: list[RouteReason] = Field(min_length=1)
    touches_plan: bool = False

    @model_validator(mode="after")
    def tools_are_unique_and_canonical(self) -> Self:
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("route tools must be unique")
        positions = [CANONICAL_TOOL_ORDER.index(tool) for tool in self.tools]
        if positions != sorted(positions):
            raise ValueError("route tools must use canonical order")
        return self


class RouteStopReason(StrEnum):
    PLANNER_OUTPUT_MISSING = "planner_output_missing"
    VALIDATION_FAILED = "validation_failed"


class RouteExecution(OrchestratorModel):
    called_tools: list[ToolName]
    narration_permitted: bool
    narration_invoked: bool
    stop_reason: RouteStopReason | None = None


class PipelineStage(StrEnum):
    UNDERSTAND = "understand"
    ROUTE = "route"
    GATHER = "gather"
    PLAN = "plan"
    FEASIBILITY = "feasibility"
    VALIDATE = "validate"
    NARRATE = "narrate"
    POST_CHECK = "post_check"


REQUIRED_STAGE_ORDER = (
    PipelineStage.UNDERSTAND,
    PipelineStage.ROUTE,
    PipelineStage.GATHER,
    PipelineStage.PLAN,
    PipelineStage.VALIDATE,
    PipelineStage.NARRATE,
    PipelineStage.POST_CHECK,
)


class WeatherProvenance(OrchestratorModel):
    source: str
    is_fixture: bool = False
    unavailable_reason: str | None = None
    summary: str = ""


class HoursFact(OrchestratorModel):
    poi_id: str
    opens_at: AwareDatetime | None = None
    closes_at: AwareDatetime | None = None
    last_entry_at: AwareDatetime | None = None
    source_rule: str | None = None
    needs_verification: bool = False
    open_on_requested_day: bool = False


class TravelProvenance(OrchestratorModel):
    source: str
    approximate: bool = False


class GatheredContext(OrchestratorModel):
    """Everything the ordered tool list returned, with provenance preserved."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    now: AwareDatetime
    plan_date: date
    tools_called: list[ToolName] = Field(default_factory=list)
    catalog_poi_ids: list[str] = Field(default_factory=list)
    weather: WeatherProvenance | None = None
    hours: list[HoursFact] = Field(default_factory=list)
    travel: TravelProvenance | None = None
    retrieval: list[RetrievalHit] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    disclosures: list[NarrationDisclosure] = Field(default_factory=list)
    planning_context: PlanningContext | None = Field(default=None, exclude=True, repr=False)


class PipelineResult(OrchestratorModel):
    """One completed turn: the checked answer plus the state to send back."""

    answer: str
    trip_state: TripState
    language: AnswerLanguage = AnswerLanguage.EN
    stages: list[PipelineStage] = Field(default_factory=list)
    route: RouteDecision | None = None
    tools_called: list[ToolName] = Field(default_factory=list)
    itinerary_version: int = 0
    plan_committed: bool = False
    violations: list[Violation] = Field(default_factory=list)
    disclosures: list[NarrationDisclosure] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    used_fallback: bool = False
    understand_fallback: bool = False
    understand_failure_category: str | None = None
    failure_category: str | None = None
    postcheck_codes: list[str] = Field(default_factory=list)
    narration_invoked: bool = False
    allowed_poi_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    narration_first_draft_passed: bool = False
    narration_retried: bool = False
    first_draft_codes: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    usages: list[LLMUsage] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    feasibility: FeasibilityResult | None = None

"""Grounded narration: the one place the model writes user-facing prose.

The narrator receives a validated plan, the catalog records the route selected,
this call's evidence registry, and the operational facts tools actually
returned. It gets exactly one retry when the deterministic post-check rejects a
draft, and falls back to a template otherwise. It cannot change the plan: the
bundle is read-only input and the rendered answer is derived from it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.domain.catalog import PoiCatalog
from app.domain.models import OPERATIONAL_EVIDENCE_KINDS, NarrationBundle
from app.domain.ports import LLMProvider, LLMUsage
from app.llm.deterministic_narration import narrate_deterministically
from app.llm.narration_tokens import (
    Citation,
    RenderedAnswer,
    TokenRenderError,
    render_tokens,
)
from app.llm.openai_provider import LLMProviderError, OpenAIProvider
from app.llm.record_replay import FixtureStore, LiveCallBudget, ReplayFixtureError
from app.safety.answer_postcheck import PostCheckReport, PostCheckViolation, check_answer

PROMPT_PATH = Path(__file__).parents[2] / "prompts" / "narrate.v2.md"
PROMPT_VERSION = "narrate.v2"
POSTCHECK_FAILED = "postcheck_failed"

logger = logging.getLogger(__name__)


#: Injectable so the pipeline can observe the post-check as its own stage.
AnswerChecker = Callable[[str, NarrationBundle, PoiCatalog], PostCheckReport]


@dataclass(frozen=True)
class _ProviderFailure:
    """Marker for a provider-side failure so it is never mistaken for a draft."""

    category: str


class NarrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[Citation] = Field(default_factory=list)
    used_fallback: bool = False
    failure_category: str | None = None
    violation_codes: list[str] = Field(default_factory=list)
    first_draft_passed: bool = False
    retried: bool = False
    first_draft_violations: list[PostCheckViolation] = Field(default_factory=list)
    retry_violations: list[PostCheckViolation] = Field(default_factory=list)
    usages: list[LLMUsage] = Field(default_factory=list)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validated_plan_json(bundle: NarrationBundle) -> str:
    """Canonical JSON of the validated plan, the only source of schedule truth."""
    if bundle.plan is None:
        return "null"
    return _canonical(bundle.plan.model_dump(mode="json"))


def available_tokens(bundle: NarrationBundle) -> dict[str, list[str]]:
    positions = range(1, len(bundle.plan.activities) + 1) if bundle.plan else range(0)
    time_tokens = [
        f"{{{{time:{position}:{boundary}}}}}"
        for position in positions
        for boundary in ("start", "end")
    ]
    return {
        "cite": [f"{{{{cite:{item.evidence_id}}}}}" for item in bundle.evidence],
        "fact": [
            f"{{{{fact:{item.evidence_id}}}}}"
            for item in bundle.evidence
            if item.kind in OPERATIONAL_EVIDENCE_KINDS
        ],
        "poi": [f"{{{{poi:{poi_id}}}}}" for poi_id in sorted(bundle.allowed_poi_ids)],
        "time": time_tokens,
    }


def build_dynamic_input(bundle: NarrationBundle) -> str:
    """Everything after the cache breakpoint, with untrusted text clearly fenced."""
    trusted = [
        {
            "evidence_id": item.evidence_id,
            "kind": item.kind.value,
            "poi_id": item.poi_id,
            "statement": item.text,
            "token": f"{{{{fact:{item.evidence_id}}}}}",
        }
        for item in bundle.evidence
        if not item.is_untrusted
    ]
    untrusted = [
        {"evidence_id": item.evidence_id, "poi_id": item.poi_id, "text": item.text}
        for item in bundle.evidence
        if item.is_untrusted
    ]
    catalog_records = [
        {"poi_id": poi_id, "token": f"{{{{poi:{poi_id}}}}}"}
        for poi_id in sorted(bundle.allowed_poi_ids)
    ]
    validation = (
        bundle.validation.model_dump(mode="json") if bundle.validation is not None else None
    )
    sections = [
        ("ANSWER_LANGUAGE", bundle.language.value),
        ("USER_QUESTION", bundle.user_question),
        ("VALIDATED_PLAN_JSON", validated_plan_json(bundle)),
        ("VALIDATION_RESULT", _canonical(validation)),
        ("CATALOG_RECORDS", _canonical(catalog_records)),
        ("OPERATIONAL_FACTS", _canonical(trusted)),
        ("DISCLOSURES", _canonical([item.value for item in bundle.disclosures])),
        ("AVAILABLE_TOKENS", _canonical(available_tokens(bundle))),
        ("UNTRUSTED_RETRIEVED_TEXT", _canonical(untrusted)),
    ]
    return "\n".join(f"[{name}]\n{body}\n[/{name}]" for name, body in sections)


def build_retry_input(bundle: NarrationBundle, violations: list[PostCheckViolation]) -> str:
    """Append only sanitized, machine-readable violations; the prefix is untouched."""
    feedback = _canonical(
        [{"code": item.code.value, "detail": item.detail} for item in violations]
    )
    return (
        f"{build_dynamic_input(bundle)}\n"
        "[POSTCHECK_VIOLATIONS]\n"
        "Your previous draft was rejected by deterministic checks. "
        "Fix every item and return the corrected answer.\n"
        f"{feedback}\n"
        "[/POSTCHECK_VIOLATIONS]"
    )


class Narrator:
    def __init__(
        self,
        provider: LLMProvider,
        catalog: PoiCatalog,
        *,
        system_prompt: str | None = None,
        checker: AnswerChecker = check_answer,
    ) -> None:
        self.provider = provider
        self.catalog = catalog
        self.checker = checker
        self.system_prompt = system_prompt or PROMPT_PATH.read_text(encoding="utf-8")

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        catalog: PoiCatalog,
        *,
        call_name: str = "narrate",
        fixture_store: FixtureStore | None = None,
        client: Any | None = None,
        live_call_budget: LiveCallBudget | None = None,
        checker: AnswerChecker = check_answer,
    ) -> Narrator:
        provider = OpenAIProvider.from_settings(
            settings,
            profile="narrate",
            call_name=call_name,
            prompt_version=PROMPT_VERSION,
            catalog=catalog,
            fixture_store=fixture_store,
            client=client,
            live_call_budget=live_call_budget,
        )
        return cls(provider, catalog, checker=checker)

    async def narrate(
        self,
        bundle: NarrationBundle,
        *,
        checker: AnswerChecker | None = None,
    ) -> NarrationResult:
        """Narrate one bundle. `checker` overrides the post-check for this call only."""
        check = checker or self.checker
        usages: list[LLMUsage] = []

        draft = await self._draft(build_dynamic_input(bundle), usages)
        if isinstance(draft, _ProviderFailure):
            return self._fallback(bundle, failure_category=draft.category, usages=usages)

        first_report = check(draft, bundle, self.catalog)
        if first_report.passed:
            rendered = self._render(draft, bundle)
            if rendered is not None:
                return NarrationResult(
                    answer=rendered.text,
                    citations=list(rendered.citations),
                    first_draft_passed=True,
                    usages=usages,
                )
            return self._fallback(
                bundle,
                failure_category=TokenRenderError.__name__,
                usages=usages,
                first_draft_violations=first_report.violations,
            )

        logger.info("narration post-check rejected the first draft: %s", first_report.codes)
        retry = await self._draft(build_retry_input(bundle, first_report.violations), usages)
        if isinstance(retry, _ProviderFailure):
            return self._fallback(
                bundle,
                failure_category=retry.category,
                usages=usages,
                retried=True,
                first_draft_violations=first_report.violations,
            )

        retry_report = check(retry, bundle, self.catalog)
        failure_category = POSTCHECK_FAILED
        if retry_report.passed:
            rendered = self._render(retry, bundle)
            if rendered is not None:
                return NarrationResult(
                    answer=rendered.text,
                    citations=list(rendered.citations),
                    retried=True,
                    first_draft_violations=first_report.violations,
                    usages=usages,
                )
            failure_category = TokenRenderError.__name__

        return self._fallback(
            bundle,
            failure_category=failure_category,
            usages=usages,
            retried=True,
            first_draft_violations=first_report.violations,
            retry_violations=retry_report.violations,
        )

    async def _draft(
        self, user_prompt: str, usages: list[LLMUsage]
    ) -> str | _ProviderFailure:
        try:
            result = await self.provider.text(
                system_prompt=self.system_prompt,
                user_prompt=user_prompt,
            )
        except (LLMProviderError, ReplayFixtureError, TimeoutError) as error:
            category = type(error).__name__
            logger.warning("narration provider failure category=%s", category)
            return _ProviderFailure(category)
        usages.append(result.usage)
        return result.output

    def _render(self, draft: str, bundle: NarrationBundle) -> RenderedAnswer | None:
        try:
            return render_tokens(draft, bundle, self.catalog)
        except TokenRenderError:
            logger.warning("narration token rendering failed after a passing post-check")
            return None

    def _fallback(
        self,
        bundle: NarrationBundle,
        *,
        failure_category: str,
        usages: list[LLMUsage],
        retried: bool = False,
        first_draft_violations: list[PostCheckViolation] | None = None,
        retry_violations: list[PostCheckViolation] | None = None,
    ) -> NarrationResult:
        rendered = narrate_deterministically(bundle, self.catalog)
        codes = [item.code.value for item in (retry_violations or first_draft_violations or [])]
        return NarrationResult(
            answer=rendered.text,
            citations=list(rendered.citations),
            used_fallback=True,
            failure_category=failure_category,
            violation_codes=codes,
            retried=retried,
            first_draft_violations=first_draft_violations or [],
            retry_violations=retry_violations or [],
            usages=usages,
        )

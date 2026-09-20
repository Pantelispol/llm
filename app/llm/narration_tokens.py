"""Controlled-token grammar shared by the narrator, post-check, and renderer.

The model never writes a POI name, a clock time, or a citation label. It writes
``{{poi:<poi_id>}}``, ``{{time:<position>:<start|end>}}``,
``{{cite:<evidence_id>}}``, and ``{{fact:<evidence_id>}}``. Deterministic code
substitutes the canonical values after the post-check passes, so every factual
substring in a final answer is traceable to the catalog, the validated plan, or
this call's evidence registry.

``fact`` exists because an operational answer has no plan positions to point
at: without it the model could not state a catalog opening time at all without
writing a raw clock time, which is exactly what it is forbidden to author. The
token emits the operational statement a tool returned, verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.domain.catalog import PoiCatalog
from app.domain.models import (
    OPERATIONAL_EVIDENCE_KINDS,
    AnswerLanguage,
    NarrationBundle,
)

TOKEN_PATTERN = re.compile(r"\{\{([a-zA-Z_]+):([^{}]*)\}\}")
LEFTOVER_BRACE_PATTERN = re.compile(r"[{}]")
TIME_ARGUMENT_PATTERN = re.compile(r"^(\d+):(start|end)$")
CLOCK_FORMAT = "%H:%M"
SOURCES_LABEL = {AnswerLanguage.EN: "Sources", AnswerLanguage.EL: "Πηγές"}


class TokenKind(StrEnum):
    POI = "poi"
    TIME = "time"
    CITE = "cite"
    FACT = "fact"


@dataclass(frozen=True)
class ParsedToken:
    raw: str
    kind: str
    argument: str
    start: int
    end: int

    @property
    def is_known_kind(self) -> bool:
        return self.kind in {item.value for item in TokenKind}


@dataclass(frozen=True)
class TokenScan:
    tokens: tuple[ParsedToken, ...]
    residual_text: str
    leftover_braces: tuple[str, ...]

    def of_kind(self, kind: TokenKind) -> tuple[ParsedToken, ...]:
        return tuple(token for token in self.tokens if token.kind == kind.value)


def scan_tokens(text: str) -> TokenScan:
    """Split model output into controlled tokens and the free prose around them."""
    tokens = tuple(
        ParsedToken(
            raw=match.group(0),
            kind=match.group(1),
            argument=match.group(2).strip(),
            start=match.start(),
            end=match.end(),
        )
        for match in TOKEN_PATTERN.finditer(text)
    )
    residual = TOKEN_PATTERN.sub(" ", text)
    leftover = tuple(dict.fromkeys(LEFTOVER_BRACE_PATTERN.findall(residual)))
    return TokenScan(tokens=tokens, residual_text=residual, leftover_braces=leftover)


def parse_time_argument(argument: str) -> tuple[int, str] | None:
    match = TIME_ARGUMENT_PATTERN.match(argument)
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: int = Field(ge=1)
    evidence_id: str = Field(min_length=1)
    source: str = ""


@dataclass(frozen=True)
class RenderedAnswer:
    text: str
    citations: tuple[Citation, ...]


class TokenRenderError(RuntimeError):
    """Raised when a token survives the post-check but cannot be resolved."""


def render_tokens(text: str, bundle: NarrationBundle, catalog: PoiCatalog) -> RenderedAnswer:
    """Replace every controlled token with its canonical, auditable value."""
    pois = {poi.id: poi for poi in catalog.pois}
    registry = bundle.registry
    timezone = ZoneInfo(bundle.timezone)
    labels: dict[str, int] = {}

    def substitute(match: re.Match[str]) -> str:
        kind, argument = match.group(1), match.group(2).strip()
        if kind == TokenKind.POI:
            poi = pois.get(argument)
            if poi is None:
                raise TokenRenderError(f"unknown POI token: {argument}")
            return poi.names.el if bundle.language is AnswerLanguage.EL else poi.names.en
        if kind == TokenKind.TIME:
            parsed = parse_time_argument(argument)
            if parsed is None or bundle.plan is None:
                raise TokenRenderError(f"unresolvable time token: {argument}")
            position, boundary = parsed
            if not 1 <= position <= len(bundle.plan.activities):
                raise TokenRenderError(f"time token position out of range: {argument}")
            activity = bundle.plan.activities[position - 1]
            moment = activity.start if boundary == "start" else activity.end
            return moment.astimezone(timezone).strftime(CLOCK_FORMAT)
        if kind == TokenKind.FACT:
            item = registry.get(argument)
            if item is None or item.kind not in OPERATIONAL_EVIDENCE_KINDS:
                raise TokenRenderError(f"fact token is not an operational fact: {argument}")
            return item.text
        if kind == TokenKind.CITE:
            if argument not in registry:
                raise TokenRenderError(f"citation outside this call's registry: {argument}")
            label = labels.setdefault(argument, len(labels) + 1)
            return f"[{label}]"
        raise TokenRenderError(f"unknown token kind: {kind}")

    rendered = TOKEN_PATTERN.sub(substitute, text).strip()
    citations = tuple(
        Citation(label=label, evidence_id=evidence_id, source=registry[evidence_id].source)
        for evidence_id, label in sorted(labels.items(), key=lambda item: item[1])
    )
    if citations:
        lines = "\n".join(
            f"[{citation.label}] {citation.evidence_id}"
            + (f" — {citation.source}" if citation.source else "")
            for citation in citations
        )
        rendered = f"{rendered}\n\n{SOURCES_LABEL[bundle.language]}:\n{lines}"
    return RenderedAnswer(text=rendered, citations=citations)

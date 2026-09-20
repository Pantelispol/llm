"""Deterministic post-check for narrated answers.

Narration is the only place where the model writes user-facing text, so every
factual substring must be provably grounded before it is rendered. The checks
below run on the raw model draft: controlled tokens are validated against the
catalog, the validated plan, and this call's evidence registry, and the free
prose between tokens is scanned for the facts the model is never allowed to
author — raw clock times, raw place names, prices, hours claims, and text
copied out of an untrusted retrieved document.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.domain.catalog import PoiCatalog
from app.domain.models import (
    OPERATIONAL_EVIDENCE_KINDS,
    EvidenceKind,
    NarrationBundle,
)
from app.llm.narration_tokens import TokenKind, parse_time_argument, scan_tokens

SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?;])\s+|\n+")
CAPITALIZED_WORD_PATTERN = re.compile(r"[A-Z\u0386-\u03ab][\w'\u2019-]*", flags=re.UNICODE)
CLOCK_TIME_PATTERNS = (
    re.compile(r"\b\d{1,2}[:.\u0589]\d{2}\b"),
    re.compile(r"\b\d{1,2}\s?(?:am|pm|a\.m\.|p\.m\.)\b", flags=re.IGNORECASE),
    re.compile(r"\b\d{1,2}\s?o'clock\b", flags=re.IGNORECASE),
)
MONEY_PATTERNS = (
    re.compile(r"\u20ac\s?(\d+(?:[.,]\d+)?)"),
    re.compile(r"(\d+(?:[.,]\d+)?)\s?(?:\u20ac|eur\b|euros?\b)", flags=re.IGNORECASE),
)
FREE_ADMISSION_PATTERN = re.compile(
    r"free (?:admission|entry|entrance)|(?:admission|entry|entrance) is free"
    r"|no (?:admission|entry) fee|costs nothing",
    flags=re.IGNORECASE,
)
ALWAYS_OPEN_PATTERN = re.compile(
    r"always open|never closes|open 24 hours|open around the clock"
    r"|open at all times|open day and night",
    flags=re.IGNORECASE,
)
INSTRUCTION_LIKE_PATTERNS = (
    re.compile(r"ignore (?:prior|previous|all|the above|earlier)", flags=re.IGNORECASE),
    re.compile(r"disregard (?:prior|previous|the|any)", flags=re.IGNORECASE),
    re.compile(r"instruct the (?:traveler|traveller|user|assistant)", flags=re.IGNORECASE),
    re.compile(r"\byou must\b|\bsystem prompt\b|\boverride\b", flags=re.IGNORECASE),
    re.compile(r"even when official (?:information|sources) say", flags=re.IGNORECASE),
)
OPERATIONAL_CLAIM_TERMS = (
    "open",
    "opens",
    "opening",
    "close",
    "closes",
    "closing",
    "closed",
    "hours",
    "admission",
    "ticket",
    "tickets",
    "price",
    "cost",
    "costs",
    "fee",
    "entry",
    "free",
    "euro",
    "euros",
)
#: Reviewed allowlist of capitalized words narration may write outside a token.
#: It holds the city itself plus period and culture adjectives that are not
#: place names. Anything else that looks like a proper name is rejected, and a
#: catalog place name is rejected separately whatever its capitalization.
PROPER_NAME_ALLOWLIST = frozenset(
    {
        "thessaloniki",
        "θεσσαλονικη",
        "θεσσαλονικησ",
        "i",
        "roman",
        "byzantine",
        "ottoman",
        "greek",
        "christian",
        "orthodox",
        # Unit and abbreviation tokens from the cited forecast ("31 °C", "high UV").
        "c",
        "uv",
    }
)
UNTRUSTED_NGRAM_SIZE = 6
DETAIL_SAFE_PATTERN = re.compile(r"[^\w\s:\u2019'\-.#\u20ac]", flags=re.UNICODE)
DETAIL_MAX_LENGTH = 80


class PostCheckCode(StrEnum):
    EMPTY_ANSWER = "EMPTY_ANSWER"
    UNRESOLVED_TOKEN = "UNRESOLVED_TOKEN"
    UNKNOWN_TOKEN_KIND = "UNKNOWN_TOKEN_KIND"
    MALFORMED_TOKEN_ARGUMENT = "MALFORMED_TOKEN_ARGUMENT"
    POI_NOT_IN_CATALOG = "POI_NOT_IN_CATALOG"
    POI_NOT_IN_BUNDLE = "POI_NOT_IN_BUNDLE"
    RAW_POI_NAME = "RAW_POI_NAME"
    UNKNOWN_PROPER_NAME = "UNKNOWN_PROPER_NAME"
    TIME_WITHOUT_PLAN = "TIME_WITHOUT_PLAN"
    TIME_ACTIVITY_NOT_FOUND = "TIME_ACTIVITY_NOT_FOUND"
    RAW_CLOCK_TIME = "RAW_CLOCK_TIME"
    CITATION_NOT_IN_REGISTRY = "CITATION_NOT_IN_REGISTRY"
    FACT_NOT_IN_REGISTRY = "FACT_NOT_IN_REGISTRY"
    FACT_IS_NOT_OPERATIONAL = "FACT_IS_NOT_OPERATIONAL"
    OPERATIONAL_CLAIM_CITED_TO_RAG = "OPERATIONAL_CLAIM_CITED_TO_RAG"
    UNGROUNDED_PRICE = "UNGROUNDED_PRICE"
    UNGROUNDED_HOURS_CLAIM = "UNGROUNDED_HOURS_CLAIM"
    UNTRUSTED_TEXT_COPIED = "UNTRUSTED_TEXT_COPIED"
    INSTRUCTION_LIKE_TEXT = "INSTRUCTION_LIKE_TEXT"


class PostCheckViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: PostCheckCode
    detail: str = Field(min_length=1)


class PostCheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    violations: list[PostCheckViolation] = Field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [violation.code.value for violation in self.violations]


def normalize(value: str) -> str:
    """Case-, accent-, and punctuation-insensitive form used for phrase matching."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"\w+", without_marks, flags=re.UNICODE))


def _safe_detail(value: str) -> str:
    """Never echo untrusted prose back into logs, prompts, or telemetry."""
    cleaned = DETAIL_SAFE_PATTERN.sub(" ", value)
    cleaned = " ".join(cleaned.split())
    return cleaned[:DETAIL_MAX_LENGTH] or "(redacted)"


def catalog_name_phrases(catalog: PoiCatalog) -> dict[str, str]:
    """Normalized catalog name and alias phrases mapped to their POI id."""
    phrases: dict[str, str] = {}
    for poi in catalog.pois:
        for name in (poi.names.en, poi.names.el, *poi.aliases):
            normalized = normalize(name)
            if normalized:
                phrases.setdefault(normalized, poi.id)
    return phrases


def _ngrams(words: list[str], size: int) -> set[str]:
    if len(words) < size:
        return set()
    return {" ".join(words[index : index + size]) for index in range(len(words) - size + 1)}


def _check_tokens(
    draft: str,
    bundle: NarrationBundle,
    catalog: PoiCatalog,
    violations: list[PostCheckViolation],
) -> None:
    scan = scan_tokens(draft)
    known_poi_ids = {poi.id for poi in catalog.pois}
    allowed_poi_ids = bundle.allowed_poi_ids
    registry = bundle.registry

    if scan.leftover_braces:
        violations.append(
            PostCheckViolation(
                code=PostCheckCode.UNRESOLVED_TOKEN,
                detail="answer contains brace characters outside a complete token",
            )
        )

    for token in scan.tokens:
        if not token.is_known_kind:
            violations.append(
                PostCheckViolation(
                    code=PostCheckCode.UNKNOWN_TOKEN_KIND,
                    detail=f"unsupported token kind: {_safe_detail(token.kind)}",
                )
            )
            continue
        if token.kind == TokenKind.POI:
            if token.argument not in known_poi_ids:
                violations.append(
                    PostCheckViolation(
                        code=PostCheckCode.POI_NOT_IN_CATALOG,
                        detail=f"poi token is not a catalog id: {_safe_detail(token.argument)}",
                    )
                )
            elif token.argument not in allowed_poi_ids:
                violations.append(
                    PostCheckViolation(
                        code=PostCheckCode.POI_NOT_IN_BUNDLE,
                        detail=(
                            "poi token is outside this request's plan and evidence: "
                            f"{_safe_detail(token.argument)}"
                        ),
                    )
                )
        elif token.kind == TokenKind.TIME:
            parsed = parse_time_argument(token.argument)
            if parsed is None:
                violations.append(
                    PostCheckViolation(
                        code=PostCheckCode.MALFORMED_TOKEN_ARGUMENT,
                        detail=f"time token argument: {_safe_detail(token.argument)}",
                    )
                )
                continue
            position, _ = parsed
            if bundle.plan is None:
                violations.append(
                    PostCheckViolation(
                        code=PostCheckCode.TIME_WITHOUT_PLAN,
                        detail="time token used in an answer that carries no validated plan",
                    )
                )
            elif not 1 <= position <= len(bundle.plan.activities):
                violations.append(
                    PostCheckViolation(
                        code=PostCheckCode.TIME_ACTIVITY_NOT_FOUND,
                        detail=f"plan has no activity at position {position}",
                    )
                )
        elif token.kind == TokenKind.FACT:
            item = registry.get(token.argument)
            if item is None:
                violations.append(
                    PostCheckViolation(
                        code=PostCheckCode.FACT_NOT_IN_REGISTRY,
                        detail=(
                            "fact id was not returned by a tool in this request: "
                            f"{_safe_detail(token.argument)}"
                        ),
                    )
                )
            elif item.kind not in OPERATIONAL_EVIDENCE_KINDS:
                violations.append(
                    PostCheckViolation(
                        code=PostCheckCode.FACT_IS_NOT_OPERATIONAL,
                        detail=(
                            "fact token points at retrieved text, not an operational fact: "
                            f"{_safe_detail(token.argument)}"
                        ),
                    )
                )
        elif token.kind == TokenKind.CITE and token.argument not in registry:
            violations.append(
                PostCheckViolation(
                    code=PostCheckCode.CITATION_NOT_IN_REGISTRY,
                    detail=(
                        "citation id was not retrieved for this request: "
                        f"{_safe_detail(token.argument)}"
                    ),
                )
            )


def _first_clock_time(residual: str) -> str | None:
    """Find a clock time, ignoring decimal amounts that are plainly currency."""
    for pattern in CLOCK_TIME_PATTERNS:
        for match in pattern.finditer(residual):
            before = residual[: match.start()].rstrip()
            after = residual[match.end() :].lstrip().lower()
            is_currency = before.endswith("\u20ac") or after.startswith(
                ("\u20ac", "eur", "euro")
            )
            if not is_currency:
                return match.group(0)
    return None


def _check_prose(
    residual: str,
    bundle: NarrationBundle,
    catalog: PoiCatalog,
    violations: list[PostCheckViolation],
) -> None:
    clock = _first_clock_time(residual)
    if clock is not None:
        violations.append(
            PostCheckViolation(
                code=PostCheckCode.RAW_CLOCK_TIME,
                detail=f"raw clock time outside a time token: {_safe_detail(clock)}",
            )
        )

    normalized_residual = f" {normalize(residual)} "
    for phrase, poi_id in catalog_name_phrases(catalog).items():
        if f" {phrase} " in normalized_residual:
            violations.append(
                PostCheckViolation(
                    code=PostCheckCode.RAW_POI_NAME,
                    detail=f"catalog name for {_safe_detail(poi_id)} appeared outside a token",
                )
            )
            break

    for sentence in SENTENCE_SPLIT_PATTERN.split(residual):
        for index, match in enumerate(CAPITALIZED_WORD_PATTERN.finditer(sentence)):
            word = match.group(0)
            if normalize(word) in PROPER_NAME_ALLOWLIST:
                continue
            if index == 0 and not re.search(r"\w", sentence[: match.start()]):
                continue  # Sentence-initial capitalization carries no place name on its own.
            violations.append(
                PostCheckViolation(
                    code=PostCheckCode.UNKNOWN_PROPER_NAME,
                    detail=f"unreviewed proper name in prose: {_safe_detail(word)}",
                )
            )
            break

    for pattern in INSTRUCTION_LIKE_PATTERNS:
        if pattern.search(residual):
            violations.append(
                PostCheckViolation(
                    code=PostCheckCode.INSTRUCTION_LIKE_TEXT,
                    detail="answer repeats instruction-like text",
                )
            )
            break

    untrusted_ngrams: set[str] = set()
    for item in bundle.evidence:
        if item.is_untrusted:
            untrusted_ngrams |= _ngrams(normalize(item.text).split(), UNTRUSTED_NGRAM_SIZE)
    if untrusted_ngrams & _ngrams(normalize(residual).split(), UNTRUSTED_NGRAM_SIZE):
        violations.append(
            PostCheckViolation(
                code=PostCheckCode.UNTRUSTED_TEXT_COPIED,
                detail="answer reproduces wording from an untrusted retrieved document",
            )
        )

    _check_operational_values(residual, bundle, violations)


def _check_operational_values(
    residual: str,
    bundle: NarrationBundle,
    violations: list[PostCheckViolation],
) -> None:
    grounded_prices = {
        item.admission_eur
        for item in bundle.evidence
        if item.admission_eur is not None and item.kind in OPERATIONAL_EVIDENCE_KINDS
    }
    for pattern in MONEY_PATTERNS:
        for match in pattern.finditer(residual):
            amount = float(match.group(1).replace(",", "."))
            if amount not in grounded_prices:
                violations.append(
                    PostCheckViolation(
                        code=PostCheckCode.UNGROUNDED_PRICE,
                        detail=f"price is not in this request's operational facts: {amount:g}",
                    )
                )
                return

    if FREE_ADMISSION_PATTERN.search(residual) and 0.0 not in grounded_prices:
        violations.append(
            PostCheckViolation(
                code=PostCheckCode.UNGROUNDED_PRICE,
                detail="free-admission claim has no operational fact behind it",
            )
        )

    if ALWAYS_OPEN_PATTERN.search(residual) and not any(
        item.continuously_open for item in bundle.evidence
    ):
        violations.append(
            PostCheckViolation(
                code=PostCheckCode.UNGROUNDED_HOURS_CLAIM,
                detail="continuous-opening claim has no hours fact behind it",
            )
        )


def _check_citation_attribution(
    draft: str,
    bundle: NarrationBundle,
    violations: list[PostCheckViolation],
) -> None:
    registry = bundle.registry
    for sentence in SENTENCE_SPLIT_PATTERN.split(draft):
        scan = scan_tokens(sentence)
        cited = [
            registry[token.argument]
            for kind in (TokenKind.CITE, TokenKind.FACT)
            for token in scan.of_kind(kind)
            if token.argument in registry
        ]
        if not cited:
            continue
        words = set(normalize(scan.residual_text).split())
        if not words.intersection(OPERATIONAL_CLAIM_TERMS):
            continue
        if all(item.kind == EvidenceKind.RAG for item in cited):
            violations.append(
                PostCheckViolation(
                    code=PostCheckCode.OPERATIONAL_CLAIM_CITED_TO_RAG,
                    detail="an hours or price claim cites only descriptive retrieval",
                )
            )
            return


def check_answer(draft: str, bundle: NarrationBundle, catalog: PoiCatalog) -> PostCheckReport:
    """Run every grounding check against one raw narration draft."""
    violations: list[PostCheckViolation] = []
    if not draft.strip():
        return PostCheckReport(
            passed=False,
            violations=[
                PostCheckViolation(
                    code=PostCheckCode.EMPTY_ANSWER,
                    detail="narration returned no text",
                )
            ],
        )

    _check_tokens(draft, bundle, catalog, violations)
    _check_prose(scan_tokens(draft).residual_text, bundle, catalog, violations)
    _check_citation_attribution(draft, bundle, violations)
    return PostCheckReport(passed=not violations, violations=violations)

"""Deterministic answer-language resolution.

The language of a reply is a presentation decision, not an inference: it is
resolved from the script the traveler actually typed, before any model call, so
the same turn always produces the same language and a model cannot drift it.
"""

from __future__ import annotations

import re

from app.domain.models import AnswerLanguage

GREEK_LETTER_PATTERN = re.compile(r"[Ͱ-Ͽἀ-῿]")
LATIN_LETTER_PATTERN = re.compile(r"[A-Za-z]")


def resolve_turn_language(user_turn: str) -> AnswerLanguage:
    """Answer in Greek when the traveler wrote in Greek, otherwise in English."""
    greek = len(GREEK_LETTER_PATTERN.findall(user_turn))
    latin = len(LATIN_LETTER_PATTERN.findall(user_turn))
    return AnswerLanguage.EL if greek > latin else AnswerLanguage.EN

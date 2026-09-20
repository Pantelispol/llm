from __future__ import annotations

import unicodedata


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    folded = without_marks.lower().replace("ς", "σ")
    without_punctuation = "".join(
        character if not unicodedata.category(character).startswith("P") else " "
        for character in folded
    )
    return " ".join(without_punctuation.split())


def tokenize(text: str) -> list[str]:
    normalized = normalize(text)
    return normalized.split() if normalized else []

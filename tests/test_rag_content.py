from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).parents[1]
CONTENT_DIR = ROOT / "data" / "content"
CATALOG_PATH = ROOT / "data" / "pois.yaml"
CONTENT_FILES = sorted(CONTENT_DIR.glob("*.md"))

CLOCK_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
OPERATIONAL_TERMS = re.compile(
    r"opening hours|last entry|closed on|ώρες λειτουργίας",
    re.IGNORECASE,
)
PRICE = re.compile(r"€|\b\d+\s?(?:euro|eur)\b|admission fee|ticket price", re.IGNORECASE)
SYSTEM_VOCABULARY = re.compile(
    r"catalog|structured|validator|needs_verification|weather unavailable|verified_at",
    re.IGNORECASE,
)


class ContentDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poi_id: str
    title: str
    aliases: list[str]
    source_urls: list[str]
    lang: Literal["en"]
    confidence: Literal["draft"]
    needs_review: Literal[True]


def catalog_ids() -> set[str]:
    catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    return {poi["id"] for poi in catalog["pois"]}


def parse_content(path: Path) -> tuple[ContentDoc, str]:
    text = path.read_text(encoding="utf-8")
    start, frontmatter, body = text.split("---", maxsplit=2)
    assert start == ""
    return ContentDoc.model_validate(yaml.safe_load(frontmatter)), body.strip()


def test_no_content_doc_contains_clock_times() -> None:
    positive_controls = "306 AD, 15th century, 1917"
    assert CLOCK_TIME.search(positive_controls) is None
    assert OPERATIONAL_TERMS.search(positive_controls) is None

    for path in CONTENT_FILES:
        text = path.read_text(encoding="utf-8")
        assert CLOCK_TIME.search(text) is None, path.name
        assert OPERATIONAL_TERMS.search(text) is None, path.name


def test_no_content_doc_contains_prices() -> None:
    positive_controls = "306 AD, 15th century, 1917"
    assert PRICE.search(positive_controls) is None

    for path in CONTENT_FILES:
        assert PRICE.search(path.read_text(encoding="utf-8")) is None, path.name


def test_no_content_doc_leaks_system_vocabulary() -> None:
    for path in CONTENT_FILES:
        assert SYSTEM_VOCABULARY.search(path.read_text(encoding="utf-8")) is None, path.name


def test_every_catalog_poi_has_a_content_doc() -> None:
    assert catalog_ids() <= {path.stem for path in CONTENT_FILES}


def test_every_content_doc_matches_a_catalog_poi() -> None:
    assert {path.stem for path in CONTENT_FILES} <= catalog_ids()


def test_frontmatter_schema_valid() -> None:
    for path in CONTENT_FILES:
        document, _body = parse_content(path)
        assert document.poi_id == path.stem


def test_content_length_and_sections_are_reviewable() -> None:
    for path in CONTENT_FILES:
        _document, body = parse_content(path)
        words = re.findall(r"\b[\w’'-]+\b", body)
        assert 150 <= len(words) <= 250, f"{path.name}: {len(words)} words"

        sections = re.split(r"^## ", body, flags=re.MULTILINE)[1:]
        assert sections, path.name
        assert all(
            "\n\n" in section and section.split("\n\n", maxsplit=1)[1].strip()
            for section in sections
        )

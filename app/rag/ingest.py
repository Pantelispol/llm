from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from app.domain.catalog import Poi, PoiCatalog
from app.rag.models import Chunk, ContentDoc
from app.rag.normalize import normalize
from app.tools.catalog import PROJECT_ROOT, CatalogRepository

CONTENT_DIR = PROJECT_ROOT / "data" / "content"
SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
MAX_SECTION_WORDS = 220


def load_content_doc(path: Path) -> ContentDoc:
    prefix, frontmatter, body = path.read_text(encoding="utf-8").split("---", maxsplit=2)
    if prefix:
        raise ValueError(f"content document must start with frontmatter: {path}")
    metadata = yaml.safe_load(frontmatter)
    if not isinstance(metadata, dict):
        raise ValueError(f"content frontmatter must be an object: {path}")
    return ContentDoc.model_validate({**metadata, "body": body.strip()})


def load_content_docs(content_dir: Path = CONTENT_DIR) -> list[ContentDoc]:
    return [load_content_doc(path) for path in sorted(content_dir.glob("*.md"))]


def ingest_corpus(
    content_dir: Path = CONTENT_DIR,
    catalog: PoiCatalog | None = None,
) -> list[Chunk]:
    poi_catalog = catalog or CatalogRepository().catalog
    poi_by_id = {poi.id: poi for poi in poi_catalog.pois}
    chunks: list[Chunk] = []
    for document in load_content_docs(content_dir):
        try:
            poi = poi_by_id[document.poi_id]
        except KeyError as error:
            raise ValueError(f"content document has unknown POI: {document.poi_id}") from error
        chunks.extend(chunk_document(document, poi))
    return chunks


def chunk_document(document: ContentDoc, poi: Poi) -> list[Chunk]:
    chunks = [_alias_chunk(document, poi)]
    for heading, text in _sections(document.body):
        parts = _split_at_paragraphs(text, MAX_SECTION_WORDS)
        slug = _section_slug(heading)
        for index, part in enumerate(parts, start=1):
            suffix = f"-{index}" if len(parts) > 1 else ""
            chunks.append(
                Chunk(
                    chunk_id=f"{document.poi_id}#{slug}{suffix}",
                    poi_id=document.poi_id,
                    section=heading,
                    text=part,
                    content_hash=_content_hash(part),
                )
            )
    return chunks


def _sections(body: str) -> list[tuple[str, str]]:
    matches = list(SECTION_PATTERN.finditer(body))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = body[match.end() : end].strip()
        if not text:
            raise ValueError(f"content section is empty: {match.group(1)}")
        sections.append((match.group(1).strip(), text))
    if not sections:
        raise ValueError("content document has no level-two sections")
    return sections


def _split_at_paragraphs(text: str, max_words: int) -> list[str]:
    if _word_count(text) <= max_words:
        return [text]
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]
    parts: list[str] = []
    current: list[str] = []
    current_words = 0
    for paragraph in paragraphs:
        paragraph_words = _word_count(paragraph)
        if current and current_words + paragraph_words > max_words:
            parts.append("\n\n".join(current))
            current = []
            current_words = 0
        current.append(paragraph)
        current_words += paragraph_words
    if current:
        parts.append("\n\n".join(current))
    return parts


def _alias_chunk(document: ContentDoc, poi: Poi) -> Chunk:
    values = [poi.names.en, poi.names.el, *poi.aliases, *document.aliases]
    text = " ".join(dict.fromkeys(values))
    return Chunk(
        chunk_id=f"{document.poi_id}#aliases",
        poi_id=document.poi_id,
        section="Aliases",
        text=text,
        content_hash=_content_hash(text),
        is_alias_chunk=True,
    )


def _section_slug(heading: str) -> str:
    return normalize(heading).replace(" ", "-")


def _word_count(text: str) -> int:
    return len(text.split())


def _content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:12]

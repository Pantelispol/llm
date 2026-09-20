from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RagModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContentDoc(RagModel):
    poi_id: str
    title: str
    aliases: list[str]
    source_urls: list[str]
    lang: Literal["en"]
    confidence: Literal["draft"]
    needs_review: bool
    body: str


class Chunk(RagModel):
    chunk_id: str
    poi_id: str
    section: str
    text: str
    content_hash: str
    is_alias_chunk: bool = False
    is_untrusted: bool = False

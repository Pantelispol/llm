from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from app.config import get_settings
from app.rag.dense import EMBEDDING_DIMENSIONS

DEFAULT_CITY_ID = "thessaloniki"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
PGVECTOR_UNAVAILABLE_MESSAGE = (
    "RAG_STORE=pgvector requires a reachable PostgreSQL database with the vector "
    "extension; run `make db-up` to start it, or set RAG_STORE=memory for a "
    "dependency-free run."
)


@dataclass(frozen=True)
class VectorRecord:
    chunk_id: str
    city_id: str
    poi_id: str
    section: str
    text: str
    content_hash: str
    embedding: Sequence[float]


@dataclass(frozen=True)
class VectorHit(VectorRecord):
    score: float


class VectorStore(Protocol):
    def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int,
        *,
        city_id: str,
        poi_ids: Sequence[str] | None = None,
    ) -> list[VectorHit]: ...

    def count(self, *, city_id: str) -> int: ...


class InMemoryVectorStore:
    """Exact cosine search used by unit tests and as the evaluation baseline."""

    def __init__(self) -> None:
        self._records: dict[str, VectorRecord] = {}

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        written = 0
        for record in records:
            existing = self._records.get(record.chunk_id)
            if existing is not None and existing.content_hash == record.content_hash:
                continue
            self._records[record.chunk_id] = _normalized_record(record)
            written += 1
        return written

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int,
        *,
        city_id: str,
        poi_ids: Sequence[str] | None = None,
    ) -> list[VectorHit]:
        if top_k < 0:
            raise ValueError("top_k must be non-negative")
        query = np.asarray(_l2_normalize(query_vector), dtype=float)
        allowed_pois = set(poi_ids) if poi_ids is not None else None
        scored = [
            (
                record,
                float(np.dot(query, np.asarray(record.embedding, dtype=float))),
            )
            for record in self._records.values()
            if record.city_id == city_id
            and (allowed_pois is None or record.poi_id in allowed_pois)
        ]
        scored.sort(key=lambda item: (-item[1], item[0].chunk_id))
        return [_vector_hit(record, score) for record, score in scored[:top_k]]

    def count(self, *, city_id: str) -> int:
        return sum(record.city_id == city_id for record in self._records.values())


class PgVectorStore:
    """PostgreSQL/pgvector adapter using parameterized SQL only."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        schema_path: Path = SCHEMA_PATH,
    ) -> None:
        self.database_url = database_url or get_settings().database_url
        self.schema_path = schema_path
        self._apply_schema()

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0
        from pgvector.psycopg import register_vector
        from psycopg import connect

        statement = """
            INSERT INTO rag_chunks (
                chunk_id, city_id, poi_id, section, text, content_hash, embedding
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chunk_id) DO UPDATE SET
                city_id = EXCLUDED.city_id,
                poi_id = EXCLUDED.poi_id,
                section = EXCLUDED.section,
                text = EXCLUDED.text,
                content_hash = EXCLUDED.content_hash,
                embedding = EXCLUDED.embedding
            WHERE rag_chunks.content_hash IS DISTINCT FROM EXCLUDED.content_hash
            RETURNING chunk_id
        """
        written = 0
        with connect(self.database_url) as connection:
            register_vector(connection)
            with connection.cursor() as cursor:
                for record in records:
                    normalized = _normalized_record(record)
                    _require_pg_dimensions(normalized.embedding)
                    cursor.execute(
                        statement,
                        (
                            normalized.chunk_id,
                            normalized.city_id,
                            normalized.poi_id,
                            normalized.section,
                            normalized.text,
                            normalized.content_hash,
                            np.asarray(normalized.embedding, dtype=np.float32),
                        ),
                    )
                    written += cursor.fetchone() is not None
        return written

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int,
        *,
        city_id: str,
        poi_ids: Sequence[str] | None = None,
    ) -> list[VectorHit]:
        if top_k < 0:
            raise ValueError("top_k must be non-negative")
        if top_k == 0:
            return []
        from pgvector.psycopg import register_vector
        from psycopg import connect

        normalized_query = _l2_normalize(query_vector)
        _require_pg_dimensions(normalized_query)
        vector = np.asarray(normalized_query, dtype=np.float32)
        base_select = """
            SELECT chunk_id, city_id, poi_id, section, text, content_hash,
                   embedding, 1 - (embedding <=> %s) AS score
            FROM rag_chunks
            WHERE city_id = %s
        """
        if poi_ids is None:
            statement = base_select + " ORDER BY embedding <=> %s LIMIT %s"
            parameters = (vector, city_id, vector, top_k)
        else:
            statement = (
                base_select
                + " AND poi_id = ANY(%s) ORDER BY embedding <=> %s LIMIT %s"
            )
            parameters = (vector, city_id, list(poi_ids), vector, top_k)

        with connect(self.database_url) as connection:
            register_vector(connection)
            rows = connection.execute(statement, parameters).fetchall()
        return [
            VectorHit(
                chunk_id=row[0],
                city_id=row[1],
                poi_id=row[2],
                section=row[3],
                text=row[4],
                content_hash=row[5],
                embedding=row[6].to_numpy().astype(float).tolist(),
                score=float(row[7]),
            )
            for row in rows
        ]

    def count(self, *, city_id: str) -> int:
        from psycopg import connect

        with connect(self.database_url) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM rag_chunks WHERE city_id = %s",
                (city_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def _apply_schema(self) -> None:
        from pgvector.psycopg import register_vector
        from psycopg import connect

        schema = self.schema_path.read_text(encoding="utf-8")
        with connect(self.database_url) as connection:
            connection.execute(schema)
            register_vector(connection)
            extension = connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname = %s",
                ("vector",),
            ).fetchone()
            if extension is None:
                raise RuntimeError("the PostgreSQL vector extension is not available")


def create_vector_store(selection: str | None = None) -> VectorStore:
    selected = selection or get_settings().rag_store
    if selected == "memory":
        return InMemoryVectorStore()
    if selected != "pgvector":
        raise ValueError(f"RAG_STORE must be 'pgvector' or 'memory', got {selected!r}")
    try:
        return PgVectorStore()
    except Exception as error:
        raise SystemExit(PGVECTOR_UNAVAILABLE_MESSAGE) from error


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return values
    return [value / norm for value in values]


def _normalized_record(record: VectorRecord) -> VectorRecord:
    return VectorRecord(
        chunk_id=record.chunk_id,
        city_id=record.city_id,
        poi_id=record.poi_id,
        section=record.section,
        text=record.text,
        content_hash=record.content_hash,
        embedding=_l2_normalize(record.embedding),
    )


def _vector_hit(record: VectorRecord, score: float) -> VectorHit:
    return VectorHit(
        chunk_id=record.chunk_id,
        city_id=record.city_id,
        poi_id=record.poi_id,
        section=record.section,
        text=record.text,
        content_hash=record.content_hash,
        embedding=record.embedding,
        score=score,
    )


def _require_pg_dimensions(vector: Sequence[float]) -> None:
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"pgvector records must have {EMBEDDING_DIMENSIONS} dimensions, got {len(vector)}"
        )

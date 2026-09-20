from __future__ import annotations

import os
from collections.abc import Callable
from uuid import uuid4

import pytest

from app.rag.dense import EMBEDDING_DIMENSIONS
from app.rag.store import (
    PGVECTOR_UNAVAILABLE_MESSAGE,
    InMemoryVectorStore,
    PgVectorStore,
    VectorRecord,
    VectorStore,
    create_vector_store,
)


def vector(first: float, second: float) -> list[float]:
    return [first, second, *([0.0] * (EMBEDDING_DIMENSIONS - 2))]


def record(
    prefix: str,
    name: str,
    embedding: list[float],
    *,
    city_id: str,
    poi_id: str | None = None,
    content_hash: str = "hash-v1",
) -> VectorRecord:
    return VectorRecord(
        chunk_id=f"{prefix}-{name}",
        city_id=city_id,
        poi_id=poi_id or name,
        section="Test",
        text=name,
        content_hash=content_hash,
        embedding=embedding,
    )


def pgvector_store() -> VectorStore:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is unset")
    return PgVectorStore(database_url)


@pytest.fixture(
    params=[
        pytest.param(InMemoryVectorStore, id="memory"),
        pytest.param(
            pgvector_store,
            id="pgvector",
            marks=pytest.mark.integration,
        ),
    ]
)
def store(request: pytest.FixtureRequest) -> VectorStore:
    factory: Callable[[], VectorStore] = request.param
    return factory()


def test_vector_store_contract_returns_identical_hand_computed_order(
    store: VectorStore,
) -> None:
    prefix = uuid4().hex
    city_id = f"contract-{prefix}"
    records = [
        record(prefix, "exact", vector(1.0, 0.0), city_id=city_id),
        record(prefix, "near", vector(0.8, 0.6), city_id=city_id),
        record(prefix, "orthogonal", vector(0.0, 1.0), city_id=city_id),
    ]

    assert store.upsert(records) == 3
    hits = store.search(vector(1.0, 0.0), 3, city_id=city_id)

    assert [hit.poi_id for hit in hits] == ["exact", "near", "orthogonal"]
    assert [hit.score for hit in hits] == pytest.approx([1.0, 0.8, 0.0])
    assert store.count(city_id=city_id) == 3


def test_city_filter_excludes_other_cities(store: VectorStore) -> None:
    prefix = uuid4().hex
    selected_city = f"selected-{prefix}"
    other_city = f"other-{prefix}"
    store.upsert(
        [
            record(prefix, "selected", vector(0.8, 0.6), city_id=selected_city),
            record(prefix, "other", vector(1.0, 0.0), city_id=other_city),
        ]
    )

    hits = store.search(vector(1.0, 0.0), 5, city_id=selected_city)

    assert [hit.poi_id for hit in hits] == ["selected"]
    assert all(hit.city_id == selected_city for hit in hits)


def test_upsert_is_idempotent(store: VectorStore) -> None:
    prefix = uuid4().hex
    city_id = f"idempotent-{prefix}"
    records = [
        record(prefix, "first", vector(1.0, 0.0), city_id=city_id),
        record(prefix, "second", vector(0.0, 1.0), city_id=city_id),
    ]

    assert store.upsert(records) == 2
    assert store.upsert(records) == 0
    assert store.count(city_id=city_id) == 2


def test_pgvector_selection_fails_fast_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_store() -> PgVectorStore:
        raise OSError("database unavailable")

    monkeypatch.setattr("app.rag.store.PgVectorStore", unavailable_store)

    with pytest.raises(SystemExit) as error:
        create_vector_store("pgvector")

    assert str(error.value) == PGVECTOR_UNAVAILABLE_MESSAGE
    assert "make db-up" in str(error.value)
    assert "RAG_STORE=memory" in str(error.value)

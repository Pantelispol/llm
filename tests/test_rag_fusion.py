import pytest

from app.rag.dense import HashEncoder
from app.rag.models import Chunk
from app.rag.retriever import HybridRetriever, reciprocal_rank_fusion


def test_rrf_matches_hand_computed_order_and_scores() -> None:
    fused = reciprocal_rank_fusion([[0, 1, 2], [1, 2, 3]], k=60)

    assert [index for index, _score in fused] == [1, 2, 0, 3]
    assert [score for _index, score in fused] == pytest.approx(
        [
            0.03252247488101534,
            0.03200204813108039,
            0.01639344262295082,
            0.015873015873015872,
        ]
    )


def test_retriever_filters_by_poi_and_returns_score_provenance() -> None:
    chunks = [
        Chunk(
            chunk_id="arch#history",
            poi_id="arch",
            section="History",
            text="Roman arch history",
            content_hash="a",
        ),
        Chunk(
            chunk_id="tower#history",
            poi_id="tower",
            section="History",
            text="Ottoman tower history",
            content_hash="b",
        ),
    ]
    retriever = HybridRetriever(chunks, HashEncoder())

    result = retriever.search_sync(
        "history",
        poi_id="arch",
        mode="hybrid",
        apply_abstention=False,
    )

    assert [hit.poi_id for hit in result.hits] == ["arch"]
    assert result.hits[0].bm25_score is not None
    assert result.hits[0].dense_score is not None
    assert result.hits[0].rrf_score is not None


def test_alias_only_hit_also_returns_history_context() -> None:
    chunks = [
        Chunk(
            chunk_id="arch#aliases",
            poi_id="arch",
            section="Aliases",
            text="Kamara",
            content_hash="a",
            is_alias_chunk=True,
        ),
        Chunk(
            chunk_id="arch#history",
            poi_id="arch",
            section="History",
            text="Roman triumphal monument",
            content_hash="b",
        ),
    ]
    retriever = HybridRetriever(chunks, HashEncoder())

    result = retriever.search_sync(
        "Kamara",
        limit=1,
        mode="bm25",
        apply_abstention=False,
    )

    assert [hit.chunk_id for hit in result.hits] == ["arch#aliases", "arch#history"]

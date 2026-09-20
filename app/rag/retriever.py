from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.domain.ports import RetrievalHit
from app.rag.bm25 import BM25Index
from app.rag.dense import (
    DenseEncoder,
    E5Encoder,
    HashEncoder,
    encode_passages_cached,
)
from app.rag.models import Chunk
from app.rag.normalize import normalize, tokenize
from app.rag.store import (
    DEFAULT_CITY_ID,
    InMemoryVectorStore,
    VectorRecord,
    VectorStore,
)

RetrievalMode = Literal["bm25", "dense", "hybrid"]


@dataclass(frozen=True)
class AbstentionThresholds:
    dense: float
    lexical: float


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[RetrievalHit]
    abstained: bool
    max_dense_cosine: float
    max_bm25_normalized: float


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]],
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, index in enumerate(ranking, start=1):
            scores[index] = scores.get(index, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def normalize_bm25_score(score: float, query: str) -> float:
    """Map mean BM25 contribution per unique query term into [0, 1)."""
    term_count = max(1, len(set(tokenize(query))))
    mean_contribution = score / term_count
    return mean_contribution / (1.0 + mean_contribution)


def should_abstain(
    max_dense_cosine: float,
    max_bm25_normalized: float,
    thresholds: AbstentionThresholds,
) -> bool:
    return (
        max_dense_cosine < thresholds.dense
        and max_bm25_normalized < thresholds.lexical
    )


def dense_encoder_from_env() -> DenseEncoder:
    selection = os.getenv("RAG_DENSE", E5Encoder.model_name)
    if selection == "off":
        return HashEncoder()
    if selection != E5Encoder.model_name:
        raise ValueError(
            f"RAG_DENSE must be 'off' or '{E5Encoder.model_name}', got {selection!r}"
        )
    return E5Encoder()


class HybridRetriever:
    def __init__(
        self,
        chunks: Sequence[Chunk],
        encoder: DenseEncoder,
        *,
        store: VectorStore | None = None,
        city_id: str = DEFAULT_CITY_ID,
        thresholds: AbstentionThresholds | None = None,
        source_urls: Mapping[str, str] | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.encoder = encoder
        self.thresholds = thresholds
        self.source_urls = dict(source_urls or {})
        self.store = store or InMemoryVectorStore()
        self.city_id = city_id
        self._bm25 = BM25Index([chunk.text for chunk in self.chunks])
        self._passage_embeddings = encode_passages_cached(encoder, self.chunks)
        self.store.upsert(
            [
                VectorRecord(
                    chunk_id=chunk.chunk_id,
                    city_id=self.city_id,
                    poi_id=chunk.poi_id,
                    section=chunk.section,
                    text=chunk.text,
                    content_hash=chunk.content_hash,
                    embedding=embedding,
                )
                for chunk, embedding in zip(
                    self.chunks,
                    self._passage_embeddings,
                    strict=True,
                )
            ]
        )

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        poi_id: str | None = None,
    ) -> list[RetrievalHit]:
        return self.search_sync(query, limit=limit, poi_id=poi_id).hits

    def search_sync(
        self,
        query: str,
        *,
        limit: int = 5,
        poi_id: str | None = None,
        mode: RetrievalMode = "hybrid",
        apply_abstention: bool = True,
    ) -> RetrievalResult:
        eligible = [
            index
            for index, chunk in enumerate(self.chunks)
            if poi_id is None or chunk.poi_id == poi_id
        ]
        bm25_scores = self._bm25.scores(query)
        bm25_ranked = sorted(
            (index for index in eligible if bm25_scores[index] > 0.0),
            key=lambda index: (-bm25_scores[index], self.chunks[index].chunk_id),
        )

        query_vector = self.encoder.encode_queries([query])[0]
        dense_hits = self.store.search(
            query_vector,
            len(eligible),
            city_id=self.city_id,
            poi_ids=None if poi_id is None else [poi_id],
        )
        index_by_chunk_id = {
            chunk.chunk_id: index for index, chunk in enumerate(self.chunks)
        }
        dense_ranked = [
            index_by_chunk_id[hit.chunk_id]
            for hit in dense_hits
            if hit.chunk_id in index_by_chunk_id
        ]
        dense_scores = [0.0] * len(self.chunks)
        for hit in dense_hits:
            index = index_by_chunk_id.get(hit.chunk_id)
            if index is not None:
                dense_scores[index] = hit.score

        top_bm25 = bm25_ranked[:20]
        top_dense = dense_ranked[:20]
        rrf_ranked = reciprocal_rank_fusion([top_bm25, top_dense])
        rrf_scores = dict(rrf_ranked)
        if mode == "bm25":
            ranked_indices = bm25_ranked
        elif mode == "dense":
            ranked_indices = dense_ranked
        else:
            ranked_indices = [index for index, _score in rrf_ranked]

        max_bm25 = max((bm25_scores[index] for index in eligible), default=0.0)
        max_dense = max((dense_scores[index] for index in eligible), default=0.0)
        normalized_bm25 = normalize_bm25_score(max_bm25, query)
        abstained = bool(
            apply_abstention
            and self.thresholds is not None
            and should_abstain(max_dense, normalized_bm25, self.thresholds)
        )
        if abstained:
            return RetrievalResult([], True, max_dense, normalized_bm25)

        selected = ranked_indices[:limit]
        selected = self._add_history_for_alias_only_hits(selected, ranked_indices)
        hits = [
            self._hit(
                index,
                mode=mode,
                bm25_score=bm25_scores[index],
                dense_score=dense_scores[index],
                rrf_score=rrf_scores.get(index, 0.0),
            )
            for index in selected
        ]
        return RetrievalResult(hits, False, max_dense, normalized_bm25)

    def score_signals(self, query: str) -> tuple[float, float]:
        result = self.search_sync(
            query,
            limit=0,
            mode="hybrid",
            apply_abstention=False,
        )
        return result.max_dense_cosine, result.max_bm25_normalized

    def _add_history_for_alias_only_hits(
        self,
        selected: list[int],
        ranked_indices: list[int],
    ) -> list[int]:
        selected_pois = [self.chunks[index].poi_id for index in selected]
        expanded: list[int] = []
        for index in selected:
            expanded.append(index)
            chunk = self.chunks[index]
            if not chunk.is_alias_chunk or selected_pois.count(chunk.poi_id) != 1:
                continue
            history = next(
                (
                    candidate
                    for candidate in ranked_indices
                    if self.chunks[candidate].poi_id == chunk.poi_id
                    and normalize(self.chunks[candidate].section) == "history"
                ),
                None,
            )
            if history is None:
                history = next(
                    (
                        candidate
                        for candidate, candidate_chunk in enumerate(self.chunks)
                        if candidate_chunk.poi_id == chunk.poi_id
                        and normalize(candidate_chunk.section) == "history"
                    ),
                    None,
                )
            if history is not None and history not in expanded:
                expanded.append(history)
        return expanded

    def _hit(
        self,
        index: int,
        *,
        mode: RetrievalMode,
        bm25_score: float,
        dense_score: float,
        rrf_score: float,
    ) -> RetrievalHit:
        chunk = self.chunks[index]
        primary_score = {
            "bm25": bm25_score,
            "dense": dense_score,
            "hybrid": rrf_score,
        }[mode]
        return RetrievalHit(
            chunk_id=chunk.chunk_id,
            poi_id=chunk.poi_id,
            section=chunk.section,
            text=chunk.text,
            source_url=self.source_urls.get(chunk.poi_id, ""),
            score=primary_score,
            bm25_score=bm25_score,
            dense_score=dense_score,
            rrf_score=rrf_score,
            is_untrusted=chunk.is_untrusted,
        )

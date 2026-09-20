from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rag.bm25 import BM25Index
from app.rag.ingest import ingest_corpus
from app.rag.models import Chunk

ROOT = Path(__file__).parents[1]
GOLD_PATH = ROOT / "evals" / "rag_gold.yaml"


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoldCase(EvalModel):
    id: str
    query: str
    lang: str
    expected_poi_ids: list[str] | None = None
    expect_abstain: bool = False
    note: str

    @model_validator(mode="after")
    def exactly_one_expectation(self) -> GoldCase:
        if (self.expected_poi_ids is not None) == self.expect_abstain:
            raise ValueError("gold case needs expected_poi_ids or expect_abstain")
        return self


class GoldSet(EvalModel):
    cases: list[GoldCase] = Field(min_length=1)


@dataclass(frozen=True)
class FailedCase:
    case_id: str
    query: str
    chunk_ids: list[str]


@dataclass(frozen=True)
class EvalRow:
    mode: str
    recall_at_5: float
    poi_deduped_recall_at_5: float
    mrr: float
    abstain_precision: float | None
    abstain_recall: float | None
    mean_latency_ms: float
    failures: list[FailedCase]


def load_gold(path: Path = GOLD_PATH) -> GoldSet:
    return GoldSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def top_deduped_poi_ids(
    ranked: list[tuple[int, float]],
    chunks: list[Chunk],
    limit: int,
) -> list[str]:
    poi_ids: list[str] = []
    for index, _score in ranked:
        poi_id = chunks[index].poi_id
        if poi_id not in poi_ids:
            poi_ids.append(poi_id)
        if len(poi_ids) == limit:
            break
    return poi_ids


def evaluate_bm25() -> EvalRow:
    chunks = ingest_corpus()
    index = BM25Index([chunk.text for chunk in chunks])
    gold = load_gold()
    recalls: list[float] = []
    poi_deduped_recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    failures: list[FailedCase] = []

    for case in gold.cases:
        started = perf_counter()
        ranked = index.rank(case.query, limit=len(chunks))
        latencies.append((perf_counter() - started) * 1000)
        top_five = ranked[:5]
        top_chunk_ids = [chunks[index].chunk_id for index, _score in top_five]

        if case.expect_abstain:
            failures.append(FailedCase(case.id, case.query, top_chunk_ids))
            continue

        expected = set(case.expected_poi_ids or [])
        top_pois = {chunks[index].poi_id for index, _score in top_five}
        recall = len(expected & top_pois) / len(expected)
        recalls.append(recall)
        deduped_top_pois = set(top_deduped_poi_ids(ranked, chunks, limit=5))
        poi_deduped_recalls.append(len(expected & deduped_top_pois) / len(expected))
        relevant_rank = next(
            (
                position
                for position, (index, _score) in enumerate(ranked, start=1)
                if chunks[index].poi_id in expected
            ),
            None,
        )
        reciprocal_ranks.append(1 / relevant_rank if relevant_rank is not None else 0.0)
        if recall < 1.0:
            failures.append(FailedCase(case.id, case.query, top_chunk_ids))

    return EvalRow(
        mode="bm25",
        recall_at_5=sum(recalls) / len(recalls),
        poi_deduped_recall_at_5=sum(poi_deduped_recalls)
        / len(poi_deduped_recalls),
        mrr=sum(reciprocal_ranks) / len(reciprocal_ranks),
        abstain_precision=None,
        abstain_recall=None,
        mean_latency_ms=sum(latencies) / len(latencies),
        failures=failures,
    )


def print_report(rows: list[EvalRow]) -> None:
    headers = (
        "mode",
        "recall@5",
        "recall@5 (POI-deduped)",
        "MRR",
        "abstain precision",
        "abstain recall",
        "mean latency (ms)",
    )
    rendered = [
        (
            row.mode,
            f"{row.recall_at_5:.3f}",
            f"{row.poi_deduped_recall_at_5:.3f}",
            f"{row.mrr:.3f}",
            "n/a" if row.abstain_precision is None else f"{row.abstain_precision:.3f}",
            "n/a" if row.abstain_recall is None else f"{row.abstain_recall:.3f}",
            f"{row.mean_latency_ms:.3f}",
        )
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rendered))
        for index in range(len(headers))
    ]
    print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rendered:
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))

    failures = [failure for row in rows for failure in row.failures]
    print("\nFailing gold cases:")
    if not failures:
        print("- none")
        return
    for failure in failures:
        print(f"- {failure.case_id}: {failure.query}")
        print(f"  top-5: {', '.join(failure.chunk_ids) or '(no results)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate tourism RAG retrieval.")
    parser.add_argument(
        "--mode",
        choices=("bm25", "dense", "hybrid", "all"),
        default="bm25",
    )
    parser.add_argument(
        "--store",
        choices=("memory", "pgvector"),
        default="memory",
    )
    args = parser.parse_args()
    if args.store != "memory":
        parser.error("pgvector evaluation is implemented in checkpoint 5d")
    if args.mode != "bm25":
        parser.error("dense and hybrid evaluation are implemented in checkpoint 5c")
    print_report([evaluate_bm25()])


if __name__ == "__main__":
    main()

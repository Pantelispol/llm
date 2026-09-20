from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rag.abstention import CalibrationResult, QuerySignals, calibrate_thresholds
from app.rag.bm25 import BM25Index
from app.rag.dense import DenseEncoder
from app.rag.ingest import ingest_corpus
from app.rag.models import Chunk
from app.rag.retriever import (
    AbstentionThresholds,
    HybridRetriever,
    RetrievalMode,
    dense_encoder_from_env,
)
from app.rag.store import VectorStore, create_vector_store

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
    mode: str
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


def evaluate_bm25(
    chunks: list[Chunk] | None = None,
    gold: GoldSet | None = None,
) -> EvalRow:
    chunks = chunks or ingest_corpus()
    gold = gold or load_gold()
    index = BM25Index([chunk.text for chunk in chunks])
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
            failures.append(FailedCase("bm25", case.id, case.query, top_chunk_ids))
            continue

        expected = set(case.expected_poi_ids or [])
        top_pois = {chunks[index].poi_id for index, _score in top_five}
        recalls.append(len(expected & top_pois) / len(expected))
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
        if len(expected & top_pois) < len(expected):
            failures.append(FailedCase("bm25", case.id, case.query, top_chunk_ids))

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


def evaluate_retriever(
    retriever: HybridRetriever,
    mode: RetrievalMode,
    gold: GoldSet,
) -> EvalRow:
    recalls: list[float] = []
    poi_deduped_recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    failures: list[FailedCase] = []
    predicted_abstentions = 0
    correct_abstentions = 0
    actual_abstentions = sum(case.expect_abstain for case in gold.cases)

    for case in gold.cases:
        started = perf_counter()
        result = retriever.search_sync(case.query, limit=len(retriever.chunks), mode=mode)
        latencies.append((perf_counter() - started) * 1000)
        if result.abstained:
            predicted_abstentions += 1
        top_five = result.hits[:5]
        top_chunk_ids = [hit.chunk_id for hit in top_five]

        if case.expect_abstain:
            if result.abstained:
                correct_abstentions += 1
            else:
                failures.append(FailedCase(mode, case.id, case.query, top_chunk_ids))
            continue

        expected = set(case.expected_poi_ids or [])
        top_pois = {hit.poi_id for hit in top_five}
        recalls.append(len(expected & top_pois) / len(expected))
        deduped_pois = list(dict.fromkeys(hit.poi_id for hit in result.hits))[:5]
        poi_deduped_recalls.append(len(expected & set(deduped_pois)) / len(expected))
        relevant_rank = next(
            (
                position
                for position, hit in enumerate(result.hits, start=1)
                if hit.poi_id in expected
            ),
            None,
        )
        reciprocal_ranks.append(1 / relevant_rank if relevant_rank is not None else 0.0)
        if len(expected & top_pois) < len(expected):
            failures.append(FailedCase(mode, case.id, case.query, top_chunk_ids))

    has_abstention = retriever.thresholds is not None
    return EvalRow(
        mode=mode,
        recall_at_5=sum(recalls) / len(recalls),
        poi_deduped_recall_at_5=sum(poi_deduped_recalls)
        / len(poi_deduped_recalls),
        mrr=sum(reciprocal_ranks) / len(reciprocal_ranks),
        abstain_precision=(
            correct_abstentions / predicted_abstentions
            if has_abstention and predicted_abstentions
            else (0.0 if has_abstention else None)
        ),
        abstain_recall=(
            correct_abstentions / actual_abstentions if has_abstention else None
        ),
        mean_latency_ms=sum(latencies) / len(latencies),
        failures=failures,
    )


def calibration_signals(
    retriever: HybridRetriever,
    gold: GoldSet,
) -> list[QuerySignals]:
    return [
        QuerySignals(
            case_id=case.id,
            in_knowledge_base=not case.expect_abstain,
            max_dense_cosine=dense,
            max_bm25_normalized=lexical,
        )
        for case in gold.cases
        for dense, lexical in [retriever.score_signals(case.query)]
    ]


def print_calibration(
    result: CalibrationResult | None,
    encoder: DenseEncoder,
    signals: list[QuerySignals],
) -> None:
    print(f"Calibration ({encoder.model_name}):")
    if result is None:
        print("- no threshold pair cleanly separates all frozen gold cases")
        print("- chosen dense threshold: none")
        print("- chosen BM25 normalized threshold: none")
        print("- margin: n/a")
        out_of_kb = [signal for signal in signals if not signal.in_knowledge_base]
        in_kb = [signal for signal in signals if signal.in_knowledge_base]
        dense_floor_case = max(out_of_kb, key=lambda signal: signal.max_dense_cosine)
        lexical_floor_case = max(
            out_of_kb,
            key=lambda signal: signal.max_bm25_normalized,
        )
        dense_floor = dense_floor_case.max_dense_cosine
        lexical_floor = lexical_floor_case.max_bm25_normalized
        blockers = [
            signal
            for signal in in_kb
            if signal.max_dense_cosine <= dense_floor
            and signal.max_bm25_normalized <= lexical_floor
        ]
        print(
            f"- out-of-KB dense floor: {dense_floor:.6f} "
            f"({dense_floor_case.case_id})"
        )
        print(
            f"- out-of-KB BM25 floor: {lexical_floor:.6f} "
            f"({lexical_floor_case.case_id})"
        )
        print("- blocking in-KB cases:")
        for blocker in blockers:
            print(
                f"  - {blocker.case_id} "
                f"(dense={blocker.max_dense_cosine:.6f}, "
                f"BM25={blocker.max_bm25_normalized:.6f})"
            )
        return
    print(f"- dense threshold: {result.thresholds.dense:.6f}")
    print(f"- BM25 normalized threshold: {result.thresholds.lexical:.6f}")
    print(f"- margin: {result.margin:.6f}")
    nearest_in = result.nearest_in_kb
    nearest_out = result.nearest_out_of_kb
    print(
        f"- nearest in-KB: {nearest_in.case_id} "
        f"(dense={nearest_in.max_dense_cosine:.6f}, "
        f"BM25={nearest_in.max_bm25_normalized:.6f})"
    )
    print(
        f"- nearest out-of-KB: {nearest_out.case_id} "
        f"(dense={nearest_out.max_dense_cosine:.6f}, "
        f"BM25={nearest_out.max_bm25_normalized:.6f})"
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
        print(f"- [{failure.mode}] {failure.case_id}: {failure.query}")
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
        default=None,
    )
    parser.add_argument("--calibrate", action="store_true")
    args = parser.parse_args()
    chunks = ingest_corpus()
    gold = load_gold()
    store: VectorStore | None = None
    rows: list[EvalRow] = []
    if args.mode in {"bm25", "all"}:
        rows.append(evaluate_bm25(chunks, gold))
    if args.mode in {"dense", "hybrid", "all"}:
        encoder = dense_encoder_from_env()
        store = create_vector_store(args.store)
        retriever = HybridRetriever(chunks, encoder, store=store)
        calibration: CalibrationResult | None = None
        if args.calibrate:
            signals = calibration_signals(retriever, gold)
            calibration = calibrate_thresholds(signals)
            print_calibration(calibration, encoder, signals)
            print()
            if calibration is not None:
                retriever.thresholds = AbstentionThresholds(
                    dense=calibration.thresholds.dense,
                    lexical=calibration.thresholds.lexical,
                )
        modes: tuple[RetrievalMode, ...] = (
            ("dense", "hybrid") if args.mode == "all" else (args.mode,)
        )
        rows.extend(evaluate_retriever(retriever, mode, gold) for mode in modes)
    print_report(rows)


if __name__ == "__main__":
    main()

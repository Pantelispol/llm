from app.rag.models import Chunk
from evals.rag_report import EvalRow, print_report, top_deduped_poi_ids


def make_chunk(chunk_id: str, poi_id: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        poi_id=poi_id,
        section="test",
        text="test",
        content_hash="hash",
    )


def test_top_deduped_poi_ids_counts_five_unique_pois() -> None:
    chunks = [
        make_chunk("a#1", "a"),
        make_chunk("a#2", "a"),
        make_chunk("b#1", "b"),
        make_chunk("c#1", "c"),
        make_chunk("d#1", "d"),
        make_chunk("e#1", "e"),
        make_chunk("f#1", "f"),
    ]
    ranked = [(index, 1.0) for index in range(len(chunks))]

    assert top_deduped_poi_ids(ranked, chunks, limit=5) == ["a", "b", "c", "d", "e"]


def test_report_prints_na_without_abstention_mechanism(capsys) -> None:
    row = EvalRow(
        mode="bm25",
        recall_at_5=0.5,
        poi_deduped_recall_at_5=0.75,
        mrr=0.4,
        abstain_precision=None,
        abstain_recall=None,
        mean_latency_ms=1.0,
        failures=[],
    )

    print_report([row])

    output = capsys.readouterr().out
    assert "recall@5 (POI-deduped)" in output
    assert output.count("n/a") == 2

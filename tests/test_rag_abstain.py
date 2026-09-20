import pytest

from app.rag.abstention import QuerySignals, calibrate_thresholds
from app.rag.dense import HashEncoder
from app.rag.models import Chunk
from app.rag.retriever import AbstentionThresholds, HybridRetriever, should_abstain


def make_chunk(chunk_id: str, poi_id: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        poi_id=poi_id,
        section="History",
        text=text,
        content_hash="hash",
    )


def test_hash_retriever_abstains_only_when_both_signals_are_below_thresholds() -> None:
    thresholds = AbstentionThresholds(dense=0.8, lexical=0.5)
    retriever = HybridRetriever(
        [make_chunk("white_tower#history", "white_tower", "white tower history")],
        HashEncoder(),
        thresholds=thresholds,
    )

    in_kb = retriever.search_sync("white tower history")
    out_of_kb = retriever.search_sync("concert tickets tonight")

    assert not in_kb.abstained
    assert out_of_kb.abstained


def test_abstention_thresholds_are_strict() -> None:
    thresholds = AbstentionThresholds(dense=0.8, lexical=0.5)

    assert not should_abstain(0.8, 0.1, thresholds)
    assert not should_abstain(0.1, 0.5, thresholds)
    assert should_abstain(0.79, 0.49, thresholds)


def test_calibration_selects_largest_hand_computed_margin() -> None:
    result = calibrate_thresholds(
        [
            QuerySignals("dense_in", True, 0.9, 0.1),
            QuerySignals("lexical_in", True, 0.1, 0.9),
            QuerySignals("out", False, 0.2, 0.2),
        ]
    )

    assert result is not None
    assert result.thresholds == AbstentionThresholds(dense=0.55, lexical=0.55)
    assert result.margin == pytest.approx(0.35)


def test_calibration_reports_no_clean_separation() -> None:
    result = calibrate_thresholds(
        [
            QuerySignals("in", True, 0.2, 0.2),
            QuerySignals("out", False, 0.3, 0.3),
        ]
    )

    assert result is None

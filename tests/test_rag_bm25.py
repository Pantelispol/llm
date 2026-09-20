import pytest

from app.rag.bm25 import BM25Index

DOCUMENTS = [
    "museum roman",
    "museum roman history",
    "museum byzantine art culture collection",
    "museum market",
]


def test_scores_match_hand_computed_literals() -> None:
    index = BM25Index(DOCUMENTS)

    assert index.scores("roman") == pytest.approx(
        [0.8154672712, 0.6931471806, 0.0, 0.0],
        abs=1e-10,
    )


def test_term_in_every_document_contributes_near_zero() -> None:
    index = BM25Index(DOCUMENTS)

    scores = index.scores("museum")

    assert scores == pytest.approx(
        [0.1239535478, 0.1053605157, 0.0810465505, 0.1239535478],
        abs=1e-10,
    )
    assert max(scores) < 0.13


def test_length_normalization_penalizes_longer_document() -> None:
    scores = BM25Index(DOCUMENTS).scores("roman")

    assert scores[0] == pytest.approx(0.8154672712, abs=1e-10)
    assert scores[1] == pytest.approx(0.6931471806, abs=1e-10)
    assert scores[0] > scores[1]


def test_rank_is_stable_for_equal_scores() -> None:
    ranked = BM25Index(DOCUMENTS).rank("museum", limit=4)

    assert [index for index, _score in ranked] == [0, 3, 1, 2]
    assert [score for _index, score in ranked] == pytest.approx(
        [0.1239535478, 0.1239535478, 0.1053605157, 0.0810465505],
        abs=1e-10,
    )

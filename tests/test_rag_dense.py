import math

import pytest

from app.rag.dense import EMBEDDING_DIMENSIONS, E5Encoder, HashEncoder


class FakeSentenceTransformer:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def encode(self, texts, **_kwargs):
        self.texts.extend(texts)
        return [[2.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _text in texts]


def test_e5_uses_query_and_passage_prefixes_and_normalizes() -> None:
    model = FakeSentenceTransformer()
    encoder = E5Encoder(model)

    query = encoder.encode_queries(["Rotunda"])[0]
    passage = encoder.encode_passages(["Roman monument"])[0]

    assert model.texts == ["query: Rotunda", "passage: Roman monument"]
    assert query[0] == 1.0
    assert passage[0] == 1.0


def test_hash_encoder_is_deterministic_and_unit_normalized() -> None:
    encoder = HashEncoder()

    first = encoder.encode_queries(["Καμάρα"])[0]
    second = encoder.encode_queries(["Καμάρα"])[0]

    assert first == second
    assert len(first) == EMBEDDING_DIMENSIONS
    assert math.sqrt(sum(value * value for value in first)) == pytest.approx(1.0)

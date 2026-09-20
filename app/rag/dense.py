from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from app.rag.models import Chunk
from app.rag.normalize import normalize, tokenize

E5_MODEL_NAME = "intfloat/multilingual-e5-small"
EMBEDDING_DIMENSIONS = 384
DEFAULT_CACHE_PATH = Path(".cache/rag_embeddings.npz")


class DenseEncoder(Protocol):
    model_name: str
    dimensions: int

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]: ...

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


class E5Encoder:
    model_name = E5_MODEL_NAME
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self, model: object | None = None) -> None:
        self._model = model

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode([f"query: {text}" for text in texts])

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode([f"passage: {text}" for text in texts])

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._get_model()
        encoded = model.encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        vectors = [_l2_normalize([float(value) for value in row]) for row in encoded]
        if any(len(vector) != self.dimensions for vector in vectors):
            raise ValueError(f"{self.model_name} must return {self.dimensions} dimensions")
        return vectors

    def _get_model(self) -> object:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as error:
                raise RuntimeError(
                    "Dense RAG requires sentence-transformers; install project dependencies "
                    "or set RAG_DENSE=off for the deterministic offline encoder."
                ) from error
            try:
                self._model = SentenceTransformer(self.model_name)
            except Exception as error:
                raise RuntimeError(
                    f"Could not load {self.model_name}; check model access or set "
                    "RAG_DENSE=off for the deterministic offline encoder."
                ) from error
        return self._model


class HashEncoder:
    """Deterministic lexical encoder for offline tests, not a semantic model."""

    model_name = "hash-encoder-v1"
    dimensions = EMBEDDING_DIMENSIONS

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._encode(text) for text in texts]

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._encode(text) for text in texts]

    def _encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        normalized = normalize(text)
        compact = normalized.replace(" ", "_")
        features = [f"token:{token}" for token in tokenize(text)]
        features.extend(f"trigram:{compact[index:index + 3]}" for index in range(len(compact) - 2))
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 2.0 if feature.startswith("token:") else 1.0
        return _l2_normalize(vector)


def dot_product(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have equal dimensions")
    return sum(a * b for a, b in zip(left, right, strict=True))


def corpus_content_hash(chunks: Sequence[Chunk]) -> str:
    material = "\n".join(f"{chunk.chunk_id}:{chunk.content_hash}" for chunk in chunks)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def encode_passages_cached(
    encoder: DenseEncoder,
    chunks: Sequence[Chunk],
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> list[list[float]]:
    if isinstance(encoder, HashEncoder):
        return encoder.encode_passages([chunk.text for chunk in chunks])

    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError("Dense embedding cache requires numpy") from error

    cache_key = f"{encoder.model_name}:{corpus_content_hash(chunks)}"
    if cache_path.exists():
        with np.load(cache_path, allow_pickle=False) as cached:
            if cached["cache_key"].item() == cache_key:
                return cached["embeddings"].astype(float).tolist()

    embeddings = encoder.encode_passages([chunk.text for chunk in chunks])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache_path,
        cache_key=np.asarray(cache_key),
        embeddings=np.asarray(embeddings, dtype=float),
    )
    return embeddings


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [0.0 for _value in vector]
    return [value / norm for value in vector]

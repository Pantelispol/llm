from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

from app.rag.normalize import tokenize


class BM25Index:
    """Rank text with Okapi BM25.

    For query term ``q`` and document ``d`` the contribution is::

        idf(q) * tf(q, d) * (k1 + 1)
        -----------------------------------------------
        tf(q, d) + k1 * (1 - b + b * len(d) / avg_len)

    IDF uses ``log(1 + (N - df + 0.5) / (df + 0.5))`` so every contribution
    remains non-negative. Scores are sums over the query's unique terms.
    """

    def __init__(
        self,
        documents: Sequence[str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b = b
        self._term_frequencies = [Counter(tokenize(document)) for document in documents]
        self._lengths = [sum(frequencies.values()) for frequencies in self._term_frequencies]
        self._document_count = len(documents)
        self._average_length = (
            sum(self._lengths) / self._document_count if self._document_count else 0.0
        )
        document_frequencies: Counter[str] = Counter()
        for frequencies in self._term_frequencies:
            document_frequencies.update(frequencies.keys())
        self._idf = {
            term: math.log(
                1 + (self._document_count - frequency + 0.5) / (frequency + 0.5)
            )
            for term, frequency in document_frequencies.items()
        }

    def scores(self, query: str) -> list[float]:
        query_terms = set(tokenize(query))
        return [
            sum(
                self._term_score(term, frequencies, document_length)
                for term in query_terms
            )
            for frequencies, document_length in zip(
                self._term_frequencies,
                self._lengths,
                strict=True,
            )
        ]

    def rank(self, query: str, *, limit: int = 5) -> list[tuple[int, float]]:
        scores = self.scores(query)
        ranked = sorted(
            ((index, score) for index, score in enumerate(scores) if score > 0.0),
            key=lambda item: (-item[1], item[0]),
        )
        return ranked[:limit]

    def _term_score(
        self,
        term: str,
        frequencies: Counter[str],
        document_length: int,
    ) -> float:
        term_frequency = frequencies.get(term, 0)
        if term_frequency == 0 or self._average_length == 0:
            return 0.0
        length_normalization = 1 - self.b + self.b * document_length / self._average_length
        numerator = term_frequency * (self.k1 + 1)
        denominator = term_frequency + self.k1 * length_normalization
        return self._idf[term] * numerator / denominator

from __future__ import annotations

import math
from dataclasses import dataclass

from app.rag.retriever import AbstentionThresholds, should_abstain


@dataclass(frozen=True)
class QuerySignals:
    case_id: str
    in_knowledge_base: bool
    max_dense_cosine: float
    max_bm25_normalized: float


@dataclass(frozen=True)
class CalibrationResult:
    thresholds: AbstentionThresholds
    margin: float
    nearest_in_kb: QuerySignals
    nearest_out_of_kb: QuerySignals


def calibrate_thresholds(signals: list[QuerySignals]) -> CalibrationResult | None:
    in_kb = [signal for signal in signals if signal.in_knowledge_base]
    out_of_kb = [signal for signal in signals if not signal.in_knowledge_base]
    if not in_kb or not out_of_kb:
        raise ValueError("calibration requires both in-KB and out-of-KB cases")

    dense_candidates = _threshold_candidates(
        [signal.max_dense_cosine for signal in signals]
    )
    lexical_candidates = _threshold_candidates(
        [signal.max_bm25_normalized for signal in signals]
    )
    best: CalibrationResult | None = None
    for dense in dense_candidates:
        for lexical in lexical_candidates:
            thresholds = AbstentionThresholds(dense=dense, lexical=lexical)
            if any(
                should_abstain(
                    signal.max_dense_cosine,
                    signal.max_bm25_normalized,
                    thresholds,
                )
                for signal in in_kb
            ):
                continue
            if any(
                not should_abstain(
                    signal.max_dense_cosine,
                    signal.max_bm25_normalized,
                    thresholds,
                )
                for signal in out_of_kb
            ):
                continue

            nearest_in = min(in_kb, key=lambda signal: _in_margin(signal, thresholds))
            nearest_out = min(
                out_of_kb,
                key=lambda signal: _out_margin(signal, thresholds),
            )
            margin = min(
                _in_margin(nearest_in, thresholds),
                _out_margin(nearest_out, thresholds),
            )
            candidate = CalibrationResult(
                thresholds=thresholds,
                margin=margin,
                nearest_in_kb=nearest_in,
                nearest_out_of_kb=nearest_out,
            )
            if best is None or _calibration_key(candidate) > _calibration_key(best):
                best = candidate
    return best


def _threshold_candidates(values: list[float]) -> list[float]:
    ordered = sorted(set(values))
    candidates = {math.nextafter(value, math.inf) for value in ordered}
    candidates.update(
        (left + right) / 2 for left, right in zip(ordered, ordered[1:], strict=False)
    )
    return sorted(candidates)


def _in_margin(signal: QuerySignals, thresholds: AbstentionThresholds) -> float:
    return max(
        signal.max_dense_cosine - thresholds.dense,
        signal.max_bm25_normalized - thresholds.lexical,
    )


def _out_margin(signal: QuerySignals, thresholds: AbstentionThresholds) -> float:
    return min(
        thresholds.dense - signal.max_dense_cosine,
        thresholds.lexical - signal.max_bm25_normalized,
    )


def _calibration_key(result: CalibrationResult) -> tuple[float, float, float]:
    return (
        result.margin,
        -result.thresholds.dense,
        -result.thresholds.lexical,
    )

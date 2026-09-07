# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/metrics.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real precision/recall/F1 over a labeled fixture - the detector's own
docstring previously only claimed "healthy scored up to ~5.3, faulty
scored in the hundreds" in prose. This module is what turns that claim
into a real, computed number a test can actually assert on, and gives
any future re-tuning of `threshold` a real metric to check against
instead of eyeballing a handful of scores again.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrecisionRecall:
    """A real confusion-matrix summary over one labeled fixture."""

    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def precision(self) -> float:
        """Of everything flagged anomalous, the real fraction that
        genuinely was. `1.0` when nothing was flagged at all - an empty
        prediction set has no false positives to be wrong about."""
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 1.0

    @property
    def recall(self) -> float:
        """Of everything genuinely anomalous, the real fraction that was
        caught. `1.0` when nothing was genuinely anomalous - there was
        nothing real to miss."""
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def precision_recall(predicted_anomalous: list[bool], actually_anomalous: list[bool]) -> PrecisionRecall:
    """Real confusion-matrix computation over two same-length real label
    lists - `predicted_anomalous` from `Verdict.anomalous`,
    `actually_anomalous` from the fixture's own known ground truth."""
    if len(predicted_anomalous) != len(actually_anomalous):
        raise ValueError(
            f"predicted_anomalous has {len(predicted_anomalous)} entries, "
            f"actually_anomalous has {len(actually_anomalous)} - they must label the same real samples"
        )
    true_positives = sum(1 for p, a in zip(predicted_anomalous, actually_anomalous) if p and a)
    false_positives = sum(1 for p, a in zip(predicted_anomalous, actually_anomalous) if p and not a)
    false_negatives = sum(1 for p, a in zip(predicted_anomalous, actually_anomalous) if not p and a)
    true_negatives = sum(1 for p, a in zip(predicted_anomalous, actually_anomalous) if not p and not a)
    return PrecisionRecall(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        true_negatives=true_negatives,
    )

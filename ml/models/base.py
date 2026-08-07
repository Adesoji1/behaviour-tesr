"""Shared helpers for detectors: a robust score normaliser + standard scaler I/O.

Each detector produces a raw anomaly score on its own scale. To make them comparable and
ensemble-able, we map every raw score to a [0, 1] percentile rank against the TRAINING
distribution (empirical CDF). 0.5 = a typical training transaction; ~1.0 = far more anomalous
than anything seen in training.
"""
from __future__ import annotations

import numpy as np


class ScoreNormalizer:
    """Empirical-CDF normaliser fitted on training raw scores (higher = more anomalous)."""

    def __init__(self):
        self.sorted_: np.ndarray | None = None

    def fit(self, raw: np.ndarray) -> "ScoreNormalizer":
        r = np.asarray(raw, dtype=float)
        r = r[np.isfinite(r)]
        self.sorted_ = np.sort(r) if r.size else np.array([0.0])
        return self

    def transform(self, raw: np.ndarray) -> np.ndarray:
        r = np.asarray(raw, dtype=float)
        idx = np.searchsorted(self.sorted_, r, side="right")
        return np.clip(idx / max(len(self.sorted_), 1), 0.0, 1.0)

    def to_dict(self) -> dict:
        # store quantiles (compact) rather than the full array
        q = np.linspace(0, 1, 1001)
        return {"quantiles": np.quantile(self.sorted_, q).tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreNormalizer":
        obj = cls()
        obj.sorted_ = np.asarray(d["quantiles"], dtype=float)
        return obj

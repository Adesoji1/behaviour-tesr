"""
Stage 7 — ENSEMBLE scorer.

Blends the per-transaction anomaly scores from whichever detectors are present (GNN, Isolation
Forest, Autoencoder) into one `risk_score` in [0, 1], with `confidence`. Weights come from
config; if a detector is missing (e.g. torch not installed) its weight is renormalised away, so
the ensemble always works with what it has.

confidence = how decisively the detectors agree — high when they cluster (all high or all low),
low when they disagree or sit near the middle.
"""
from __future__ import annotations

import numpy as np

from .. import config


class Ensemble:
    def __init__(self, weights: dict | None = None):
        self.weights = {**config.ENSEMBLE_WEIGHTS, **(weights or {})}

    def blend(self, scores: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """scores: {detector_name: array[N]} for the detectors that ran."""
        present = {k: np.asarray(v, dtype=float) for k, v in scores.items()
                   if v is not None and len(v)}
        if not present:
            raise ValueError("ensemble: no detector scores provided")
        n = len(next(iter(present.values())))
        w = np.array([self.weights.get(k, 0.0) for k in present])
        w = w / (w.sum() or 1.0)
        stack = np.vstack([present[k] for k in present])          # [D, N]
        wmean = (w[:, None] * stack).sum(axis=0)                   # weighted mean [N]

        # --- escalation-aware combine (config.BLEND_MODE) -------------------------------------------
        # A plain weighted mean lets a quiet, capability-blind detector DILUTE detectors that strongly
        # agree. "escalate" fixes that: a strongly-agreeing MAJORITY is never averaged away, while a
        # lone spurious spike still cannot dominate. Changing the mode needs a retrain (recalibrates
        # the p95/p99 tiering cuts on the new risk distribution).
        mode = getattr(config, "BLEND_MODE", "escalate")
        if mode == "mean" or stack.shape[0] == 1:
            risk = wmean
        elif mode == "max":
            risk = stack.max(axis=0)
        elif mode == "noisy_or":
            risk = 1.0 - np.prod(1.0 - np.clip(stack, 0, 1), axis=0)
        else:  # "escalate" (default): weighted mean, but floored by the 2nd-highest detector
            second_high = np.sort(stack, axis=0)[-2]              # needs >= 2 detectors (guarded above)
            risk = np.maximum(wmean, second_high)

        # confidence: 1 - normalised spread across detectors, nudged by extremeness
        spread = stack.std(axis=0) if stack.shape[0] > 1 else np.zeros(n)
        agree = 1.0 - np.clip(spread / 0.5, 0, 1)
        extreme = np.abs(risk - 0.5) * 2.0
        confidence = np.clip(0.5 * agree + 0.5 * extreme, 0.05, 0.99)
        return {"risk_score": np.clip(risk, 0, 1), "confidence": confidence,
                "detectors": list(present.keys())}

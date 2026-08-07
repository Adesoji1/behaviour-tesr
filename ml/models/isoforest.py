"""
Detector — Isolation Forest (tabular anomaly detection).

Trains on the customers' clean deviation vectors; a transaction that is easy to "isolate"
(few random splits) is anomalous. No labels, no GPU. Fast to fit and to serve.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from .. import config
from .base import ScoreNormalizer

log = logging.getLogger("ml.isoforest")


class IsoForestDetector:
    name = "isoforest"
    available = True

    def __init__(self, params: dict | None = None):
        self.params = {**config.ISO_FOREST, **(params or {})}
        self.model = None
        self.scaler = None
        self.norm = ScoreNormalizer()

    def fit(self, X: np.ndarray) -> "IsoForestDetector":
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import RobustScaler
        X = np.asarray(X, dtype=np.float32)
        self.scaler = RobustScaler().fit(X)
        Xs = self.scaler.transform(X)
        self.model = IsolationForest(**self.params).fit(Xs)
        # raw anomaly = -score_samples (higher = more anomalous); calibrate normaliser
        raw = -self.model.score_samples(Xs)
        self.norm.fit(raw)
        log.info("isoforest: fitted on %d x %d", X.shape[0], X.shape[1])
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        Xs = self.scaler.transform(np.asarray(X, dtype=np.float32))
        return self.norm.transform(-self.model.score_samples(Xs))

    def save(self, d: str | Path) -> None:
        import joblib
        d = Path(d); d.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": self.model, "scaler": self.scaler, "params": self.params}, d / "isoforest.joblib")
        (d / "isoforest_norm.json").write_text(json.dumps(self.norm.to_dict()))

    def load(self, d: str | Path) -> "IsoForestDetector":
        import joblib
        d = Path(d)
        blob = joblib.load(d / "isoforest.joblib")
        self.model, self.scaler, self.params = blob["model"], blob["scaler"], blob["params"]
        self.norm = ScoreNormalizer.from_dict(json.loads((d / "isoforest_norm.json").read_text()))
        return self

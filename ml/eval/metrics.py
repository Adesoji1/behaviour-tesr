"""
Evaluation metrics — supervised scores against the (weak proxy) labels + all the plots.

Given ensemble risk scores and the eval labels, pick the operating threshold, compute
accuracy / precision / recall / F1 / ROC-AUC / PR-AP + the confusion matrix, and render every
figure. Returns a metrics dict (also written to artifacts/metrics/).
"""
from __future__ import annotations

import json
import logging

import numpy as np

from .. import config
from . import plots

log = logging.getLogger("ml.metrics")


def evaluate(risk: np.ndarray, labels: np.ndarray, threshold: float | None = None,
             histories: dict | None = None, tag: str = "run",
             unsupervised: dict | None = None) -> dict:
    from sklearn.metrics import (accuracy_score, precision_score, recall_score, f1_score,
                                 roc_auc_score, average_precision_score)
    risk = np.asarray(risk, dtype=float)
    labels = np.asarray(labels, dtype=int)

    # operating threshold: the one that maximises F1 on the proxy labels (reported, not final)
    if threshold is None:
        grid = np.quantile(risk, np.linspace(0.80, 0.999, 40))
        f1s = [f1_score(labels, (risk >= t).astype(int), zero_division=0) for t in grid]
        threshold = float(grid[int(np.argmax(f1s))]) if len(grid) else 0.9
    pred = (risk >= threshold).astype(int)

    metrics = {
        "tag": tag,
        "n": int(len(risk)),
        "n_positive_proxy": int(labels.sum()),
        "threshold": float(threshold),
        "accuracy": float(accuracy_score(labels, pred)),
        "precision": float(precision_score(labels, pred, zero_division=0)),
        "recall": float(recall_score(labels, pred, zero_division=0)),
        "f1": float(f1_score(labels, pred, zero_division=0)),
        "labels_note": "WEAK PROXY (status/blocked/blacklisted) — not confirmed fraud",
    }
    if labels.sum() and labels.sum() < len(labels):
        metrics["roc_auc"] = float(roc_auc_score(labels, risk))
        metrics["pr_ap"] = float(average_precision_score(labels, risk))

    figs = {"confusion_matrix": plots.confusion_matrix(labels, pred),
            "metrics_bar": plots.metric_bars(metrics),
            "score_distribution": plots.score_distribution(risk, labels)}
    if labels.sum() and labels.sum() < len(labels):
        figs["precision_recall"] = plots.pr_curve(labels, risk)
        figs["roc_curve"] = plots.roc_curve(labels, risk)
    for name, hist in (histories or {}).items():
        p = plots.training_curve(hist, name)
        if p:
            figs[f"training_{name}"] = p

    # unsupervised validation (label-free): synthetic-anomaly AUC + contamination gap
    if unsupervised:
        rn = unsupervised.pop("_risk_normal", None)
        rs = unsupervised.pop("_risk_synth", None)
        if rn is not None and rs is not None and len(rn) and len(rs):
            figs["validation_synthetic_roc"] = plots.synthetic_roc(rn, rs)
            figs["validation_contamination"] = plots.contamination(rn, rs)
        metrics["unsupervised_validation"] = unsupervised

    metrics["plots"] = figs

    out = config.DIR_METRICS / f"metrics_{tag}.json"
    out.write_text(json.dumps(metrics, indent=2))
    log.info("metrics: acc=%.3f prec=%.3f rec=%.3f f1=%.3f -> %s",
             metrics["accuracy"], metrics["precision"], metrics["recall"], metrics["f1"], out)
    return metrics

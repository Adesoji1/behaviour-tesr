"""
Plotting utilities — every figure the training run produces, saved under artifacts/plots/.

Headless (Agg backend), so it runs on a server / in CI with no display. Each function returns
the saved path. Supervised-metric plots use the weak proxy labels (blocked/blacklisted) until
the real feedback loop exists, and are titled accordingly.
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .. import config  # noqa: E402

log = logging.getLogger("ml.plots")
# We have no confirmed-fraud labels yet, so the supervised plots use WEAK PROXY labels
# (status='blocked' / is_blocked / sender_blacklisted). Titled plainly — no reviewer wording.
_PROXY = "(weak proxy labels: blocked / blacklisted)"


def _save(fig, name: str) -> str:
    p = config.DIR_PLOTS / name
    fig.tight_layout(); fig.savefig(p, dpi=130, bbox_inches="tight"); plt.close(fig)
    log.info("plot: wrote %s", p)
    return str(p)


def confusion_matrix(y_true, y_pred, name="confusion_matrix.png") -> str:
    from sklearn.metrics import confusion_matrix as cm
    m = cm(y_true, y_pred, labels=[0, 1])
    fig, ax = plt.subplots(figsize=(4.5, 4))
    im = ax.imshow(m, cmap="Blues")
    for (i, j), v in np.ndenumerate(m):
        ax.text(j, i, f"{v:,}", ha="center", va="center",
                color="white" if v > m.max() / 2 else "black", fontsize=12)
    ax.set_xticks([0, 1], ["clean", "anomaly"]); ax.set_yticks([0, 1], ["clean", "anomaly"])
    ax.set_xlabel("Predicted"); ax.set_ylabel("Actual")
    ax.set_title(f"Confusion matrix\n{_PROXY}", fontsize=9)
    fig.colorbar(im, fraction=0.046)
    return _save(fig, name)


def pr_curve(y_true, scores, name="precision_recall.png") -> str:
    from sklearn.metrics import precision_recall_curve, average_precision_score
    p, r, _ = precision_recall_curve(y_true, scores)
    ap = average_precision_score(y_true, scores)
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.plot(r, p, lw=2); ax.set_xlabel("Recall"); ax.set_ylabel("Precision")
    ax.set_title(f"Precision-Recall (AP={ap:.3f})\n{_PROXY}", fontsize=9)
    ax.grid(alpha=0.3)
    return _save(fig, name)


def roc_curve(y_true, scores, name="roc_curve.png") -> str:
    from sklearn.metrics import roc_curve as rc, roc_auc_score
    fpr, tpr, _ = rc(y_true, scores)
    auc = roc_auc_score(y_true, scores)
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.plot(fpr, tpr, lw=2, label=f"AUC={auc:.3f}")
    ax.plot([0, 1], [0, 1], "--", color="grey", lw=1)
    ax.set_xlabel("False positive rate"); ax.set_ylabel("True positive rate")
    ax.set_title(f"ROC curve\n{_PROXY}", fontsize=9); ax.legend(); ax.grid(alpha=0.3)
    return _save(fig, name)


def metric_bars(metrics: dict, name="metrics_bar.png") -> str:
    keys = ["accuracy", "precision", "recall", "f1"]
    vals = [metrics.get(k, 0.0) for k in keys]
    fig, ax = plt.subplots(figsize=(5, 4))
    bars = ax.bar(keys, vals, color=["#4C78A8", "#F58518", "#54A24B", "#B279A2"])
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.3f}", ha="center", fontsize=10)
    ax.set_ylim(0, 1.05); ax.set_title(f"Detection metrics\n{_PROXY}", fontsize=9)
    return _save(fig, name)


def score_distribution(scores, labels=None, name="score_distribution.png") -> str:
    fig, ax = plt.subplots(figsize=(6, 4))
    if labels is not None:
        labels = np.asarray(labels)
        ax.hist(np.asarray(scores)[labels == 0], bins=60, alpha=0.6, label="clean", density=True)
        ax.hist(np.asarray(scores)[labels == 1], bins=60, alpha=0.6, label="anomaly (proxy)", density=True)
        ax.legend()
    else:
        ax.hist(scores, bins=60, alpha=0.8)
    ax.set_xlabel("ensemble risk score"); ax.set_ylabel("density")
    ax.set_title("Risk-score distribution")
    return _save(fig, name)


def synthetic_roc(risk_normal, risk_synth, name="validation_synthetic_roc.png") -> str:
    """Unsupervised validation: ROC separating real held-out NORMAL from SYNTHETIC anomalies."""
    from sklearn.metrics import roc_curve as rc, roc_auc_score
    y = np.concatenate([np.zeros(len(risk_normal)), np.ones(len(risk_synth))])
    r = np.concatenate([np.asarray(risk_normal), np.asarray(risk_synth)])
    fpr, tpr, _ = rc(y, r); auc = roc_auc_score(y, r)
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.plot(fpr, tpr, lw=2, color="#4C78A8", label=f"AUC={auc:.3f}")
    ax.axhspan(0, 0, color="none")
    ax.plot([0, 1], [0, 1], "--", color="grey", lw=1)
    ax.set_xlabel("False positive rate (on real normal)"); ax.set_ylabel("True positive rate (on synthetic)")
    ax.set_title("Unsupervised validation ROC\n(synthetic anomalies vs held-out normal — target 0.75-0.85)",
                 fontsize=9)
    ax.legend(); ax.grid(alpha=0.3)
    return _save(fig, name)


def contamination(risk_normal, risk_synth, name="validation_contamination.png") -> str:
    """Held-out normal vs synthetic-anomaly risk, with the DYNAMIC alert zones drawn:
    p95 (review / grey zone) and p99 (Priority-1 unsafe) — both computed from the held-out normal
    distribution via np.percentile, so there is no hard-coded boundary."""
    rn, rs = np.asarray(risk_normal), np.asarray(risk_synth)
    p95, p99 = float(np.percentile(rn, 95)), float(np.percentile(rn, 99))
    fig, ax = plt.subplots(figsize=(6.6, 4))
    ax.hist(rn, bins=60, alpha=0.6, density=True, label="held-out normal", color="#4C78A8")
    ax.hist(rs, bins=60, alpha=0.6, density=True, label="synthetic anomaly", color="#E45756")
    ax.axvline(p95, color="#555", ls=":", lw=1.3, label=f"p95 = {p95:.3f}  (review / grey zone)")
    ax.axvline(p99, color="black", ls="--", lw=1.5, label=f"p99 = {p99:.3f}  (Priority-1 unsafe)")
    ax.set_xlabel("ensemble risk score"); ax.set_ylabel("density")
    ax.set_title("Score contamination — normal vs synthetic anomaly (dynamic zones)", fontsize=10)
    ax.legend(fontsize=8, loc="upper left")
    return _save(fig, name)


def training_curve(history: dict, model_name: str, name=None) -> str | None:
    if not history or not history.get("train_loss"):
        return None
    name = name or f"training_loss_{model_name}.png"
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(history["train_loss"], label="train")
    if history.get("val_loss"):
        ax.plot(history["val_loss"], label="val")
    ax.set_xlabel("epoch"); ax.set_ylabel("loss")
    ax.set_title(f"{model_name} training loss"); ax.legend(); ax.grid(alpha=0.3)
    return _save(fig, name)

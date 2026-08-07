"""
Unsupervised validation — how good is the detector WITHOUT fraud labels?

We cannot compute true fraud precision/recall (no confirmed-fraud labels yet). Instead we
validate on unseen NORMAL traffic held out at the customer level:

  1. Synthetic-anomaly ROC-AUC — take real held-out normal transactions, synthesise
     fraud-like deviations from them (spike a random subset of deviation axes), and measure
     how well the ensemble ranks the synthetic anomalies above the genuine normals. Target
     band 0.75-0.85 (config.SYNTH_ANOMALY_TARGET). This is the honest, label-free proxy for
     detection quality — it rewards a TIGHT normal profile, not overfitting.
  2. Score-contamination gap — the separation between the bulk of normal scores (p99) and the
     synthetic anomalies (median). A healthy model shows a clear positive gap.

Both are computed on the 20% holdout of eligible-customer normal rows (no leakage: a customer
is entirely in train or in holdout).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .. import config
from ..pipeline import features as feat

log = logging.getLogger("ml.unsupervised")

# Deviation axes a real anomaly tends to trip. Each synthetic anomaly spikes a RANDOM subset of
# these (so anomalies are not all trivially separable), leaving the rest at the normal value.
_SPIKE = {
    "amt_z": 6.0, "amt_over_median": 25.0, "amt_over_p95": 8.0, "amt_over_max": 4.0,
    "above_max": 1.0, "hour_rarity": 0.99, "dow_rarity": 0.9, "is_night": 1.0,
    "location_new": 1.0, "country_new": 1.0, "cross_border": 1.0, "beneficiary_new": 1.0,
    "type_rare": 1.0, "ip_new": 1.0, "vel_1h": 8.0, "vel_24h": 12.0, "amt_1h_ratio": 10.0,
    "g_fanout": 30.0, "g_distinct_benef": 25.0, "g_shared_cp": 15.0,
}


def make_synthetic(normal_df: pd.DataFrame, n: int | None = None, seed: int = config.SYNTH_SEED
                   ) -> pd.DataFrame:
    """Derive fraud-like rows from real held-out normal rows (same feature schema)."""
    rng = np.random.default_rng(seed)
    n = n or len(normal_df)
    base = normal_df.sample(n=n, replace=len(normal_df) < n, random_state=seed).reset_index(drop=True)
    A = base.copy()
    axes = [c for c in _SPIKE if c in A.columns]
    if axes:                                             # the .iat writes below assign float64
        A[axes] = A[axes].astype("float64")              # magnitudes; match dtype to avoid the
                                                         # pandas "incompatible dtype" FutureWarning
    for i in range(len(A)):
        # vary difficulty: some anomalies trip few axes and mildly (subtle fraud), others many
        # and hard (blatant fraud). A per-row subtlety factor scales magnitude deviations so the
        # validation set is a realistic mix, not a trivially separable one.
        k = int(rng.integers(2, 6))                      # spike 2-5 axes per anomaly
        subtle = float(rng.uniform(0.35, 1.2))           # 0.35 = mild, >1 = blatant
        for c in rng.choice(axes, size=min(k, len(axes)), replace=False):
            v = _SPIKE[c]
            if v <= 1.0:                                 # binary novelty flag: fires or not
                A.iat[i, A.columns.get_loc(c)] = 1.0 if rng.random() < 0.5 + 0.4 * subtle else 0.0
            else:                                        # magnitude axis: scaled by subtlety
                A.iat[i, A.columns.get_loc(c)] = 1.0 + (v - 1.0) * subtle * float(rng.uniform(0.7, 1.3))
    if "amt_log" in A.columns:                            # keep amount coherent with amt_over
        A["amt_log"] = base["amt_log"] + np.log1p(np.maximum(A.get("amt_over_median", 1) - 1, 0))
    return A


def evaluate(score_fn, holdout_normal: pd.DataFrame, tag: str = "run") -> dict:
    """
    score_fn: (DataFrame with FEATURES + customer_key) -> risk array in [0,1].
    holdout_normal: unseen normal rows (eligible customers), feature schema of ml.pipeline.features.
    Returns unsupervised validation metrics + the risk arrays for plotting.
    """
    from sklearn.metrics import roc_auc_score
    if holdout_normal.empty:
        return {}
    synth = make_synthetic(holdout_normal)
    r_norm = np.asarray(score_fn(holdout_normal), dtype=float)
    r_synth = np.asarray(score_fn(synth), dtype=float)
    y = np.concatenate([np.zeros(len(r_norm)), np.ones(len(r_synth))])
    r = np.concatenate([r_norm, r_synth])
    auc = float(roc_auc_score(y, r)) if len(np.unique(y)) == 2 else float("nan")
    lo, hi = config.SYNTH_ANOMALY_TARGET
    p99 = float(np.percentile(r_norm, 99))
    out = {
        "synthetic_auc": auc,
        "synthetic_auc_target": [lo, hi],
        "synthetic_auc_in_band": bool(lo <= auc <= hi) if auc == auc else False,
        "synthetic_auc_at_or_above_target": bool(auc >= lo) if auc == auc else False,
        "n_holdout_normal": int(len(r_norm)),
        "n_synthetic": int(len(r_synth)),
        "normal_risk_median": float(np.median(r_norm)),
        "normal_risk_p99": p99,
        "synthetic_risk_median": float(np.median(r_synth)),
        "contamination_gap": float(np.median(r_synth) - p99),  # >0 = clean separation
        "_risk_normal": r_norm, "_risk_synth": r_synth,        # for plots (stripped before JSON)
    }
    log.info("unsupervised(%s): synthetic AUC=%.3f (target %.2f-%.2f) | contamination gap=%.3f",
             tag, auc, lo, hi, out["contamination_gap"])
    return out

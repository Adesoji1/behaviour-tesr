"""
Training orchestrator — runs the full pipeline (stages 1-8 + eval) and registers the model.

    python -m ml.train                 # full run on the whole eligible population
    python -m ml.train --sample 0.05   # fast dev run on ~5% of customers
    python -m ml.train --limit 200000  # cap rows (dev)
    python -m ml.train --promote       # promote the trained model to active if it validates

Trains ONLY on active/trusted/clean customers (§1/§7). GPU is used automatically for the
Autoencoder/GNN when available (ml.hardware); otherwise they run on CPU or are skipped, and the
tree + graph-feature path still produces a working model. Everything is versioned and every
figure lands in artifacts/plots/.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone

import numpy as np

from . import config, hardware, registry
from .codes import Tiering
from .eval import metrics as evalm
from .eval import unsupervised
from .models.autoencoder import AutoencoderDetector
from .models.ensemble import Ensemble
from .models.gnn import GNNDetector
from .models.isoforest import IsoForestDetector
from .pipeline import clean, features, graph, ingest, validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
config.attach_file_log()
log = logging.getLogger("ml.train")


def _watermark(raw) -> dict:
    """Snapshot the training data so ml.retrain_trigger can later measure the delta + drift:
    the latest transaction seen, row count, and the reference amount distribution (for PSI)."""
    amt = raw["amount"].to_numpy(dtype=float)
    amt = amt[np.isfinite(amt) & (amt > 0)]
    edges = np.unique(np.quantile(amt, np.linspace(0, 1, 11))).tolist() if amt.size else []
    ref = ((np.histogram(amt, bins=edges)[0] / amt.size).tolist()
           if len(edges) > 1 else [])
    return {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "max_date": str(raw["date_created"].max()),
        "rows": int(len(raw)),
        "max_id": int(raw["id"].max()) if "id" in raw.columns and raw["id"].notna().any() else None,
        "amount_bin_edges": edges,
        "amount_ref_frac": ref,
    }


def run(sample=None, limit=None, promote=False) -> dict:
    t0 = time.time()
    hw = hardware.log_summary(log)
    hardware.require_gpu(log)    # no-op unless BF_REQUIRE_GPU=1 (then fail-fast if no usable GPU)
    version = registry.new_version()
    mdir = registry.model_dir(version)
    mdir.mkdir(parents=True, exist_ok=True)
    log.info("=== training %s (feature_version=%s) ===", version, config.FEATURE_VERSION)

    # 1-3: ingest -> validate -> clean
    raw = ingest.load(limit=limit, sample=sample)
    if raw.empty:
        raise SystemExit("no data in the cache — run the sync/backfill first")
    watermark = _watermark(raw)   # so ml.retrain_trigger can measure the delta + drift later
    valid, vrep = validate.validate(raw)
    train_df, eval_df, crep = clean.clean(valid)
    if train_df.empty:
        raise SystemExit("no eligible clean customers to train on (check BF_MIN_TXNS)")

    # 4: features (customer baseline + graph features). Baselines + graph aggregates are
    # per-customer, label-free descriptors, so they cover ALL eligible customers.
    fb = features.FeatureBuilder()
    gfeat = graph.customer_features(train_df)
    fb.fit(train_df)
    Xall = fb.transform(train_df, gfeat)          # every eligible-customer NORMAL row
    Xev = fb.transform(eval_df, gfeat)
    feat_cols = features.FEATURES
    import joblib
    joblib.dump({"baselines": fb.baselines, "global": fb.global_,
                 "feature_version": fb.feature_version,
                 "features": list(features.FEATURES)},   # schema guard: what this model was trained on
                mdir / "featurebuilder.joblib")
    # persist per-customer graph features so inference computes the SAME g_* values it trained on
    # (otherwise a single-row payload has g_*=0 — a train/serve skew that inflates scores).
    if not gfeat.empty:
        gf_map = gfeat.set_index("customer_key")[["g_fanout", "g_distinct_benef", "g_shared_cp"]] \
                      .to_dict("index")
        joblib.dump(gf_map, mdir / "graph_features.joblib")

    # 4b: 80/20 split at the CUSTOMER level (no leakage — a customer is entirely train or
    # holdout). The density detectors learn the normal manifold on 80%; the unseen 20% of
    # normal customers validate generalisation and calibrate the alert threshold (PDF §1 clean
    # split; workflow step 10 "validate against a holdout before deployment").
    keys = np.array(sorted(fb.baselines.keys()))
    rng = np.random.default_rng(config.SYNTH_SEED)
    hold_keys = set(rng.choice(keys, size=max(1, int(len(keys) * config.HOLDOUT_FRAC)),
                               replace=False).tolist())
    is_hold = Xall["customer_key"].isin(hold_keys)
    Xtr, Xhold = Xall[~is_hold].copy(), Xall[is_hold].copy()
    train_keys = [k for k in keys if k not in hold_keys]
    crep["holdout_customers"] = len(hold_keys)
    crep["train_customers"] = len(train_keys)
    crep["holdout_rows"] = int(len(Xhold))
    log.info("split: %d train customers / %d holdout customers | %d train rows / %d holdout rows",
             len(train_keys), len(hold_keys), len(Xtr), len(Xhold))

    # 5: GNN embeddings — trained on the 80% training customers only (GPU-aware, optional)
    gnn = GNNDetector()
    gnn.fit(graph.build_edges(train_df[train_df["customer_key"].isin(set(train_keys))]))
    gnn.save(mdir)

    # 6: Isolation Forest + Autoencoder — fit on the 80% training rows only
    iso = IsoForestDetector().fit(Xtr[feat_cols].to_numpy()); iso.save(mdir)
    ae = AutoencoderDetector().fit(Xtr[feat_cols].to_numpy()); ae.save(mdir)

    # 7: ensemble scorer — include ONLY the detectors that actually ran
    ens = Ensemble()

    def _scores(X):
        s = {"isoforest": iso.score(X[feat_cols].to_numpy())}
        if ae.available and ae.model is not None:
            s["autoencoder"] = ae.score(X[feat_cols].to_numpy())
        if gnn.available and gnn.customer_score_:
            s["gnn"] = gnn.score(X["customer_key"].to_numpy())
        return s

    def _risk(X):
        return ens.blend(_scores(X))["risk_score"]

    blended = ens.blend(_scores(Xev))
    risk = blended["risk_score"]

    # 8: tiering — calibrate the alert cut-offs on the UNSEEN holdout-normal distribution
    # (PDF: threshold at the 98th/99th percentile of the validation distribution), falling back
    # to train risk only if the holdout is empty.
    calib = _risk(Xhold) if len(Xhold) else _risk(Xtr)
    tiering = Tiering().fit(calib)
    (mdir / "tiering.json").write_text(json.dumps(tiering.to_dict()))
    (mdir / "ensemble.json").write_text(json.dumps({"weights": ens.weights}))

    # 8b: unsupervised validation on the holdout normal (synthetic-anomaly AUC + contamination)
    unsup = unsupervised.evaluate(_risk, Xhold, tag=version) if len(Xhold) else None

    # eval: metrics + plots (weak proxy labels) + the unsupervised validation section
    histories = {"autoencoder": ae.history, "gnn": gnn.history}
    m = evalm.evaluate(risk, Xev["label"].to_numpy(), histories=histories, tag=version,
                       unsupervised=unsup)

    synth_auc = (unsup or {}).get("synthetic_auc")
    manifest = {
        "feature_version": config.FEATURE_VERSION,
        "data": {"train_rows": crep["train_rows"], "eval_rows": crep["eval_rows"],
                 "eligible_customers": crep["eligible_customers"],
                 "train_customers": crep.get("train_customers"),
                 "holdout_customers": crep.get("holdout_customers"),
                 "min_txns": crep.get("min_txns"), "min_days_active": crep.get("min_days_active"),
                 "lookback_months": config.LOOKBACK_MONTHS},
        "hardware": hw,
        "detectors": blended["detectors"],
        "metrics": {k: m[k] for k in ("accuracy", "precision", "recall", "f1")
                    if k in m} | {k: m[k] for k in ("roc_auc", "pr_ap") if k in m},
        "unsupervised_validation": m.get("unsupervised_validation"),
        "data_watermark": watermark,
        "plots": m["plots"],
        "elapsed_sec": round(time.time() - t0, 1),
        "validation": vrep, "cleaning": crep,
    }
    registry.register(version, manifest)
    # stable, easy-to-find copy of the current run's metrics (+ a canonical metrics.json)
    (config.DIR_METRICS / "metrics.json").write_text(json.dumps(
        {"active_after_run": version, **{k: manifest[k] for k in
         ("detectors", "metrics", "unsupervised_validation", "data", "elapsed_sec")}}, indent=2))
    # record performance-over-time + run the health gate (alerts if below the floors)
    try:
        from . import monitor
        monitor.record(version, manifest)
        monitor.check_health(version, manifest, alert=True)
        monitor.write_thresholds(version)   # regenerate the fraud-team threshold handover (dynamic)
    except Exception as e:                       # monitoring must never fail a training run
        log.warning("monitor step failed: %s", e)
    log.info("=== done %s in %.1fs | detectors=%s | synthetic-AUC=%s | f1(proxy)=%.3f ===",
             version, manifest["elapsed_sec"], blended["detectors"],
             f"{synth_auc:.3f}" if synth_auc is not None else "n/a", m["f1"])

    if promote:
        # promotion gate: prefer the label-free synthetic-anomaly AUC (our real quality signal);
        # fall back to proxy F1 when the holdout was empty. Must be >= the current active.
        cur = registry.active(); curm = (registry.get(cur) or {}) if cur else {}
        if synth_auc is not None:
            cur_auc = ((curm.get("unsupervised_validation") or {}).get("synthetic_auc")) or 0.0
            ok, why = synth_auc >= cur_auc, f"synthetic-AUC {synth_auc:.3f} vs active {cur_auc:.3f}"
        else:
            cur_f1 = curm.get("metrics", {}).get("f1", 0.0)
            ok, why = m["f1"] >= cur_f1, f"f1 {m['f1']:.3f} vs active {cur_f1:.3f}"
        if ok:
            registry.promote(version)
        else:
            log.warning("registry: NOT promoting %s (%s)", version, why)
    return manifest


def main():
    ap = argparse.ArgumentParser(description="Train the behavioural anti-fraud ensemble")
    ap.add_argument("--sample", type=float, default=None, help="fraction of rows (dev)")
    ap.add_argument("--limit", type=int, default=None, help="row cap (dev)")
    ap.add_argument("--promote", action="store_true", help="promote to active if it validates")
    a = ap.parse_args()
    print(json.dumps(run(sample=a.sample, limit=a.limit, promote=a.promote), indent=2, default=str))


if __name__ == "__main__":
    main()

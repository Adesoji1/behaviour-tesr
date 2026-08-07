"""
Retraining trigger (PDF §4 & §14) — retrain an existing model when ANY condition holds (an OR):

  * NEW transactions since training  >= BF_RETRAIN_MIN_NEW   (default 100), OR
  * days since training              >= BF_RETRAIN_MAX_DAYS   (default 30),  OR
  * behavioural DRIFT on the new data (amount-distribution PSI) >= BF_DRIFT_PSI (default 0.25).

The delta is measured against the active model's data_watermark (written by ml.train). This is
the OR of §4 — refresh cadence — as opposed to §1 eligibility (an AND). Read-only DB access.

    python -m ml.retrain_trigger          # print the decision (does nothing else)
    python -m ml.retrain_trigger --run    # retrain + promote if any trigger fired
    # schedule this (cron) to get event/drift/periodic retraining automatically.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

import numpy as np

from . import config, db, registry

log = logging.getLogger("ml.retrain_trigger")


def _psi(ref_frac, cur_frac) -> float:
    """Population Stability Index between two binned distributions (0=identical)."""
    eps = 1e-6
    ref = np.asarray(ref_frac, dtype=float) + eps
    cur = np.asarray(cur_frac, dtype=float) + eps
    return float(np.sum((cur - ref) * np.log(cur / ref)))


def _amount_drift(watermark: dict, since) -> float | None:
    edges = watermark.get("amount_bin_edges") or []
    ref = watermark.get("amount_ref_frac") or []
    if len(edges) < 2 or not ref:
        return None
    df = db.read_sql("SELECT amount FROM bp_transactions_cache "
                     "WHERE date_created > %s AND amount > 0", (since,))
    if df.empty:
        return 0.0
    amt = df["amount"].to_numpy(dtype=float)
    cur = np.histogram(amt, bins=edges)[0] / max(len(amt), 1)
    return _psi(ref, cur)


def evaluate() -> dict:
    active = registry.active()
    if not active:
        return {"should_retrain": True, "reasons": ["no active model — train one first"]}
    man = registry.get(active) or {}
    wm = man.get("data_watermark") or {}
    since = wm.get("max_date")
    trained_at = wm.get("trained_at")

    new_txns = (int(db.scalar("SELECT count(*) FROM bp_transactions_cache "
                              "WHERE date_created > %s", (since,)) or 0) if since else None)
    days = None
    if trained_at:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(trained_at)).days
    psi = _amount_drift(wm, since) if since else None

    reasons = []
    if new_txns is not None and new_txns >= config.RETRAIN_MIN_NEW_TXNS:
        reasons.append(f"new_transactions={new_txns} >= {config.RETRAIN_MIN_NEW_TXNS}")
    if days is not None and days >= config.RETRAIN_MAX_AGE_DAYS:
        reasons.append(f"age_days={days} >= {config.RETRAIN_MAX_AGE_DAYS}")
    if psi is not None and psi >= config.DRIFT_PSI_THRESHOLD:
        reasons.append(f"amount_drift_psi={psi:.3f} >= {config.DRIFT_PSI_THRESHOLD}")

    return {
        "active_model": active,
        "should_retrain": bool(reasons),
        "reasons": reasons,
        "signals": {"new_transactions": new_txns, "age_days": days,
                    "amount_drift_psi": None if psi is None else round(psi, 4)},
        "thresholds": {"min_new_txns": config.RETRAIN_MIN_NEW_TXNS,
                       "max_age_days": config.RETRAIN_MAX_AGE_DAYS,
                       "drift_psi": config.DRIFT_PSI_THRESHOLD},
    }


def main():
    ap = argparse.ArgumentParser(description="Behavioural model retraining trigger (§4/§14)")
    ap.add_argument("--run", action="store_true", help="retrain + promote if a trigger fired")
    a = ap.parse_args()
    decision = evaluate()
    print(json.dumps(decision, indent=2, default=str))
    if a.run and decision["should_retrain"]:
        log.info("retrain_trigger: firing retrain — %s", "; ".join(decision["reasons"]))
        from . import train
        train.run(promote=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    config.attach_file_log()
    main()

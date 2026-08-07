"""
Stage 3 — CLEAN + establish the TRAINING population (Practical rules §1 & §7).

The behavioural baseline must be learned from legitimate, well-evidenced customers only:
  * learn ONLY from clean transactions (exclude blocked / blacklisted / non-clean),
  * a customer needs a minimum history to be eligible,
  * confirmed-bad transactions are NEVER used for training (they are the thing we detect).

Returns:
  train_df  — clean transactions of eligible customers (used to fit the models)
  eval_df   — a labelled evaluation set (clean vs weak-proxy-bad) for metrics/plots ONLY
"""
from __future__ import annotations

import logging

import pandas as pd

from .. import config

log = logging.getLogger("ml.clean")


def _is_bad(df: pd.DataFrame) -> pd.Series:
    """Weak proxy label (eval only): blocked / blacklisted / non-clean status."""
    status = df.get("status")
    blocked = df.get("is_blocked")
    black = df.get("sender_blacklisted")
    bad = pd.Series(False, index=df.index)
    if status is not None:
        bad |= status.astype(str).str.lower().ne("clean") & status.notna()
    if blocked is not None:
        bad |= blocked.fillna(False).astype(bool)
    if black is not None:
        bad |= black.fillna(False).astype(bool)
    return bad


def _load_feedback() -> dict[str, str]:
    """Analyst verdicts from bp_decision_feedback: {transaction_id -> 'genuine'|'fraud'} (the §11
    loop, wired via POST /feedback). Best-effort — if the store/table is unavailable we simply
    train without overrides (never fail a retrain on the feedback read)."""
    try:
        from .. import db
        fb = db.read_sql("SELECT transaction_id, verdict FROM bp_decision_feedback "
                         "WHERE transaction_id IS NOT NULL")
    except Exception as e:                       # table missing / store down -> no overrides
        log.info("clean: no analyst feedback applied (%s)", e)
        return {}
    if fb is None or fb.empty:
        return {}
    m = {str(t): str(v).strip().lower()
         for t, v in zip(fb["transaction_id"], fb["verdict"])
         if str(v).strip().lower() in ("genuine", "fraud")}
    log.info("clean: loaded %d analyst verdicts (feedback loop)", len(m))
    return m


def _apply_feedback(df: pd.DataFrame, bad: pd.Series, feedback: dict[str, str]) -> pd.Series:
    """Override the weak proxy label with confirmed analyst ground truth (§7/§11): a 'fraud' verdict
    forces the row bad (excluded from clean training); a 'genuine' verdict forces it clean (learned
    as normal even if the source status looked suspicious). Returns the corrected `bad` mask."""
    if not feedback or "transaction_id" not in df.columns:
        return bad
    bad = bad.copy()
    verdicts = df["transaction_id"].astype(str).map(feedback)
    forced_fraud = verdicts.eq("fraud")
    forced_genuine = verdicts.eq("genuine")
    bad.loc[forced_fraud.values] = True
    bad.loc[forced_genuine.values] = False
    n_fraud, n_genuine = int(forced_fraud.sum()), int(forced_genuine.sum())
    if n_fraud or n_genuine:
        log.info("clean: applied analyst feedback — %d forced fraud (excluded), "
                 "%d forced genuine (kept clean)", n_fraud, n_genuine)
    return bad


def clean(df: pd.DataFrame, feedback: dict[str, str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    report: dict = {"rows_in": len(df)}
    if df.empty:
        return df, df, report

    df = df.copy()
    bad = _is_bad(df)
    # §11 feedback loop: analyst-confirmed ground truth overrides the weak proxy label before the
    # clean/fraud split, so confirmed-genuine rows are LEARNED and confirmed-fraud rows are EXCLUDED.
    fb = _load_feedback() if feedback is None else feedback
    report["feedback_verdicts"] = len(fb or {})
    df["_bad"] = _apply_feedback(df, bad, fb or {})

    clean_rows = df[~df["_bad"]].copy()
    report["clean_rows"] = len(clean_rows)
    report["bad_rows_weak_label"] = int(df["_bad"].sum())

    # §1 eligibility is an AND: enough CLEAN transactions AND enough days of history AND no
    # confirmed fraud (the confirmed-fraud/blocked rows are already excluded from clean_rows).
    grp = clean_rows.groupby("customer_key")
    counts = grp.size()
    if "date_created" in clean_rows.columns:
        span = grp["date_created"].agg(lambda s: (s.max() - s.min()).days if len(s) else 0)
    else:
        span = pd.Series(config.MIN_DAYS_ACTIVE, index=counts.index)
    enough_txns = counts >= config.MIN_TXNS_PER_CUSTOMER
    enough_days = span >= config.MIN_DAYS_ACTIVE
    eligible = set(counts[enough_txns & enough_days].index)
    report["eligible_customers"] = len(eligible)
    report["eligible_by_txns_only"] = int(enough_txns.sum())
    report["eligible_by_days_only"] = int(enough_days.sum())
    report["min_txns"] = config.MIN_TXNS_PER_CUSTOMER
    report["min_days_active"] = config.MIN_DAYS_ACTIVE

    # TRAIN: clean transactions of eligible customers only (§1/§7 — never learn bad data)
    train_df = clean_rows[clean_rows["customer_key"].isin(eligible)].copy()
    report["train_rows"] = len(train_df)

    # EVAL: same eligible customers, but keep BOTH clean and weak-bad rows, labelled.
    # Used ONLY to compute precision/recall/F1/confusion until the real feedback loop exists.
    eval_df = df[df["customer_key"].isin(eligible)].copy()
    eval_df["label"] = eval_df["_bad"].astype(int)   # 1 = (weak) bad, 0 = clean
    report["eval_rows"] = len(eval_df)
    report["eval_bad"] = int(eval_df["label"].sum())

    log.info("clean: %s", {k: report[k] for k in
                           ("clean_rows", "bad_rows_weak_label", "eligible_customers",
                            "eligible_by_txns_only", "eligible_by_days_only",
                            "train_rows", "eval_rows", "eval_bad")})
    return train_df.drop(columns=["_bad"]), eval_df.drop(columns=["_bad"]), report

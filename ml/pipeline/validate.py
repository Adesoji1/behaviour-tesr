"""
Stage 2 — VALIDATE.

Structural / data-quality checks before we learn anything. Returns a report and the subset of
rows fit to proceed. We do NOT silently drop everything; we log what and why (auditable).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .. import config

log = logging.getLogger("ml.validate")


def validate(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    report: dict = {"rows_in": len(df)}
    if df.empty:
        return df, report

    ok = pd.Series(True, index=df.index)

    # amount must be a sane positive number (drops corrupt / impossible values, §1 cleaning)
    bad_amount = ~(df["amount"].notna() & (df["amount"] > 0) & (df["amount"] <= config.MAX_SANE_AMOUNT))
    report["dropped_bad_amount"] = int(bad_amount.sum())
    ok &= ~bad_amount

    # timestamp must parse
    bad_ts = df["date_created"].isna()
    report["dropped_bad_timestamp"] = int(bad_ts.sum())
    ok &= ~bad_ts

    # must have the customer key
    bad_key = df["customer_key"].isna() | (df["customer_key"].astype(str).str.len() == 0)
    report["dropped_no_customer_key"] = int(bad_key.sum())
    ok &= ~bad_key

    # exact-duplicate transaction_ids: keep the first (dedupe, §1)
    dup = df["transaction_id"].notna() & df["transaction_id"].duplicated(keep="first")
    report["dropped_duplicate_txn_id"] = int(dup.sum())
    ok &= ~dup

    out = df[ok].copy()
    report["rows_out"] = len(out)
    report["distinct_customers"] = int(out["customer_key"].nunique())
    log.info("validate: %s", {k: report[k] for k in report if k.startswith(("rows", "dropped"))})
    return out, report

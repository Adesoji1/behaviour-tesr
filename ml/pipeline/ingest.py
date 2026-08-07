"""
Stage 1 — INGEST.

Load transactions from the local cache (bp_transactions_cache), READ-ONLY. The cache is
already populated by the scheduled sync job, so this never touches production. We pull the
columns the behavioural features need, keyed so each row can be attributed to a CUSTOMER
(the stable `identifier`) — per 1.md, branch and transaction_type are attributes, not identity.

`limit` / `sample` support fast dev iterations; a full run passes neither.
"""
from __future__ import annotations

import logging

import pandas as pd

from .. import config, db

log = logging.getLogger("ml.ingest")

# Columns the behavioural pipeline uses. `identifier` is the customer key; branch_id,
# transaction_type, bank codes, ip, location, counterparties are features/context.
COLS = [
    "id", "transaction_id", "identifier", "identifier_type_id", "bvn",
    "amount", "currency", "transaction_type", "transaction_type_normalized",
    "branch_id", "origin_account_no", "destination_account_no", "destination_bank_code",
    "account_type", "customer_ip_address", "customer_location",
    "origin_country", "destination_country", "date_created",
    "status", "is_blocked", "sender_blacklisted",   # weak proxy labels (eval only)
]


def load(limit: int | None = None, sample: float | None = None,
         within_window: bool = True) -> pd.DataFrame:
    """Return a DataFrame of cached transactions with a resolved `customer_key`.

    customer_key = the stable customer identifier. Rows with no identifier cannot be
    attributed to a customer and are dropped here (1.md: never key card/airtime on their own).
    """
    where = ["identifier IS NOT NULL"]
    if within_window:
        where.append(
            f"date_created >= (now() - interval '{int(config.LOOKBACK_MONTHS)} months')")
    sql = f"SELECT {', '.join(COLS)} FROM {config.CACHE_TABLE} WHERE {' AND '.join(where)}"
    if sample and 0 < sample < 1:
        sql += f" AND random() < {float(sample)}"
    sql += " ORDER BY identifier, date_created"
    if limit:
        sql += f" LIMIT {int(limit)}"

    log.info("ingest: querying cache (limit=%s sample=%s window=%s)", limit, sample, within_window)
    df = db.read_sql(sql)
    if df.empty:
        log.warning("ingest: no rows returned")
        return df

    # the customer key (1.md): the person, not the account
    df["customer_key"] = df["identifier"].astype(str)
    df["date_created"] = pd.to_datetime(df["date_created"], utc=True, errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    log.info("ingest: %d rows, %d distinct customers", len(df), df["customer_key"].nunique())
    return df

"""
Stage 4b / 5 input — GRAPH construction and graph features.

We build the graph we actually have (1.md: we only see our own customers' transactions, no
richer network): a bipartite **customer -> counterparty** graph, edges = origin customer paying
a destination account. From it we derive:

  * per-customer graph FEATURES (fan-out, distinct beneficiaries, shared-counterparty score) —
    merged into the feature matrix for Isolation Forest / Autoencoder, and
  * the edge list + node index for the GNN (ml.models.gnn) to learn structural embeddings.

The `shared_cp` signal is the honest, high-value one: it counts how many DISTINCT customers pay
each destination, so an account that many of our customers suddenly pay (a mule collector)
scores high — detectable even though we never see what that account does next.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger("ml.graph")


def customer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-customer graph features, returned keyed by customer_key."""
    d = df.dropna(subset=["destination_account_no"]).copy()
    d["destination_account_no"] = d["destination_account_no"].astype(str)
    if d.empty:
        return pd.DataFrame(columns=["customer_key", "g_fanout", "g_distinct_benef", "g_shared_cp"])

    # how many distinct customers pay each destination (collector-account signal)
    cp_pop = d.groupby("destination_account_no")["customer_key"].nunique()
    d["_cp_pop"] = d["destination_account_no"].map(cp_pop)

    agg = d.groupby("customer_key").agg(
        g_distinct_benef=("destination_account_no", "nunique"),
        g_fanout=("destination_account_no", "count"),
        g_shared_cp=("_cp_pop", "max"),
    ).reset_index()
    # normalise fan-out to a ratio (distinct / total), log the popularity
    agg["g_fanout"] = agg["g_distinct_benef"] / agg["g_fanout"].clip(lower=1)
    agg["g_distinct_benef"] = np.log1p(agg["g_distinct_benef"])
    agg["g_shared_cp"] = np.log1p(agg["g_shared_cp"].fillna(1))
    log.info("graph: features for %d customers", len(agg))
    return agg


def build_edges(df: pd.DataFrame) -> dict:
    """Edge list + node index for the GNN.

    Returns node names (customers first, then counterparties), the [2, E] edge_index (customer
    -> counterparty), and the count of customer nodes (so the GNN can read back their
    embeddings). Counterparties are 'thin' nodes (structure only)."""
    d = df.dropna(subset=["destination_account_no"]).copy()
    d["destination_account_no"] = "cp:" + d["destination_account_no"].astype(str)
    d["customer_key"] = "cu:" + d["customer_key"].astype(str)
    if d.empty:
        return {"nodes": [], "edge_index": np.zeros((2, 0), dtype=np.int64), "n_customers": 0,
                "customer_keys": []}

    customers = pd.Index(d["customer_key"].unique())
    counterparties = pd.Index(d["destination_account_no"].unique()).difference(customers)
    nodes = customers.append(counterparties)
    idx = {n: i for i, n in enumerate(nodes)}

    src = d["customer_key"].map(idx).to_numpy()
    dst = d["destination_account_no"].map(idx).to_numpy()
    edge_index = np.vstack([src, dst]).astype(np.int64)
    log.info("graph: %d nodes (%d customers, %d counterparties), %d edges",
             len(nodes), len(customers), len(counterparties), edge_index.shape[1])
    return {
        "nodes": nodes.tolist(),
        "edge_index": edge_index,
        "n_customers": len(customers),
        "customer_keys": [n[3:] for n in customers],  # strip 'cu:' prefix
    }

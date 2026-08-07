"""
Stage 4 — FEATURE ENGINEERING (behavioural deviation features).

For each transaction we compute how far it deviates from THAT CUSTOMER's own normal, learned
from their clean history. These deviation vectors are what the unsupervised models learn: a
normal transaction sits near the customer's centre; an anomaly is far out.

`FeatureBuilder.fit(train_df)` learns a compact per-customer baseline (amount stats, hour/day
histograms, usual places / beneficiaries / types / IP subnets). `.transform(df)` turns any
transactions into the numeric feature matrix. Baselines are serialisable so inference computes
identical features for a single live transaction.

Behavioural signals only — NO AML rules (1.md). Branch and transaction_type are features.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .. import config

log = logging.getLogger("ml.features")
EPS = 1e-9


def _ip_subnet(ip):
    if not isinstance(ip, str) or "." not in ip:
        return None
    parts = ip.split(".")
    return ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else None


def _loc(s):
    return s.strip().lower() if isinstance(s, str) and s.strip() else None


def _decay_weights(dates: pd.Series, ref, half_life_days: float) -> np.ndarray:
    """Exponential time-decay weight per transaction: 0.5 ** (age_days / half_life), age measured
    from `ref` (the most recent transaction in the training window). half_life<=0 disables decay
    (all weights 1.0). Recent behaviour therefore dominates the learned baseline (config §decay)."""
    n = len(dates)
    if half_life_days <= 0 or ref is None or n == 0:
        return np.ones(n, dtype=float)
    age_days = (ref - dates).dt.total_seconds().to_numpy(dtype=float) / 86400.0
    age_days = np.clip(np.nan_to_num(age_days, nan=0.0), 0.0, None)   # future/NaT -> 0 (weight 1)
    return np.power(0.5, age_days / float(half_life_days))


def _wmean(x: np.ndarray, w: np.ndarray) -> float:
    sw = w.sum()
    return float((x * w).sum() / sw) if sw > 0 else float(x.mean() if x.size else 0.0)


def _wstd(x: np.ndarray, w: np.ndarray, mean: float) -> float:
    sw = w.sum()
    if sw <= 0 or x.size == 0:
        return 0.0
    return float(np.sqrt(max(0.0, ((x - mean) ** 2 * w).sum() / sw)))


def _wquantile(x: np.ndarray, w: np.ndarray, q: float) -> float:
    """Weighted quantile (q in [0,1]) via the cumulative-midpoint method."""
    if x.size == 0:
        return 0.0
    if w.sum() <= 0:
        return float(np.quantile(x, q))
    order = np.argsort(x)
    xs, ws = x[order], w[order]
    cw = np.cumsum(ws) - 0.5 * ws
    cw /= ws.sum()
    return float(np.interp(q, cw, xs))


def _cross_border(r) -> float:
    """1.0 only when BOTH the origin and destination country are known AND they differ.
    A missing side is unknown, not cross-border, so it must not fire."""
    o, d = r.origin_country, r.destination_country
    if o is None or d is None or o != o or d != d or str(o) == "" or str(d) == "":
        return 0.0
    return float(str(o).strip().lower() != str(d).strip().lower())


# The feature vector, in a fixed order (also the model input contract).
FEATURES = [
    "amt_log", "amt_z", "amt_over_median", "amt_over_p95", "amt_over_max", "above_max",
    "hour_rarity", "dow_rarity", "is_night",
    "location_new", "country_new", "cross_border", "beneficiary_new", "type_rare", "ip_new",
    # velocity / burst — short windows added (1m/2m/3m/10m/15m) + 1h/24h; all log1p-squashed
    "vel_1m", "vel_2m", "vel_3m", "vel_10m", "vel_15m", "vel_1h", "vel_24h",
    "amt_1h_ratio", "recency_hours",
    "g_fanout", "g_distinct_benef", "g_shared_cp",   # graph features (merged in)
]


class FeatureBuilder:
    def __init__(self, feature_version: str = config.FEATURE_VERSION):
        self.feature_version = feature_version
        self.baselines: dict[str, dict] = {}
        self.global_: dict = {}

    # ---- fit: learn per-customer baselines from CLEAN history -----------------
    def fit(self, df: pd.DataFrame) -> "FeatureBuilder":
        log.info("features.fit: learning baselines for %d customers", df["customer_key"].nunique())
        g = df.copy()
        g["hour"] = g["date_created"].dt.hour
        g["dow"] = g["date_created"].dt.dayofweek
        g["ip_subnet"] = g["customer_ip_address"].map(_ip_subnet)
        g["loc"] = g["customer_location"].map(_loc)

        self.global_ = {
            "amt_median": float(np.nanmedian(g["amount"])),
            "amt_p95": float(np.nanpercentile(g["amount"], 95)),
        }
        # Time-decay reference = the most recent transaction in the training window. Every
        # customer's baseline is weighted toward their RECENT behaviour (config.DECAY_HALF_LIFE_DAYS).
        half_life = getattr(config, "DECAY_HALF_LIFE_DAYS", 0.0)
        ref_date = g["date_created"].max() if "date_created" in g.columns else None
        self.decay_half_life_days = float(half_life)
        for key, grp in g.groupby("customer_key", sort=False):
            amt = grp["amount"].to_numpy(dtype=float)
            w = _decay_weights(grp["date_created"], ref_date, half_life) \
                if "date_created" in grp.columns else np.ones(len(grp), dtype=float)
            hrs = np.bincount(grp["hour"].to_numpy(), minlength=24, weights=w).astype(float)
            dows = np.bincount(grp["dow"].to_numpy(), minlength=7, weights=w).astype(float)
            amt_mean = _wmean(amt, w)
            self.baselines[str(key)] = {
                "n": int(len(grp)),
                "amt_mean": float(amt_mean), "amt_std": float(_wstd(amt, w, amt_mean) or 1.0),
                "amt_median": float(_wquantile(amt, w, 0.50)),
                "amt_p95": float(_wquantile(amt, w, 0.95)),
                # the historical MAX is a hard ceiling ("above anything ever seen") — NOT decayed,
                # so a large-but-old legitimate transaction still counts as previously-seen.
                "amt_max": float(amt.max()),
                "hour_hist": (hrs / (hrs.sum() or 1)).tolist(),
                "dow_hist": (dows / (dows.sum() or 1)).tolist(),
                "locs": set(grp["loc"].dropna().unique().tolist()),
                "countries": set(grp["origin_country"].dropna().astype(str).unique().tolist()),
                "benefs": set(grp["destination_account_no"].dropna().astype(str).unique().tolist()),
                # canonicalise identically to the /score validator (strip+lower) so the learned
                # vocabulary and the incoming transaction_type compare on the same footing.
                "types": set(config.normalize_transaction_type(x)
                             for x in grp["transaction_type"].dropna().astype(str)),
                "ips": set(grp["ip_subnet"].dropna().unique().tolist()),
            }
        return self

    # ---- transform: per-transaction deviation features ------------------------
    def transform(self, df: pd.DataFrame, graph_feats: pd.DataFrame | None = None) -> pd.DataFrame:
        g = df.copy().reset_index(drop=True)
        g["hour"] = g["date_created"].dt.hour
        g["dow"] = g["date_created"].dt.dayofweek
        g["ip_subnet"] = g["customer_ip_address"].map(_ip_subnet)
        g["loc"] = g["customer_location"].map(_loc)

        rows = []
        gm, gp = self.global_.get("amt_median", 1.0), self.global_.get("amt_p95", 1.0)
        for r in g.itertuples(index=False):
            b = self.baselines.get(str(r.customer_key))
            amt = float(r.amount) if r.amount == r.amount else 0.0
            if b is None:  # unseen customer (cold start) -> NEUTRAL; caller sees is_cold_start.
                # We cannot judge novelty without a baseline, so do not manufacture anomaly
                # signals here (that wrongly flagged every new customer). Warming-up handling
                # (peer baseline / monitor) is the caller's job, per PDF §2/§10.
                rows.append([np.log1p(amt), 0.0, amt / (gm + EPS), amt / (gp + EPS), 0.0, 0.0,
                             0.5, 0.5, float(int(r.hour) in (0, 1, 2, 3, 4, 5)) if r.hour == r.hour else 0.0,
                             0.0, 0.0, _cross_border(r), 0.0, 0.0, 0.0,
                             0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,   # 9 velocity
                             0.0, 0.0, 0.0])                                # 3 graph
                continue
            hour_freq = b["hour_hist"][int(r.hour)] if r.hour == r.hour else 0.0
            dow_freq = b["dow_hist"][int(r.dow)] if r.dow == r.dow else 0.0
            # Rarity is RELATIVE to the customer's own peak hour/day, so their usual time reads
            # as 0 (not rare) and a time they never use reads as 1 — a spread-out schedule no
            # longer makes every hour look rare.
            hmax = max(b["hour_hist"]) or 1.0
            dmax = max(b["dow_hist"]) or 1.0
            # A novelty flag only fires when we have an ESTABLISHED set of observed values AND
            # the incoming value is outside it. An empty set means "never observed" -> neutral 0,
            # so unpopulated country/IP/location fields cannot fabricate anomalies.
            rows.append([
                np.log1p(amt),
                (amt - b["amt_mean"]) / (b["amt_std"] + EPS),
                amt / (b["amt_median"] + EPS),
                amt / (b["amt_p95"] + EPS),
                amt / (b["amt_max"] + EPS),
                float(amt > b["amt_max"]),
                float(np.clip(1.0 - hour_freq / hmax, 0.0, 1.0)),
                float(np.clip(1.0 - dow_freq / dmax, 0.0, 1.0)),
                float(int(r.hour) in (0, 1, 2, 3, 4, 5)) if r.hour == r.hour else 0.0,
                float(bool(b["locs"]) and r.loc is not None and r.loc not in b["locs"]),
                float(bool(b["countries"]) and bool(r.origin_country)
                      and str(r.origin_country) not in b["countries"]),
                _cross_border(r),
                float(bool(b["benefs"]) and bool(r.destination_account_no)
                      and str(r.destination_account_no) not in b["benefs"]),
                float(bool(b["types"]) and bool(r.transaction_type)
                      and config.normalize_transaction_type(r.transaction_type) not in b["types"]),
                float(bool(b["ips"]) and r.ip_subnet is not None and r.ip_subnet not in b["ips"]),
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,   # 9 velocity, filled below
                0.0, 0.0, 0.0,               # graph filled below
            ])
        X = pd.DataFrame(rows, columns=FEATURES)

        # velocity (preceding-window counts per customer) — vectorised per group
        vel = _velocity(g)
        for c in _VELOCITY_COLS:
            X[c] = vel[c].to_numpy()

        # graph features merged by customer_key
        if graph_feats is not None and not graph_feats.empty:
            gf = g[["customer_key"]].merge(graph_feats, on="customer_key", how="left")
            for c in ("g_fanout", "g_distinct_benef", "g_shared_cp"):
                if c in gf:
                    X[c] = gf[c].fillna(0.0).to_numpy()

        X = X.replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(np.float32)
        # carry identity + weak label alongside (never fed to the model)
        X.insert(0, "customer_key", g["customer_key"].values)
        X.insert(1, "transaction_id", g["transaction_id"].values)
        if "label" in g:
            X["label"] = g["label"].to_numpy()
        return X

    def fit_transform(self, train_df, graph_feats=None):
        return self.fit(train_df).transform(train_df, graph_feats)

    @property
    def n_features(self) -> int:
        return len(FEATURES)


# Velocity/burst windows in MINUTES. Short windows (1m/2m/3m) catch rapid bursts; 10m/15m the
# medium window; 1h/24h the day-scale. Counts are log1p-squashed (below) so the heavy upper tail
# of a bursty customer no longer stretches the normal distribution.
_VEL_WINDOWS_MIN = {"vel_1m": 1, "vel_2m": 2, "vel_3m": 3, "vel_10m": 10, "vel_15m": 15,
                    "vel_1h": 60, "vel_24h": 1440}
_VELOCITY_COLS = list(_VEL_WINDOWS_MIN) + ["amt_1h_ratio", "recency_hours"]
_NS_MIN = 60 * 1_000_000_000


def _velocity(g: pd.DataFrame) -> pd.DataFrame:
    """Per-customer preceding-window counts using sorted-time searchsorted (fast). All counts are
    log1p-transformed to compress the long tail (Part 2: feature tail squashing)."""
    out = pd.DataFrame(index=g.index, data={c: 0.0 for c in _VELOCITY_COLS})
    ts = g["date_created"].astype("int64").to_numpy()  # ns
    amt = g["amount"].to_numpy(dtype=float)
    for _, idx in g.groupby("customer_key", sort=False).indices.items():
        idx = np.asarray(idx)
        order = np.argsort(ts[idx]); idx = idx[order]
        t = ts[idx]; a = amt[idx]; pos = np.arange(len(t))
        for col, mins in _VEL_WINDOWS_MIN.items():
            lo = np.searchsorted(t, t - mins * _NS_MIN, side="left")
            out.loc[idx, col] = np.log1p((pos - lo).astype(float))     # count, log1p-squashed
        lo1 = np.searchsorted(t, t - 60 * _NS_MIN, side="left")
        cum = np.concatenate([[0.0], np.cumsum(a)])
        med = np.median(a) or 1.0
        out.loc[idx, "amt_1h_ratio"] = np.log1p((cum[pos + 1] - cum[lo1]) / med)  # long tail -> log1p
        prev_t = np.concatenate([[t[0]], t[:-1]])
        out.loc[idx, "recency_hours"] = np.clip((t - prev_t) / 3.6e12, 0, 24 * 30)
    return out

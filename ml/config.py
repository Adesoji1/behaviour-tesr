"""
Central, env-driven configuration for the behavioural anti-fraud ML subsystem.

Everything tunable lives here. Paths, DB connection, the §1 eligibility gate (train only on
active/trusted/clean customers), feature/model versioning, and per-model hyper-parameters.
Nothing is hard-coded that an operator might reasonably want to change.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Identity & versioning ---------------------------------------------------
# Bumped whenever the feature set changes, so every model/prediction is traceable (§12).
# -2 = per-customer baselines are now TIME-DECAYED (recent behaviour weighted more).
FEATURE_VERSION = os.getenv("BF_FEATURE_VERSION", "feat-2026.08-2")
MODEL_FAMILY = os.getenv("BF_MODEL_FAMILY", "bf-ensemble")

# --- Behaviour time-decay (applied INSIDE the model's baselines) -------------
# People's "normal" drifts, so recent history should count more than old history when we learn
# the per-customer baseline the detectors compare against. Each clean transaction is weighted by
# an exponential half-life: weight = 0.5 ** (age_days / half_life), age measured from the most
# recent transaction in the training window. So with a 90-day half-life a 90-day-old transaction
# counts half as much as today's, 180-day-old a quarter, etc. This makes the amount mean/std/
# median/p95 and the hour/day histograms LEAN TOWARD RECENT BEHAVIOUR — it is applied in
# ml.pipeline.features.FeatureBuilder.fit (the /score decision path), not only in the statistical
# profile layer (retrain.py). Set the half-life to 0 to disable decay (uniform weighting).
DECAY_HALF_LIFE_DAYS = float(os.getenv("BF_DECAY_HALF_LIFE_DAYS", "90"))

# --- Paths (all artifacts are git-ignored) -----------------------------------
ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = Path(os.getenv("BF_ARTIFACTS_DIR", str(ROOT / "artifacts")))
DIR_MODELS = ARTIFACTS / "models"        # serialized models per version
DIR_PLOTS = ARTIFACTS / "plots"          # evaluation & training figures
DIR_METRICS = ARTIFACTS / "metrics"      # metrics JSON per run
DIR_REGISTRY = ARTIFACTS / "registry"    # model registry (versions, status)
DIR_DATA = ARTIFACTS / "data"            # cached feature matrices (parquet)
DIR_INFERENCE_LOG = ARTIFACTS / "inference_log"   # per-inference compliance audit (JSONL/day)
DIR_MONITOR = ARTIFACTS / "monitor"      # model-performance-over-time history + health
for _d in (DIR_MODELS, DIR_PLOTS, DIR_METRICS, DIR_REGISTRY, DIR_DATA,
           DIR_INFERENCE_LOG, DIR_MONITOR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Store database (READ-ONLY: the behaviour profile store) -----------------
# On the host the store Postgres is published on localhost:5433 (see docker-compose).
# Inside a container use the compose service name. All overridable via env.
PG = {
    "host": os.getenv("BF_PG_HOST", os.getenv("STORE_PG_HOST", "localhost")),
    "port": int(os.getenv("BF_PG_PORT", os.getenv("STORE_PG_PORT", "5433"))),
    "user": os.getenv("BF_PG_USER", os.getenv("STORE_PG_USER", "behaviour")),
    "password": os.getenv("BF_PG_PASSWORD", os.getenv("STORE_PG_PASSWORD", "")),
    "dbname": os.getenv("BF_PG_DB", os.getenv("STORE_PG_DB", "behaviour")),
}


def pg_dsn() -> str:
    return (f"host={PG['host']} port={PG['port']} user={PG['user']} "
            f"password={PG['password']} dbname={PG['dbname']} "
            f"options='-c default_transaction_read_only=on'")


def normalize_transaction_type(raw) -> str:
    """Canonical form of a transaction channel/type.

    `transaction_type` is a MODEL FEATURE: the model learns each customer's channel vocabulary
    (the per-customer `types` set → the `type_rare` signal) from the RAW values in the store. So the
    SAME canonicalisation MUST run at BOTH training (the feature builder) and scoring (the /score
    validator) — otherwise the two drift apart and `type_rare` mis-fires.

    We ONLY strip + lowercase — matching how the values already appear in `bp_transactions_cache`
    (all lowercase). We deliberately do NOT collapse to a fixed enum and do NOT normalise separators
    (the store holds e.g. `bank transfer`, `bank-transfer`, `bank_transfer` as DISTINCT values, so
    collapsing them would mismatch the already-trained baseline). Real platform values
    (`inward_transfer`, `vas`, `ussd_session`, …) therefore pass through unchanged. Idempotent."""
    return str(raw if raw is not None else "").strip().lower()


# --- live-velocity feed (Redis) — OPTIONAL enrichment for the velocity/recency features ----------
# When BP_REDIS_URL is set, /score records each transaction in Redis and reads the customer's very
# recent ones back, so vel_* / amt_1h_ratio / recency reflect activity not yet in bp_transactions_cache
# (real-time bursts across separate /score calls). EMPTY = disabled -> velocity uses the cache only,
# exactly as before. Fail-safe: if Redis is unreachable, scoring silently continues cache-only.
REDIS_URL = os.getenv("BP_REDIS_URL", "").strip()
VELOCITY_RETAIN_HOURS = float(os.getenv("BP_VELOCITY_RETAIN_HOURS", "48"))   # TTL of the live window
# Master switch for the live Redis window (default ON). When off, /score + /score/audit score
# cache-only (Redis is still the transport when on; this just gates whether we read/record it).
LIVE_VELOCITY = os.getenv("BP_LIVE_VELOCITY", "1") == "1"

# --- geo-velocity enrichment (SHADOW / Phase 1) — OPTIONAL, best-effort, FIRST-PARTY only ---------
# When enabled, /score computes a geo-velocity observation (impossible-travel km/h) from FIRST-PARTY
# geo evidence ONLY and LOGS it as internal telemetry — it does NOT (yet) affect the score or the
# feature vector (that is a later, retrain-gated phase). No external/paid geolocation (no MaxMind /
# GeoLite2 / geo APIs). Fail-safe: if no coordinates, no usable internal IP resolver, and no approved
# location registry are available, geo is simply "unavailable" and scoring continues unchanged.
GEO_ENABLED = os.getenv("BF_GEO_ENABLED", "1") == "1"           # master switch for the shadow enrichment
# P2 hook: dotted path "module:function" of an INTERNAL, first-party IP->(lat,lon) resolver. Empty =
# no IP resolution (there is no such mapping in this repo — supply your own to activate P2).
GEO_IP_RESOLVER = os.getenv("BF_GEO_IP_RESOLVER", "").strip()
# P3 hook: dotted path "module:function" of an INTERNAL, approved location-string->(lat,lon) registry
# lookup. Empty = no registry (none exists yet). Never string-parse the free-text location otherwise.
GEO_LOCATION_REGISTRY = os.getenv("BF_GEO_LOCATION_REGISTRY", "").strip()
GEO_PREV_RETAIN_HOURS = float(os.getenv("BF_GEO_PREV_RETAIN_HOURS", "168"))   # TTL of the last-geo point
GEO_MAX_KMH = float(os.getenv("BF_GEO_MAX_KMH", "0") or 0)      # optional clip on reported km/h (0 = off)


def attach_file_log(default: str = "/app/logs/ml.log") -> None:
    """Also write ML-job logs to a rotated plain-text file (bind-mounted to ./logs), so a
    Dockerised training/monitor/retrain run leaves an on-disk audit trail. Never fatal."""
    import logging
    import logging.handlers
    path = os.getenv("BF_LOG_FILE", default)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
        logging.getLogger().addHandler(fh)
    except Exception:
        pass


CACHE_TABLE = "bp_transactions_cache"

# --- §1 eligibility gate: TRAIN ONLY ON ACTIVE / TRUSTED / CLEAN --------------
# The behavioural baseline must be learned from legitimate, well-evidenced customers only
# (Practical rules §1/§7). Enforced in ml.pipeline.clean.
MIN_TXNS_PER_CUSTOMER = int(os.getenv("BF_MIN_TXNS", "50"))    # PDF §2 table: 50-500
# PDF §1 is an AND: eligible = enough clean txns AND enough days of history AND no confirmed
# fraud. The doc's example is ≥90 days, but the whole cache window is only ~91 days today, so a
# ≥90-day PERSONAL span is impossible for anyone yet. We therefore ramp this: it defaults to a
# value the current window supports and should rise toward 90 as the rolling window grows.
MIN_DAYS_ACTIVE = int(os.getenv("BF_MIN_DAYS_ACTIVE", "30"))   # PDF §1 (AND); target 90
LOOKBACK_MONTHS = int(os.getenv("BF_LOOKBACK_MONTHS", "3"))
MAX_SANE_AMOUNT = float(os.getenv("BF_MAX_SANE_AMOUNT", "1e13"))
# Customer identity: key on the stable customer identifier (1.md). Branch/type are features.
IDENTITY_COLS = ("identifier",)  # +identifier_type is carried alongside

# --- Unsupervised validation (no fraud labels) -------------------------------
# Hold out a fraction of eligible-customer NORMAL transactions (split at the CUSTOMER level to
# avoid leakage). Used to (a) calibrate the alert threshold on unseen normal traffic and
# (b) compute a synthetic-anomaly ROC-AUC (target 0.75-0.85) as an unsupervised proxy for
# detection quality until analyst-confirmed labels exist. See ml.eval.unsupervised.
HOLDOUT_FRAC = float(os.getenv("BF_HOLDOUT_FRAC", "0.20"))
SYNTH_ANOMALY_TARGET = (0.75, 0.85)   # benchmark band for the unsupervised validation AUC
SYNTH_SEED = 42

# Weak proxy labels for evaluation until the real feedback loop exists (B6). A transaction is
# treated as "known-bad" for evaluation ONLY if any of these hold. NEVER used for training.
PROXY_BAD_SQL = "(status <> 'clean' OR is_blocked = true OR sender_blacklisted = true)"

# --- Model hyper-parameters --------------------------------------------------
ISO_FOREST = {
    "n_estimators": int(os.getenv("BF_IF_TREES", "300")),
    "max_samples": os.getenv("BF_IF_MAX_SAMPLES", "auto"),
    "contamination": float(os.getenv("BF_IF_CONTAMINATION", "0.02")),
    "random_state": 42,
    "n_jobs": -1,
}
AUTOENCODER = {
    "hidden": [64, 32, 16, 32, 64],          # symmetric, bottleneck 16
    "epochs": int(os.getenv("BF_AE_EPOCHS", "40")),
    "batch_size": int(os.getenv("BF_AE_BATCH", "1024")),
    "lr": float(os.getenv("BF_AE_LR", "1e-3")),
    "weight_decay": float(os.getenv("BF_AE_WD", "1e-5")),
    "val_frac": 0.15,
    "patience": int(os.getenv("BF_AE_PATIENCE", "5")),  # early stopping (stop after 5 flat val epochs)
    "seed": 42,
}
GNN = {
    "embed_dim": int(os.getenv("BF_GNN_DIM", "32")),
    "epochs": int(os.getenv("BF_GNN_EPOCHS", "30")),
    "lr": float(os.getenv("BF_GNN_LR", "1e-2")),
    "hidden": int(os.getenv("BF_GNN_HIDDEN", "64")),
    "seed": 42,
    "patience": int(os.getenv("BF_GNN_PATIENCE", "5")),  # early stopping on val link-loss
    # cap the graph for a 6 GB GPU; sample the largest components if exceeded
    "max_nodes": int(os.getenv("BF_GNN_MAX_NODES", "400000")),
}

# Ensemble: weights for [gnn, isoforest, autoencoder]. Missing detectors are renormalised.
ENSEMBLE_WEIGHTS = {
    "gnn": float(os.getenv("BF_W_GNN", "0.30")),
    "isoforest": float(os.getenv("BF_W_IF", "0.35")),
    "autoencoder": float(os.getenv("BF_W_AE", "0.35")),
}
# How the detector scores combine into the final risk. The old default ("mean") let a quiet,
# amount-blind detector (e.g. the GNN) DILUTE two detectors that strongly agree — so a huge
# above-historical-max transaction could blend below the review cut. "escalate" (default) never
# lets an AGREEING majority be diluted: risk = max(weighted_mean, 2nd-highest detector). It resists
# single-detector false positives (a lone spike doesn't dominate) while preserving majority extremes.
#   mean     — legacy weighted mean
#   escalate — max(weighted_mean, second-highest)   [recommended]
#   max      — max of all detectors (most aggressive; a single high fires)
#   noisy_or — 1 - Π(1 - score)  (probabilistic OR)
# Changing this REQUIRES a retrain so the p95/p99 tiering cuts recalibrate to the new distribution.
BLEND_MODE = os.getenv("BF_BLEND_MODE", "escalate").strip().lower()

# --- Dynamic alert zones (computed with np.percentile on the VALIDATION distribution) ---------
# NOT hard-coded score boundaries — these are the PERCENTILE LEVELS; the actual cut-offs are
# recomputed on the held-out-normal risk distribution at every training run (ml.codes.Tiering.fit),
# so they scale automatically on retrain. Zones (Part 1):
#   >= p99   -> UNSAFE  (Priority-1 / high-confidence anomaly queue)
#   p95..p99 -> REVIEW  (Priority-2 / grey-zone review)
#   <  p95   -> SAFE    (auto-pass)
# unsafe_high (p99.9) is the strongest sub-tier of Priority-1 (BF-400).
TIER_PERCENTILES = {
    "unsafe_high": float(os.getenv("BF_TIER_HIGH", "99.9")),   # >= p99.9 -> BF-400 (strongest)
    "unsafe": float(os.getenv("BF_TIER_UNSAFE", "99.0")),      # >= p99   -> BF-3xx (Priority-1)
    "review": float(os.getenv("BF_TIER_REVIEW", "95.0")),      # >= p95   -> BF-200 (Priority-2)
    # below review (p95) -> safe / auto-pass (BF-1xx)
}

# --- Retraining triggers (PDF §4/§14 — an OR of conditions on the fetched delta) -------------
# Retrain an existing model when ANY holds: enough NEW transactions since training, OR enough
# days elapsed, OR behavioural drift is detected on the new data. Evaluated by ml.retrain_trigger.
RETRAIN_MIN_NEW_TXNS = int(os.getenv("BF_RETRAIN_MIN_NEW", "100"))    # ≥100 new txns
RETRAIN_MAX_AGE_DAYS = int(os.getenv("BF_RETRAIN_MAX_DAYS", "30"))    # OR 30 days elapsed
DRIFT_PSI_THRESHOLD = float(os.getenv("BF_DRIFT_PSI", "0.25"))        # OR PSI drift > 0.25 (0.1 mild/0.25 major)

# --- Model-health gate (ml.monitor) ------------------------------------------
# If a (re)trained model falls below these floors it is flagged UNHEALTHY: an alert is emitted
# (Slack when BF_SLACK_WEBHOOK_URL is set) warning that continued use is unsafe. Until confirmed
# fraud labels exist, the synthetic-anomaly AUC is the PRIMARY health signal; the proxy-precision
# floor is wired and configurable for when real labels arrive.
MIN_ACCEPTABLE_PRECISION = float(os.getenv("BF_MIN_PRECISION", "0.02"))  # proxy floor for now
MIN_ACCEPTABLE_SYNTH_AUC = float(os.getenv("BF_MIN_SYNTH_AUC", "0.75"))  # unsupervised floor

# --- LIVE serving monitor (ml.monitor --live) --------------------------------
# Reads the decisions the SERVICE wrote to bp_decision and watches the model in production.
# Until confirmed-fraud labels exist there is no live precision, so the health signal is the
# FLAGGED RATE (review+unsafe share): the tiering is calibrated to flag ~10% review + ~2% unsafe
# (~12%), so a live rate far outside the band means the model/data has drifted -> retrain. When
# analyst feedback lands, live precision is added here and gated on MIN_ACCEPTABLE_PRECISION.
LIVE_WINDOW_HOURS = int(os.getenv("BF_LIVE_WINDOW_HOURS", "24"))
LIVE_MIN_SAMPLE = int(os.getenv("BF_LIVE_MIN_SAMPLE", "200"))   # need enough decisions to judge
LIVE_FLAG_RATE_MIN = float(os.getenv("BF_LIVE_FLAG_MIN", "0.02"))
LIVE_FLAG_RATE_MAX = float(os.getenv("BF_LIVE_FLAG_MAX", "0.30"))
DECISION_TABLE = os.getenv("BF_DECISION_TABLE", "bp_decision")
# Real precision/recall from the analyst feedback loop (bp_decision_feedback, POST /feedback).
# Feedback lands LATER than the decision (an analyst reviews after the fact), so this looks over a
# wider window (by feedback time) than the flag-rate drift band above. Reported once enough
# analyst-labelled decisions exist; precision below MIN_ACCEPTABLE_PRECISION then raises an alert.
FEEDBACK_TABLE = os.getenv("BF_FEEDBACK_TABLE", "bp_decision_feedback")
LIVE_PRECISION_WINDOW_DAYS = int(os.getenv("BF_LIVE_PRECISION_DAYS", "30"))
LIVE_MIN_LABELS = int(os.getenv("BF_LIVE_MIN_LABELS", "20"))    # min analyst verdicts to score precision

SLACK_WEBHOOK_URL = (os.getenv("BF_SLACK_WEBHOOK_URL")
                     or os.getenv("BP_SLACK_WEBHOOK_URL", "")).strip()  # MLOps alerts; one var everywhere

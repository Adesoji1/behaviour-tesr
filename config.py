"""
Central config for the behaviour-profile pipeline + microservice.

Everything is driven by ENVIRONMENT VARIABLES (nothing behavioural is hard-coded).
Values are read from a local `.env` file if present (see `.env.example`), then the
process environment, and finally the built-in dev defaults below.

- PROD (PostgreSQL, DigitalOcean): READ-ONLY, the transactions SOURCE. Only
  sync_manager.py reads it, in bounded chunks, into the local cache.
- STORE (PostgreSQL): the profile store — every profile / rule / cache row / event
  we create lives here. (This replaced the old MySQL store; the source is already
  Postgres, so we now run one engine end-to-end. See ingestionstratimprove.md §7.)

============================ ENVIRONMENT VARIABLES ============================
Every setting below is an environment variable. Nothing behavioural is hard-coded:
compliance can retune the system by editing `.env` and restarting — no code change,
no redeploy. Each entry says, in one line, WHAT it does and WHAT the number means.

--- 1. WHERE THE DATA COMES FROM (production — we only ever read it) -----------
  PROD_PG_HOST        Address of the live adhere database.
  PROD_PG_PORT        Its port.                                       (25061)
  PROD_PG_USER        Username we read with.
  PROD_PG_PASSWORD    Its password.                                   (SECRET)
  PROD_PG_DB          Which database on that server.                  (adhere)
  PROD_PG_SSLMODE     How to encrypt the connection.                  (require)
  -> Only sync_manager.py ever connects here, and only to SELECT.

--- 2. WHERE WE SAVE OUR WORK (the profile store — we own this one) ------------
  STORE_PG_HOST       Address of our own database. In docker compose it is "db".
  STORE_PG_PORT       Its port inside the container network.          (5432)
  STORE_PG_HOST_PORT  Port published on YOUR laptop (5433 avoids a local postgres).
  STORE_PG_USER       Username.                                       (behaviour)
  STORE_PG_PASSWORD   Password. REQUIRED — compose refuses to start without it.
  STORE_PG_DB         Database name.                                  (behaviour)
  STORE_PG_SSLMODE    TLS mode.                                       (prefer)
  -> Profiles, rules, the transaction cache and the audit log all live here.

--- 3. HOW WE LEARN A CUSTOMER'S "NORMAL" --------------------------------------
  BP_LOOKBACK_MONTHS   How far back we learn from.            3  = the last 3 months.
                       Older behaviour is ignored entirely.
  BP_DECAY_HALF_LIFE   How fast old behaviour stops counting. 90 = a transaction from
                       90 days ago counts HALF as much as one from today. (Today 1.0,
                       30d 0.8, 90d 0.5, 180d 0.2.) Lower = forget faster.
  BP_LEARN_CLEAN_ONLY  1 = learn ONLY from clean transactions; never learn from
                       suspicious/blocked/blacklisted ones, so fraud can never become
                       part of someone's "normal". Leave this ON.
  BP_MAX_SANE_AMOUNT   Amounts above this are treated as bad data and thrown away.
                       1e13 = 10 trillion NGN. Production contains impossible values
                       (we found one at 300 quadrillion); without this, ONE junk row
                       would wreck a customer's "biggest ever" and their averages.
  BP_MIN_PATTERN_OBS   How many times we must see a city/merchant before calling it
                       "usual". 2 = seen at least twice. Stops a single trip to Kano
                       from becoming part of their normal.

--- 4. WHO IS TRUSTED ("Practical rules" §1) -----------------------------------
  A customer is judged against their OWN history only if they pass ALL FOUR below.
  Fail any one and they are judged against their PEER GROUP instead (safer: a
  fraudster cannot make a little fake activity and have it accepted as "normal").
  Re-checked on EVERY transaction, not just when the profile was built.

  BP_MIN_TENURE_DAYS   How long they must have been a customer.  90 = 90 days.
  BP_MIN_TXNS          How many clean transactions they must have EVER made. 100.
                       (§1 says >=100; §2's table allows 50-500. 100 satisfies both.)
  BP_ELIGIBLE_MAX_FRAUD_TXNS
                       How many confirmed-fraud transactions they may have and still
                       be trusted. 0 = §1's literal rule: none at all.
  BP_CONFIDENCE_TRUST  Minimum confidence score (0-100) to trust their own profile.
                       60. Confidence = how well we can MODEL them, not how risky they
                       are: 50% history (do we have enough of their past?) + 30%
                       consistency (is their spending steady?) + 20% completeness (do
                       we have enough dimensions?). Low confidence means "we do not
                       know you well enough yet", so we use peers — it is NOT suspicion.
                       NOTE: at 60 this is near-inert (it denies 1 of 6,653), because
                       BP_MIN_TENURE_DAYS + BP_MIN_TXNS already guarantee 50 of the
                       100 points. It is a cheap backstop that starts to matter only
                       if those two are lowered. See RUNBOOK before raising it.

--- 5. WHEN A CUSTOMER IS RE-LEARNED (event-driven; there is no cron) ----------
  A customer is retrained when ANY ONE of these is true. It rides on a transaction
  the app already sent, so nothing is scheduled.

  BP_RETRAIN_MIN_NEW_TXNS   Retrain after this many transactions since their last
                            rebuild. 100.
  BP_RETRAIN_MAX_AGE_DAYS   Retrain if their profile is this many days old. 30.
  BP_DRIFT_SIGNAL_THRESHOLD Retrain after this many anomalies IN A ROW. 5. One normal
                            transaction resets the count, so a single odd payment
                            never triggers it — only sustained change does.
  BP_DRIFT_AMOUNT_PCT       How big an average-spend jump counts as drift. 0.5 = 50%.
  BP_INCREMENTAL            1 = skip rewriting a profile that has barely changed.

--- 6. PROTECTING THE LIVE DATABASE (the dials the DB engineer turns) ----------
  The sync job is the ONLY thing that reads production. Every read is bounded.

  BP_ALLOW_PROD_PULL   THE MASTER SWITCH. 0 = refuse to read production at all; the
                       service keeps serving from the local cache. Use it whenever
                       production must be shielded. Every blocked attempt is logged.
  BP_SYNC_CHUNK_SIZE   Rows fetched per query. 5000 = "give me 5,000 rows", never
                       "give me everything".
  BP_SYNC_MAX_ROWS     Most rows one sync run may pull in total. 50000. (0 = no cap.)
                       Stops a first run trying to drag the whole table down.
  BP_SYNC_SLEEP_SECONDS
                       Pause between chunks. 0.2 = wait 0.2s before asking again.
                       RAISE THIS to be gentler on production.
  BP_SYNC_STATEMENT_TIMEOUT_MS
                       Kill any query that runs longer than this. 30000 = 30 seconds.
                       The server enforces it, so a runaway query cannot sit on prod.
  BP_SYNC_REFRESH_DAYS Re-read the last N days every run. 2. This is how we notice a
                       transaction that was clean yesterday and is blocked today.
  BP_SYNC_PRUNE        1 = delete cached rows older than BP_LOOKBACK_MONTHS, so the
                       cache cannot grow forever or skew the baseline.
  BP_DEMO_SYNC_MAX_ROWS
                       Rows GET /demo pulls in its live-ingest stage. 2000 — small on
                       purpose, so the demo is provably light on production.
  BP_LIVE_VELOCITY     1 = check recent-window activity per transaction (catches a
                       burst inside one minute). Reads production, so it also obeys
                       BP_ALLOW_PROD_PULL.

--- 7. STORAGE ----------------------------------------------------------------
  BP_STORE_HISTORY_JSON
                       1 = also keep a full JSON snapshot of every profile on every
                       build. Great for point-in-time analysis, but heavy (~2KB x 100k
                       profiles per run — it once filled the test DB and flipped it
                       read-only). Default 0: keep only the compact timeline.
==============================================================================
"""
import os

# Load a local .env (if present) so `docker compose`/local runs pick up secrets
# without exporting them by hand. Safe no-op if python-dotenv isn't installed.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

# Directory this file lives in (all data artifacts are written alongside it).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Production PostgreSQL — READ ONLY. Never write here.
# NOTE: no secrets are committed here — all real values come from .env / the
# environment (see .env.example). Defaults are harmless placeholders.
# ---------------------------------------------------------------------------
PROD_PG = {
    "host": os.getenv("PROD_PG_HOST", "localhost"),
    "port": int(os.getenv("PROD_PG_PORT", "5432")),
    "user": os.getenv("PROD_PG_USER", "postgres"),
    "password": os.getenv("PROD_PG_PASSWORD", ""),
    "dbname": os.getenv("PROD_PG_DB", "postgres"),
    "sslmode": os.getenv("PROD_PG_SSLMODE", "require"),
}

def prod_pg_dsn() -> str:
    p = PROD_PG
    return (
        f"host={p['host']} port={p['port']} dbname={p['dbname']} "
        f"user={p['user']} password={p['password']} sslmode={p['sslmode']}"
    )

# ---------------------------------------------------------------------------
# PROFILE STORE — PostgreSQL (read/write). Everything we build is saved here.
# This replaces the old MySQL store: the transaction SOURCE is already Postgres,
# so we now run one engine / one driver / one dialect end-to-end.
# See ingestionstratimprove.md §7. Real values come from .env (see .env.example).
# ---------------------------------------------------------------------------
STORE_PG = {
    "host": os.getenv("STORE_PG_HOST", "db"),          # 'db' = the compose service name
    "port": int(os.getenv("STORE_PG_PORT", "5432")),
    "user": os.getenv("STORE_PG_USER", "postgres"),
    "password": os.getenv("STORE_PG_PASSWORD", ""),
    "dbname": os.getenv("STORE_PG_DB", "behaviour"),
    "sslmode": os.getenv("STORE_PG_SSLMODE", "prefer"),
}


def pg_store_dsn() -> str:
    """libpq DSN for the profile store (used by db.connect())."""
    p = STORE_PG
    return (
        f"host={p['host']} port={p['port']} dbname={p['dbname']} "
        f"user={p['user']} password={p['password']} sslmode={p['sslmode']} "
        f"connect_timeout=30 application_name=behaviour-profile"
    )


def prod_connect():
    """Return a live psycopg connection to production.

    NOTE: production is behind PgBouncer (a transaction-pooling pooler). Do NOT issue a
    session-level `SET default_transaction_read_only` / `SET statement_timeout` on this
    connection — it would persist on the pooled server connection and leak to the next
    client, corrupting the pool. Scope read-only + timeout to each transaction with
    `SET LOCAL` instead (see sync_manager._guarded_txn). This helper is currently unused;
    the live path is sync_manager.py.
    """
    import psycopg  # type: ignore
    return psycopg.connect(prod_pg_dsn())

# ---------------------------------------------------------------------------
# Pipeline parameters
# ---------------------------------------------------------------------------
LOOKBACK_MONTHS = int(os.getenv("BP_LOOKBACK_MONTHS", "3"))   # feature-learning window (quarterly, per CTO)
# Decay half-life 90 days => weights ≈ 1.0(now) / 0.8(30d) / 0.5(90d) / 0.2(180d),
# matching the CTO "Practical rules" §6 forgetting schedule.
DECAY_HALF_LIFE_DAYS = int(os.getenv("BP_DECAY_HALF_LIFE", "90"))
TOP_N_CATEGORICAL = 15   # how many top merchants/locations/ips to retain per profile

# ---------------------------------------------------------------------------
# Governance gate — "Practical rules we can use" (Anita). Only build/trust a
# profile once there is enough clean history; otherwise it is "Warming Up" and
# judged against peers, not its own thin history. Stops behaviour-poisoning.
# ---------------------------------------------------------------------------
# Eligibility (§1/§2): account age is measured over the FULL lifetime (from
# production), while features are still learned from the recent LOOKBACK window.
ELIGIBLE_MIN_TENURE_DAYS = int(os.getenv("BP_MIN_TENURE_DAYS", "90"))   # account age
# "Practical rules" §1 example: ">= 100 transactions". §2's table sanctions 50-500.
# 100 satisfies BOTH, so it is the default — the earlier 50 met only §2.
ELIGIBLE_MIN_TXNS = int(os.getenv("BP_MIN_TXNS", "100"))                # lifetime clean txns
# §1: "Only build a behavior profile if the customer has ... No confirmed fraud cases".
# Max confirmed-fraud transactions (suspicious / blocked / blacklisted) a customer may
# have in the learning window and still earn a trusted `active` profile. 0 = the PDF's
# literal rule. Raise it only with compliance sign-off.
ELIGIBLE_MAX_FRAUD_TXNS = int(os.getenv("BP_ELIGIBLE_MAX_FRAUD_TXNS", "0"))
# §1/§7: learn ONLY from clean transactions (exclude suspicious / blocked / blacklisted).
LEARN_FROM_CLEAN_ONLY = os.getenv("BP_LEARN_CLEAN_ONLY", "1") == "1"
# §10: a profile is trusted by the rule engine only when Active AND confidence >= this.
CONFIDENCE_TRUST_THRESHOLD = int(os.getenv("BP_CONFIDENCE_TRUST", "60"))  # 0-100
# Data-quality guard: amounts above this (NGN) are impossible (> national GDP) and
# are treated as bad data — excluded from the learned baseline so they can't poison
# a customer's max/avg/p95 or the peer baseline. Default 10 trillion NGN.
MAX_SANE_AMOUNT = float(os.getenv("BP_MAX_SANE_AMOUNT", "1e13"))

# ---- Currency normalization -------------------------------------------------
# The model is multi-currency (per the DB engineer: "define the model so it can support
# multiple currencies"). Production currency values are NOT ISO-clean — e.g. 'POUND',
# 'EURO', 'KSH', 'naira', stray whitespace/case. The learned profile is stored per
# normalized currency, and scoring resolves a transaction's currency the SAME way, so a
# transaction and its profile always agree on the code. Unknown codes pass through
# upper-cased (a new currency becomes usable as pure data — no code change).
DEFAULT_CURRENCY = os.getenv("BP_DEFAULT_CURRENCY", "NGN")
_CURRENCY_ALIASES = {
    "NAIRA": "NGN", "₦": "NGN",
    "POUND": "GBP", "POUNDS": "GBP", "£": "GBP", "STERLING": "GBP",
    "EURO": "EUR", "EUROS": "EUR", "€": "EUR",
    "DOLLAR": "USD", "DOLLARS": "USD", "$": "USD", "USDOLLAR": "USD",
    "KSH": "KES", "KSHS": "KES", "SHILLING": "KES",
    "CEDI": "GHS", "CEDIS": "GHS",
    "RAND": "ZAR",
}


def normalize_currency(raw) -> str:
    """Canonical currency code. None / '' -> DEFAULT_CURRENCY. Known local aliases are
    mapped (POUND->GBP, EURO->EUR, KSH->KES, ...); anything else is trimmed + upper-cased
    and passed through, so an unseen ISO code just works."""
    if raw is None:
        return DEFAULT_CURRENCY
    c = str(raw).strip().upper()
    if not c:
        return DEFAULT_CURRENCY
    return _CURRENCY_ALIASES.get(c, c)
# The offline history log can store a full JSON snapshot per entity per run. That
# is heavy (~2KB/entity/run) and overflows a small test-DB's storage. Default OFF:
# we keep only the compact scalar timeline (counts/amounts). Turn ON in production
# where storage is ample and full point-in-time snapshots are wanted for retraining.
STORE_HISTORY_JSON = os.getenv("BP_STORE_HISTORY_JSON", "0") == "1"

# §8 Behaviour stability: a value (city/country/merchant) must be seen at least
# this many times before it counts as part of the customer's "usual" set — so a
# single one-off event never becomes "normal". (PDF suggests up to 10; 2 is a
# gentle default for a 90-day window.)
MIN_PATTERN_OBS = int(os.getenv("BP_MIN_PATTERN_OBS", "2"))
# §4 Retrain only when enough new data: skip re-writing a profile that barely
# changed. Retrain if >= this many NEW clean txns since last build, OR it has
# been >= this many days, OR drift is detected, OR it is a new/uninitialised profile.
ENABLE_INCREMENTAL_RETRAIN = os.getenv("BP_INCREMENTAL", "1") == "1"
RETRAIN_MIN_NEW_TXNS = int(os.getenv("BP_RETRAIN_MIN_NEW_TXNS", "100"))
RETRAIN_MAX_AGE_DAYS = int(os.getenv("BP_RETRAIN_MAX_AGE_DAYS", "30"))
# §9 Drift detection (basic): flag "sudden" drift when the customer's recency-
# weighted average amount jumps by more than this fraction vs the previous build,
# or their dominant city / countries change. Sudden drift is surfaced for review.
DRIFT_AMOUNT_PCT = float(os.getenv("BP_DRIFT_AMOUNT_PCT", "0.5"))   # 0.5 = 50%
# Event-driven retrain: a customer is retrained when they cross this many
# consecutive "anomalous vs their own profile" transactions (sustained drift =
# repeated evidence, per §8) — not on a single one-off.
DRIFT_SIGNAL_THRESHOLD = int(os.getenv("BP_DRIFT_SIGNAL_THRESHOLD", "5"))
# Microservice: use the LIVE (production) recent-window velocity look-up (hits production
# read-only per transaction — slow + prod load). Off by default; prefer LOCAL_VELOCITY.
LIVE_VELOCITY = os.getenv("BP_LIVE_VELOCITY", "1") == "1"
# LOCAL velocity (default ON): compute the recent-window burst features from the local
# bp_recent_txn table — the transactions /score itself has seen. Real-time, ~ms, and NO
# production read. This is the recommended burst-detection path. Takes precedence over
# LIVE_VELOCITY when enabled.
LOCAL_VELOCITY = os.getenv("BP_LOCAL_VELOCITY", "1") == "1"
# Retention for bp_recent_txn (hours). Must exceed the longest velocity window (24h);
# rows older than this are pruned off the hot path. Default 48h.
VELOCITY_RETAIN_HOURS = float(os.getenv("BP_VELOCITY_RETAIN_HOURS", "48"))
# Fraction of /score calls that also trigger a background prune of bp_recent_txn, so the
# table stays bounded without a dedicated job or any hot-path cost. Default ~2%.
VELOCITY_PRUNE_PROB = float(os.getenv("BP_VELOCITY_PRUNE_PROB", "0.02"))
# SAFETY SWITCH — protect production. ALL live reads from the production Postgres
# (per-customer retrain, the batch extract scripts, and live velocity) go through
# this gate. Set BP_ALLOW_PROD_PULL=0 to STOP every live pull: retrains skip with a
# logged reason instead of hitting prod, and extracts refuse to run. Every attempt
# — allowed or blocked — is logged so it is visible in `docker compose logs`.
# Default ON; turn OFF whenever production must be shielded from load.
ALLOW_PROD_PULL = os.getenv("BP_ALLOW_PROD_PULL", "1") == "1"

# ---------------------------------------------------------------------------
# INGESTION (sync_manager.py) — how we pull from production SAFELY.
# These are the dials the DB engineer can turn. The sync job is the ONLY thing
# that reads production; it pages with a keyset cursor in bounded chunks, caps
# how much any single run may pull, and sleeps between chunks so the live DB is
# never hammered. See ingestionstratimprove.md §5.
# ---------------------------------------------------------------------------
# Rows per chunk. Each chunk is ONE bounded query (WHERE id > last ORDER BY id LIMIT n).
SYNC_CHUNK_SIZE = int(os.getenv("BP_SYNC_CHUNK_SIZE", "5000"))
# Hard ceiling on rows a single sync run may pull (0 = no cap). Stops a first
# run from trying to drag the whole table down in one go.
SYNC_MAX_ROWS = int(os.getenv("BP_SYNC_MAX_ROWS", "50000"))
# Seconds to sleep between chunks — the throttle. Raise it to be gentler on prod.
SYNC_SLEEP_SECONDS = float(os.getenv("BP_SYNC_SLEEP_SECONDS", "0.2"))
# Statement timeout (ms) applied to every production query — a runaway query is
# killed by the server rather than sitting on prod.
SYNC_STATEMENT_TIMEOUT_MS = int(os.getenv("BP_SYNC_STATEMENT_TIMEOUT_MS", "30000"))
# Re-pull the last N days on every run so status flips (clean -> blocked /
# blacklisted) are corrected in the cache. This closes the "watermark misses
# UPDATEs" hole documented in ingestionstratimprove.md §6.1.
SYNC_REFRESH_DAYS = int(os.getenv("BP_SYNC_REFRESH_DAYS", "2"))
# Drop cached rows older than the learning window so the cache cannot grow
# forever or skew the baseline with out-of-window rows (§6.2).
SYNC_PRUNE = os.getenv("BP_SYNC_PRUNE", "1") == "1"
# GET /demo runs a deliberately small live pull so the demo stays quick and is
# provably light on production. The scheduled sync job uses the full cap above.
DEMO_SYNC_MAX_ROWS = int(os.getenv("BP_DEMO_SYNC_MAX_ROWS", "2000"))
# SCHEDULED INGESTION (production). The sync runs automatically on this interval as a
# dedicated background service (`python sync_manager.py --loop`) — the ONLY production
# reader, and NOT triggered by any HTTP request. This interval is exactly how fresh the
# cache (and therefore the profiles) stays. Default 300s (5 min).
SYNC_INTERVAL_SECONDS = int(os.getenv("BP_SYNC_INTERVAL_SECONDS", "300"))
# DAILY schedule (production default): run the ingestion once a day at this wall-clock
# time instead of on a fixed interval. Set BP_SYNC_AT_HOUR (0-23) to enable it; leave it
# empty to fall back to the interval above. Minute defaults to 0. BP_SYNC_TZ names the
# timezone the hour is read in (IANA name, e.g. Africa/Lagos) so "4am" means local 4am,
# not UTC. Equivalent to a crontab `0 4 * * *` or a k8s CronJob, but self-contained in
# the sync container (no host crond needed).
_at = os.getenv("BP_SYNC_AT_HOUR", "").strip()
SYNC_AT_HOUR = int(_at) if _at else None
SYNC_AT_MINUTE = int(os.getenv("BP_SYNC_AT_MINUTE", "0"))
SYNC_TZ = os.getenv("BP_SYNC_TZ", "UTC")
# Run one sync immediately on scheduler start (a catch-up), then follow the schedule
# above. 0 = wait for the first scheduled time instead. Useful so a deploy/restart does
# not leave the cache stale until the next daily run.
SYNC_RUN_ON_START = os.getenv("BP_SYNC_RUN_ON_START", "1") == "1"
# ONE-TIME batch rebuild (`python retrain.py --rebuild-all`) — the initial seed of
# profiles from the cache, the modern equivalent of build_profiles.py. It is NOT a
# cron and NOT auto-fired: steady-state refresh is purely event-driven per customer
# (retrain.maybe_retrain). This only skips customers with fewer than this many cached
# rows (one-off noise); thin customers stay warming_up / peer-judged anyway.
REBUILD_MIN_ROWS = int(os.getenv("BP_REBUILD_MIN_ROWS", "1"))

# ---------------------------------------------------------------------------
# SCORING (POST /score) — the fast, live decision endpoint.
# ---------------------------------------------------------------------------
# The behavioural anti-fraud MODEL (the unsupervised ensemble in ml/) is the decision
# engine behind /score. When true (default) /score returns the MODEL's decision — the
# transaction is compared against the customer's learned profile/history and the ensemble
# decides. Set BP_USE_MODEL=false to fall back to the legacy rule-engine path (rollback).
# Inference is CPU-only; the GPU is used ONLY by the separate offline training job.
USE_MODEL = os.getenv("BP_USE_MODEL", "true").lower() in ("1", "true", "yes", "on")

# Where to POST every decision (the webhook). Empty = webhook disabled (the
# decision is still returned in the HTTP response and saved to bp_decision).
# Nothing is hard-coded — set BP_SCORE_WEBHOOK_URL to your consumer's endpoint.
SCORE_WEBHOOK_URL = os.getenv("BP_SCORE_WEBHOOK_URL", "").strip()
# How long to wait for the webhook before giving up (seconds). The webhook is sent
# AFTER the HTTP response returns, so this never slows the caller down.
SCORE_WEBHOOK_TIMEOUT = float(os.getenv("BP_SCORE_WEBHOOK_TIMEOUT", "5"))
# Optional shared secret. When set, each webhook carries an HMAC-SHA256 signature
# header (X-Behaviour-Signature) so the receiver can verify it came from us.
SCORE_WEBHOOK_SECRET = os.getenv("BP_SCORE_WEBHOOK_SECRET", "")
# ---- Webhook OUTBOX relay (guaranteed, retried delivery) --------------------
# Each /score writes webhook_status='pending' in the SAME commit as the decision, so
# no decision's delivery is ever lost to an API crash. A relay loop (in the sync
# service) redelivers pending rows with exponential backoff until they succeed or
# exhaust the retry budget — at-least-once delivery, no extra infrastructure.
WEBHOOK_RELAY_ENABLED = os.getenv("BP_WEBHOOK_RELAY_ENABLED", "1") == "1"
# How often the relay sweeps for due 'pending' rows.
WEBHOOK_RELAY_INTERVAL_SECONDS = float(os.getenv("BP_WEBHOOK_RELAY_INTERVAL_SECONDS", "5"))
# Max rows the relay handles per sweep (backpressure; the rest wait for the next sweep).
WEBHOOK_RELAY_BATCH = int(os.getenv("BP_WEBHOOK_RELAY_BATCH", "100"))
# Give up (mark 'dead' / dead-letter) after this many failed attempts.
WEBHOOK_MAX_ATTEMPTS = int(os.getenv("BP_WEBHOOK_MAX_ATTEMPTS", "8"))
# Exponential backoff: delay = min(base * 2^(attempt-1), cap), with +-20% jitter.
# Defaults (base 5s, cap 1h, 8 attempts) span ~5s -> ~1h over roughly a day.
WEBHOOK_BACKOFF_BASE_SECONDS = float(os.getenv("BP_WEBHOOK_BACKOFF_BASE_SECONDS", "5"))
WEBHOOK_BACKOFF_CAP_SECONDS = float(os.getenv("BP_WEBHOOK_BACKOFF_CAP_SECONDS", "3600"))
# Grace before the relay first touches a FRESH pending row — lets the fast inline
# delivery in /score win the common case, so the relay only handles what that missed
# (e.g. an API crash). A failed inline attempt sets its own shorter backoff, so this
# grace does not delay retries — only the relay's very first look at an untouched row.
WEBHOOK_RELAY_GRACE_SECONDS = float(os.getenv("BP_WEBHOOK_RELAY_GRACE_SECONDS", "30"))
# Retraining rides on the transaction, but must NOT slow the live decision.
# 1 = run the possible retrain AFTER the response is sent (recommended, fast).
# 0 = run it inline before responding (slower; only for debugging).
SCORE_RETRAIN_ASYNC = os.getenv("BP_SCORE_RETRAIN_ASYNC", "1") == "1"
# ---- API-key auth for POST /score (header X-Adhere-Key) ---------------------
# The single active key. Set BP_API_KEY in .env for a simple/fixed key (this value,
# hashed, becomes THE active key and OVERRIDES the DB) — the container reads it from
# the environment. Leave it EMPTY to use the DB-managed rotating key instead
# (manage_api_key.py rotate: hashed, one active at a time, rotating invalidates the
# former). Generate the value with `openssl rand -hex 32`.
API_KEY = os.getenv("BP_API_KEY", "").strip()
# Turn auth OFF entirely (internal/dev only) — /score then needs no header.
API_KEY_DISABLED = os.getenv("BP_API_KEY_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")
# Seconds the service caches the active DB key hash (rotation is picked up within this
# window, or immediately via POST /reload). Ignored when BP_API_KEY is set.
API_KEY_CACHE_TTL = int(os.getenv("BP_API_KEY_CACHE_TTL", "30"))
# Rule/blacklist/peer-baseline reference data is cached in process to keep /score fast.
# It refreshes automatically after this many seconds; POST /reload forces it sooner.
# This is the MAX time a rule/threshold/blacklist change takes to take effect. Lower =
# fresher but more DB reads; higher = faster but staler. The per-customer profile is
# NOT cached — it is always read live, so behaviour is never stale.
RULES_CACHE_TTL = float(os.getenv("BP_RULES_CACHE_TTL", "30"))
# Connection pool for the profile store. Opening a fresh Postgres connection costs
# ~30ms; a pool reuses established ones so acquiring is microseconds — this is what
# makes /score fast. Sized per process (uvicorn runs BP_WORKERS processes).
STORE_POOL_MIN = int(os.getenv("BP_STORE_POOL_MIN", "2"))
STORE_POOL_MAX = int(os.getenv("BP_STORE_POOL_MAX", "10"))
# Max seconds to wait for a free connection before erroring (backpressure, not a hang).
STORE_POOL_TIMEOUT = float(os.getenv("BP_STORE_POOL_TIMEOUT", "10"))

# Where GET /demo writes the exact JSON it returns (one JSON object per line). The
# database engineer asked to see and reuse these, so the folder is bind-mounted to
# ./logs/demo on the host in docker-compose. Env-driven so the path is not hard-coded.
DEMO_LOG_DIR = os.getenv("BP_DEMO_LOG_DIR", "/app/logs/demo")


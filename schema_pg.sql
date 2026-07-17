-- ============================================================================
-- Customer Behaviour Profile — PostgreSQL schema (profile store)
-- ============================================================================
-- Ported from the original MySQL schema.sql. See ingestionstratimprove.md §7:
-- the transaction SOURCE is already Postgres, so the profile store is now
-- Postgres too — one engine, one driver, one dialect end-to-end.
--
-- All objects are namespaced `bp_`.
--
--   * bp_user_behaviour_profile  = ONLINE store  (one row per entity, UPSERT)
--   * bp_profile_history         = OFFLINE store (append-only snapshot log)
--   * bp_incremental_state       = EWMA / time-decay accumulators
--   * bp_blacklist               = read-only mirror of production blacklist
--   * bp_rule_definition         = AML rule catalogue + thresholds
--   * bp_rule_settings           = PER-CLIENT threshold overrides
--   * bp_peer_baseline           = non-ML cold-start baseline for new accounts
--   * bp_rule_event              = log of rule firings
--   * bp_event_log               = per-customer accountability trail
--   * bp_build_run               = one row per batch run (lineage)
--   * bp_transactions_cache      = NEW: shared local cache of production txns
--   * bp_sync_state              = NEW: ingestion watermark (resume point)
--
-- ENTITY KEY: entity_key = branch_id || ':' || origin_account_no
--
-- Type mapping from MySQL: DATETIME->TIMESTAMP, DECIMAL->NUMERIC, DOUBLE->
-- DOUBLE PRECISION, JSON->TEXT , TINYINT(1)->SMALLINT (kept as 0/1 so the
-- application code is unchanged), AUTO_INCREMENT->BIGSERIAL.
-- JSON columns are TEXT (not jsonb) on purpose: the application already does
-- json.dumps() on write and json.loads() on read (as it did against MySQL).
-- psycopg would auto-deserialize jsonb to dicts and break those call sites, so
-- TEXT keeps behaviour byte-identical. We never query *inside* the JSON; switch
-- to jsonb only if that ever changes.
--
-- This file is idempotent (CREATE ... IF NOT EXISTS) and is applied automatically
-- by the postgres container on first init.
-- ============================================================================

-- Keeps `updated_at` fresh — Postgres has no `ON UPDATE CURRENT_TIMESTAMP`.
CREATE OR REPLACE FUNCTION bp_set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 1. ONLINE STORE — the live behaviour profile (one row per entity, upserted)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_user_behaviour_profile (
    entity_key              VARCHAR(128) NOT NULL,
    currency                VARCHAR(8)   NOT NULL DEFAULT 'NGN',  -- per-currency grain
    branch_id               BIGINT       NULL,
    origin_account_no       VARCHAR(64)  NULL,
    customer_id             BIGINT       NULL,
    customer_name           VARCHAR(255) NULL,
    identifier              VARCHAR(64)  NULL,
    bvn                     VARCHAR(32)  NULL,
    account_type            VARCHAR(32)  NULL,

    -- GOVERNANCE gate: only an Active, confident profile is trusted by the engine.
    profile_status          VARCHAR(16)  NOT NULL DEFAULT 'warming_up',
    confidence_score        INTEGER      NULL,
    drift_status            VARCHAR(16)  NULL,
    drift_reason            VARCHAR(255) NULL,
    retrain_reason          VARCHAR(64)  NULL,
    txns_since_build        INTEGER      NOT NULL DEFAULT 0,
    drift_signal_count      INTEGER      NOT NULL DEFAULT 0,
    last_retrained_at       TIMESTAMP    NULL,
    tenure_days             INTEGER      NULL,
    lifetime_txns           INTEGER      NULL,
    lifetime_clean_txns     INTEGER      NULL,

    first_seen              TIMESTAMP    NULL,
    last_seen               TIMESTAMP    NULL,
    age_days                INTEGER      NULL,
    dormant_days            INTEGER      NULL,

    total_tx_count          INTEGER      NOT NULL DEFAULT 0,
    total_tx_amount         NUMERIC(30,2) NOT NULL DEFAULT 0,

    tx_count_24h            INTEGER NOT NULL DEFAULT 0,
    tx_count_7d             INTEGER NOT NULL DEFAULT 0,
    tx_count_30d            INTEGER NOT NULL DEFAULT 0,
    tx_count_60d            INTEGER NOT NULL DEFAULT 0,
    tx_count_90d            INTEGER NOT NULL DEFAULT 0,

    amt_sum_30d             NUMERIC(30,2) NOT NULL DEFAULT 0,
    amt_sum_90d             NUMERIC(30,2) NOT NULL DEFAULT 0,

    avg_amount              NUMERIC(30,4) NULL,
    max_amount              NUMERIC(30,2) NULL,
    min_amount              NUMERIC(30,2) NULL,
    std_amount              NUMERIC(30,4) NULL,
    median_amount           NUMERIC(30,2) NULL,
    p95_amount              NUMERIC(30,2) NULL,

    avg_amount_30d          NUMERIC(30,4) NULL,
    max_amount_30d          NUMERIC(30,2) NULL,
    std_amount_30d          NUMERIC(30,4) NULL,

    decayed_avg_amount      NUMERIC(30,4) NULL,
    decayed_tx_count        DOUBLE PRECISION NULL,
    decay_half_life_days    INTEGER       NOT NULL DEFAULT 30,

    avg_monthly_tx_count    DOUBLE PRECISION NULL,
    avg_monthly_amount      NUMERIC(30,2) NULL,

    distinct_beneficiaries      INTEGER NOT NULL DEFAULT 0,
    distinct_beneficiaries_30d  INTEGER NOT NULL DEFAULT 0,
    distinct_beneficiaries_24h  INTEGER NOT NULL DEFAULT 0,
    beneficiaries               TEXT  NULL,

    usual_transaction_types     TEXT  NULL,
    transaction_type_entropy    DOUBLE PRECISION NULL,
    usual_merchants             TEXT  NULL,
    merchant_entropy            DOUBLE PRECISION NULL,

    usual_locations             TEXT  NULL,
    usual_cities                TEXT  NULL,
    usual_countries             TEXT  NULL,
    location_entropy            DOUBLE PRECISION NULL,
    known_ip_addresses          TEXT  NULL,
    known_ip_subnets            TEXT  NULL,
    last_location               VARCHAR(255) NULL,
    last_city                   VARCHAR(128) NULL,
    last_country                VARCHAR(16)  NULL,
    last_ip                     VARCHAR(64)  NULL,
    last_event_ts               TIMESTAMP    NULL,

    peak_transaction_hours      TEXT  NULL,
    top_hour                    INTEGER NULL,
    peak_transaction_days       TEXT  NULL,
    top_day_of_week             VARCHAR(4) NULL,
    night_activity_ratio        DOUBLE PRECISION NULL,

    is_blacklisted          SMALLINT NOT NULL DEFAULT 0,
    is_pep                  SMALLINT NOT NULL DEFAULT 0,
    is_sanction             SMALLINT NOT NULL DEFAULT 0,
    risk_level              VARCHAR(16) NULL,
    risk_score              INTEGER NULL,
    suspicious_tx_count     INTEGER NOT NULL DEFAULT 0,
    suspicious_ratio        DOUBLE PRECISION NULL,

    profile_version         INTEGER NOT NULL DEFAULT 1,
    build_run_id            VARCHAR(64) NULL,
    window_start            TIMESTAMP NULL,
    window_end              TIMESTAMP NULL,
    created_at              TIMESTAMP NOT NULL DEFAULT now(),
    updated_at              TIMESTAMP NOT NULL DEFAULT now(),

    PRIMARY KEY (entity_key, currency)          -- one row per customer PER CURRENCY
);
-- "all of a customer's currencies" reads (endpoints) still hit an index on entity_key.
CREATE INDEX IF NOT EXISTS idx_bp_profile_entity ON bp_user_behaviour_profile (entity_key);
CREATE INDEX IF NOT EXISTS idx_bp_profile_customer ON bp_user_behaviour_profile (customer_id);
CREATE INDEX IF NOT EXISTS idx_bp_profile_branch   ON bp_user_behaviour_profile (branch_id);
CREATE INDEX IF NOT EXISTS idx_bp_profile_acct     ON bp_user_behaviour_profile (origin_account_no);
CREATE INDEX IF NOT EXISTS idx_bp_status           ON bp_user_behaviour_profile (profile_status);

DROP TRIGGER IF EXISTS trg_bp_profile_updated ON bp_user_behaviour_profile;
CREATE TRIGGER trg_bp_profile_updated BEFORE UPDATE ON bp_user_behaviour_profile
    FOR EACH ROW EXECUTE FUNCTION bp_set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. OFFLINE STORE — append-only history (one snapshot per entity per run)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_profile_history (
    id              BIGSERIAL PRIMARY KEY,
    entity_key      VARCHAR(128) NOT NULL,
    currency        VARCHAR(8)   NULL,          -- which currency profile this snapshot is
    build_run_id    VARCHAR(64)  NOT NULL,
    snapshot_ts     TIMESTAMP    NOT NULL DEFAULT now(),
    profile_version INTEGER      NOT NULL,
    total_tx_count  INTEGER      NULL,
    total_tx_amount NUMERIC(30,2) NULL,
    avg_amount      NUMERIC(30,4) NULL,
    decayed_avg_amount NUMERIC(30,4) NULL,
    profile_json    TEXT         NULL
);
CREATE INDEX IF NOT EXISTS idx_bp_hist_entity ON bp_profile_history (entity_key);
CREATE INDEX IF NOT EXISTS idx_bp_hist_run    ON bp_profile_history (build_run_id);

-- ---------------------------------------------------------------------------
-- 3. INCREMENTAL STATE — EWMA / time-decay accumulators
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_incremental_state (
    entity_key       VARCHAR(128) NOT NULL PRIMARY KEY,
    ewma_mean_amount DOUBLE PRECISION NULL,
    ewma_var_amount  DOUBLE PRECISION NULL,
    decayed_count    DOUBLE PRECISION NULL,
    last_amount      DOUBLE PRECISION NULL,
    last_event_ts    TIMESTAMP NULL,
    last_decay_ts    TIMESTAMP NULL,
    half_life_days   INTEGER NOT NULL DEFAULT 30,
    updated_at       TIMESTAMP NOT NULL DEFAULT now()
);
DROP TRIGGER IF EXISTS trg_bp_incr_updated ON bp_incremental_state;
CREATE TRIGGER trg_bp_incr_updated BEFORE UPDATE ON bp_incremental_state
    FOR EACH ROW EXECUTE FUNCTION bp_set_updated_at();

-- ---------------------------------------------------------------------------
-- 4. BLACKLIST MIRROR — snapshot of production users_blacklist
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_blacklist (
    id              BIGINT NOT NULL PRIMARY KEY,
    blacklist_type  VARCHAR(64) NULL,
    source          VARCHAR(128) NULL,
    risk_level      VARCHAR(32) NULL,
    name            VARCHAR(255) NULL,
    entity_type     VARCHAR(64) NULL,
    identifier_type VARCHAR(64) NULL,
    identifier      VARCHAR(128) NULL,
    status          VARCHAR(32) NULL,
    date_created    TIMESTAMP NULL
);
CREATE INDEX IF NOT EXISTS idx_bp_bl_identifier ON bp_blacklist (identifier);
CREATE INDEX IF NOT EXISTS idx_bp_bl_name       ON bp_blacklist (name);

-- ---------------------------------------------------------------------------
-- 5. RULE CATALOGUE — the AML rules that fire off the profile
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_rule_definition (
    rule_code    VARCHAR(64)  NOT NULL PRIMARY KEY,
    category     VARCHAR(128) NULL,
    name         VARCHAR(255) NOT NULL,
    description  TEXT         NULL,
    params       TEXT         NULL,
    enabled      SMALLINT     NOT NULL DEFAULT 1,
    created_at   TIMESTAMP    NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 5b. RULE SETTINGS — PER-CLIENT threshold overrides (tier-1/2/3 differ)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_rule_settings (
    branch_id   BIGINT       NOT NULL,
    rule_code   VARCHAR(64)  NOT NULL,
    params      TEXT         NULL,
    enabled     SMALLINT     NULL,          -- NULL = inherit global; 0/1 = force off/on
    updated_at  TIMESTAMP    NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, rule_code)
);
CREATE INDEX IF NOT EXISTS idx_bp_rs_branch ON bp_rule_settings (branch_id);
DROP TRIGGER IF EXISTS trg_bp_rs_updated ON bp_rule_settings;
CREATE TRIGGER trg_bp_rs_updated BEFORE UPDATE ON bp_rule_settings
    FOR EACH ROW EXECUTE FUNCTION bp_set_updated_at();

-- ---------------------------------------------------------------------------
-- 5c. PEER BASELINE — non-ML cold-start profile for brand-new accounts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_peer_baseline (
    branch_id               BIGINT       NOT NULL,
    account_type            VARCHAR(32)  NOT NULL,
    currency                VARCHAR(8)   NOT NULL DEFAULT 'NGN',  -- per-currency peers
    peer_entities           INTEGER      NULL,
    peer_tx_count           BIGINT       NULL,
    avg_amount              NUMERIC(30,4) NULL,
    median_amount           NUMERIC(30,2) NULL,
    p95_amount              NUMERIC(30,2) NULL,
    max_amount              NUMERIC(30,2) NULL,
    std_amount              NUMERIC(30,4) NULL,
    avg_monthly_tx_count    DOUBLE PRECISION NULL,
    usual_cities            TEXT          NULL,
    usual_countries         TEXT          NULL,
    peak_transaction_hours  TEXT          NULL,
    build_run_id            VARCHAR(64)   NULL,
    updated_at              TIMESTAMP     NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, account_type, currency)
);
DROP TRIGGER IF EXISTS trg_bp_peer_updated ON bp_peer_baseline;
CREATE TRIGGER trg_bp_peer_updated BEFORE UPDATE ON bp_peer_baseline
    FOR EACH ROW EXECUTE FUNCTION bp_set_updated_at();

-- ---------------------------------------------------------------------------
-- 6. RULE EVENTS — every firing when an incoming txn is compared to a profile
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_rule_event (
    id             BIGSERIAL PRIMARY KEY,
    entity_key     VARCHAR(128) NULL,
    -- transaction_id mirrors production monitoring_transactionmonitoring.transaction_id (VARCHAR(255));
    -- keeping it narrow here caused sync_manager to fail with `value too long for type character varying(64)`.
    transaction_id VARCHAR(255) NULL,
    rule_code      VARCHAR(64)  NOT NULL,
    fired_at       TIMESTAMP    NOT NULL DEFAULT now(),
    severity       VARCHAR(16)  NULL,
    details        TEXT         NULL
);
CREATE INDEX IF NOT EXISTS idx_bp_evt_entity ON bp_rule_event (entity_key);
CREATE INDEX IF NOT EXISTS idx_bp_evt_rule   ON bp_rule_event (rule_code);

-- ---------------------------------------------------------------------------
-- 6b. EVENT LOG — per-transaction / per-customer accountability trail
-- ---------------------------------------------------------------------------
-- Durable audit: every score / retrain / skip / failure. This is the "no log
-- loss" guarantee that survives container restarts (see ingestionstratimprove §8).
CREATE TABLE IF NOT EXISTS bp_event_log (
    id             BIGSERIAL PRIMARY KEY,
    entity_key     VARCHAR(128) NULL,
    -- transaction_id mirrors production monitoring_transactionmonitoring.transaction_id (VARCHAR(255));
    -- keeping it narrow here caused sync_manager to fail with `value too long for type character varying(64)`.
    transaction_id VARCHAR(255) NULL,
    event_type     VARCHAR(32)  NOT NULL,
    outcome        VARCHAR(32)  NULL,
    detail         TEXT         NULL,
    created_at     TIMESTAMP    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evt_entity ON bp_event_log (entity_key, created_at);
CREATE INDEX IF NOT EXISTS idx_evt_type   ON bp_event_log (event_type);

-- ---------------------------------------------------------------------------
-- 7. BUILD RUN — one row per batch execution (lineage / audit)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_build_run (
    run_id       VARCHAR(64) NOT NULL PRIMARY KEY,
    started_at   TIMESTAMP NOT NULL DEFAULT now(),
    finished_at  TIMESTAMP NULL,
    window_start TIMESTAMP NULL,
    window_end   TIMESTAMP NULL,
    source_rows  BIGINT NULL,
    entities     INTEGER NULL,
    status       VARCHAR(32) NOT NULL DEFAULT 'running',
    notes        TEXT NULL
);

-- ---------------------------------------------------------------------------
-- 8. TRANSACTIONS CACHE — the SHARED local copy of production transactions
-- ---------------------------------------------------------------------------
-- This is what stops us hammering production (ingestionstratimprove.md §5).
-- The sync job is the ONLY process that reads production; it writes here in
-- bounded chunks. Every service replica then learns/retrains from THIS table,
-- so production sees exactly one reader no matter how many replicas run.
--
-- `id` is the production row id — it is the keyset-pagination cursor AND the
-- conflict key, so a re-synced row UPDATES in place (which is how status flips
-- clean -> blocked are corrected; see §6.1).
CREATE TABLE IF NOT EXISTS bp_transactions_cache (
    id                          BIGINT       NOT NULL PRIMARY KEY,  -- production row id
    entity_key                  VARCHAR(128) NOT NULL,              -- branch_id:origin_account_no
    -- transaction_id mirrors production monitoring_transactionmonitoring.transaction_id (VARCHAR(255));
    -- keeping it narrow here caused sync_manager to fail with `value too long for type character varying(64)`.
    transaction_id              VARCHAR(255) NULL,
    amount                      NUMERIC(30,2) NULL,
    currency                    VARCHAR(8)   NULL,
    transaction_type            VARCHAR(64)  NULL,
    transaction_type_normalized VARCHAR(64)  NULL,
    status                      VARCHAR(32)  NULL,
    branch_id                   BIGINT       NULL,
    origin_account_no           VARCHAR(64)  NULL,
    origin_account_type         VARCHAR(32)  NULL,
    destination_account_no      VARCHAR(64)  NULL,
    destination_bank_code       VARCHAR(32)  NULL,
    customer_name               VARCHAR(255) NULL,
    customer_email              VARCHAR(255) NULL,
    identifier                  VARCHAR(64)  NULL,
    identifier_type_id          BIGINT       NULL,
    bvn                         VARCHAR(32)  NULL,
    account_type                VARCHAR(32)  NULL,
    customer_ip_address         VARCHAR(64)  NULL,
    customer_location           VARCHAR(255) NULL,
    merchant_name               VARCHAR(255) NULL,
    merchant_location           VARCHAR(255) NULL,
    origin_country              VARCHAR(16)  NULL,
    destination_country         VARCHAR(16)  NULL,
    date_created                TIMESTAMP    NULL,
    sender_blacklisted          BOOLEAN      NULL,
    receiver_blacklisted        BOOLEAN      NULL,
    is_blocked                  BOOLEAN      NULL,
    indicator                   VARCHAR(64)  NULL,
    synced_at                   TIMESTAMP    NOT NULL DEFAULT now()
);
-- The retrain query filters on exactly this: one customer's recent clean history.
CREATE INDEX IF NOT EXISTS idx_bp_cache_entity_date ON bp_transactions_cache (entity_key, date_created);
CREATE INDEX IF NOT EXISTS idx_bp_cache_date        ON bp_transactions_cache (date_created);
CREATE INDEX IF NOT EXISTS idx_bp_cache_branch_acct ON bp_transactions_cache (branch_id, origin_account_no, date_created);

-- ---------------------------------------------------------------------------
-- 9. SYNC STATE — the ingestion watermark (resume point)
-- ---------------------------------------------------------------------------
-- One row per source table. `last_id` is the keyset cursor: the sync resumes from
-- here after any failure, so a crash never re-reads the whole table.
CREATE TABLE IF NOT EXISTS bp_sync_state (
    source          VARCHAR(64)  NOT NULL PRIMARY KEY,  -- e.g. 'monitoring_transactionmonitoring'
    last_id         BIGINT       NOT NULL DEFAULT 0,    -- keyset watermark (max production id synced)
    last_synced_at  TIMESTAMP    NULL,                  -- when the last successful chunk landed
    rows_synced     BIGINT       NOT NULL DEFAULT 0,    -- cumulative rows ingested
    last_status     VARCHAR(32)  NULL,                  -- ok | running | failed | blocked
    last_detail     TEXT         NULL,
    updated_at      TIMESTAMP    NOT NULL DEFAULT now()
);
DROP TRIGGER IF EXISTS trg_bp_sync_updated ON bp_sync_state;
CREATE TRIGGER trg_bp_sync_updated BEFORE UPDATE ON bp_sync_state
    FOR EACH ROW EXECUTE FUNCTION bp_set_updated_at();

-- ---------------------------------------------------------------------------
-- 10. DECISION LOG — the behavioural analysis of every scored transaction
-- ---------------------------------------------------------------------------
-- The audit record Anita asked for: one row per POST /score. Stores the decision,
-- the exact rules that fired (with severity + details), what the account was judged
-- against, why it was/wasn't trusted, how long scoring took, and the webhook delivery
-- outcome. This is the queryable behavioural-analysis trail for compliance — separate
-- from the generic bp_event_log so a reviewer can go straight to "every decision".
CREATE TABLE IF NOT EXISTS bp_decision (
    id              BIGSERIAL    PRIMARY KEY,
    entity_key      VARCHAR(128) NOT NULL,
    transaction_id TEXT         NULL,
    decision        VARCHAR(16)  NOT NULL,      -- allow | review
    fired_rules     TEXT         NULL,          -- JSON: [{rule, severity, details}, ...]
    rules_fired_n   INTEGER      NOT NULL DEFAULT 0,
    judged_against  VARCHAR(32)  NULL,          -- own_profile | peer_group | peer_group(new)
    trust_reason    TEXT         NULL,          -- why it was / was not judged on its own profile
    own_profile_anomaly BOOLEAN  NOT NULL DEFAULT false,
    amount          NUMERIC(30,2) NULL,
    currency        VARCHAR(8)   NULL,
    latency_ms      DOUBLE PRECISION NULL,      -- how long the decision took (speed audit)
    -- Transactional OUTBOX for guaranteed webhook delivery. The decision AND its
    -- webhook_status='pending' marker are written in the SAME commit, so a delivery is
    -- never lost to an API crash. A relay (webhook_relay.py, in the sync service)
    -- redelivers 'pending' rows with exponential backoff until they succeed or exhaust
    -- the retry budget. Lifecycle:  pending -> sent | dead  (disabled = no webhook set).
    webhook_status  VARCHAR(16)  NULL,          -- pending | sent | dead | disabled
    webhook_detail  TEXT         NULL,          -- last attempt's reason / error
    webhook_attempts INTEGER     NOT NULL DEFAULT 0,   -- delivery attempts made so far
    webhook_next_attempt_at TIMESTAMP NULL,     -- when the relay may next try (NULL = terminal)
    scored_at       TIMESTAMP    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bp_decision_entity ON bp_decision (entity_key, scored_at DESC);
CREATE INDEX IF NOT EXISTS idx_bp_decision_txn    ON bp_decision (transaction_id);
CREATE INDEX IF NOT EXISTS idx_bp_decision_review ON bp_decision (decision, scored_at DESC);
-- NOTE: the relay's partial index on webhook_next_attempt_at is created in the MIGRATIONS
-- section below, AFTER the guarded ALTER that adds the column — so this whole script still
-- applies cleanly (in one transaction) against a bp_decision created before those columns.

-- ---------------------------------------------------------------------------
-- 11. WEBHOOK DELIVERY LOG — every attempt to deliver a decision (append-only)
-- ---------------------------------------------------------------------------
-- Compliance/audit trail of outbound webhook deliveries. bp_decision holds the LATEST
-- webhook status for a decision; this table records EVERY attempt (append-only) with
-- the URL, HTTP status, response detail and latency — so an auditor can prove exactly
-- when and how each decision was (or was not) delivered to the downstream system.
CREATE TABLE IF NOT EXISTS bp_webhook_delivery (
    id              BIGSERIAL    PRIMARY KEY,
    decision_id     BIGINT       NULL,          -- FK-ish to bp_decision.id
    entity_key      VARCHAR(128) NULL,
    transaction_id TEXT         NULL,
    url             TEXT         NULL,          -- where we posted (no secrets)
    status          VARCHAR(16)  NOT NULL,      -- sent | failed | disabled
    http_status     INTEGER      NULL,          -- 200, 500, ... (null on transport error)
    detail          TEXT         NULL,          -- reason / error, truncated
    signed          BOOLEAN      NOT NULL DEFAULT false,  -- was an HMAC signature sent?
    latency_ms      DOUBLE PRECISION NULL,      -- how long the delivery attempt took
    attempted_at    TIMESTAMP    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bp_wh_decision ON bp_webhook_delivery (decision_id);
CREATE INDEX IF NOT EXISTS idx_bp_wh_txn      ON bp_webhook_delivery (transaction_id);
CREATE INDEX IF NOT EXISTS idx_bp_wh_status   ON bp_webhook_delivery (status, attempted_at DESC);

-- ---------------------------------------------------------------------------
-- 12. RECENT TRANSACTIONS — the real-time velocity/burst source.
-- ---------------------------------------------------------------------------
-- Every transaction POST /score sees is recorded here, so the velocity rules
-- (1m/10m/15m/1h/24h bursts) are computed from what the SERVICE itself has just seen —
-- real-time, ~ms, and with NO production read. Pruned to a short retention window (the
-- longest velocity window is 24h). This is the local recent-window store that replaced
-- the per-score production lookup.
CREATE TABLE IF NOT EXISTS bp_recent_txn (
    id                      BIGSERIAL    PRIMARY KEY,
    entity_key              VARCHAR(128) NOT NULL,      -- branch_id:origin_account_no
    ts                      TIMESTAMP    NOT NULL,      -- the transaction time (as scored)
    amount                  NUMERIC(30,2) NULL,
    destination_account_no  VARCHAR(128) NULL,          -- beneficiary (distinct-recipient windows)
    origin_country          VARCHAR(16)  NULL,          -- distinct-countries-in-1h window
    destination_country     VARCHAR(16)  NULL,
    currency                VARCHAR(8)   NULL,
    created_at              TIMESTAMP    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bp_recent_entity_ts ON bp_recent_txn (entity_key, ts DESC);
CREATE INDEX IF NOT EXISTS idx_bp_recent_ts        ON bp_recent_txn (ts);

-- ---------------------------------------------------------------------------
-- MIGRATIONS (idempotent, guarded) — applied by ensure_schema() on startup.
-- CREATE ... IF NOT EXISTS never alters an existing table, so additive column
-- changes go here. Each is a no-op once applied (checks the current type first,
-- so a large table is never rewritten on every boot).
-- ---------------------------------------------------------------------------
-- Real production transaction_id values are up to 88 chars; widen from VARCHAR(64).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['bp_transactions_cache','bp_rule_event','bp_event_log',
                           'bp_decision','bp_webhook_delivery'] LOOP
    IF to_regclass(t) IS NOT NULL AND EXISTS (
         SELECT 1 FROM information_schema.columns
          WHERE table_name = t AND column_name = 'transaction_id'
            AND data_type <> 'text') THEN
      EXECUTE format('ALTER TABLE %I ALTER COLUMN transaction_id TYPE TEXT', t);
      RAISE NOTICE 'migrated %.transaction_id -> TEXT', t;
    END IF;
  END LOOP;
END $$;

-- Webhook OUTBOX columns on an existing bp_decision (added after the table shipped).
-- ADD COLUMN IF NOT EXISTS is itself idempotent, so this is safe to run every boot.
ALTER TABLE bp_decision ADD COLUMN IF NOT EXISTS webhook_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bp_decision ADD COLUMN IF NOT EXISTS webhook_next_attempt_at TIMESTAMP NULL;
CREATE INDEX IF NOT EXISTS idx_bp_decision_wh_pending
    ON bp_decision (webhook_next_attempt_at)
    WHERE webhook_status = 'pending';

-- PER-CURRENCY grain (multi-currency model). Add the `currency` column to existing
-- profile / peer-baseline / history tables and repoint the primary keys to include it.
-- Existing rows default to NGN (the ~all-NGN population), and are superseded per currency
-- by the normal event-driven retrain / one-time --rebuild-all seed. Guarded so the PK
-- swap (an index rebuild) happens ONCE, not on every boot.
DO $$
BEGIN
  IF to_regclass('bp_user_behaviour_profile') IS NOT NULL THEN
    ALTER TABLE bp_user_behaviour_profile ADD COLUMN IF NOT EXISTS currency VARCHAR(8) NOT NULL DEFAULT 'NGN';
    IF NOT EXISTS (SELECT 1 FROM information_schema.key_column_usage
                    WHERE table_name='bp_user_behaviour_profile'
                      AND constraint_name='bp_user_behaviour_profile_pkey'
                      AND column_name='currency') THEN
      ALTER TABLE bp_user_behaviour_profile DROP CONSTRAINT IF EXISTS bp_user_behaviour_profile_pkey;
      ALTER TABLE bp_user_behaviour_profile ADD PRIMARY KEY (entity_key, currency);
      RAISE NOTICE 'bp_user_behaviour_profile -> PK (entity_key, currency)';
    END IF;
  END IF;
  IF to_regclass('bp_peer_baseline') IS NOT NULL THEN
    ALTER TABLE bp_peer_baseline ADD COLUMN IF NOT EXISTS currency VARCHAR(8) NOT NULL DEFAULT 'NGN';
    IF NOT EXISTS (SELECT 1 FROM information_schema.key_column_usage
                    WHERE table_name='bp_peer_baseline'
                      AND constraint_name='bp_peer_baseline_pkey'
                      AND column_name='currency') THEN
      ALTER TABLE bp_peer_baseline DROP CONSTRAINT IF EXISTS bp_peer_baseline_pkey;
      ALTER TABLE bp_peer_baseline ADD PRIMARY KEY (branch_id, account_type, currency);
      RAISE NOTICE 'bp_peer_baseline -> PK (branch_id, account_type, currency)';
    END IF;
  END IF;
  IF to_regclass('bp_profile_history') IS NOT NULL THEN
    ALTER TABLE bp_profile_history ADD COLUMN IF NOT EXISTS currency VARCHAR(8) NULL;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_bp_profile_entity ON bp_user_behaviour_profile (entity_key);

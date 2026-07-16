-- ############################################################################
-- LEGACY / SUPERSEDED — this is the OLD MySQL schema. The profile store is now
-- PostgreSQL: use `schema_pg.sql` (applied automatically by the db container).
-- Kept only for reference and for migrate_mysql_to_pg.py. Do not apply this.
-- ############################################################################
-- ============================================================================
-- Customer Behaviour Profile — MySQL test-DB schema (Aiven defaultdb)
-- ============================================================================
-- All objects are namespaced `bp_` so they never collide with the other
-- project's tables already living in this database.
--
-- Architecture (from "customer behavior profile build thought flow.md"):
--   * bp_user_behaviour_profile  = ONLINE store  (one row per entity, UPSERT).
--                                  Sub-millisecond lookup key for the rule
--                                  engine / future ML feature factory.
--   * bp_profile_history         = OFFLINE store (append-only snapshot log).
--                                  A timeline of every nightly build for
--                                  point-in-time joins & future model training.
--   * bp_incremental_state       = the self-updating "state machine": EWMA /
--                                  time-decay accumulators so the nightly job
--                                  updates the baseline WITHOUT a full recompute.
--   * bp_blacklist               = read-only mirror of production blacklist,
--                                  drives the "Flag Blacklisted" rule.
--   * bp_rule_definition         = AML rule catalogue + thresholds (AML_Rules).
--   * bp_rule_event              = log of rule firings when a txn is scored.
--   * bp_build_run               = one row per batch run (lineage / auditing).
--
-- ENTITY KEY DECISION:
--   entity_key = CONCAT(branch_id, ':', origin_account_no)
--   origin_account_no is ~100% populated and 99.4% stable to a single customer
--   name, whereas `identifier` is only ~45% populated and full of 'N/A' /
--   placeholder-BVN junk. When the account maps to monitoring_customer via
--   account_numbers, customer_id is filled so we can later replicate the
--   profile back into monitoring_customerbranchprofile in production.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. ONLINE STORE — the live behaviour profile (one row per entity, upserted)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_user_behaviour_profile (
    entity_key              VARCHAR(128) NOT NULL,          -- '{branch_id}:{origin_account_no}'
    branch_id               BIGINT       NULL,
    origin_account_no       VARCHAR(64)  NULL,
    customer_id             BIGINT       NULL,              -- resolved from monitoring_customer (nullable)
    customer_name           VARCHAR(255) NULL,
    identifier              VARCHAR(64)  NULL,
    bvn                     VARCHAR(32)  NULL,
    account_type            VARCHAR(32)  NULL,

    -- GOVERNANCE gate ("Practical rules" §1/§2/§10): only an Active profile with
    -- enough clean history is trusted by the rule engine; others are Warming Up
    -- and judged against peers. Stops behaviour-poisoning.
    profile_status          VARCHAR(16)  NOT NULL DEFAULT 'warming_up',  -- 'active' | 'warming_up'
    confidence_score        INT          NULL,      -- 0-100 (history + consistency + completeness)
    drift_status            VARCHAR(16)  NULL,      -- 'none' | 'gradual' | 'sudden' (§9)
    drift_reason            VARCHAR(255) NULL,      -- why drift was flagged
    retrain_reason          VARCHAR(64)  NULL,      -- why this profile was (re)built this run (§4)
    txns_since_build        INT          NOT NULL DEFAULT 0,  -- new txns since last retrain (event-driven trigger)
    drift_signal_count      INT          NOT NULL DEFAULT 0,  -- consecutive anomalies vs profile (drift trigger)
    last_retrained_at       DATETIME     NULL,      -- when features were last recomputed
    tenure_days             INT          NULL,      -- account age over FULL lifetime (production)
    lifetime_txns           INT          NULL,      -- total transactions ever
    lifetime_clean_txns     INT          NULL,      -- clean transactions ever (the trusted count)

    -- lifecycle / dormancy
    first_seen              DATETIME     NULL,
    last_seen               DATETIME     NULL,
    age_days                INT          NULL,
    dormant_days            INT          NULL,              -- window_end - last_seen (days)

    -- volume
    total_tx_count          INT          NOT NULL DEFAULT 0,
    total_tx_amount         DECIMAL(30,2) NOT NULL DEFAULT 0,

    -- sliding-window transaction COUNTS (relative to window_end)
    tx_count_24h            INT NOT NULL DEFAULT 0,
    tx_count_7d             INT NOT NULL DEFAULT 0,
    tx_count_30d            INT NOT NULL DEFAULT 0,
    tx_count_60d            INT NOT NULL DEFAULT 0,
    tx_count_90d            INT NOT NULL DEFAULT 0,

    -- sliding-window monetary SUMS
    amt_sum_30d             DECIMAL(30,2) NOT NULL DEFAULT 0,
    amt_sum_90d             DECIMAL(30,2) NOT NULL DEFAULT 0,

    -- monetary baseline (full 6-month window)
    avg_amount              DECIMAL(30,4) NULL,
    max_amount              DECIMAL(30,2) NULL,
    min_amount              DECIMAL(30,2) NULL,
    std_amount              DECIMAL(30,4) NULL,
    median_amount           DECIMAL(30,2) NULL,
    p95_amount              DECIMAL(30,2) NULL,

    -- monetary baseline (last 30 days)
    avg_amount_30d          DECIMAL(30,4) NULL,
    max_amount_30d          DECIMAL(30,2) NULL,
    std_amount_30d          DECIMAL(30,4) NULL,

    -- TIME-DECAY (exponential smoothing) — older activity matters less
    decayed_avg_amount      DECIMAL(30,4) NULL,
    decayed_tx_count        DOUBLE        NULL,
    decay_half_life_days    INT           NOT NULL DEFAULT 30,

    -- cadence
    avg_monthly_tx_count    DOUBLE NULL,
    avg_monthly_amount      DECIMAL(30,2) NULL,

    -- beneficiary / mule-detection signals
    distinct_beneficiaries      INT NOT NULL DEFAULT 0,
    distinct_beneficiaries_30d  INT NOT NULL DEFAULT 0,
    distinct_beneficiaries_24h  INT NOT NULL DEFAULT 0,
    beneficiaries               JSON NULL,   -- {destination_account_no: count}

    -- categorical diversity
    usual_transaction_types     JSON NULL,   -- {type: count}
    transaction_type_entropy    DOUBLE NULL, -- Shannon entropy (nats)
    usual_merchants             JSON NULL,   -- {merchant: count}
    merchant_entropy            DOUBLE NULL,

    -- geo
    usual_locations             JSON NULL,   -- {raw customer_location: count}
    usual_cities                JSON NULL,   -- {city: count}
    usual_countries             JSON NULL,   -- {origin_country: count}
    location_entropy            DOUBLE NULL,
    known_ip_addresses          JSON NULL,   -- {ip: count} (top N)
    known_ip_subnets            JSON NULL,   -- {/24 subnet: count}
    last_location               VARCHAR(255) NULL,
    last_city                   VARCHAR(128) NULL,
    last_country                VARCHAR(16)  NULL,
    last_ip                     VARCHAR(64)  NULL,
    last_event_ts               DATETIME     NULL,          -- for impossible-travel

    -- temporal fingerprint
    peak_transaction_hours      JSON NULL,   -- {hour(0-23): count}
    top_hour                    INT NULL,
    peak_transaction_days       JSON NULL,   -- {Mon..Sun: count}  (day-of-week pattern)
    top_day_of_week             VARCHAR(4) NULL,
    night_activity_ratio        DOUBLE NULL, -- share of txns 00:00-05:59

    -- risk flags (snapshotted from production reference data)
    is_blacklisted          TINYINT(1) NOT NULL DEFAULT 0,
    is_pep                  TINYINT(1) NOT NULL DEFAULT 0,
    is_sanction             TINYINT(1) NOT NULL DEFAULT 0,
    risk_level              VARCHAR(16) NULL,
    risk_score              INT NULL,
    suspicious_tx_count     INT NOT NULL DEFAULT 0,
    suspicious_ratio        DOUBLE NULL,

    -- lineage
    profile_version         INT NOT NULL DEFAULT 1,
    build_run_id            VARCHAR(64) NULL,
    window_start            DATETIME NULL,
    window_end              DATETIME NULL,
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (entity_key),
    KEY idx_bp_profile_customer (customer_id),
    KEY idx_bp_profile_branch (branch_id),
    KEY idx_bp_profile_acct (origin_account_no),
    KEY idx_bp_status (profile_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 2. OFFLINE STORE — append-only history (one snapshot per entity per run)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_profile_history (
    id              BIGINT NOT NULL AUTO_INCREMENT,
    entity_key      VARCHAR(128) NOT NULL,
    build_run_id    VARCHAR(64)  NOT NULL,
    snapshot_ts     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    profile_version INT          NOT NULL,
    total_tx_count  INT          NULL,
    total_tx_amount DECIMAL(30,2) NULL,
    avg_amount      DECIMAL(30,4) NULL,
    decayed_avg_amount DECIMAL(30,4) NULL,
    profile_json    JSON         NULL,       -- full snapshot (only when STORE_HISTORY_JSON=1; heavy)
    PRIMARY KEY (id),
    KEY idx_bp_hist_entity (entity_key),
    KEY idx_bp_hist_run (build_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 3. INCREMENTAL STATE — EWMA / time-decay accumulators (self-updating machine)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_incremental_state (
    entity_key       VARCHAR(128) NOT NULL,
    ewma_mean_amount DOUBLE NULL,      -- exponentially-weighted mean amount
    ewma_var_amount  DOUBLE NULL,      -- exponentially-weighted variance
    decayed_count    DOUBLE NULL,      -- decayed transaction count
    last_amount      DOUBLE NULL,
    last_event_ts    DATETIME NULL,    -- timestamp of most recent event folded in
    last_decay_ts    DATETIME NULL,    -- when decay was last applied
    half_life_days   INT NOT NULL DEFAULT 30,
    updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (entity_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 4. BLACKLIST MIRROR — snapshot of production users_blacklist (read reference)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_blacklist (
    id              BIGINT NOT NULL,
    blacklist_type  VARCHAR(64) NULL,
    source          VARCHAR(128) NULL,
    risk_level      VARCHAR(32) NULL,
    name            VARCHAR(255) NULL,
    entity_type     VARCHAR(64) NULL,
    identifier_type VARCHAR(64) NULL,
    identifier      VARCHAR(128) NULL,
    status          VARCHAR(32) NULL,
    date_created    DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_bp_bl_identifier (identifier),
    KEY idx_bp_bl_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 5. RULE CATALOGUE — the AML rules that fire off the profile (AML_Rules.xlsx)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_rule_definition (
    rule_code    VARCHAR(64)  NOT NULL,
    category     VARCHAR(128) NULL,
    name         VARCHAR(255) NOT NULL,
    description  TEXT         NULL,
    params       JSON         NULL,     -- thresholds / window sizes
    enabled      TINYINT(1)   NOT NULL DEFAULT 1,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 5b. RULE SETTINGS — PER-CLIENT threshold overrides (tier-1/2/3 banks differ)
-- ---------------------------------------------------------------------------
-- Each client (a branch_id = one institution) can override the global default
-- thresholds in bp_rule_definition. The rule engine reads the client override
-- first and falls back to the global default when none is set. This is how a
-- tier-1 bank and a tier-3 bank can run the same rule with different limits.
CREATE TABLE IF NOT EXISTS bp_rule_settings (
    branch_id   BIGINT       NOT NULL,          -- the client / institution
    rule_code   VARCHAR(64)  NOT NULL,
    params      JSON         NULL,              -- overrides merged over the global params
    enabled     TINYINT(1)   NULL,              -- NULL = inherit global; 0/1 = force off/on
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (branch_id, rule_code),
    KEY idx_bp_rs_branch (branch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 5c. PEER BASELINE — non-ML cold-start profile for brand-new accounts
-- ---------------------------------------------------------------------------
-- A brand-new account has no history of its own, so no learned baseline. Rather
-- than fall back to only the hard rules, it INHERITS the average behaviour of its
-- peers — same institution (branch_id) + same account type (individual/business).
-- This is plain arithmetic (group averages), NOT clustering / embeddings / ML.
-- Once the account builds its own history, its personal profile takes over.
CREATE TABLE IF NOT EXISTS bp_peer_baseline (
    branch_id               BIGINT       NOT NULL,   -- the institution
    account_type            VARCHAR(32)  NOT NULL,   -- individual / business / unknown
    peer_entities           INT          NULL,       -- how many accounts formed this baseline
    peer_tx_count           BIGINT       NULL,
    avg_amount              DECIMAL(30,4) NULL,
    median_amount           DECIMAL(30,2) NULL,
    p95_amount              DECIMAL(30,2) NULL,
    max_amount              DECIMAL(30,2) NULL,
    std_amount              DECIMAL(30,4) NULL,
    avg_monthly_tx_count    DOUBLE        NULL,
    usual_cities            JSON          NULL,       -- top cities across the peer group
    usual_countries         JSON          NULL,
    peak_transaction_hours  JSON          NULL,       -- {hour: count} across the peer group
    build_run_id            VARCHAR(64)   NULL,
    updated_at              DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (branch_id, account_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 6. RULE EVENTS — every firing when an incoming txn is compared to a profile
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_rule_event (
    id             BIGINT NOT NULL AUTO_INCREMENT,
    entity_key     VARCHAR(128) NULL,
    transaction_id VARCHAR(64)  NULL,
    rule_code      VARCHAR(64)  NOT NULL,
    fired_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    severity       VARCHAR(16)  NULL,
    details        JSON         NULL,
    PRIMARY KEY (id),
    KEY idx_bp_evt_entity (entity_key),
    KEY idx_bp_evt_rule (rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 6b. EVENT LOG — per-transaction / per-customer accountability trail
-- ---------------------------------------------------------------------------
-- Every meaningful thing the microservice does is recorded here: a transaction
-- scored, a retrain triggered, a retrain skipped (with the reason), or a retrain
-- that failed (with the error). Powers GET /customer/{key} and audits.
CREATE TABLE IF NOT EXISTS bp_event_log (
    id             BIGINT      NOT NULL AUTO_INCREMENT,
    entity_key     VARCHAR(128) NULL,
    transaction_id VARCHAR(64)  NULL,
    event_type     VARCHAR(32)  NOT NULL,   -- score | retrain | retrain_skip | retrain_fail
    outcome        VARCHAR(32)  NULL,        -- allow | review | retrained | skipped | failed
    detail         JSON         NULL,
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_evt_entity (entity_key, created_at),
    KEY idx_evt_type (event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 7. BUILD RUN — one row per batch execution (lineage / audit)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bp_build_run (
    run_id       VARCHAR(64) NOT NULL,
    started_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at  DATETIME NULL,
    window_start DATETIME NULL,
    window_end   DATETIME NULL,
    source_rows  BIGINT NULL,
    entities     INT NULL,
    status       VARCHAR(32) NOT NULL DEFAULT 'running',
    notes        TEXT NULL,
    PRIMARY KEY (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

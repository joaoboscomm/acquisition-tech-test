-- ============================================================
-- Nexus Collective — Reconciliation Data Infrastructure
-- Part 2A: Relational Schema (Postgres 15+)
-- ============================================================
-- 11 tables across 6 layers: identity → raw events → reference
-- data → transformed → reconciliation → audit. Every table is
-- referenced by the n8n pipelines (n8n_pipeline.json and
-- data_ingestion_workflow.json).


-- =========================================
-- 1. IDENTITY — Contributor Model
-- =========================================
-- One row per real person, linked to all their emails and
-- system IDs across ChargeHub, HubSpot, Skool, PayEngine.

CREATE TABLE contributors (
    contributor_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE contributor_identifiers (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contributor_id  UUID NOT NULL REFERENCES contributors(contributor_id),
    identifier_type TEXT NOT NULL,          -- 'email', 'hubspot_contact_id', 'chargehub_customer_id', 'skool_user_id'
    identifier_value TEXT NOT NULL,
    source_system   TEXT NOT NULL,          -- 'hubspot', 'chargehub', 'payengine', 'skool'
    is_primary      BOOLEAN DEFAULT FALSE,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (identifier_type, identifier_value, source_system)
);

-- Given an email, find the contributor in O(1)
CREATE INDEX idx_identifiers_lookup
    ON contributor_identifiers(identifier_type, identifier_value);
CREATE INDEX idx_identifiers_contributor
    ON contributor_identifiers(contributor_id);


-- =========================================
-- 2. RAW EVENT STORE — Immutable Ingestion
-- =========================================
-- Append-only log of every billing webhook/pull.
-- Full JSONB payload preserved so we never lose source data.

CREATE TABLE raw_billing_events (
    event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_system   TEXT NOT NULL,          -- 'chargehub', 'payengine', 'skool'
    event_type      TEXT NOT NULL,          -- 'charge_processed', 'charge_failed', 'refund'
    source_event_id TEXT NOT NULL,
    event_timestamp TIMESTAMPTZ NOT NULL,
    payload         JSONB NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingestion_run_id UUID,

    UNIQUE (source_system, source_event_id)
);

CREATE INDEX idx_raw_events_source_time
    ON raw_billing_events(source_system, event_timestamp);


-- =========================================
-- 3. REFERENCE DATA & PIPELINE STATE
-- =========================================

-- Cursor store for stateful ingestion (Skool daily pull).
CREATE TABLE pipeline_state (
    key             TEXT PRIMARY KEY,        -- e.g. 'skool_last_pull'
    value           TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Daily FX snapshots for revenue conversion independent of
-- ChargeHub's point-in-time rate.
CREATE TABLE exchange_rates (
    rate_date       DATE NOT NULL,
    currency_from   TEXT NOT NULL,
    currency_to     TEXT NOT NULL DEFAULT 'USD',
    rate            NUMERIC(12,6) NOT NULL,
    source          TEXT NOT NULL DEFAULT 'ecb',
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (rate_date, currency_from, currency_to)
);

-- Tier pricing with temporal validity (supports price changes
-- without breaking historical reconciliation).
CREATE TABLE tier_definitions (
    tier_name       TEXT PRIMARY KEY,
    usd_amount      NUMERIC(10,2) NOT NULL,
    billing_cycle   TEXT NOT NULL DEFAULT 'monthly',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    effective_from  DATE NOT NULL,
    effective_to    DATE,

    UNIQUE (tier_name, effective_from)
);

INSERT INTO tier_definitions (tier_name, usd_amount, billing_cycle, effective_from) VALUES
    ('Starter',          750,   'monthly', '2020-01-01'),
    ('Pro',             2500,   'monthly', '2020-01-01'),
    ('Executive',       6000,   'monthly', '2020-01-01'),
    ('Executive Annual',24000,  'annual',  '2020-01-01');


-- =========================================
-- 4. TRANSFORMED LAYER
-- =========================================
-- Clean, typed tables materialized from raw events.
-- This is what the reconciliation engine queries.

CREATE TABLE charges (
    charge_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raw_event_id        BIGINT NOT NULL REFERENCES raw_billing_events(event_id),
    contributor_id      UUID REFERENCES contributors(contributor_id),
    source_system       TEXT NOT NULL,
    source_charge_id    TEXT NOT NULL,
    email               TEXT NOT NULL,
    processed_at        TIMESTAMPTZ NOT NULL,
    processed_at_pst    TIMESTAMPTZ NOT NULL,
    tier_name           TEXT,
    presentment_amount  NUMERIC(10,2),
    presentment_currency TEXT,
    usd_amount          NUMERIC(10,2),
    normalized_usd      NUMERIC(10,2),
    billing_period      TEXT NOT NULL,        -- 'YYYY-MM'

    UNIQUE (source_system, source_charge_id)
);

CREATE INDEX idx_charges_period ON charges(billing_period);
CREATE INDEX idx_charges_contributor ON charges(contributor_id);


-- Immutable snapshots — each HubSpot sync inserts a new row,
-- never updates. Preserves full history of tier/status changes.
CREATE TABLE memberships (
    membership_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contributor_id          UUID REFERENCES contributors(contributor_id),
    hubspot_membership_id   TEXT NOT NULL,
    hubspot_contact_id      TEXT NOT NULL,
    tier_name               TEXT,
    mrr                     NUMERIC(10,2),
    mrr_currency            TEXT,
    mrr_normalized_usd      NUMERIC(10,2),
    billing_source          TEXT,
    billing_date            DATE,
    status                  TEXT,
    membership_type         TEXT,
    created_at              TIMESTAMPTZ NOT NULL,
    snapshot_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (hubspot_membership_id, snapshot_at)
);

CREATE INDEX idx_memberships_contributor ON memberships(contributor_id);
CREATE INDEX idx_memberships_hubspot ON memberships(hubspot_membership_id);


-- =========================================
-- 5. RECONCILIATION — Revenue Protection
-- =========================================
--
-- This is the business core of the system. Every charge that
-- comes in gets matched against HubSpot and classified into
-- one of four revenue statuses. Each status maps directly to
-- a financial risk that leadership needs to see:
--
--   ┌──────────────┬─────────────────────────────────────────────┐
--   │ STATUS       │ WHAT IT MEANS FOR THE BUSINESS              │
--   ├──────────────┼─────────────────────────────────────────────┤
--   │ confirmed    │ Charge collected, CRM agrees. Revenue is    │
--   │              │ real and reportable.                         │
--   ├──────────────┼─────────────────────────────────────────────┤
--   │ disputed     │ Charge + CRM both exist, but data doesn't   │
--   │              │ match (wrong tier, wrong MRR, wrong date).   │
--   │              │ Risk: incorrect investor reports, wrong      │
--   │              │ billing amounts, silent revenue erosion.     │
--   ├──────────────┼─────────────────────────────────────────────┤
--   │ phantom      │ CRM says "active member" but NO charge was  │
--   │              │ collected. Giving away access for free.      │
--   │              │ This is a direct revenue leak.               │
--   ├──────────────┼─────────────────────────────────────────────┤
--   │ untracked    │ Charge collected but ZERO CRM record.       │
--   │              │ Money is coming in that nobody knows about.  │
--   │              │ Can't attribute, can't renew, can't upsell.  │
--   └──────────────┴─────────────────────────────────────────────┘
--
-- The 8 mismatch flags pinpoint exactly WHAT is wrong per record.
-- revenue_at_risk turns each mismatch into a dollar amount so ops
-- sees "$12,500/mo in untracked revenue" instead of "4 data issues."

CREATE TABLE reconciliation_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    billing_period  TEXT NOT NULL,
    source_system   TEXT NOT NULL,           -- 'chargehub', 'payengine', 'skool'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',
    summary         JSONB,
    triggered_by    TEXT,                    -- 'scheduled', 'manual', 'retry'

    -- One successful run per period per source (idempotency)
    UNIQUE (billing_period, source_system, status)
);


CREATE TABLE reconciliation_results (
    result_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES reconciliation_runs(run_id),

    -- Matching context
    matching_email      TEXT,
    contact_id          TEXT,
    contributor_id      UUID REFERENCES contributors(contributor_id),
    charge_id           BIGINT REFERENCES charges(charge_id),
    membership_id       TEXT,
    match_type          TEXT NOT NULL,       -- 'matched', 'charge_no_crm', 'crm_no_charge'

    -- Revenue classification (see table above for business impact)
    revenue_status      TEXT NOT NULL,       -- 'confirmed', 'disputed', 'phantom', 'untracked'

    -- ChargeHub snapshot (captured at reconciliation time)
    chargehub_line_item             TEXT,
    chargehub_processed_date        TEXT,
    chargehub_total_price           NUMERIC(10,2),
    chargehub_total_price_normalized NUMERIC(10,2),
    chargehub_currency              TEXT,

    -- HubSpot snapshot (captured at reconciliation time)
    membership_tier                 TEXT,
    membership_billing_date         TEXT,
    membership_mrr                  NUMERIC(10,2),
    membership_mrr_normalized       NUMERIC(10,2),
    membership_currency             TEXT,
    membership_billing_source       TEXT,
    membership_status               TEXT,
    membership_type                 TEXT,

    -- 8 mismatch flags — pinpoint exactly what's wrong
    tier_mismatch               BOOLEAN DEFAULT FALSE,
    billing_date_mismatch       BOOLEAN DEFAULT FALSE,
    mrr_mismatch                BOOLEAN DEFAULT FALSE,
    mrr_normalized_mismatch     BOOLEAN DEFAULT FALSE,
    currency_mismatch           BOOLEAN DEFAULT FALSE,
    billing_source_mismatch     BOOLEAN DEFAULT FALSE,
    status_mismatch             BOOLEAN DEFAULT FALSE,
    type_mismatch               BOOLEAN DEFAULT FALSE,

    -- Impact & resolution
    revenue_at_risk     NUMERIC(10,2),       -- $ impact of this mismatch
    severity            TEXT,                -- 'critical', 'warning', 'info'
    fix_instructions    TEXT,
    resolved_at         TIMESTAMPTZ,
    resolved_by         TEXT,
    days_to_resolve     INTEGER              -- set on resolution for SLA tracking
);

CREATE INDEX idx_recon_results_run ON reconciliation_results(run_id);
CREATE INDEX idx_recon_results_status ON reconciliation_results(revenue_status);
CREATE INDEX idx_recon_results_unresolved
    ON reconciliation_results(severity, resolved_at)
    WHERE resolved_at IS NULL;


-- =========================================
-- 6. AUDIT LOG — Operational Intelligence
-- =========================================
-- Business-level event trail for dashboards: trend analysis,
-- resolution time tracking, and revenue leak quantification.

CREATE TABLE audit_log (
    log_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type      TEXT NOT NULL,          -- 'mismatch_detected', 'fix_applied', 'identity_merged'
    category        TEXT NOT NULL,          -- 'tier', 'mrr', 'currency', 'missing_record'
    severity        TEXT NOT NULL,
    source_system   TEXT,
    contributor_id  UUID,
    reference_ids   JSONB,                  -- {"charge_id": X, "run_id": Z}
    summary         TEXT NOT NULL,
    revenue_impact  NUMERIC(10,2),          -- positive = recovered, negative = at risk
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_time ON audit_log(created_at);
CREATE INDEX idx_audit_log_category ON audit_log(category, severity);
CREATE INDEX idx_audit_log_contributor ON audit_log(contributor_id);


-- =========================================
-- DESIGN RATIONALE
-- =========================================
-- • Contributor model (not flat email lookup) → same person can
--   have different emails in ChargeHub, HubSpot, and Skool.
-- • Immutable raw events + membership snapshots → full audit
--   trail; can replay or debug without re-pulling from APIs.
-- • Four revenue statuses → turns vague "data quality issues"
--   into quantified dollar impact for leadership reporting.
-- • Idempotent reconciliation runs → prevents duplicate alerts
--   and inflated mismatch counts when pipelines retry.

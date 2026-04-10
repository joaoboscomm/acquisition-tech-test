# n8n Workflow Guide — Import, Configure, Run

This document explains the two n8n workflows included in this repo. Both are importable JSON files with **all triggers disabled** so you can safely import, inspect, and enable selectively.

Together they implement the full data platform described in [pipeline_architecture.md](../output/pipeline_architecture.md) — from raw event ingestion through reconciliation to alerting.

---

## How the Two Workflows Fit Together

```
 data_ingestion_workflow.json          n8n_pipeline.json
 ─────────────────────────────        ──────────────────────────
 POPULATES the database:              READS the database:
   • contributors                       • reconciliation_runs
   • contributor_identifiers            • reconciliation_results
   • raw_billing_events                 • audit_log
   • charges
   • memberships                      QUERIES external systems:
   • exchange_rates                     • HubSpot API (contact search)
   • pipeline_state                     • Slack (alerts)
                                        • HubSpot (task creation)
         │                                       │
         └────────── schema.sql ─────────────────┘
                  (shared database)
```

**Ingestion runs first** (populates data), then **Reconciliation runs** (detects mismatches and alerts). In production, ingestion runs continuously (webhooks) or daily (scheduled pulls), and reconciliation runs daily at 8 AM PST after all data has settled.

---

## Workflow 1 — Data Ingestion (20 nodes)

**File:** `extra_files/data_ingestion_workflow.json`
**Purpose:** Get billing and CRM data from all four sources into the Postgres database.

### Five Independent Paths

Each path has its own trigger and runs independently. They share a single n8n workflow canvas but do not depend on each other.

---

### Path 1 — HubSpot Nightly Sync (4 nodes)

**What it does:** Pulls all HubSpot contacts + membership properties nightly, upserts into the contributor identity model.

| # | Node | Type | What It Does |
|---|------|------|-------------|
| 1 | **HubSpot Sync Schedule (Daily 2AM)** | Schedule Trigger | Fires at 2 AM PST daily. Disabled — enable when credentials are configured. |
| 2 | **HubSpot — Get All Contacts** | HubSpot Node | Pulls every contact with 12 membership properties (tier, MRR, currency, billing date, billing source, status, type, etc.). |
| 3 | **Transform Contacts → Contributors + Memberships** | Python Code | For each contact: generates a deterministic `contributor_id` (UUID5 from email), extracts all membership fields, builds insert-ready rows. |
| 4 | **Persist Contributors + Memberships** | Postgres | Three SQL operations per contact: (1) upsert `contributors`, (2) upsert `contributor_identifiers` (email + HubSpot ID), (3) insert immutable `memberships` snapshot. |

**Why nightly, not real-time:** HubSpot webhooks exist but can miss events or deliver out of order. The nightly full sync is a safety net — the 48-hour overlap window catches anything the webhooks missed.

**Tables populated:** `contributors`, `contributor_identifiers`, `memberships`

---

### Path 2 — ChargeHub Charge Ingestion (3 nodes)

**What it does:** Receives a ChargeHub `charge.processed` webhook, transforms it, stores both the raw event and a clean charge row.

| # | Node | Type | What It Does |
|---|------|------|-------------|
| 1 | **ChargeHub Charge Webhook** | Webhook Trigger | Listens on `/chargehub-charge-event`. Disabled — enable and point ChargeHub webhooks here. |
| 2 | **Transform ChargeHub → Raw Event + Charge** | Python Code | Extracts email, amount, currency, line item. Converts UTC → PST. Normalizes non-USD amounts via tier lookup. Generates `contributor_id` from email. |
| 3 | **Persist ChargeHub Raw Event + Charge** | Postgres | Two SQL operations: (1) insert raw JSONB payload into `raw_billing_events` (ON CONFLICT = dedup), (2) insert clean typed row into `charges` with a sub-select to link the `raw_event_id`. |

**Dedup logic:** `ON CONFLICT (source_system, source_event_id) DO NOTHING` — if the same charge arrives twice (webhook retry), it's silently ignored.

**Tables populated:** `raw_billing_events`, `charges`

---

### Path 3 — PayEngine Charge Ingestion (3 nodes)

**What it does:** Identical to Path 2 but maps PayEngine's field names (`transaction_id`, `customer_email`, `amount`, `product_name`) to the shared schema.

| # | Node | Type | What It Does |
|---|------|------|-------------|
| 1 | **PayEngine Charge Webhook** | Webhook Trigger | Listens on `/payengine-charge-event`. Disabled. |
| 2 | **Transform PayEngine → Raw Event + Charge** | Python Code | Same logic as ChargeHub transform, different field mapping. Sets `source_system = 'payengine'`. |
| 3 | **Persist PayEngine Raw Event + Charge** | Postgres | Same SQL as ChargeHub persist — the schema is source-agnostic. |

**Design note:** Paths 2 and 3 are intentionally near-identical. Adding a new billing source is a copy-paste-adapt operation (change the field mapping in the transform node), not a redesign. This is deliberate.

**Tables populated:** `raw_billing_events`, `charges`

---

### Path 4 — Skool Daily Pull (6 nodes)

**What it does:** Skool has no webhooks. This path pulls new transactions daily using a cursor pattern — it only fetches records newer than the last successful pull.

| # | Node | Type | What It Does |
|---|------|------|-------------|
| 1 | **Skool Pull Schedule (Daily 6AM)** | Schedule Trigger | Fires at 6 AM PST daily (before the 8 AM reconciliation window). Disabled. |
| 2 | **Get Skool Cursor** | Postgres | Reads the last successful pull timestamp from `pipeline_state` (key = `skool_last_pull`). Falls back to 48 hours ago on first run. |
| 3 | **Skool — Fetch Transactions** | HTTP Request | Calls `api.skool.com/v1/transactions?since={cursor}`. Placeholder — actual endpoint and auth configured when credentials are provisioned. Disabled. |
| 4 | **Transform Skool → Raw Events + Charges** | Python Code | Batch transform: loops through all Skool transactions, applies the same normalization as ChargeHub/PayEngine. Tracks the latest timestamp for cursor update. |
| 5 | **Persist Skool Raw Event + Charge** | Postgres | Same SQL pattern as Paths 2 and 3. |
| 6 | **Update Skool Cursor** | Postgres | `INSERT ... ON CONFLICT DO UPDATE SET value = EXCLUDED.value WHERE EXCLUDED.value > pipeline_state.value` — the cursor only advances forward, never backwards. Safe to retry. |

**Why the cursor matters:** Without it, every daily run would re-pull the entire transaction history. The cursor makes each run incremental. And because the cursor only advances after a successful persist (node 6 runs after node 5), a failed run simply retries from the same point.

**Tables populated:** `raw_billing_events`, `charges`, `pipeline_state`

---

### Path 5 — FX Rate Ingestion (4 nodes)

**What it does:** Pulls daily exchange rates so we can normalize non-USD charges independently of ChargeHub's point-in-time conversion.

| # | Node | Type | What It Does |
|---|------|------|-------------|
| 1 | **FX Rate Schedule (Daily 6AM)** | Schedule Trigger | Fires at 6 AM PST daily. Disabled. |
| 2 | **Fetch FX Rates** | HTTP Request | Calls `api.exchangerate.host/latest` for BRL, GBP, EUR, CAD, AUD, MXN rates against USD. |
| 3 | **Parse FX Rates** | Python Code | Inverts rates to get foreign-currency-to-USD conversion. Outputs one row per currency pair. |
| 4 | **Upsert FX Rates** | Postgres | Inserts into `exchange_rates` with `ON CONFLICT` upsert — if today's rate already exists, update it. |

**Tables populated:** `exchange_rates`

---

## Workflow 2 — Reconciliation Pipeline (16 nodes)

**File:** `output/n8n_pipeline.json`
**Purpose:** Take a single ChargeHub charge, match it against HubSpot, classify the revenue status, persist results, and alert the team.

### The Flow

```
Webhook ──► Set Run Params ──► Idempotency Check ──► Already Processed?
                                                          │
                                              ┌───── NO ──┘ (YES = stop)
                                              ▼
                                        Register Run
                                              │
                                              ▼
                                    HubSpot Search (email)
                                              │
                                       Contact Found?
                                        │           │
                                      YES          NO
                                        │           │
                                        │     Fallback Search
                                        │     (domain match)
                                        │           │
                                        └─────┬─────┘
                                              ▼
                                      Process & Classify
                                              │
                                              ▼
                                    Persist Results + Audit
                                              │
                                              ▼
                                        Finalize Run
                                              │
                                    ┌─────────┴─────────┐
                                    ▼                   ▼
                              Slack Alert         Is Critical?
                                                      │
                                                    YES
                                                      ▼
                                              Create HubSpot Task
```

### Node-by-Node

| # | Node | Type | What It Does |
|---|------|------|-------------|
| 1 | **Webhook Trigger** | Webhook | Receives a POST to `/chargehub-reconciliation` with charge data (email, amount, tier, currency, date). |
| 2 | **Schedule Trigger (Daily 8AM)** | Schedule | Alternative trigger for batch mode. Disabled — the webhook is the primary entry point. |
| 3 | **Manual Trigger** | Manual | For testing in the n8n UI. Disabled. |
| 4 | **Set Run Parameters** | Python Code | Extracts charge fields from the webhook body. Generates a `run_id` (UUID). Writes a context file to `/tmp` so downstream nodes can access charge data. |
| 5 | **Check Idempotency** | Postgres | Queries `reconciliation_runs` — has this exact `charge_id` already been reconciled successfully? |
| 6 | **Already Processed?** | IF Node | If a completed run exists for this charge → stop (prevent duplicate processing). Otherwise → continue. |
| 7 | **Register Run** | Postgres | Inserts a new row into `reconciliation_runs` with `status = 'running'`. This is the start of the audit trail. |
| 8 | **HubSpot — Search by Email** | HTTP Request | Calls HubSpot's Contact Search API with the charge email. Requests 12 membership properties. Uses HTTP Request (not the native node) for custom property support. |
| 9 | **Contact Found?** | IF Node | Did HubSpot return at least one contact? YES → go to classification. NO → try fallback search. |
| 10 | **HubSpot — Fallback Search** | HTTP Request | Searches HubSpot by email domain (`CONTAINS_TOKEN` on the domain part). Returns up to 5 contacts for manual review. Catches cases where the charge email is slightly different from HubSpot. |
| 11 | **Process & Classify** | Python Code | **The core logic.** Same algorithm as `reconcile.py`: (1) load charge data from context file, (2) extract HubSpot membership properties, (3) normalize non-USD amounts via tier lookup, (4) compute 8 mismatch flags, (5) classify as confirmed / disputed / phantom / untracked, (6) calculate `revenue_at_risk`, (7) generate fix instructions and alert text. |
| 12 | **Persist Results + Audit** | Postgres | Two SQL operations: (1) INSERT into `reconciliation_results` with all 30 columns (matching email, both-side snapshots, 8 flags, severity, fix instructions), (2) INSERT into `audit_log` for non-confirmed results (mismatch detected event with revenue impact). |
| 13 | **Finalize Run** | Postgres | Updates `reconciliation_runs` SET `status = 'completed'`, `completed_at = NOW()`, and writes a JSON summary (charge_id, email, revenue_status, severity, revenue_at_risk). |
| 14 | **Slack — Alert** | Slack | Posts to `#billing-reconciliation` channel. Message includes revenue status, severity, revenue at risk, fix instructions, and the run ID. Fires for every charge (confirmed = info, disputed/untracked = action needed). |
| 15 | **Is Critical?** | IF Node | Checks if severity = 'critical'. Only critical issues (tier mismatches, missing CRM records) get a HubSpot task. |
| 16 | **Create HubSpot Task** | HTTP Request | Creates a HIGH priority task in HubSpot with subject, description (email, status, revenue at risk, fix instructions), and assigns it for follow-up. Only fires for critical issues. |

### Revenue Classification Logic (Node 11)

This is the same classification used in `reconcile.py` and stored in `schema.sql`:

| Status | Condition | Severity | Example |
|--------|-----------|----------|---------|
| **confirmed** | Match found, zero mismatches | info | Tyler Brooks — $750 Starter, everything aligns |
| **disputed** | Match found, tier mismatch | critical | Natasha Bloom — charged $6K Executive, HubSpot says $750 Starter |
| **disputed** | Match found, other mismatches only | warning | Camila Ferreira — BRL currency, MRR amounts differ |
| **untracked** | No HubSpot contact found at all | critical | Daniel Marsh — $750 charge, zero CRM record |

(Phantom revenue — CRM active but no charge — is detected by the batch reconciliation described in [pipeline_architecture.md](../output/pipeline_architecture.md), not by this single-record webhook flow.)

---

## Prerequisites for Running

### Both Workflows Need:

1. **Postgres database** — Run `output/schema.sql` to create all 11 tables
2. **n8n Postgres credential** — Configure connection to your database
3. **Python 3** on the n8n server — Both workflows use `pythonNative` code nodes. Requires `N8N_PYTHON_INSTALLED=true` environment variable for self-hosted n8n.

### Reconciliation Pipeline Additionally Needs:

4. **HubSpot API credential** — Private app token with `crm.objects.contacts.read` scope
5. **Slack credential** — Bot token with `chat:write` permission to the target channel
6. **HubSpot API for tasks** — Same private app, also needs `crm.objects.custom.write` for task creation

### Ingestion Workflow Additionally Needs:

7. **HubSpot credential** (Path 1) — Same as above
8. **Skool API credential** (Path 4) — HTTP header auth, configured when API access is provisioned
9. **ChargeHub webhook URL** (Path 2) — Point ChargeHub's webhook settings to the n8n webhook URL
10. **PayEngine webhook URL** (Path 3) — Same as ChargeHub, different endpoint

---

## Safe Import Guide

1. Open n8n → **Workflows** → **Import from File**
2. Import both JSON files
3. All triggers are **disabled by default** — nothing runs until you enable it
4. Configure credentials (Postgres, HubSpot, Slack) in n8n's credential manager
5. Run `schema.sql` against your Postgres database
6. Enable one trigger at a time to test each path independently
7. Use the **Manual Trigger** (node 3 in the reconciliation pipeline) to test without a real webhook

---

## Schema Coverage

Every table in `schema.sql` is written to by at least one workflow:

| Table | Written By | Workflow |
|-------|-----------|----------|
| `contributors` | Persist Contributors + Memberships | Ingestion (Path 1) |
| `contributor_identifiers` | Persist Contributors + Memberships | Ingestion (Path 1) |
| `raw_billing_events` | Persist ChargeHub / PayEngine / Skool | Ingestion (Paths 2, 3, 4) |
| `charges` | Persist ChargeHub / PayEngine / Skool | Ingestion (Paths 2, 3, 4) |
| `memberships` | Persist Contributors + Memberships | Ingestion (Path 1) |
| `exchange_rates` | Upsert FX Rates | Ingestion (Path 5) |
| `pipeline_state` | Update Skool Cursor | Ingestion (Path 4) |
| `tier_definitions` | Pre-populated by schema.sql | — |
| `reconciliation_runs` | Register Run, Finalize Run | Reconciliation |
| `reconciliation_results` | Persist Results + Audit | Reconciliation |
| `audit_log` | Persist Results + Audit | Reconciliation |

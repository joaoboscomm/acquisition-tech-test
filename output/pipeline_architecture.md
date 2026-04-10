# Pipeline Architecture — Nexus Collective Reconciliation

## Overview

The pipeline replaces the current manual process (CSV exports → Python script → monthly review) with an automated system that ingests billing events in real time, reconciles daily, and surfaces gaps within 24 hours.

> 📊 **Visual diagram:** See [`nexus-macro-architecture.svg`](nexus-macro-architecture.svg) for the full architecture diagram.

---

## 1. Ingestion Layer

### ChargeHub & PayEngine — Webhook-Driven

Both systems fire webhooks on `charge.processed`, `charge.failed`, and `subscription.cancelled`.

**n8n workflow: `ingest-chargehub-webhook`**
1. Webhook trigger node receives the event
2. Validate payload (check required fields, reject malformed)
3. Dedup check: query `raw_billing_events` for `(source_system, source_event_id)`. If exists → acknowledge and stop (idempotent)
4. Insert into `raw_billing_events` with full JSONB payload
5. Trigger identity resolution (see §2)
6. Respond 200 OK

Same pattern for PayEngine with a separate workflow.

**Why webhooks first:** Near-real-time ingestion means charges appear in the database within seconds. No waiting for end-of-month exports.

### Skool — Scheduled Pull

Skool has no webhooks. An n8n scheduled workflow runs daily at 6:00 AM PST.

**n8n workflow: `ingest-skool-daily`**
1. Cron trigger (daily 6 AM PST)
2. Call Skool API for transactions since last successful pull (store cursor in a `pipeline_state` key-value table)
3. For each transaction: dedup check → insert into `raw_billing_events`
4. Trigger identity resolution
5. Update cursor timestamp

**Why daily, not hourly:** Skool's volume is lower. Daily is sufficient and avoids API rate limits.

### HubSpot — Webhook + Nightly Sync

HubSpot webhooks fire on contact and custom object (membership) changes. However, webhooks can be unreliable (missed events, out-of-order delivery), so we add a nightly full sync as a safety net.

**n8n workflow: `ingest-hubspot-webhook`**
- On membership create/update → upsert into `contributors` + snapshot into `memberships`
- On contact create/update (email change) → trigger identity resolution

**n8n workflow: `sync-hubspot-nightly`**
- Cron trigger (2 AM PST daily)
- Pull all memberships modified in last 48 hours (overlap window catches anything missed)
- Upsert into `contributors` + snapshot into `memberships` (dedup by hubspot_membership_id + snapshot_at)

---

## 2. Transformation Layer

### Identity Resolution

Runs as a sub-workflow triggered after every ingestion event.

**n8n workflow: `resolve-identity`**
1. Extract email from the ingested event
2. Query `contributor_identifiers` for that email
3. If found → link to existing `contributor_id`
4. If not found → check if any other identifier from the same source links to an existing contributor (e.g., ChargeHub customer_id). If yes → add the new email to that contributor. If no → create new contributor + identifier.
5. Periodically (weekly), run a batch merge job that looks for cross-system matches (same email in different source_systems → merge contributors)

**Why real-time + batch:** Real-time resolution handles the common case (known email). Batch merging catches cross-system duplicates that require comparing across sources.

### Charge & Membership Materialization

After raw events are ingested and identity is resolved, materialized views (or a lightweight dbt job) transform raw data into the `charges` and `memberships` tables.

**Key transformations:**
- Parse `processed_at` from JSONB → convert to PST → extract `billing_period`
- Map `line_item_title` → tier via `tier_definitions`
- Normalize amounts using `exchange_rates` + `tier_definitions`
- For memberships: insert new snapshot (never update existing)

**Trigger:** Runs after each ingestion batch. For webhooks, this is nearly immediate. For Skool, after the daily pull.

---

## 3. Reconciliation Engine

### When It Runs

**Daily at 8:00 AM PST** (after all ingestion is complete, including Skool's 6 AM pull).

Not just end-of-month. Daily reconciliation means a Natasha Bloom tier mismatch ($5,250/month at risk) is caught the day after her charge processes, not 30 days later.

**n8n workflow: `run-reconciliation-daily`**
1. Cron trigger (8 AM PST)
2. Determine current billing period (`YYYY-MM`)
3. Check `reconciliation_runs` — if a successful run exists for today's date, skip (idempotency)
4. For each billing source (ChargeHub, PayEngine, Skool):
   a. Pull charges for current period from `charges` table
   b. Pull current membership snapshots from `memberships` table
   c. Run matching logic (same algorithm as Part 1, using `contributor_id` instead of email index)
   d. Compute mismatch flags, revenue classification, severity
   e. Insert results into `reconciliation_results`
   f. Insert summary into `reconciliation_runs`
   g. Write audit log entries for new mismatches
5. Trigger alerting workflow

### Idempotency

If the pipeline runs twice for the same day:
- The `reconciliation_runs` unique constraint prevents duplicate completed runs
- Before starting, the workflow checks for an existing successful run and skips if found
- If the previous run failed, it's marked as `failed` and a new run proceeds
- Reconciliation results reference a `run_id`, so even if we re-run, results are scoped to their run and don't double-count

---

## 4. Alerting & Reporting

### Daily Digest (8:30 AM PST)

**n8n workflow: `alert-daily-digest`**
1. Triggered 30 minutes after reconciliation completes
2. Query `reconciliation_results` where `resolved_at IS NULL` and `severity IN ('critical', 'warning')`
3. Group by severity, calculate total `revenue_at_risk`
4. Format digest:
   - **Critical** (tier mismatches, missing CRM records, phantom revenue) — needs same-day action
   - **Warning** (billing date off, currency mismatch) — needs attention this week
   - **Summary** — total revenue at risk, trend vs. yesterday
5. Send via Slack channel + email to the responsible team member
6. Include deep links to HubSpot records for each issue

### Revenue Impact Tracking

Every mismatch gets a `revenue_at_risk` dollar amount. When resolved (`resolved_at` is set), the system calculates `days_to_resolve`. Monthly, an automated report shows:

- Total revenue protected (sum of resolved `revenue_at_risk`)
- Average resolution time (improving = automation is working)
- Revenue at risk trend (decreasing = fewer mismatches = systems are cleaner)

This is how we quantify the ROI of the entire infrastructure: "This month, daily detection saved $X by catching problems 29 days earlier than the old monthly process."

### Tier Change Guardrails

When reconciliation detects a tier mismatch:
- If charge tier > CRM tier (possible upsell not reflected): flag as **critical**, auto-create a HubSpot task assigned to the account manager, include both possibilities in the task description
- If charge tier < CRM tier (possible downgrade or billing error): flag as **critical**, send an immediate Slack alert to the billing team
- **No automated CRM updates for tier changes.** A human confirms before the membership record changes. This prevents a billing system bug from silently downgrading 50 members.

---

## 5. Failure Handling

| Failure | Handling |
|---------|----------|
| Webhook delivery fails | ChargeHub/PayEngine retry automatically. Our endpoint is idempotent (dedup check), so retries are safe. |
| Skool API pull fails | n8n retry policy: 3 attempts with exponential backoff. Cursor is only updated on success, so the next run re-pulls. |
| HubSpot nightly sync fails | 48-hour overlap window means tomorrow's sync catches today's missed records. |
| Reconciliation crashes mid-run | Run status stays `running`. Next trigger detects stale running state (>1 hour), marks as `failed`, starts fresh. |
| Identity resolution conflict | Two contributors with the same email → flagged for manual review in the audit log, not auto-merged. |

---

## Architecture Diagram (Simplified)

```
 BILLING SOURCES                    INGESTION                    STORAGE                     OUTPUTS
┌──────────────┐                                          ┌─────────────────┐
│  ChargeHub   │──webhook──┐                              │ raw_billing_    │
│  PayEngine   │──webhook──┼──► n8n ingest ──► dedup ──►  │ events (JSONB)  │
│              │           │    workflows      check       │                 │
│  Skool       │──cron─────┘                              │ memberships     │
│              │                                          └────────┬────────┘
│  HubSpot     │──webhook + nightly sync──────────────────►        │
└──────────────┘                                                   │
                                                          ┌────────▼────────┐
                              n8n: resolve-identity  ◄────┤  contributor_   │
                                                    ──►   │  identifiers    │
                                                          └────────┬────────┘
                                                                   │
                                                          ┌────────▼────────┐
                              dbt / materialized views ──►│  charges        │
                                                          │  memberships    │
                                                          │  exchange_rates │
                                                          └────────┬────────┘
                                                                   │
                              n8n: reconciliation-daily ◄──────────┘
                                        │
                                        ▼
                              ┌────────────────────┐        ┌──────────────┐
                              │ reconciliation_    │───────► │ Metabase     │
                              │ results + runs     │        │ dashboards   │
                              │ audit_log          │        └──────────────┘
                              └────────┬───────────┘
                                       │
                                       ▼
                              n8n: alert-daily-digest
                                       │
                              ┌────────┴────────┐
                              │  Slack / Email   │
                              │  HubSpot tasks   │
                              └─────────────────┘
```

---

## Included n8n Workflows — What's Built, What's Designed, What's Next

Two importable n8n workflow JSONs ship with this repo — **36 nodes total** across **6 independent paths**. All triggers are disabled so the reviewer can import safely, inspect every node, and enable selectively.

### Workflow 1 — `n8n_pipeline.json` (Reconciliation Pipeline, 16 nodes)

**Maps to:** §3 Reconciliation Engine + §4 Alerting

This is the core detection loop. It processes **one charge at a time** via webhook, using the exact same classification logic as `reconcile.py` (tier matching, ±1-day date proximity, non-USD normalization via tier lookup).

**Flow:** Webhook receives charge → idempotency check (by `charge_id`) → registers run → searches HubSpot by email (with domain fallback) → classifies revenue status → persists to `reconciliation_results` + `audit_log` → Slack alert → HubSpot task for critical issues.

**What's built:** The full detect → classify → persist → alert loop, including idempotency, fallback search, and severity-based routing (Slack for all, HubSpot task for critical only).

**What scaling looks like:** The architecture (§3) describes a daily batch version that loops over all charges for a billing period. The classification logic and DB schema are identical — the batch version wraps this single-record flow in a "for each charge" loop and adds multi-source iteration.

### Workflow 2 — `data_ingestion_workflow.json` (Data Ingestion, 20 nodes)

**Maps to:** §1 Ingestion Layer + §2 Transformation Layer

This workflow populates the data platform tables that feed the reconciliation engine: `contributors`, `contributor_identifiers`, `memberships`, `raw_billing_events`, `charges`, and `exchange_rates`.

**Five independent paths (all triggers disabled):**

| # | Path | Trigger | Nodes | Tables Populated | Architecture Section |
|---|------|---------|-------|-----------------|---------------------|
| 1 | **HubSpot Sync** | Schedule (daily 2 AM) | 4 | `contributors`, `contributor_identifiers`, `memberships` | §1 HubSpot nightly sync + §2 Identity Resolution |
| 2 | **ChargeHub Ingestion** | Webhook | 3 | `raw_billing_events`, `charges` | §1 ChargeHub webhook + §2 Charge Materialization |
| 3 | **PayEngine Ingestion** | Webhook | 3 | `raw_billing_events`, `charges` | §1 PayEngine webhook (same pattern, different field mapping) |
| 4 | **Skool Daily Pull** | Schedule (daily 6 AM) | 6 | `raw_billing_events`, `charges`, `pipeline_state` | §1 Skool scheduled pull (cursor-based, no webhooks) |
| 5 | **FX Rates** | Schedule (daily 6 AM) | 4 | `exchange_rates` | §2 Amount Normalization |

**Design highlights by path:**
- **Paths 2 & 3** are intentionally near-identical — the only difference is the field mapping in the transform node (`charge_id` vs `transaction_id`, `email` vs `customer_email`). This demonstrates how adding a new billing source is a copy-paste-adapt operation, not a redesign.
- **Path 4 (Skool)** uses a cursor pattern: read last pull timestamp from `pipeline_state` → fetch only new transactions → persist → advance cursor. The cursor only advances after a successful persist, making it safe to retry on failure.
- **Path 1 (HubSpot)** uses an immutable snapshot model — every sync inserts a new membership row, never updates existing ones. This preserves history for audit and trending.

### Coverage Map — Architecture vs. Implemented Workflows

| Architecture Concept | Implemented? | Where |
|---------------------|--------------|-------|
| ChargeHub webhook ingestion (§1) | ✅ | Ingestion Path 2 |
| PayEngine webhook ingestion (§1) | ✅ | Ingestion Path 3 |
| Skool scheduled pull (§1) | ✅ | Ingestion Path 4 |
| HubSpot nightly sync (§1) | ✅ | Ingestion Path 1 |
| Identity resolution (§2) | ✅ Inline | UUID5(email) in each transform node |
| Charge materialization (§2) | ✅ | Ingestion Paths 2, 3, 4 |
| FX rate ingestion (§2) | ✅ | Ingestion Path 5 |
| Reconciliation engine (§3) | ✅ Single-record | Reconciliation Pipeline |
| Idempotency checks (§3) | ✅ | Reconciliation Pipeline + all ingestion dedup |
| Slack alerting (§4) | ✅ | Reconciliation Pipeline |
| HubSpot task creation (§4) | ✅ | Reconciliation Pipeline (critical only) |
| Daily digest workflow (§4) | 🔲 Future | Described in architecture, not yet built |
| HubSpot real-time webhook (§1) | 🔲 Future | Nightly batch covers it; webhook adds speed |
| Batch reconciliation loop (§3) | 🔲 Future | Single-record PoC proves the logic; batch wraps it |
| Metabase dashboards (§4) | 🔲 Future | Schema is dashboard-ready (`audit_log`, `reconciliation_results`) |

Together, the two workflows cover **every table in `schema.sql`** — the reconciliation pipeline handles runs, results, and audit; the ingestion workflow handles identity, raw events, materialized charges, memberships, and reference data.

---

## Design Notes

**Why n8n over a pure code pipeline:** n8n gives the ops team visibility into pipeline status without needing engineering support. Failed nodes show up in the UI. Non-technical team members can monitor execution and see where things break. This matters for a team that's operating across 500 members and 3 billing platforms — you want the operations person, not just the engineer, to know when something is wrong.

**Why daily reconciliation, not real-time:** Real-time reconciliation on every webhook would require solving the problem of "the charge arrived but HubSpot hasn't updated yet." A daily batch at a fixed time ensures both sides have settled. The 24-hour detection window is a 29x improvement over the current monthly process.

**Why not auto-fix CRM records:** The temptation is to have the pipeline automatically update HubSpot when it finds a mismatch. But mismatches have two possible explanations (the billing system is right, or the CRM is right), and the business impact of guessing wrong on an Executive member ($72K/year) is too high. Human-in-the-loop for corrections, automated for detection and alerting.

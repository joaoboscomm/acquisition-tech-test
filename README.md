# Nexus Collective — Data Reconciliation & Infrastructure Design

**Technical Validation — AI & Automation Specialist**
João Bosco Mesquita · April 2026

---

## Overview

Nexus Collective is a paid membership community (~500 members) billing through three platforms: **ChargeHub**, **PayEngine**, and **Skool**. HubSpot is the single source of truth for membership data.

This repo contains my solution to a two-part technical assessment:

- **Part 1** — Reconcile ChargeHub charges against HubSpot, surface mismatches, and produce actionable gap fixes
- **Part 2** — Design the database schema, automated pipeline, and alerting infrastructure to make this process run daily across all billing sources

### Key Findings (8 test charges)

| Metric | Value |
|--------|-------|
| Total charges processed | 8 |
| Clean matches | 4 |
| Revenue gap | **$5,250 (42.9%)** |
| Largest single issue | Tier mismatch — $6,000 Executive charged, HubSpot shows $750 Starter |
| Untracked revenue | 1 charge with no HubSpot contact |
| Phantom revenue | 1 HubSpot member with no charge |

---

## Repo Structure

```
├── README.md
│
├── test/                        ← Original test files from the hiring manager
│   ├── test_CLAUDE.md               Full test brief (Part 1 + Part 2 spec)
│   ├── charges_processed_test.csv   ChargeHub charges export (8 rows)
│   ├── ContactDealMembership_test.csv    HubSpot CDM export
│   └── Exclude_test.csv                 Email exclusion list
│
├── extra_files/                 ← Supplementary deliverables
│   ├── data_ingestion_workflow.json  n8n ingestion workflow — 5 paths (HubSpot, ChargeHub, PayEngine, Skool, FX)
│   ├── n8n_workflow_guide.md        Node-by-node explainer for both n8n workflows
│   └── case_by_case_analysis.md     Detailed walkthrough of each charge
│
└── output/                      ← All deliverables
    ├── reconcile.py                 Python reconciliation script
    ├── chargehub_reconciliation_test.csv  Full reconciliation output (9 rows)
    ├── chargehub_gap_fixes_test.csv      Actionable fixes (4 rows)
    ├── console_summary.txt              Summary totals and mismatch counts
    ├── schema.sql                   Postgres schema (contributor model, event store, reconciliation tables)
    ├── pipeline_architecture.md     n8n pipeline design (ingestion → reconciliation → alerting)
    ├── nexus-macro-architecture.svg  Visual architecture diagram (open in browser)
    ├── n8n_pipeline.json            Importable n8n workflow (16 nodes)
    └── summary.md                   Non-technical stakeholder memo
```

---

## Running the Reconciliation Script

### Prerequisites

- Python 3.9+
- pandas, numpy

```bash
pip install pandas numpy
```

### Run

From the repo root:

```bash
python3 output/reconcile.py
```

Or explicitly specifying input/output directories:

```bash
python3 output/reconcile.py test output
```

### Output

The script produces three files in `output/`:

| File | Description |
|------|-------------|
| `chargehub_reconciliation_test.csv` | One row per charge + unmatched HubSpot records, with 8 mismatch flags |
| `chargehub_gap_fixes_test.csv` | Only mismatched rows, with HubSpot links and fix instructions |
| `console_summary.txt` | Totals, gap percentage, and mismatch counts |

---

## Part 1 — What the Script Handles

| Edge Case | How It's Handled |
|-----------|-----------------|
| **Timezone (UTC → PST)** | May 1 06:45 UTC = Apr 30 22:45 PST — correctly included in April |
| **Multi-email contacts** | Builds email → Contact ID → all emails index from the denormalized CDM |
| **Non-USD currency** | Normalizes both sides to the tier's fixed USD amount to avoid FX false positives |
| **Dual memberships** | Prefers ChargeHub-sourced records, falls back to all records |
| **No HubSpot match** | Flagged as untracked revenue with all mismatch flags set to TRUE |
| **Phantom revenue** | HubSpot members with ChargeHub billing source but no charge this month |
| **Exclusion list** | Emails in the exclude CSV are filtered out before reconciliation |

See [extra_files/case_by_case_analysis.md](extra_files/case_by_case_analysis.md) for a detailed walkthrough of each of the 9 reconciliation rows.

---

## Part 2 — Infrastructure Design

### 2A — Data Model ([output/schema.sql](output/schema.sql))

- **Contributor model** — universal identity across all systems (like merged contacts on a phone)
- **Immutable event store** — raw billing events preserved as JSONB, never modified
- **Reconciliation tracking** — results tied to run IDs for month-over-month trending
- **Revenue classification** — confirmed, disputed, phantom, untracked

### 2B — Pipeline Architecture ([output/pipeline_architecture.md](output/pipeline_architecture.md))

- **Daily reconciliation** at 8 AM PST (29x improvement over monthly)
- **Webhook ingestion** for ChargeHub and PayEngine, scheduled pull for Skool
- **Idempotent** — safe to re-run, dedup at every layer
- **Slack alerting** with severity ranking and revenue-at-risk quantification
- **Human-in-the-loop** for corrections — automated detection, manual approval

Two n8n workflows are included: the [reconciliation pipeline](output/n8n_pipeline.json) (16 nodes) and the [data ingestion workflow](extra_files/data_ingestion_workflow.json) (20 nodes, 5 independent paths) — 36 nodes total implementing the full architecture across all four billing sources.

### 2C — Stakeholder Memo ([output/summary.md](output/summary.md))

Non-technical summary covering: what the data showed, what the infrastructure changes, and one question to answer before committing to the design.

---

## License

This repository was created as part of a technical assessment and is not licensed for redistribution.

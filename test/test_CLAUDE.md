# Nexus Collective — Data Reconciliation & Infrastructure Test

## Context

Nexus Collective is a paid membership community (~500 members). Members pay through three billing platforms: **ChargeHub**, **PayEngine**, and **Skool**. HubSpot is the single source of truth for membership data. A Metabase dashboard pulls from HubSpot for revenue reporting.

This test has two parts. Part 1 is a reconciliation task. Part 2 is a design task that builds directly on what you find in Part 1.

---

# PART 1 — ChargeHub Reconciliation

Your task covers **ChargeHub only**.

## You Have Been Given

1. A ChargeHub processed charges export (`charges_processed_test.csv`)
2. A HubSpot Contact-Deal-Membership export (`ContactDealMembership_test.csv`)
3. An exclude list (`Exclude_test.csv`)

Write a Python script (or use Claude Code) to reconcile the two datasets and produce:
1. A **reconciliation CSV** — one row per charge or unmatched HubSpot record, with mismatch flags
2. A **gap fix CSV** — actionable rows for any records that need to be corrected in HubSpot
3. A **console summary** — printed totals and mismatch counts

---

## Data Model: HubSpot CDM Export

The CDM (Contact-Deal-Membership) export is **denormalized**: one row per **email × membership record**.

A contact with 3 associated emails and 2 membership records will appear as **6 rows** — all sharing the same Contact ID and Membership IDs, but with different Email values.

This means:
- You cannot sum MRR from the CDM directly without deduplicating first
- You must build a contact-level index: `email → Contact ID → all associated emails → all membership records`
- Matching a charge to HubSpot requires checking **all emails associated with a contact**, not just the one on the charge

### Key CDM Columns

| Column | Description |
|--------|-------------|
| `Contact ID` | HubSpot contact identifier |
| `Email` | One associated email (may be primary, secondary, or further) |
| `First Name` / `Last Name` | Contact name |
| `Current Membership Tier` | Contact-level current tier |
| `Current Membership Status` | Contact-level current status |
| `Membership ID` | HubSpot membership record ID |
| `Membership Currency` | Currency of the membership record |
| `Membership Type` | e.g. "Paying Member" |
| `Membership Billing Date` | Date of membership billing (YYYY-MM-DD) |
| `Membership Create Date` | ISO timestamp when the record was created |
| `Membership MRR` | MRR stored in HubSpot (may be non-USD) |
| `Membership Billing Source` | e.g. "ChargeHub", "PayEngine", "Skool" |
| `Membership Status` | e.g. "Active", "Cancellation", "Payment Failed" |
| `Membership Tier` | e.g. "Starter", "Pro", "Executive" |

---

## Data Model: ChargeHub Charges Export

80-column CSV. Key columns:

| Column Name | Index | Description |
|-------------|-------|-------------|
| `processed_at` | 13 | Charge timestamp — **UTC** |
| `email` | 34 | Customer email |
| `line_item_title` | 55 | Product name — use for tier mapping |
| `usd_total_price` | 76 | Charge amount converted to USD |
| `presentment_currency` | 77 | Original charge currency |

---

## Timezone Standard: PST

**All date filtering must use PST (America/Los_Angeles).**

The `processed_at` field in ChargeHub is stored in UTC. Convert to PST before filtering to the current month. Without this, charges processed between midnight and 8am UTC on the 1st of a month will appear to belong to the prior month in PST — causing valid charges to be excluded from the reconciliation.

---

## Filtering Rules

### ChargeHub Charges
1. `processed_at` (converted to PST) >= first day of current month
2. `line_item_title` must be one of the valid products below
3. `email` must not be blank
4. Remove emails found in `Exclude_test.csv` (email is in the `Email` column)

### HubSpot CDM
- No filters applied. Include all records — we want to surface mismatches.

---

## Valid Products & Tier Map

| `line_item_title` | Tier | USD Amount |
|-------------------|------|------------|
| Nexus Collective - Starter - $750 / Month | Starter | 750 |
| Nexus Collective - Pro - $2,500 / Month | Pro | 2,500 |
| Nexus Collective - Executive - $6,000 / Month | Executive | 6,000 |
| Nexus Collective - Executive Annual - $24,000 / Year | Executive Annual | 24,000 |

---

## Normalization Rules

### Why normalization matters
ChargeHub supports multi-currency billing. A member may be charged in BRL, AUD, or other currencies. The `usd_total_price` column reflects a live exchange-rate conversion — it will rarely equal the tier's fixed USD amount exactly.

HubSpot membership MRR may also be stored in the original presentment currency.

For comparison purposes, **normalize both sides to the tier's fixed USD amount**:

- **ChargeHub side:** Create `chargehub_total_price_normalized`. For USD charges, use `usd_total_price`. For non-USD charges, use the tier's USD amount from the map above.
- **HubSpot side:** Create `membership_mrr_normalized`. For USD memberships, use `Membership MRR`. For non-USD memberships, use the tier USD amount based on `Membership Tier`.

**Important:** Only normalize when MRR > 0. Records with $0 MRR must stay at $0 regardless of tier.

---

## Matching Logic

### Direction: Full Outer Join
1. **ChargeHub → HubSpot:** For each charge, find the best HubSpot membership record
2. **HubSpot → ChargeHub:** For each HubSpot ChargeHub-source contact, check if they appear in the charge export

### ChargeHub → HubSpot (charge-by-charge)
1. Build a contact-level email index from CDM: `email → Contact ID → all emails for that contact → all their membership records`
2. Keep **all** filtered charges — no deduplication. Each charge gets its own output row.
3. For each charge, resolve `email → Contact ID → all membership records for that contact`
4. **Billing-source-preferred selection:** Filter candidate membership records to those where `Membership Billing Source = ChargeHub` first. If none found, fall back to all records.
5. **Date-proximity matching:** Within preferred records, find where `Membership Billing Date` is within ±1 day of `processed_at` (PST). If multiple qualify, prefer matching tier, then most recent `Create Date`.
6. **Fallback:** If no date-proximity match, use the contact's most recent membership (Create Date desc, Billing Date desc).
7. **Claim tracking:** Once a membership record is matched to a charge, mark it as claimed. Subsequent charges for the same contact must use different membership records.
8. If no contact found at all, HubSpot columns are blank in the output row.

### HubSpot → ChargeHub (reverse direction)
1. For each contact with `Membership Billing Source = ChargeHub`, check if any associated email appears in the charge export
2. Deduplicate by Contact ID × Membership ID (CDM has multiple rows per email)
3. If no match found, include as an unmatched HubSpot row (ChargeHub columns blank)

---

## Mismatch Flags

For each output row, compute:

| Flag | Logic |
|------|-------|
| `tier_mismatch` | `Membership Tier` ≠ tier derived from `line_item_title` |
| `billing_date_mismatch` | `Membership Billing Date` ≠ `processed_at` date (PST) |
| `mrr_mismatch` | `Membership MRR` ≠ `usd_total_price` (raw comparison) |
| `mrr_normalized_mismatch` | `membership_mrr_normalized` ≠ `chargehub_total_price_normalized` |
| `currency_mismatch` | `Membership Currency` ≠ `presentment_currency` |
| `billing_source_mismatch` | `Membership Billing Source` ≠ "ChargeHub" |
| `status_mismatch` | `Membership Status` ≠ "Active" |
| `type_mismatch` | `Membership Type` ≠ "Paying Member" |

---

## Output 1: Reconciliation CSV (`chargehub_reconciliation_test.csv`)

| Column | Description |
|--------|-------------|
| `contact_id` | HubSpot Contact ID |
| `membership_id` | HubSpot Membership ID |
| `matching_email` | Email used for the match |
| `membership_tier` | From HubSpot |
| `chargehub_line_item` | Tier derived from ChargeHub |
| `membership_billing_date` | From HubSpot (date only) |
| `chargehub_processed_date` | From ChargeHub (date only, PST) |
| `membership_mrr` | From HubSpot |
| `chargehub_total_price` | `usd_total_price` from ChargeHub |
| `membership_mrr_normalized` | Calculated |
| `chargehub_total_price_normalized` | Calculated |
| `membership_currency` | From HubSpot |
| `chargehub_currency` | `presentment_currency` from ChargeHub |
| `membership_billing_source` | From HubSpot |
| `membership_status` | From HubSpot |
| `membership_type` | From HubSpot |
| All 8 mismatch flags | TRUE/FALSE |

Row count = ChargeHub filtered rows + HubSpot ChargeHub-source rows with no ChargeHub match

---

## Output 2: Gap Fix CSV (`chargehub_gap_fixes_test.csv`)

Rows where any mismatch flag is TRUE, or where there is no HubSpot match. One row per issue.

| Column | Description |
|--------|-------------|
| `contact_id` | HubSpot Contact ID |
| `membership_id` | HubSpot Membership ID |
| `email` | Matching email |
| `hubspot_contact_link` | `https://app.hubspot.com/contacts/99887766/record/0-1/{contact_id}` |
| `hubspot_membership_link` | `https://app.hubspot.com/contacts/99887766/record/2-11223344/{membership_id}` |
| `current_tier` | Current HubSpot tier |
| `current_mrr` | Current HubSpot MRR |
| `current_status` | Current HubSpot status |
| `fix_instructions` | Plain-English description of what to fix |

---

## Output 3: Console Summary

```
=== CHARGEHUB RECONCILIATION — {MONTH YEAR} ===

ChargeHub:
  Total charges: {n}
  Total amount (USD): ${x}
  Total amount (normalized): ${y}

HubSpot Matched:
  Contacts matched: {n}
  Total MRR: ${x}
  Total MRR (normalized): ${y}

Gap:
  Amount: ${x}
  Percentage: {x}%

Mismatches:
  Tier: {n}
  Billing Date: {n}
  MRR: {n}
  MRR Normalized: {n}
  Currency: {n}
  Billing Source: {n}
  Status: {n}
  Type: {n}

Not Found:
  ChargeHub charges without HubSpot match: {n}
  HubSpot ChargeHub members without charge: {n}
```

---

# PART 2 — Data Infrastructure Design

Part 1 gave you a snapshot of the problem: manual CSV exports, Python script, monthly reconciliation. This part asks you to design the infrastructure that makes that process automated, reliable, and scalable across all three billing platforms.

There is no single right answer. We're evaluating how you think about data architecture and operational tradeoffs, not whether you match a specific design.

---

## 2A — Data Model

Design the relational schema you would build to power this reconciliation automatically — replacing the CSV-based approach.

You have three billing sources (ChargeHub, PayEngine, Skool) and one CRM (HubSpot). Each billing source fires webhooks or can be polled via API. HubSpot is the source of truth.

Your schema should answer:

1. How do you store raw events from each billing source without losing history?
2. How do you model contacts and their emails in a way that supports multi-email matching?
3. How do you store membership records given that they are **immutable billing event stamps** — you never update a record, you create a new one?
4. How do you track reconciliation results over time so you can see accuracy trending month over month?

Deliver this as:
- A set of `CREATE TABLE` statements (Postgres syntax), or
- A written schema description with table names, columns, primary keys, and relationships clearly stated

Include a brief note on any design decisions you made and why — especially anywhere you had to make a tradeoff.

---

## 2B — Pipeline Architecture

Sketch the pipeline that runs this reconciliation automatically each month and surfaces gaps without manual intervention.

You can assume:
- Webhooks are available from ChargeHub and PayEngine for new charges and cancellations
- Skool does not have webhooks — it requires a scheduled export pull via API
- HubSpot webhooks are available for contact and membership record changes
- n8n is available as the orchestration layer
- The database from 2A is available as your storage layer

Your pipeline design should address:

1. **Ingestion** — how does raw event data from each source get into your database? What triggers it (webhook, schedule, both)?
2. **Transformation** — where does normalization, deduplication, and email index building happen? In the pipeline, in the database (views/DBT), or both?
3. **Reconciliation trigger** — when and how does reconciliation run? End of month only, or continuously?
4. **Alerting** — how does a gap or mismatch surface to the right person without requiring a manual report pull?
5. **Idempotency** — if the pipeline runs twice for the same period (e.g., a retry after failure), how do you prevent double-counting or duplicate alerts?

Deliver this as:
- A written architecture description (clear enough that an engineer could implement it), or
- A diagram with annotations (a rough sketch exported as image or ASCII is fine), or
- Both

You do not need to build this — design it.

---

## 2C — Written Summary

A short memo (one page max) covering:

1. What you found in Part 1 — the gap, what's causing it, what you'd fix first
2. What the infrastructure in Part 2 would change — specifically, which problems from Part 1 it prevents vs. which ones it doesn't
3. One thing you'd want to know about the current system before committing to the design in 2A/2B

This memo should be readable by a non-technical stakeholder (the head of the business, not the engineering team).

---

## What We're Looking For

**Part 1:** Correct output, edge case handling, clean gap fix CSV, accurate summary.

**Part 2A:** Do you understand immutable event sourcing? Do you model multi-email contacts correctly? Do you separate raw ingestion from transformed/reconciled data? Do your design decisions reflect real tradeoffs or just textbook answers?

**Part 2B:** Is the pipeline operationally realistic? Does it handle failure gracefully? Do you think about idempotency without being prompted? Is the alerting specific enough to be actionable?

**2C:** Can you communicate findings and architecture decisions to someone who doesn't know what a webhook is?

---

## File Structure

```
test/
├── test_CLAUDE.md
├── charges_processed_test.csv
├── ContactDealMembership_test.csv
└── Exclude_test.csv
```

Deliverables:
```
output/
├── reconcile.py
├── chargehub_reconciliation_test.csv
├── chargehub_gap_fixes_test.csv
├── console_summary.txt
├── schema.sql (or schema.md)
├── pipeline_architecture.md (or diagram)
└── summary.md
```

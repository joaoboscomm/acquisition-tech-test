# Case-by-Case Analysis — ChargeHub Reconciliation April 2026

## Overview

8 charges processed, 8 unique HubSpot contacts (after dedup), 9 reconciliation rows. This document walks through each case: what the data shows, what the reconciliation found, and what we'd improve to prevent or catch it faster.

---

## CASE 1 — Tyler Brooks

**Status: ✅ Clean Match**

| Field | ChargeHub | HubSpot |
|-------|-----------|---------|
| Email | tyler@brooksconsulting.io | tyler@brooksconsulting.io |
| Tier | Starter ($750/mo) | Starter |
| Amount | $750.00 USD | $750.00 USD |
| Date | 2026-04-06 | 2026-04-06 |
| Billing Source | — | ChargeHub |

**What happened:** Perfect match. Email matches directly, tier aligns, MRR aligns, dates align, currency is USD on both sides.

**Mismatch flags:** None.

**Revenue classification:** Confirmed.

**Improvement ideas:** Tyler is the baseline — this is what every record should look like. The goal of the infrastructure is to make every member look like Tyler. When they don't, we catch it on day 1.

---

## CASE 2 — Camila Ferreira

**Status: ⚠️ Currency & MRR Mismatch**

| Field | ChargeHub | HubSpot |
|-------|-----------|---------|
| Email | camila@ferreiraco.com | camila@ferreiraco.com |
| Tier | Starter ($750/mo) | Starter |
| Charged Amount | 3,750.66 BRL | — |
| USD Received | $710.00 | — |
| MRR Recorded | — | 3,900.00 BRL |
| Currency | BRL | BRL |
| Normalized USD | $750 (tier amount) | $750 (tier amount) |

**What happened:** Camila is billed in BRL. ChargeHub charged 3,750.66 BRL, which converted to $710 USD at the live exchange rate. HubSpot recorded her MRR as 3,900 BRL — a different BRL amount than what was actually charged. After normalizing both to the tier's fixed USD amount ($750), the normalized comparison passes. But the raw numbers don't match on either side.

**Mismatch flags:** `mrr_mismatch` = TRUE (3900 ≠ 710), `currency_mismatch` = FALSE (both BRL).

**Revenue classification:** Disputed.

**What's actually wrong:**
1. The BRL amount charged (3,750.66) doesn't match the BRL amount in HubSpot (3,900). Either HubSpot was set up with an old exchange rate or a rounded estimate.
2. The USD actually received ($710) is $40 less than the tier amount ($750). That's a 5.3% margin erosion from FX conversion on a single charge.

**Business impact:** If 10% of 500 members pay in foreign currency, that's ~50 subscriptions leaking margin every month. At $40/member average, that's $2,000/month or $24,000/year in silent revenue erosion.

**Improvement ideas:**
- **Short term:** Add the exchange rate table (`exchange_rates`) to track daily rates. Recompute expected BRL amounts using the rate on the charge date so HubSpot always reflects reality.
- **Long term:** Standardize all billing to USD. The member's bank handles their local conversion at checkout. The business always receives the exact $750. FX risk transfers to the financial institution. This eliminates the entire category of currency mismatches.
- **Audit log:** Flag every non-USD charge with the FX delta (difference between tier USD and actual USD received). Aggregate monthly to quantify total FX erosion and build the business case for USD standardization.

---

## CASE 3 — Ryan Patel

**Status: ✅ Clean Match (Multi-Email Resolution Required)**

| Field | ChargeHub | HubSpot |
|-------|-----------|---------|
| Charge Email | r.patel.contracts@protonmail.com | — |
| HubSpot Emails | — | ryan@patelgroupinc.com, rpatel@legacyventures.net, ryan.patel@outlook.com, r.patel.contracts@protonmail.com |
| Contact ID | — | 2203 |
| Tier | Pro ($2,500/mo) | Pro |
| Amount | $2,500.00 USD | $2,500.00 USD |
| Date | 2026-04-09 | 2026-04-09 |

**What happened:** Ryan's ChargeHub charge uses his protonmail address. In HubSpot, Contact 2203 has 4 associated emails — the CDM export has 4 rows, all pointing to the same Contact ID and Membership ID. The reconciliation script built an email → Contact ID index, resolved `r.patel.contracts@protonmail.com` → Contact 2203 → Membership 70002203, and matched correctly.

**Mismatch flags:** None.

**Revenue classification:** Confirmed.

**What could go wrong without proper handling:** If you match on email alone (no contact-level index), Ryan would appear as "no HubSpot match" because `r.patel.contracts@protonmail.com` wouldn't find a membership unless you traverse the contact's other emails. He'd show up as untracked revenue — a false alarm that wastes ops time and inflates the gap number.

**Improvement ideas:**
- **Contributor model (universal ID):** Ryan gets one `contributor_id` with all 4 emails registered in `contributor_identifiers`. Any future charge from any of his emails resolves instantly. No more email-level guessing.
- **AI chat enablement:** With the universal ID, you can ask "show me Ryan Patel's full history" and get charges from ChargeHub + membership from HubSpot + any Skool activity, all unified. Without it, you'd need to search 4 different emails manually.
- **Marketing deduplication:** Without identity resolution, Ryan could receive the same campaign 4 times. The contributor model ensures he's one person in every system.

---

## CASE 4 — Marcus Webb

**Status: ✅ Clean Match (Dual Membership, Source-Preferred Selection)**

| Field | ChargeHub | HubSpot (ChargeHub record) | HubSpot (Skool record) |
|-------|-----------|---------------------------|------------------------|
| Email | marcus@webbgroupllc.com | marcus@webbgroupllc.com | marcus@webbgroupllc.com |
| Membership ID | — | 70002204 | 70002244 |
| Tier | Starter | Starter | Starter |
| MRR | $750.00 | $750.00 | $750.00 |
| Billing Source | — | ChargeHub | Skool |
| Billing Date | 2026-04-11 | 2026-04-11 | 2026-03-05 |
| Create Date | — | 2026-01-22 | 2026-03-05 |

**What happened:** Marcus has two membership records in HubSpot — one sourced from ChargeHub, one from Skool. The reconciliation logic correctly preferred the ChargeHub-sourced record (70002204) for matching, as specified by the billing-source-preferred selection rule. The billing date matches the charge date.

**Mismatch flags:** None.

**Revenue classification:** Confirmed.

**What this reveals about the business:** Marcus appears to have been onboarded through Skool and later migrated to (or also billed through) ChargeHub. Or he was always on ChargeHub and someone manually created a Skool record. Either way, he has two active membership records for the same tier — this is a data hygiene issue even though reconciliation passes.

**Improvement ideas:**
- **Source priority hierarchy:** Don't hardcode "prefer ChargeHub." Build a configurable priority: ChargeHub > PayEngine > Skool (or whatever the business decides). Store it in a config table so it can change without code changes.
- **Duplicate membership detection:** Add a reconciliation check that flags contacts with multiple active memberships across different billing sources. Marcus might be getting billed twice — once through ChargeHub ($750) and once through Skool ($750). That's a $750/month overcharge if both are active.
- **Contributor model benefit:** With universal ID, you can run a query: "show me all contributors with active memberships in more than one billing source." That's a dashboard widget, not a manual investigation.

---

## CASE 5 — Priya Nair

**Status: ✅ Clean Match (Timezone Edge Case)**

| Field | ChargeHub (UTC) | ChargeHub (PST) | HubSpot |
|-------|----------------|-----------------|---------|
| Email | priya@nairventures.co | — | priya@nairventures.co |
| Processed At | 2026-05-01 06:45:00 UTC | 2026-04-30 22:45:00 PST | — |
| Billing Date | — | — | 2026-04-30 |
| Tier | Starter | — | Starter |
| Amount | $750.00 USD | — | $750.00 USD |

**What happened:** Priya's charge was processed at 6:45 AM UTC on May 1st. In PST (the reconciliation standard), that's 10:45 PM on April 30th — still within the April billing period. Without PST conversion, this charge would be filtered out as a "May charge" and Priya would appear as a phantom member (active in HubSpot, no charge in April).

**Mismatch flags:** None.

**Revenue classification:** Confirmed.

**What could go wrong without proper handling:** If you filter on UTC dates, Priya disappears from the April reconciliation. Her $750 shows up as phantom revenue — HubSpot says she's active, but "no charge was processed." The ops team investigates, finds nothing wrong, wastes time. Meanwhile the gap report shows $750 in phantom revenue that doesn't actually exist.

**Improvement ideas:**
- **Standardize timezone handling at ingestion:** When charges land in the database, always store both UTC and PST timestamps. The `charges` table in the schema has both `processed_at` (UTC) and `processed_at_pst`. Reconciliation always filters on PST. No ambiguity.
- **Unit test:** This is an edge case worth a dedicated test: "charge processed between midnight and 8 AM UTC on the 1st of a month should belong to the previous month in PST." If anyone modifies the reconciliation logic in the future, this test catches regressions.

---

## CASE 6 — Daniel Marsh (Zephyr Corp)

**Status: 🔴 No HubSpot Match — Untracked Revenue**

| Field | ChargeHub | HubSpot |
|-------|-----------|---------|
| Email | billing@zephyrcorp.net | ❌ Not found |
| Tier | Starter ($750/mo) | — |
| Amount | $750.00 USD | — |
| Date | 2026-04-13 | — |
| Contact ID | — | None |

**What happened:** Daniel was charged $750 through ChargeHub, but `billing@zephyrcorp.net` doesn't exist anywhere in the HubSpot CDM. No contact, no membership, no record at all. The business is collecting revenue from a member who doesn't exist in the CRM.

**Mismatch flags:** All TRUE (no HubSpot data to compare against).

**Revenue classification:** Untracked.

**Why this is a problem:**
1. **No account management visibility.** Nobody at Nexus is tracking Daniel's membership. He's not getting onboarding emails, not being invited to events, not getting renewal outreach.
2. **Churn risk.** If Daniel doesn't feel the value, he'll cancel — and nobody will know until the revenue disappears. No proactive retention possible.
3. **Revenue reporting gap.** This $750/month isn't in the HubSpot MRR number. The business is underreporting actual revenue.
4. **Compliance/audit risk.** Collecting money from someone not in your CRM creates a paper trail gap.

**Gap fix instruction:** Create HubSpot contact for `billing@zephyrcorp.net` (Daniel Marsh, Zephyr Corp). Create membership record: Starter, $750 USD, ChargeHub, Active. Billing date: April 13.

**Improvement ideas:**
- **Webhook-triggered contact creation:** When ChargeHub fires a `charge.processed` webhook and the email doesn't resolve to any contributor, automatically create a draft HubSpot contact (flagged for review). Don't wait for monthly reconciliation to discover ghosts.
- **Onboarding verification step:** Add a pipeline check: "within 48 hours of first charge, does a HubSpot contact exist?" If not → Slack alert + HubSpot task. This catches Daniels on day 2 instead of day 30.
- **Audit log entry:** Category = `missing_record`, severity = `critical`. Revenue impact = -$750/month. This feeds the Metabase trend: "how many untracked charges are we finding each month? Is it getting better or worse?"

---

## CASE 7 — Natasha Bloom

**Status: 🔴 Tier Mismatch — Highest Revenue Impact**

| Field | ChargeHub | HubSpot |
|-------|-----------|---------|
| Email | natasha@bloomstrategies.com | natasha@bloomstrategies.com |
| Tier | Executive ($6,000/mo) | Starter ($750/mo) |
| Amount | $6,000.00 USD | $750.00 USD |
| Normalized USD | $6,000 | $750 |
| Date | 2026-04-08 | 2026-04-08 |
| Billing Source | — | ChargeHub |

**What happened:** Natasha is being charged $6,000/month at the Executive tier through ChargeHub, but HubSpot has her recorded as a Starter member at $750/month MRR. This is the single largest discrepancy in the dataset — $5,250/month gap from one record.

**Mismatch flags:** `tier_mismatch` = TRUE, `mrr_mismatch` = TRUE, `mrr_normalized_mismatch` = TRUE.

**Revenue classification:** Disputed.

**Two scenarios — both are critical:**

**Scenario A: Executive is correct (she upgraded, HubSpot wasn't updated)**
- Natasha is paying $6,000/month but HubSpot says Starter. She may only be receiving Starter-level service, access, or support.
- She's paying 8x what she's getting. The moment she notices, you lose a $72,000/year member.
- This is a **retention bomb** on your highest-value tier.
- **Fix:** Update HubSpot to Executive tier, $6,000 MRR. Verify she has Executive-level access. Reach out proactively to confirm her experience.

**Scenario B: Starter is correct (she was overcharged, billing error)**
- The business is collecting $5,250/month more than it should.
- That's $63,000/year in overcharges. Depending on jurisdiction, this could be a compliance issue.
- **Fix:** Escalate to billing team immediately. Issue refund for the difference. Correct ChargeHub subscription.

**Business impact framing:** One Executive member at $6,000/month represents the same revenue as 8 Starter members. A single misclassification at this tier has outsized impact — protecting it justifies the entire reconciliation infrastructure.

**Improvement ideas:**
- **Tier change guardrails:** When reconciliation detects a tier mismatch, never auto-correct. Create a HubSpot task with both scenarios explained, assigned to the account manager, priority HIGH. The human decides.
- **Revenue at risk quantification:** Natasha's mismatch = $5,250/month at risk. If caught on day 1 instead of day 30, that's ~$5,075 in protected revenue (29 days × ~$175/day). This is the number that proves the daily digest ROI.
- **Upsell/downsell alerting logic:** The pipeline should distinguish: charge > CRM (possible upsell not reflected, retention risk) vs. charge < CRM (possible overcharge in CRM, revenue reporting inflated). Different alerts, different urgency, different owners.
- **LTV protection:** Executive members are the highest-LTV segment. Any mismatch at this tier should auto-escalate to the account manager AND the billing team simultaneously. Don't wait for someone to read a Slack message.

---

## CASE 8 — Jake Larson

**Status: ✅ Clean Match (Multi-Email)**

| Field | ChargeHub | HubSpot |
|-------|-----------|---------|
| Charge Email | j.larson.biz@gmail.com | j.larson.biz@gmail.com |
| Other Email | — | jake@larsoncg.com |
| Contact ID | — | 2209 |
| Tier | Starter ($750/mo) | Starter |
| Amount | $750.00 USD | $750.00 USD |
| Date | 2026-04-15 | 2026-04-15 |

**What happened:** Similar to Ryan Patel — Jake has two emails in HubSpot (Contact 2209). His charge came through the Gmail address, which is present in the CDM. The email index resolved it correctly.

**Mismatch flags:** None.

**Revenue classification:** Confirmed.

**Improvement ideas:** Same as Ryan Patel — the contributor model handles this natively. Jake gets one universal ID with both emails registered.

---

## CASE 9 — Derek Osei

**Status: 🔴 No Charge — Phantom Revenue**

| Field | ChargeHub | HubSpot |
|-------|-----------|---------|
| Email | — | derek@oseipartners.com |
| Contact ID | — | 2206 |
| Membership ID | — | 70002206 |
| Tier | — | Starter ($750/mo) |
| MRR | — | $750.00 USD |
| Billing Source | — | ChargeHub |
| Billing Date | — | 2026-03-18 |
| Status | — | Active |

**What happened:** Derek has an active ChargeHub membership in HubSpot with a billing date of March 18, but no charge was processed for him in April 2026. The reverse lookup (HubSpot → ChargeHub) found no matching email in the charges export.

**Mismatch flags:** `billing_date_mismatch` = TRUE, `mrr_mismatch` = TRUE, `mrr_normalized_mismatch` = TRUE.

**Revenue classification:** Phantom.

**Possible explanations:**
1. **Churned** — Derek cancelled after March and the HubSpot status wasn't updated. The membership still says "Active" but no money is coming in.
2. **Payment failed** — His card was declined and the charge didn't process. ChargeHub may have retry logic, but this month's billing didn't succeed.
3. **Billing cycle shifted** — His billing date moved and the April charge will come later, or his March charge covered a different cycle than expected.
4. **Migrated billing source** — He moved to PayEngine or Skool and the ChargeHub membership in HubSpot is stale.

**Business impact:** Derek's $750/month shows as Active MRR in HubSpot. If he's actually churned, the business is overreporting MRR by $750/month. At 500 members, if even 5% are phantom, that's ~25 members × $750 = $18,750/month in phantom revenue. That's the difference between "we think our MRR is X" and "our MRR is actually X minus $18,750."

**Gap fix instruction:** Investigate: check ChargeHub for cancellation or failed payment for derek@oseipartners.com. If churned: update HubSpot membership status to Cancellation. If payment failed: update to Payment Failed and trigger retry or outreach. If billing cycle shifted: update billing date.

**Improvement ideas:**
- **Automated investigation:** When reconciliation detects a phantom member, the pipeline should query ChargeHub's API for that customer's recent events (cancellations, failures, retries). Include the finding in the Slack alert so the ops person doesn't have to log in and search manually.
- **HubSpot note:** Write a timestamped note on Derek's HubSpot contact: "April 2026 reconciliation: no ChargeHub charge found. Membership status may be stale. Investigate."
- **Phantom revenue dashboard:** Metabase widget showing total phantom MRR month over month. If the number is growing, something systemic is breaking in the billing → CRM sync. If it's shrinking, the infrastructure is working.
- **Revenue classification:** Derek's $750 moves from "confirmed" to "phantom" in the revenue breakdown. The CFO/portfolio operator sees: "Of our $X MRR, $Y is confirmed and $Z is phantom — here's where to investigate."

---

## Summary of Revenue Impact

| Case | Type | Monthly Impact | Annual Impact |
|------|------|---------------|---------------|
| Camila Ferreira | FX erosion | -$40 | -$480 |
| Daniel Marsh | Untracked revenue | +$750 unreported | +$9,000 unreported |
| Natasha Bloom | Tier mismatch | ±$5,250 at risk | ±$63,000 at risk |
| Derek Osei | Phantom revenue | -$750 phantom | -$9,000 phantom |
| **Total identifiable risk** | | **$6,790/month** | **$81,480/year** |

*From 8 charges. Extrapolated to 500 members across 3 billing platforms, the total exposure is significantly higher.*

---

## Cross-Cutting Improvement Themes

### 1. Contributor Model (Universal ID)
Solves: Ryan Patel multi-email, Jake Larson multi-email, Marcus Webb dual membership detection, marketing deduplication, AI chat enablement.

### 2. Daily Detection (vs. Monthly)
Solves: Natasha Bloom caught on day 1 = ~$5,075 protected. Derek Osei investigated on day 1 = confirmed or recovered $750. Daniel Marsh contact created on day 2 = proper onboarding begins immediately.

### 3. Revenue Classification
Solves: "Is our MRR real?" Confirmed vs. disputed vs. phantom vs. untracked gives leadership a trustworthy number instead of a hopeful one.

### 4. USD Standardization
Solves: Camila's entire category of issues. Eliminates FX erosion, removes currency mismatch flag, simplifies MRR reporting.

### 5. Tier Change Guardrails
Solves: Natasha's scenario safely. Human confirms before CRM changes. Upsell vs. downsell routed to different teams.

### 6. Audit Log → Trend Intelligence
Solves: "Are we getting better?" Monthly accuracy trends, category distribution, revenue at risk over time. Metabase dashboards that answer operational health questions without manual investigation.

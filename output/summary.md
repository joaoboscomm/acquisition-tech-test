# Reconciliation Findings & Infrastructure Proposal

**To:** Leadership Team
**From:** João Bosco Mesquita
**Date:** April 2026
**Re:** ChargeHub billing reconciliation — what we found, what it means, what to build

---

## What We Found

We reconciled 8 ChargeHub charges from April 2026 against what HubSpot has on record. Of those 8, only 4 were clean matches. The rest had problems:

- **One member is paying $6,000/month (Executive tier) but HubSpot has them listed as a $750/month Starter.** That's either $5,250/month we're not properly tracking — or $5,250/month we shouldn't be charging. Both are urgent.
- **One member is actively being charged but doesn't exist in HubSpot at all.** We're collecting revenue with no CRM record — no account manager visibility, no proper onboarding, no renewal tracking.
- **One member shows as active in HubSpot but wasn't charged this month.** That's phantom revenue — it inflates our MRR on paper but no money came in.
- **One international member (Brazil, paying in BRL) shows a different MRR in HubSpot than what ChargeHub actually collected.** The exchange rate fluctuation created a silent gap.

In total: **$5,250 in discrepancies across 8 charges — a 42.9% gap between what we collected and what HubSpot thinks we collected.** At 500 members across three billing platforms, that pattern could mean tens of thousands in misreported revenue every month.

---

## What the Infrastructure Would Change

Today, we find these problems once a month, manually, by exporting CSVs and running a script. That means every mismatch sits undetected for up to 30 days.

The proposed infrastructure changes three things:

1. **Detection speed goes from monthly to daily.** Billing events flow into a central database automatically. Reconciliation runs every morning. The team gets a Slack alert by 8:30 AM with any new issues ranked by dollar impact. A $6,000 tier mismatch gets flagged the day after it happens, not 30 days later. Over a year, catching issues 29 days earlier could protect tens of thousands in revenue that would otherwise go unreported.

2. **We get a single, reliable member identity.** Right now, the same person can have different emails across ChargeHub, HubSpot, and Skool. The system merges them into one identity — like how your phone combines the same person from Gmail, LinkedIn, and iCloud into one contact card. This gives us a trustworthy member count, prevents duplicate outreach, and becomes the foundation for any future member intelligence: health scoring, churn prediction, personalized engagement.

3. **Revenue becomes classified, not just counted.** Instead of one MRR number, we'll know: how much is *confirmed* (charge matched, CRM matched, no issues), how much is *disputed* (matched but something is wrong), how much is *phantom* (CRM says active but no charge came in), and how much is *untracked* (money collected with no CRM record). This is the difference between "we think our MRR is X" and "we *know* our MRR is X" — which matters for forecasting, for investor conversations, and for making confident growth decisions.

**What it doesn't fix:** The infrastructure detects and alerts. It does not auto-correct records — because when a member is being charged $6,000 but HubSpot says $750, you need a human to determine which is correct before changing anything. Especially at the Executive tier, where one member represents the same revenue as eight Starters.

---

## One Thing I'd Want to Know First

**How often do members change tiers, and what's the current process for updating HubSpot when they do?**

If tier changes are rare and always handled by the same person, the fix might be as simple as a checklist. If they're frequent and handled by multiple people, we need the full infrastructure. The answer determines whether we build the complete pipeline now or start with just the daily reconciliation and alerting — which captures 80% of the value at 20% of the complexity.

---

## Recommendation on International Billing

One additional finding: every non-USD subscription carries silent margin erosion from FX fluctuations. One BRL charge produced $710 instead of $750 — a $40 gap from one member in one month. At 50 international subscriptions, that could mean $2,000+/month in invisible loss. **Consider standardizing billing to USD** — the member's bank handles conversion, we always receive the exact tier amount, and an entire category of reconciliation mismatches disappears.

#!/usr/bin/env python3
"""
ChargeHub ↔ HubSpot Reconciliation Script
Nexus Collective — April 2026

Reconciles ChargeHub processed charges against HubSpot Contact-Deal-Membership
export. Produces:
  1. chargehub_reconciliation_test.csv — full reconciliation with mismatch flags
  2. chargehub_gap_fixes_test.csv       — actionable rows for HubSpot corrections
  3. console_summary.txt                — printed totals and mismatch counts
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import sys
import io

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
PST = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")

# Current billing period for filtering.
# Hard-coded to April 2026 for deterministic, reproducible test output.
# For production: datetime.now(tz=PST).month / .year, or accept via CLI:
#   python3 reconcile.py --month 5 --year 2026
CURRENT_MONTH = 4
CURRENT_YEAR = 2026

# ChargeHub line-item strings → (canonical tier name, USD price).
# Used to validate charges and normalize cross-system comparisons.
VALID_PRODUCTS = {
    "Nexus Collective - Starter - $750 / Month":           ("Starter",          750),
    "Nexus Collective - Pro - $2,500 / Month":             ("Pro",             2500),
    "Nexus Collective - Executive - $6,000 / Month":       ("Executive",       6000),
    "Nexus Collective - Executive Annual - $24,000 / Year":("Executive Annual",24000),
}

# Tier → fixed USD amount. Fallback for non-USD charges/memberships where
# currency conversion is unreliable — we substitute the known tier price.
TIER_USD = {
    "Starter":          750,
    "Pro":             2500,
    "Executive":       6000,
    "Executive Annual":24000,
}

# HubSpot URL templates — used in Step 6 (gap-fix CSV) to generate clickable
# deep links so the ops team can jump directly to the relevant record in HubSpot.
HUBSPOT_PORTAL   = "99887766"
HUBSPOT_CONTACT  = f"https://app.hubspot.com/contacts/{HUBSPOT_PORTAL}/record/0-1"
HUBSPOT_MEMBERSHIP = f"https://app.hubspot.com/contacts/{HUBSPOT_PORTAL}/record/2-11223344"

# ---------------------------------------------------------------------------
# FILE PATHS
# Defaults assume the script lives in output/ and input CSVs are in
# ../test/. Override via: python3 reconcile.py <input_dir> <output_dir>
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else SCRIPT_DIR.parent / "test"
OUT_DIR  = Path(sys.argv[2]) if len(sys.argv) > 2 else SCRIPT_DIR

CHARGES_FILE = BASE_DIR / "charges_processed_test.csv"
CDM_FILE     = BASE_DIR / "ContactDealMembership_test.csv"
EXCLUDE_FILE = BASE_DIR / "Exclude_test.csv"


# ===================================================================
# STEP 1 — LOAD & FILTER
# ===================================================================

def load_charges(path: Path) -> pd.DataFrame:
    """Read ChargeHub charges CSV and parse timestamps.

    ChargeHub stores processed_at in UTC. We convert to PST because the
    business filters charges by calendar month in Pacific time.
    """
    df = pd.read_csv(path)
    df["processed_at_utc"] = pd.to_datetime(df["processed_at"], utc=True)
    df["processed_at_pst"] = df["processed_at_utc"].dt.tz_convert(PST)
    return df


def load_cdm(path: Path) -> pd.DataFrame:
    """Read the HubSpot Contact-Deal-Membership (CDM) export and normalize its fields.

    The CDM is a flat export where each row is a Contact × Deal × Membership
    combination. One contact can appear on multiple rows if they have several
    memberships or deals. We clean MRR formatting and parse date columns here
    so downstream code can compare values directly.
    """
    df = pd.read_csv(path)
    # MRR comes as a string with commas (e.g. "2,500.0") — strip commas, cast to float
    df["Membership MRR"] = (
        df["Membership MRR"]
        .astype(str)
        .str.replace(",", "", regex=False)
        .astype(float)
    )
    # Billing Date: used for ±1-day matching against ChargeHub processed_at.
    # errors="coerce" turns unparseable/blank values into NaT instead of raising,
    # so rows with missing dates still flow through (they just won't date-match).
    df["Membership Billing Date"] = pd.to_datetime(
        df["Membership Billing Date"], errors="coerce"
    )
    # Create Date: used as a tiebreaker (prefer most-recent membership).
    # Parsed as UTC because HubSpot exports timestamps in UTC.
    # errors="coerce" again to handle blanks gracefully.
    df["Membership Create Date"] = pd.to_datetime(
        df["Membership Create Date"], errors="coerce", utc=True
    )
    return df


def load_exclude(path: Path) -> set:
    """Load the exclusion list — emails that should be skipped during reconciliation.

    These are typically test accounts, internal staff, or known exceptions
    that would create false-positive mismatches. Emails are lowercased so
    lookups are case-insensitive.
    """
    df = pd.read_csv(path)
    return set(df["Email"].str.strip().str.lower())


def filter_charges(df: pd.DataFrame, exclude_emails: set) -> pd.DataFrame:
    """Keep only the charges that are in-scope for this month's reconciliation.

    Four filters are applied in order:
      1. Current month only (PST) — we reconcile one month at a time
      2. Valid product — ignore non-Nexus or legacy line items
      3. Non-blank email — can't match to HubSpot without an email
      4. Not in exclude list — skip test/internal accounts
    """
    # 1. Current month only — keep charges whose PST-converted date falls in the billing period
    mask_month = (
        (df["processed_at_pst"].dt.month == CURRENT_MONTH)
        & (df["processed_at_pst"].dt.year == CURRENT_YEAR)
    )

    # 2. Valid product — exclude non-Nexus or legacy line items from reconciliation
    mask_product = df["line_item_title"].isin(VALID_PRODUCTS.keys())

    # 3. Email present — can't match a charge to HubSpot without an email address
    mask_email = df["email"].notna() & (df["email"].str.strip() != "")

    # 4. Not excluded — skip test accounts, internal employees, and known exceptions
    mask_exclude = ~df["email"].str.strip().str.lower().isin(exclude_emails)

    filtered = df[mask_month & mask_product & mask_email & mask_exclude].copy()
    return filtered


# ===================================================================
# STEP 2 — BUILD CONTACT INDEX FROM CDM
# ===================================================================

def build_contact_index(cdm: pd.DataFrame):
    """Build three lookup structures from the CDM export for fast matching.

    The CDM has duplicate rows (one per Contact × Deal × Membership), so we
    iterate once and deduplicate into:
        email_to_contact    — email → Contact ID  (for charge→contact lookup)
        contact_emails      — Contact ID → {all emails}  (a contact may have
                              multiple emails; needed to check if ANY email
                              appears in the charge file during Direction 2)
        contact_memberships — Contact ID → [membership records]  (deduplicated
                              by Membership ID so each membership appears once)
    """
    email_to_contact = {}
    contact_emails = {}
    contact_memberships = {}

    for _, row in cdm.iterrows():
        cid = int(row["Contact ID"])
        email = str(row["Email"]).strip().lower()
        mid = int(row["Membership ID"])

        # Map this email to its contact — enables charge → contact lookup by email
        email_to_contact[email] = cid
        # Collect ALL emails for this contact — needed for multi-email matching in Direction 2
        contact_emails.setdefault(cid, set()).add(email)

        # Initialize this contact's membership dict on first encounter
        if cid not in contact_memberships:
            contact_memberships[cid] = {}

        # Store each membership exactly once — the CDM repeats memberships across
        # deal rows, but we need one copy per membership for the matching cascade
        if mid not in contact_memberships[cid]:
            contact_memberships[cid][mid] = {
                "membership_id":       mid,
                "membership_tier":     row["Membership Tier"],
                "membership_mrr":      row["Membership MRR"],
                "membership_currency": row["Membership Currency"],
                "membership_type":     row["Membership Type"],
                "membership_billing_date": row["Membership Billing Date"],
                "membership_create_date":  row["Membership Create Date"],
                "membership_billing_source": row["Membership Billing Source"],
                "membership_status":   row["Membership Status"],
                "first_name":          row["First Name"],
                "last_name":           row["Last Name"],
            }

    # Flatten inner dicts {mid: record} → lists [record, ...] for simpler
    # iteration in match_charge_to_membership() during the matching cascade
    contact_memberships = {
        cid: list(mdict.values())
        for cid, mdict in contact_memberships.items()
    }

    # email_to_contact:    {"email@x.com": 2201, ...}        — charge email → Contact ID
    # contact_emails:      {2201: {"email1@...", "email2@..."}} — Contact ID → all known emails
    # contact_memberships: {2201: [{...membership record...}]}  — Contact ID → membership list
    return email_to_contact, contact_emails, contact_memberships


# ===================================================================
# STEP 3 — NORMALIZATION HELPERS
# ===================================================================

def chargehub_tier(line_item_title: str) -> str:
    """Extract the canonical tier name from a ChargeHub line-item string.

    Converts verbose product names like 'Nexus Collective - Pro - $2,500 / Month'
    into the short tier name ('Pro') that HubSpot uses for side-by-side comparison.

    Args:
        line_item_title: Full product name from ChargeHub's line_item_title column.
    Returns:
        Tier name ('Starter', 'Pro', 'Executive', 'Executive Annual') or None.
    """
    return VALID_PRODUCTS.get(line_item_title, (None, None))[0]


def normalize_chargehub_price(row) -> float:
    """Return the USD-equivalent price for a charge.

    USD charges use the actual amount. Non-USD charges use the fixed tier
    price instead, because ChargeHub's currency conversion can drift from
    the canonical price and would cause false MRR mismatches.

    Args:
        row: A single ChargeHub charge (pandas Series) with 'line_item_title',
             'presentment_currency', and 'usd_total_price' columns.
    Returns:
        Normalized USD amount (float).
    """
    # Derive the tier so we know which canonical USD price to use as fallback
    tier = chargehub_tier(row["line_item_title"])
    if row["presentment_currency"] == "USD":
        # USD charges are already in the right currency — use the actual amount
        return float(row["usd_total_price"])
    else:
        # Non-USD: substitute the tier's fixed USD price to avoid FX drift
        return float(TIER_USD.get(tier, row["usd_total_price"]))


def normalize_membership_mrr(mrr: float, currency: str, tier: str) -> float:
    """Normalize a HubSpot membership's MRR to a comparable USD amount.

    Non-USD amounts are unreliable after currency conversion, so we
    substitute the known tier price to keep comparisons consistent.
    $0 MRR is preserved as-is (indicates a free or paused membership).

    Args:
        mrr:      Raw MRR value from HubSpot's 'Membership MRR' column.
        currency: Currency code (e.g. 'USD', 'BRL') from 'Membership Currency'.
        tier:     Tier name (e.g. 'Starter') used to look up the canonical USD price.
    Returns:
        Normalized USD amount (float). Returns 0.0 if mrr is 0.
    """
    if mrr == 0:
        return 0.0                             # $0 stays $0 (free or paused membership)
    if currency == "USD":
        return float(mrr)                      # Already in USD — use the actual MRR
    else:
        return float(TIER_USD.get(tier, mrr))  # Non-USD: use the tier's canonical USD price


# ===================================================================
# STEP 4 — MATCHING LOGIC
# ===================================================================

def match_charge_to_membership(
    charge_email: str,
    charge_date_pst: datetime,
    charge_tier: str,
    email_to_contact: dict,
    contact_memberships: dict,
    claimed: set,
) -> tuple:
    """Find the best HubSpot membership for a given ChargeHub charge.

    Matching cascade (each step narrows candidates):
      1. Look up contact by email
      2. Prefer ChargeHub-sourced memberships (most likely to be the correct record)
      3. Exclude already-claimed memberships (prevents double-matching)
      4. Date-proximity match (±1 day window) with tier-preference tiebreaker
      5. Fallback: pick the most recently created membership

    Args:
        charge_email:         Email from the ChargeHub charge (used for contact lookup).
        charge_date_pst:      Charge processed_at converted to PST (for ±1 day date matching).
        charge_tier:          Canonical tier name derived from the charge's line_item_title.
        email_to_contact:     Dict mapping email → Contact ID (from build_contact_index).
        contact_memberships:  Dict mapping Contact ID → list of membership records.
        claimed:              Set of already-matched Membership IDs (mutated in-place).
    Returns:
        (contact_id, membership_record, matching_email) — membership_record is None
        if no match found; contact_id is None if the email isn't in HubSpot at all.
    """
    email_lower = charge_email.strip().lower()
    cid = email_to_contact.get(email_lower)

    if cid is None:
        return None, None, email_lower  # No HubSpot contact for this email

    memberships = contact_memberships.get(cid, [])
    if not memberships:
        return cid, None, email_lower  # Contact exists but has no memberships

    # Step 2: Prefer ChargeHub-sourced records — they're the most likely match
    # for a ChargeHub charge. Fall back to all memberships if none are ChargeHub.
    chargehub_records = [m for m in memberships if m["membership_billing_source"] == "ChargeHub"]
    candidates = chargehub_records if chargehub_records else memberships

    # Step 3: Remove already-claimed memberships so each membership maps to
    # at most one charge. Cascading fallbacks ensure we always have candidates.
    available = [m for m in candidates if m["membership_id"] not in claimed]
    if not available:
        available = [m for m in memberships if m["membership_id"] not in claimed]
    if not available:
        # Everything claimed — allow re-use as a last resort rather than
        # reporting a false "no match"
        available = candidates

    # Step 4: Date-proximity match — billing date within ±1 day of the charge.
    # This handles minor date shifts between ChargeHub processing and HubSpot.
    charge_date = charge_date_pst.date() if hasattr(charge_date_pst, "date") else charge_date_pst
    proximity = []
    for m in available:
        bd = m["membership_billing_date"]
        if pd.notna(bd):
            bd_date = bd.date() if hasattr(bd, "date") else pd.to_datetime(bd).date()
            delta = abs((charge_date - bd_date).days)
            if delta <= 1:
                proximity.append((m, delta))

    if proximity:
        # Among date-proximate matches, prefer: same tier > closer date > newer record
        def sort_key(item):
            m, delta = item
            tier_match = 0 if m["membership_tier"] == charge_tier else 1
            create_dt = m["membership_create_date"]
            create_ts = -create_dt.timestamp() if pd.notna(create_dt) else 0
            return (tier_match, delta, create_ts)

        proximity.sort(key=sort_key)
        best = proximity[0][0]
        claimed.add(best["membership_id"])
        return cid, best, email_lower

    # Step 5: No date match — fall back to the most recently created membership.
    # This is the weakest signal but still better than reporting "no match".
    def fallback_sort(m):
        create_dt = m["membership_create_date"]
        create_ts = -create_dt.timestamp() if pd.notna(create_dt) else 0
        bd = m["membership_billing_date"]
        bd_ts = -bd.timestamp() if pd.notna(bd) else 0
        return (create_ts, bd_ts)

    available.sort(key=fallback_sort)
    best = available[0]
    claimed.add(best["membership_id"])
    return cid, best, email_lower


# ===================================================================
# STEP 5 — BUILD RECONCILIATION
# ===================================================================

def build_reconciliation(
    charges: pd.DataFrame,
    cdm: pd.DataFrame,
    email_to_contact: dict,
    contact_emails: dict,
    contact_memberships: dict,
) -> pd.DataFrame:
    """Two-directional reconciliation between ChargeHub charges and HubSpot memberships.

    Direction 1 (ChargeHub → HubSpot): for every filtered charge, find the best
    matching membership and flag any field-level mismatches.
    Direction 2 (HubSpot → ChargeHub): find ChargeHub-billed members with no
    charge this month — potential churn or payment failures.

    Args:
        charges:              Filtered ChargeHub charges (output of filter_charges).
        cdm:                  Full HubSpot CDM export (unfiltered — all contacts/memberships).
        email_to_contact:     Dict mapping email → Contact ID.
        contact_emails:       Dict mapping Contact ID → set of all known emails.
        contact_memberships:  Dict mapping Contact ID → list of membership records.
    Returns:
        DataFrame with one row per reconciliation result (both directions),
        including 16 data columns, 8 mismatch flags, and an internal _source tag.
    """

    rows = []
    claimed = set()  # Prevent double-matching the same membership to multiple charges
    matched_contacts = set()  # Contacts already reconciled (skipped in Direction 2)

    # ── Direction 1: ChargeHub → HubSpot ──
    # For each filtered charge, find the best-matching HubSpot membership
    # and compute 8 mismatch flags comparing the two records side by side.
    for _, ch in charges.iterrows():

        # Extract and normalize the ChargeHub side of the comparison
        tier = chargehub_tier(ch["line_item_title"])
        ch_date_pst = ch["processed_at_pst"]
        ch_date_only = ch_date_pst.date()
        ch_usd = float(ch["usd_total_price"])
        ch_currency = ch["presentment_currency"]
        ch_normalized = normalize_chargehub_price(ch)

        # Run the matching cascade to find the best HubSpot membership for this charge
        cid, mem, matching_email = match_charge_to_membership(
            ch["email"], ch_date_pst, tier,
            email_to_contact, contact_memberships, claimed,
        )

        # Track which contacts were reconciled so Direction 2 can skip them
        if cid is not None:
            matched_contacts.add(cid)

        if mem:
            # ── Match found: extract the HubSpot side for comparison ──
            m_tier = mem["membership_tier"]
            m_bd = mem["membership_billing_date"]
            m_bd_date = m_bd.date() if pd.notna(m_bd) else None
            m_mrr = float(mem["membership_mrr"])
            m_currency = mem["membership_currency"]
            m_mrr_norm = normalize_membership_mrr(m_mrr, m_currency, m_tier)
            m_source = mem["membership_billing_source"]
            m_status = mem["membership_status"]
            m_type = mem["membership_type"]
            m_id = mem["membership_id"]

            # Build the output row: HubSpot fields (left) vs ChargeHub fields (right),
            # then compute each of the 8 mismatch flags by comparing the paired values
            row = {
                "contact_id":                     cid,
                "membership_id":                  m_id,
                "matching_email":                 matching_email,
                "membership_tier":                m_tier,
                "chargehub_line_item":            tier,
                "membership_billing_date":        str(m_bd_date) if m_bd_date else "",
                "chargehub_processed_date":       str(ch_date_only),
                "membership_mrr":                 m_mrr,
                "chargehub_total_price":          ch_usd,
                "membership_mrr_normalized":      m_mrr_norm,
                "chargehub_total_price_normalized": ch_normalized,
                "membership_currency":            m_currency,
                "chargehub_currency":             ch_currency,
                "membership_billing_source":      m_source,
                "membership_status":              m_status,
                "membership_type":                m_type,
                # Mismatch flags — each True means the systems disagree on this field.
                # Any True flag causes this row to appear in the gap-fix CSV.
                "tier_mismatch":            m_tier != tier,
                "billing_date_mismatch":    m_bd_date != ch_date_only if m_bd_date else True,
                "mrr_mismatch":             m_mrr != ch_usd,
                "mrr_normalized_mismatch":  m_mrr_norm != ch_normalized,
                "currency_mismatch":        m_currency != ch_currency,
                "billing_source_mismatch":  m_source != "ChargeHub",
                "status_mismatch":          m_status != "Active",
                "type_mismatch":            m_type != "Paying Member",
                # Internal tracking — used to separate Direction 1 vs 2 rows
                "_source": "chargehub",
            }
        else:
            # ── No match: this charge has no HubSpot counterpart ──
            # Either the email isn't in HubSpot at all, or the contact exists
            # but has no membership records. All 8 mismatch flags → True so this
            # row appears in the gap-fix CSV and the "Not Found" summary count.
            row = {
                "contact_id":                     cid if cid else "",
                "membership_id":                  "",
                "matching_email":                 matching_email,
                "membership_tier":                "",
                "chargehub_line_item":            tier,
                "membership_billing_date":        "",
                "chargehub_processed_date":       str(ch_date_only),
                "membership_mrr":                 "",
                "chargehub_total_price":          ch_usd,
                "membership_mrr_normalized":      "",
                "chargehub_total_price_normalized": ch_normalized,
                "membership_currency":            "",
                "chargehub_currency":             ch_currency,
                "membership_billing_source":      "",
                "membership_status":              "",
                "membership_type":                "",
                "tier_mismatch":            True,
                "billing_date_mismatch":    True,
                "mrr_mismatch":             True,
                "mrr_normalized_mismatch":  True,
                "currency_mismatch":        True,
                "billing_source_mismatch":  True,
                "status_mismatch":          True,
                "type_mismatch":            True,
                "_source": "chargehub_no_hubspot",
            }

        rows.append(row)

    # --- Direction 2: HubSpot → ChargeHub (unmatched) ---
    # Only ChargeHub-sourced memberships are checked here — memberships billed
    # through other sources (e.g. Stripe) are outside this reconciliation's scope.
    charge_emails_lower = set(charges["email"].str.strip().str.lower())

    seen_cid_mid = set()  # Dedup by (Contact ID, Membership ID) since CDM can have duplicate rows
    for _, cdm_row in cdm.iterrows():
        if cdm_row["Membership Billing Source"] != "ChargeHub":
            continue

        cid = int(cdm_row["Contact ID"])
        mid = int(cdm_row["Membership ID"])

        # Dedup by Contact ID × Membership ID
        key = (cid, mid)
        if key in seen_cid_mid:
            continue
        seen_cid_mid.add(key)

        # If any email for this contact appears in the charge file, they were
        # already reconciled in Direction 1 — skip them here.
        all_emails = contact_emails.get(cid, set())
        if all_emails & charge_emails_lower:
            continue

        # This ChargeHub-billed member had no charge this month.
        # Could indicate churn, a failed payment, or a shifted billing cycle.
        mem = contact_memberships[cid]
        # Find the specific membership record
        mem_rec = next((m for m in mem if m["membership_id"] == mid), None)
        if not mem_rec:
            continue

        m_mrr = float(mem_rec["membership_mrr"])
        m_currency = mem_rec["membership_currency"]
        m_tier = mem_rec["membership_tier"]
        m_bd = mem_rec["membership_billing_date"]
        m_bd_date = m_bd.date() if pd.notna(m_bd) else None

        row = {
            "contact_id":                     cid,
            "membership_id":                  mid,
            "matching_email":                 cdm_row["Email"],
            "membership_tier":                m_tier,
            "chargehub_line_item":            "",
            "membership_billing_date":        str(m_bd_date) if m_bd_date else "",
            "chargehub_processed_date":       "",
            "membership_mrr":                 m_mrr,
            "chargehub_total_price":          "",
            "membership_mrr_normalized":      normalize_membership_mrr(m_mrr, m_currency, m_tier),
            "chargehub_total_price_normalized": "",
            "membership_currency":            m_currency,
            "chargehub_currency":             "",
            "membership_billing_source":      mem_rec["membership_billing_source"],
            "membership_status":              mem_rec["membership_status"],
            "membership_type":                mem_rec["membership_type"],
            "tier_mismatch":            False,
            "billing_date_mismatch":    True,
            "mrr_mismatch":             True,
            "mrr_normalized_mismatch":  True,
            "currency_mismatch":        False,
            "billing_source_mismatch":  False,
            "status_mismatch":          mem_rec["membership_status"] != "Active",
            "type_mismatch":            mem_rec["membership_type"] != "Paying Member",
            "_source": "hubspot_no_charge",
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    return df


# ===================================================================
# STEP 6 — GAP FIX CSV
# ===================================================================

# The 8 boolean columns that determine whether a row has a gap.
# Any row where at least one flag is True will appear in the gap-fix CSV.
MISMATCH_FLAGS = [
    "tier_mismatch", "billing_date_mismatch", "mrr_mismatch",
    "mrr_normalized_mismatch", "currency_mismatch",
    "billing_source_mismatch", "status_mismatch", "type_mismatch",
]


def generate_fix_instructions(row) -> str:
    """Generate a plain-English description of what needs fixing for a gap row.

    Called once per gap-fix row. The instructions vary by source type:
      - chargehub_no_hubspot → create a new contact + membership in HubSpot
      - hubspot_no_charge    → investigate missing payment (churn / failure)
      - chargehub (matched)  → list each specific field mismatch with both values

    Args:
        row: A single reconciliation row (pandas Series) including mismatch flags and _source.
    Returns:
        Human-readable fix string. Multiple fixes are pipe-separated ('|').
    """
    parts = []
    source = row.get("_source", "")

    if source == "chargehub_no_hubspot":
        parts.append(
            f"No HubSpot contact found for email '{row['matching_email']}'. "
            f"Create contact and membership record. "
            f"Tier: {row['chargehub_line_item']}, "
            f"MRR: ${row['chargehub_total_price']} USD, "
            f"Billing Source: ChargeHub."
        )
        return " ".join(parts)

    if source == "hubspot_no_charge":
        parts.append(
            f"HubSpot shows active ChargeHub membership (Tier: {row['membership_tier']}, "
            f"MRR: ${row['membership_mrr']}) but no charge was processed in April 2026. "
            f"Investigate: possible churn, payment failure, or billing cycle shift. "
            f"Update membership status if member is no longer active."
        )
        return " ".join(parts)

    # Matched but with mismatches
    if row.get("tier_mismatch"):
        ch_tier = row["chargehub_line_item"]
        hs_tier = row["membership_tier"]
        parts.append(
            f"Tier mismatch — ChargeHub: {ch_tier}, HubSpot: {hs_tier}. "
            f"If {ch_tier} is correct: update HubSpot tier and MRR. "
            f"If {hs_tier} is correct: escalate billing discrepancy to billing team."
        )

    if row.get("billing_date_mismatch"):
        parts.append(
            f"Billing date mismatch — ChargeHub: {row['chargehub_processed_date']}, "
            f"HubSpot: {row['membership_billing_date']}. Update Membership Billing Date."
        )

    if row.get("mrr_mismatch") and not row.get("tier_mismatch"):
        parts.append(
            f"MRR mismatch — ChargeHub: ${row['chargehub_total_price']}, "
            f"HubSpot: ${row['membership_mrr']}. Verify correct amount and update."
        )

    if row.get("currency_mismatch"):
        parts.append(
            f"Currency mismatch — ChargeHub: {row['chargehub_currency']}, "
            f"HubSpot: {row['membership_currency']}. Align currency records."
        )

    if row.get("billing_source_mismatch"):
        parts.append(
            f"Billing source is '{row['membership_billing_source']}', expected 'ChargeHub'. "
            f"Update Membership Billing Source."
        )

    if row.get("status_mismatch"):
        parts.append(
            f"Status is '{row['membership_status']}', expected 'Active'. "
            f"Verify membership status and update."
        )

    if row.get("type_mismatch"):
        parts.append(
            f"Type is '{row['membership_type']}', expected 'Paying Member'. "
            f"Update Membership Type."
        )

    return " | ".join(parts) if parts else "Review record."


def build_gap_fixes(recon: pd.DataFrame) -> pd.DataFrame:
    """Build the actionable gap-fix CSV from reconciliation results.

    Filters to only rows where at least one of the 8 mismatch flags is True,
    then enriches with clickable HubSpot links and human-readable fix instructions
    so the ops team can action each row directly without manual lookups.

    Args:
        recon: Full reconciliation DataFrame (from build_reconciliation), including
               the _source tag and all 8 mismatch flag columns.
    Returns:
        DataFrame with 9 columns matching the spec: contact_id, membership_id, email,
        hubspot_contact_link, hubspot_membership_link, current_tier, current_mrr,
        current_status, fix_instructions.
    """
    has_mismatch = recon[MISMATCH_FLAGS].any(axis=1)
    gaps = recon[has_mismatch].copy()

    # Add convenience columns for the ops team
    gaps["email"] = gaps["matching_email"]
    gaps["hubspot_contact_link"] = gaps["contact_id"].apply(
        lambda x: f"{HUBSPOT_CONTACT}/{int(x)}" if x != "" and pd.notna(x) else ""
    )
    gaps["hubspot_membership_link"] = gaps["membership_id"].apply(
        lambda x: f"{HUBSPOT_MEMBERSHIP}/{int(x)}" if x != "" and pd.notna(x) else ""
    )
    gaps["current_tier"] = gaps["membership_tier"]
    gaps["current_mrr"] = gaps["membership_mrr"]
    gaps["current_status"] = gaps["membership_status"]
    gaps["fix_instructions"] = gaps.apply(generate_fix_instructions, axis=1)

    out_cols = [
        "contact_id", "membership_id", "email",
        "hubspot_contact_link", "hubspot_membership_link",
        "current_tier", "current_mrr", "current_status",
        "fix_instructions",
    ]
    return gaps[out_cols]


# ===================================================================
# STEP 7 — CONSOLE SUMMARY
# ===================================================================

def build_summary(recon: pd.DataFrame, charges_filtered: pd.DataFrame) -> str:
    """Aggregate reconciliation results into a human-readable text summary.

    Computes totals, gap amount/percentage, per-flag mismatch counts,
    and not-found counts. Output format matches the template in the test spec.

    Args:
        recon:             Full reconciliation DataFrame (with _source column still present).
        charges_filtered:  Filtered ChargeHub charges (for raw totals including all in-scope charges).
    Returns:
        Multi-line formatted string for console printing and saving to console_summary.txt.
    """
    month_label = datetime(CURRENT_YEAR, CURRENT_MONTH, 1).strftime("%B %Y").upper()

    # ChargeHub totals — count and raw USD include ALL filtered charges;
    # normalized total uses only matched charges because the gap metric
    # compares matched pairs (unmatched charges are reported in "Not Found")
    ch_total = charges_filtered["usd_total_price"].sum()
    ch_normalized = recon[recon["_source"] == "chargehub"]["chargehub_total_price_normalized"].sum()
    ch_count = len(charges_filtered)

    # HubSpot matched — only Direction 1 charges that successfully found a membership
    matched = recon[recon["_source"] == "chargehub"]
    has_hubspot = matched[matched["membership_id"] != ""]
    hs_mrr = pd.to_numeric(has_hubspot["membership_mrr"], errors="coerce").sum()
    hs_mrr_norm = pd.to_numeric(has_hubspot["membership_mrr_normalized"], errors="coerce").sum()
    contacts_matched = has_hubspot["contact_id"].nunique()

    # Gap = difference between what ChargeHub billed vs what HubSpot recorded
    gap_amount = ch_normalized - hs_mrr_norm
    gap_pct = (gap_amount / ch_normalized * 100) if ch_normalized > 0 else 0

    # Per-flag mismatch counts across all reconciliation rows
    mismatch_counts = {f: int(recon[f].sum()) for f in MISMATCH_FLAGS}

    # Rows where one system has a record but the other doesn't
    ch_no_hs = len(recon[recon["_source"] == "chargehub_no_hubspot"])
    hs_no_ch = len(recon[recon["_source"] == "hubspot_no_charge"])

    summary = f"""=== CHARGEHUB RECONCILIATION — {month_label} ===

ChargeHub:
  Total charges: {ch_count}
  Total amount (USD): ${ch_total:,.2f}
  Total amount (normalized): ${ch_normalized:,.2f}

HubSpot Matched:
  Contacts matched: {contacts_matched}
  Total MRR: ${hs_mrr:,.2f}
  Total MRR (normalized): ${hs_mrr_norm:,.2f}

Gap:
  Amount: ${gap_amount:,.2f}
  Percentage: {gap_pct:.1f}%

Mismatches:
  Tier: {mismatch_counts['tier_mismatch']}
  Billing Date: {mismatch_counts['billing_date_mismatch']}
  MRR: {mismatch_counts['mrr_mismatch']}
  MRR Normalized: {mismatch_counts['mrr_normalized_mismatch']}
  Currency: {mismatch_counts['currency_mismatch']}
  Billing Source: {mismatch_counts['billing_source_mismatch']}
  Status: {mismatch_counts['status_mismatch']}
  Type: {mismatch_counts['type_mismatch']}

Not Found:
  ChargeHub charges without HubSpot match: {ch_no_hs}
  HubSpot ChargeHub members without charge: {hs_no_ch}
"""
    return summary


# ===================================================================
# MAIN
# ===================================================================

def main():
    """Orchestrate the full reconciliation pipeline: load → filter → index → reconcile → export."""

    # ── Load all three input files ──
    print("Loading data...")
    charges_raw = load_charges(CHARGES_FILE)       # ChargeHub processed charges (80 cols)
    cdm = load_cdm(CDM_FILE)                       # HubSpot Contact-Deal-Membership export
    exclude_emails = load_exclude(EXCLUDE_FILE)     # Emails to skip (employees, test accounts)

    # ── Apply the four charge filters (month, product, email, exclude list) ──
    print("Filtering charges...")
    charges = filter_charges(charges_raw, exclude_emails)
    print(f"  {len(charges_raw)} raw → {len(charges)} after filters")

    # ── Build lookup structures from the CDM for email → contact → membership matching ──
    print("Building contact index...")
    email_to_contact, contact_emails, contact_memberships = build_contact_index(cdm)
    print(f"  {len(email_to_contact)} emails → {len(contact_memberships)} contacts")

    # ── Run the two-direction reconciliation (ChargeHub→HubSpot + HubSpot→ChargeHub) ──
    print("Running reconciliation...")
    recon = build_reconciliation(
        charges, cdm, email_to_contact, contact_emails, contact_memberships
    )

    # ── Export Output 1: reconciliation CSV (drop internal _source tag before writing) ──
    recon_out = recon.drop(columns=["_source"])
    recon_path = OUT_DIR / "chargehub_reconciliation_test.csv"
    recon_out.to_csv(recon_path, index=False)
    print(f"  → {recon_path} ({len(recon_out)} rows)")

    # ── Export Output 2: gap-fix CSV (only rows with at least one mismatch flag) ──
    print("Building gap fixes...")
    gap_fixes = build_gap_fixes(recon)
    gap_path = OUT_DIR / "chargehub_gap_fixes_test.csv"
    gap_fixes.to_csv(gap_path, index=False)
    print(f"  → {gap_path} ({len(gap_fixes)} rows)")

    # ── Export Output 3: console summary with totals, gap, and mismatch counts ──
    print()
    summary = build_summary(recon, charges)
    print(summary)

    summary_path = OUT_DIR / "console_summary.txt"
    summary_path.write_text(summary)
    print(f"Summary saved to {summary_path}")


if __name__ == "__main__":
    main()

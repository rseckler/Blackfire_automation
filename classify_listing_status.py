#!/usr/bin/env python3
"""
Classify companies as public/private/pre_ipo/acquired/unknown.

Priority order (Tommi 2026-04-14: Excel is source of truth):
  1. extra_data.Status keywords   → acquired/pre_ipo/private/public (explicit Excel value wins)
  2. IPO_expected present         → pre_ipo
  3. current_price > 0            → public
  4. extra_data.Current_Price > 0 → public
  5. Has validated symbol         → public (weakest heuristic)
  6. invalid_companies.json       → private (all ticker methods failed)
  7. else                         → unknown

Usage:
  python3 classify_listing_status.py            # dry-run (preview only)
  python3 classify_listing_status.py --apply    # write to Supabase
  python3 classify_listing_status.py --verbose  # print reason per change
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# Keywords in extra_data.Status that indicate listing status.
# Check order: more-specific first (PRIVATE before PUBLIC so "not listed" → private, not public via "listed").
ACQUIRED_KEYWORDS = ['acquired', 'übernommen', 'merged', 'delisted', 'bankrupt', 'insolvent']
PRE_IPO_KEYWORDS = ['pre-ipo', 'pre ipo', 'ipo planned', 'ipo expected', 'ipo geplant', 'spac']
PRIVATE_KEYWORDS = ['private', 'privat', 'not listed', 'nicht gelistet', 'unlisted']
PUBLIC_KEYWORDS = ['public', 'öffentlich', 'listed', 'gelistet', 'börsennotiert', 'börse', 'ipo done', 'ipo completed']

BLACKLIST_FILE = os.path.join(SCRIPT_DIR, 'invalid_companies.json')


def load_blacklist() -> set:
    """Load company IDs from invalid_companies.json (ticker validation failures)."""
    if not os.path.exists(BLACKLIST_FILE):
        return set()
    try:
        with open(BLACKLIST_FILE, 'r') as f:
            data = json.load(f)
        return set(data.keys())
    except Exception as e:
        print(f"  Warning: Could not load blacklist: {e}")
        return set()


def classify(company: dict, blacklisted_ids: set) -> tuple:
    """Determine listing_status for a single company.

    Returns (status, reason) where reason is a short tag like 'excel:private'
    or 'has_current_price' for debugging.
    """
    extra = company.get('extra_data') or {}
    company_id = company.get('id', '')

    # 1. Excel Status wins (explicit user-maintained value is source of truth).
    # Only classify if a keyword actually matches; unrecognized values fall through to heuristics.
    status_raw = str(extra.get('Status', '')).lower().strip()
    if status_raw:
        for kw in ACQUIRED_KEYWORDS:
            if kw in status_raw:
                return 'acquired', f'excel:acquired({status_raw!r})'
        for kw in PRE_IPO_KEYWORDS:
            if kw in status_raw:
                return 'pre_ipo', f'excel:pre_ipo({status_raw!r})'
        for kw in PRIVATE_KEYWORDS:
            if kw in status_raw:
                return 'private', f'excel:private({status_raw!r})'
        for kw in PUBLIC_KEYWORDS:
            if kw in status_raw:
                return 'public', f'excel:public({status_raw!r})'

    # 2. IPO_expected set → pre_ipo (before price signal — a scheduled IPO overrides stale price data)
    ipo_expected = extra.get('IPO_expected') or extra.get('IPO Expected') or extra.get('IPO_Expected')
    if ipo_expected:
        return 'pre_ipo', 'ipo_expected_set'

    # 3. Has a valid current price → public
    price = company.get('current_price')
    if price and float(price) > 0:
        return 'public', 'has_current_price'

    # 4. Has a price in extra_data
    ed_price = extra.get('Current_Price')
    if ed_price:
        try:
            if float(str(ed_price).replace(',', '.')) > 0:
                return 'public', 'has_extra_price'
        except (ValueError, TypeError):
            pass

    # 5. Has a validated symbol → public (weakest heuristic; a private company with pending ticker
    # pollutes here — this is why Excel Status must take priority above)
    symbol = company.get('symbol')
    if symbol and symbol.strip():
        return 'public', 'has_symbol'

    # 6. In blacklist (all ticker methods failed) → private
    if company_id in blacklisted_ids:
        return 'private', 'blacklisted'

    return 'unknown', 'no_signal'


def main():
    parser = argparse.ArgumentParser(description='Classify company listing status')
    parser.add_argument('--apply', action='store_true', help='Write results to Supabase')
    parser.add_argument('--verbose', action='store_true', help='Print reason tag per change')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  CLASSIFY LISTING STATUS")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print("=" * 70)

    # Load data
    print("\n  Loading companies...")
    try:
        companies = supabase_helper.get_all_companies('id, name, symbol, current_price, extra_data, listing_status')
    except Exception:
        # listing_status column may not exist yet — fetch without it
        print("  Note: listing_status column not found, fetching without it")
        companies = supabase_helper.get_all_companies('id, name, symbol, current_price, extra_data')
    print(f"  Loaded {len(companies)} companies")

    blacklisted_ids = load_blacklist()
    print(f"  Blacklist: {len(blacklisted_ids)} companies")

    # Classify
    results = Counter()
    reason_counts = Counter()
    changes = []
    unrecognized_excel_values = Counter()

    for company in companies:
        new_status, reason = classify(company, blacklisted_ids)
        old_status = company.get('listing_status')
        results[new_status] += 1
        reason_counts[reason] += 1

        # Track Excel Status values that didn't match any keyword list
        status_raw = str((company.get('extra_data') or {}).get('Status', '')).lower().strip()
        if status_raw and not reason.startswith('excel:'):
            unrecognized_excel_values[status_raw] += 1

        if new_status != old_status:
            changes.append({
                'id': company['id'],
                'name': company.get('name', '?'),
                'old': old_status,
                'new': new_status,
                'reason': reason,
            })

    # Report
    print(f"\n  Classification results:")
    for status, count in sorted(results.items(), key=lambda x: -x[1]):
        print(f"    {status:12s}: {count:5d}")
    print(f"    {'TOTAL':12s}: {sum(results.values()):5d}")

    print(f"\n  Reason breakdown:")
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"    {reason:35s}: {count:5d}")

    if unrecognized_excel_values:
        print(f"\n  ⚠  Unrecognized Excel Status values (first 10) — consider adding keywords:")
        for val, count in unrecognized_excel_values.most_common(10):
            print(f"    {val!r:40s}: {count:5d}")

    print(f"\n  Changes needed: {len(changes)}")

    # Show sample changes
    if changes:
        print(f"\n  Sample changes (first 20):")
        for c in changes[:20]:
            reason_str = f" [{c['reason']}]" if args.verbose else ''
            print(f"    {c['name'][:40]:40s}  {str(c['old']):10s} → {c['new']:10s}{reason_str}")

    # Apply
    if args.apply and changes:
        print(f"\n  Applying {len(changes)} updates...")
        success = 0
        for i, c in enumerate(changes):
            if supabase_helper.update_company(c['id'], {'listing_status': c['new']}):
                success += 1
            if (i + 1) % 100 == 0:
                print(f"    ... {i + 1}/{len(changes)}")
        print(f"  Updated: {success}/{len(changes)}")
    elif not args.apply and changes:
        print(f"\n  Run with --apply to write changes to Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Classify companies as public/private/pre_ipo/acquired/unknown
using existing data only (no API calls).

Signals:
  - current_price > 0           → public
  - symbol in valid_tickers      → public
  - extra_data.Status keywords   → acquired/private/pre_ipo
  - IPO_expected present         → pre_ipo
  - invalid_companies.json       → private (all ticker methods failed)
  - else                         → unknown

Usage:
  python3 classify_listing_status.py            # dry-run (preview only)
  python3 classify_listing_status.py --apply    # write to Supabase
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

# Keywords in extra_data.Status that indicate listing status
ACQUIRED_KEYWORDS = ['acquired', 'übernommen', 'merged', 'delisted', 'bankrupt', 'insolvent']
PRIVATE_KEYWORDS = ['private', 'privat', 'not listed', 'nicht gelistet']
PRE_IPO_KEYWORDS = ['pre-ipo', 'pre ipo', 'ipo planned', 'ipo expected', 'ipo geplant', 'spac']

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


def classify(company: dict, blacklisted_ids: set) -> str:
    """Determine listing_status for a single company."""
    extra = company.get('extra_data') or {}
    company_id = company.get('id', '')

    # 1. Has a valid current price → public
    price = company.get('current_price')
    if price and float(price) > 0:
        return 'public'

    # 2. Has a price in extra_data
    ed_price = extra.get('Current_Price')
    if ed_price:
        try:
            if float(str(ed_price).replace(',', '.')) > 0:
                return 'public'
        except (ValueError, TypeError):
            pass

    # 3. Has a validated symbol → public
    symbol = company.get('symbol')
    if symbol and symbol.strip():
        return 'public'

    # 4. Check Status field for keywords
    status = str(extra.get('Status', '')).lower().strip()
    if status:
        for kw in ACQUIRED_KEYWORDS:
            if kw in status:
                return 'acquired'
        for kw in PRIVATE_KEYWORDS:
            if kw in status:
                return 'private'
        for kw in PRE_IPO_KEYWORDS:
            if kw in status:
                return 'pre_ipo'

    # 5. Has IPO_expected → pre_ipo
    ipo_expected = extra.get('IPO_expected') or extra.get('IPO Expected') or extra.get('IPO_Expected')
    if ipo_expected:
        return 'pre_ipo'

    # 6. In blacklist (all ticker methods failed) → private
    if company_id in blacklisted_ids:
        return 'private'

    return 'unknown'


def main():
    parser = argparse.ArgumentParser(description='Classify company listing status')
    parser.add_argument('--apply', action='store_true', help='Write results to Supabase')
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
    changes = []

    for company in companies:
        new_status = classify(company, blacklisted_ids)
        old_status = company.get('listing_status')
        results[new_status] += 1

        if new_status != old_status:
            changes.append({
                'id': company['id'],
                'name': company.get('name', '?'),
                'old': old_status,
                'new': new_status
            })

    # Report
    print(f"\n  Classification results:")
    for status, count in sorted(results.items(), key=lambda x: -x[1]):
        print(f"    {status:12s}: {count:5d}")
    print(f"    {'TOTAL':12s}: {sum(results.values()):5d}")
    print(f"\n  Changes needed: {len(changes)}")

    # Show sample changes
    if changes:
        print(f"\n  Sample changes (first 20):")
        for c in changes[:20]:
            print(f"    {c['name'][:40]:40s}  {str(c['old']):10s} → {c['new']}")

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

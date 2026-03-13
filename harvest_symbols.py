#!/usr/bin/env python3
"""
harvest_symbols.py - Copy symbol values from extra_data to the core symbol field.

Finds companies where `symbol` is NULL/empty but extra_data contains a symbol
under keys like 'Company_Symbol', 'Ticker', 'Symbol', or 'Company Symbol'.

Usage:
    python3 harvest_symbols.py              # dry-run (default)
    python3 harvest_symbols.py --apply      # actually update Supabase
"""

import os
import re
import sys
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# Keys to check in extra_data, in priority order
SYMBOL_KEYS = ['Company_Symbol', 'Ticker', 'Symbol', 'Company Symbol']

# Regex for valid ticker symbols: 1-10 chars, uppercase letters, digits, dots, hyphens
# Examples: AAPL, TSLA, BRK.B, RDS-A, 0700.HK, NKTX
TICKER_RE = re.compile(r'^[A-Z0-9][A-Z0-9.\-]{0,9}$')


def looks_like_ticker(val, company_name):
    """Return True if the value looks like a real ticker symbol, not a company name."""
    val = val.strip()
    # Must match ticker pattern
    if not TICKER_RE.match(val):
        return False
    # If it's the same as the company name (case-insensitive), it's not a ticker
    if val.lower() == company_name.lower().strip():
        return False
    return True


def find_harvestable(companies):
    """Find companies with empty symbol but a valid ticker in extra_data."""
    candidates = []
    skipped_names = []
    for c in companies:
        # Skip if symbol is already set
        symbol = (c.get('symbol') or '').strip()
        if symbol:
            continue

        extra = c.get('extra_data')
        if not extra or not isinstance(extra, dict):
            continue

        name = c.get('name', '(unknown)')

        # Check keys in priority order
        for key in SYMBOL_KEYS:
            val = extra.get(key)
            if val and isinstance(val, str) and val.strip():
                val = val.strip()
                if looks_like_ticker(val, name):
                    candidates.append({
                        'id': c['id'],
                        'name': name,
                        'source_key': key,
                        'symbol_value': val,
                    })
                else:
                    skipped_names.append({
                        'name': name,
                        'key': key,
                        'value': val,
                    })
                break

    return candidates, skipped_names


def main():
    parser = argparse.ArgumentParser(description='Harvest symbols from extra_data into core symbol field.')
    parser.add_argument('--apply', action='store_true', help='Actually apply updates (default is dry-run)')
    args = parser.parse_args()

    mode = 'APPLY' if args.apply else 'DRY-RUN'
    print(f"=== harvest_symbols.py [{mode}] ===\n")

    print("Fetching all companies from Supabase...")
    companies = supabase_helper.get_all_companies('id, name, symbol, isin, wkn, extra_data')
    print(f"Total companies: {len(companies)}")

    # Build set of symbols already in use
    existing_symbols = set()
    for c in companies:
        s = (c.get('symbol') or '').strip()
        if s:
            existing_symbols.add(s)

    candidates, skipped = find_harvestable(companies)

    # Filter out symbols already assigned to another company
    unique_candidates = []
    duplicate_symbols = []
    for c in candidates:
        if c['symbol_value'] in existing_symbols:
            duplicate_symbols.append(c)
        else:
            unique_candidates.append(c)

    print(f"Companies with valid ticker in extra_data: {len(candidates)}")
    print(f"  - Symbol already used by another company: {len(duplicate_symbols)}")
    print(f"  - Available to harvest: {len(unique_candidates)}")
    print(f"Skipped (value looks like name, not ticker): {len(skipped)}\n")

    if duplicate_symbols:
        print("Duplicate symbol conflicts (already assigned to another row):")
        for c in duplicate_symbols:
            print(f"  - {c['name'][:50]:<50s}  {c['symbol_value']} (already in use)")
        print()

    if not unique_candidates:
        print("Nothing to harvest -- all valid tickers are already assigned.")
        return

    # Show candidates
    print("Companies to update:")
    for i, c in enumerate(unique_candidates, 1):
        print(f"  {i:3d}. {c['name'][:50]:<50s}  <- {c['source_key']}: {c['symbol_value']}")

    print()

    if not args.apply:
        print(f"[DRY-RUN] Would update {len(unique_candidates)} companies. Run with --apply to execute.")
        return

    # Apply updates
    success = 0
    failed = 0
    for c in unique_candidates:
        ok = supabase_helper.update_company(c['id'], {'symbol': c['symbol_value']})
        if ok:
            success += 1
        else:
            failed += 1
            print(f"  FAILED: {c['name']} (id={c['id']})")

    print(f"\nDone. Updated: {success}, Failed: {failed}")


if __name__ == '__main__':
    main()

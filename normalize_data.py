#!/usr/bin/env python3
"""
Normalize string data in companies table:
  - Status → lowercase canonical map
  - Prio_Buy → integer 1-5 (promoted to prio_buy column)
  - Thier_Group / VIP → trim whitespace
  - Preserves originals in *_Original fields in extra_data

Usage:
  python3 normalize_data.py            # dry-run (preview only)
  python3 normalize_data.py --apply    # write to Supabase
"""

import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# Status normalization map (lowercase input → canonical value)
STATUS_MAP = {
    # Active/public
    'active': 'active',
    'aktiv': 'active',
    'listed': 'active',
    'gelistet': 'active',
    'public': 'active',
    # Watch
    'watch': 'watch',
    'watchlist': 'watch',
    'beobachten': 'watch',
    # Hold
    'hold': 'hold',
    'halten': 'hold',
    # Buy
    'buy': 'buy',
    'kaufen': 'buy',
    # Sell
    'sell': 'sell',
    'verkaufen': 'sell',
    'sold': 'sold',
    'verkauft': 'sold',
    # Inactive
    'inactive': 'inactive',
    'inaktiv': 'inactive',
    'paused': 'inactive',
    'pausiert': 'inactive',
    # Acquired/Merged
    'acquired': 'acquired',
    'übernommen': 'acquired',
    'merged': 'acquired',
    'fusioniert': 'acquired',
    # Delisted
    'delisted': 'delisted',
    # Bankrupt
    'bankrupt': 'bankrupt',
    'insolvent': 'bankrupt',
    'insolvenz': 'bankrupt',
}

# Prio_Buy normalization: extract integer 1-5 from various formats
PRIO_PATTERNS = [
    (re.compile(r'^(\d)$'), lambda m: int(m.group(1))),           # "3"
    (re.compile(r'^(\d)\s*[-–/]'), lambda m: int(m.group(1))),    # "3 - high"
    (re.compile(r'prio\s*(\d)', re.I), lambda m: int(m.group(1))),# "Prio 2"
]


def normalize_status(raw: str) -> str | None:
    """Normalize Status string to canonical value."""
    if not raw or not raw.strip():
        return None
    key = raw.strip().lower()
    return STATUS_MAP.get(key)  # None if not in map (keep as-is)


def normalize_prio_buy(raw) -> int | None:
    """Extract integer 1-5 from Prio_Buy value."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # Direct integer
    try:
        val = int(float(s))
        if 1 <= val <= 5:
            return val
    except (ValueError, TypeError):
        pass

    # Pattern matching
    for pattern, extractor in PRIO_PATTERNS:
        m = pattern.search(s)
        if m:
            val = extractor(m)
            if 1 <= val <= 5:
                return val

    return None


def compute_changes(company: dict) -> dict | None:
    """Compute normalized updates for a single company. Returns update dict or None."""
    extra = company.get('extra_data') or {}
    updates = {}
    extra_updates = {}
    changed = False

    # 1. Normalize Status
    raw_status = extra.get('Status')
    if raw_status and str(raw_status).strip():
        normalized = normalize_status(str(raw_status))
        if normalized and normalized != str(raw_status).strip():
            # Preserve original
            if 'Status_Original' not in extra:
                extra_updates['Status_Original'] = str(raw_status).strip()
            extra_updates['Status'] = normalized
            changed = True

    # 2. Normalize Prio_Buy → prio_buy column
    raw_prio = extra.get('Prio_Buy') or extra.get('Prio Buy')
    current_prio = company.get('prio_buy')
    if raw_prio is not None:
        normalized_prio = normalize_prio_buy(raw_prio)
        if normalized_prio and normalized_prio != current_prio:
            updates['prio_buy'] = normalized_prio
            if 'Prio_Buy_Original' not in extra:
                prio_key = 'Prio_Buy' if 'Prio_Buy' in extra else 'Prio Buy'
                extra_updates[f'{prio_key}_Original'] = str(raw_prio)
            changed = True

    # 3. Trim Thier_Group whitespace
    raw_tg = extra.get('Thier_Group')
    if raw_tg and str(raw_tg).strip() != str(raw_tg):
        extra_updates['Thier_Group'] = str(raw_tg).strip()
        changed = True
    # Also check promoted column
    col_tg = company.get('thier_group')
    if col_tg and str(col_tg).strip() != str(col_tg):
        updates['thier_group'] = str(col_tg).strip()
        changed = True

    # 4. Trim VIP whitespace
    raw_vip = extra.get('VIP')
    if raw_vip and str(raw_vip).strip() != str(raw_vip):
        extra_updates['VIP'] = str(raw_vip).strip()
        changed = True
    col_vip = company.get('vip')
    if col_vip and str(col_vip).strip() != str(col_vip):
        updates['vip'] = str(col_vip).strip()
        changed = True

    if not changed:
        return None

    # Merge extra_data updates
    if extra_updates:
        merged_extra = dict(extra)
        merged_extra.update(extra_updates)
        updates['extra_data'] = merged_extra

    return updates


def main():
    parser = argparse.ArgumentParser(description='Normalize company data strings')
    parser.add_argument('--apply', action='store_true', help='Write results to Supabase')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  NORMALIZE DATA")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print("=" * 70)

    # Load data
    print("\n  Loading companies...")
    try:
        companies = supabase_helper.get_all_companies(
            'id, name, extra_data, prio_buy, thier_group, vip'
        )
    except Exception:
        # prio_buy column may not exist yet — fetch without it
        print("  Note: prio_buy column not found, fetching without it")
        companies = supabase_helper.get_all_companies(
            'id, name, extra_data, thier_group, vip'
        )
    print(f"  Loaded {len(companies)} companies")

    # Compute changes
    all_changes = []
    stats = Counter()

    for company in companies:
        updates = compute_changes(company)
        if updates:
            all_changes.append({
                'id': company['id'],
                'name': company.get('name', '?'),
                'updates': updates
            })
            if 'prio_buy' in updates:
                stats['prio_buy'] += 1
            if 'extra_data' in updates:
                ed = updates['extra_data']
                if 'Status' in ed and ed.get('Status') != (company.get('extra_data') or {}).get('Status'):
                    stats['status'] += 1
                if 'Thier_Group' in ed:
                    stats['thier_group_trim'] += 1
                if 'VIP' in ed:
                    stats['vip_trim'] += 1
            if 'thier_group' in updates:
                stats['thier_group_col'] += 1
            if 'vip' in updates:
                stats['vip_col'] += 1

    # Report
    print(f"\n  Changes needed: {len(all_changes)}")
    for key, count in sorted(stats.items()):
        print(f"    {key:20s}: {count}")

    # Sample
    if all_changes:
        print(f"\n  Sample changes (first 10):")
        for c in all_changes[:10]:
            keys = list(c['updates'].keys())
            print(f"    {c['name'][:40]:40s}  fields: {', '.join(keys)}")

    # Apply
    if args.apply and all_changes:
        print(f"\n  Applying {len(all_changes)} updates...")
        success = 0
        for i, c in enumerate(all_changes):
            if supabase_helper.update_company(c['id'], c['updates']):
                success += 1
            if (i + 1) % 100 == 0:
                print(f"    ... {i + 1}/{len(all_changes)}")
        print(f"  Updated: {success}/{len(all_changes)}")
    elif not args.apply and all_changes:
        print(f"\n  Run with --apply to write changes to Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Source Normalizer — Cleans up extra_data.Source field using source_mapping.json.
Usage:
  python3 normalize_sources.py           # Dry-run (default, shows changes)
  python3 normalize_sources.py --apply   # Actually apply changes
"""

import json
import os
import sys
from collections import Counter
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper


def load_mapping():
    path = os.path.join(SCRIPT_DIR, 'source_mapping.json')
    with open(path, 'r') as f:
        mapping = json.load(f)
    # Remove comments
    mapping.pop('_comment', None)
    return mapping


def main():
    dry_run = '--apply' not in sys.argv
    mapping = load_mapping()

    print("=" * 70)
    print(f"  SOURCE NORMALIZER {'(DRY RUN)' if dry_run else '(APPLYING)'}")
    print("=" * 70)
    print(f"  Mapping: {len(mapping)} entries")

    companies = supabase_helper.get_all_companies('id, name, extra_data')
    print(f"  Companies: {len(companies)}")

    changes = []
    unmapped = Counter()
    canonical_counts = Counter()

    for c in companies:
        ed = c.get('extra_data') or {}
        raw_source = ed.get('Source', '')
        if not raw_source:
            continue

        if raw_source in mapping:
            normalized = mapping[raw_source]
            if normalized and normalized != raw_source:
                changes.append({
                    'id': c['id'],
                    'name': c.get('name', ''),
                    'old': raw_source,
                    'new': normalized,
                    'extra_data': ed,
                })
            canonical_counts[normalized or '(empty)'] += 1
        else:
            unmapped[raw_source] += 1
            canonical_counts[raw_source] += 1

    print(f"\n  Changes needed: {len(changes)}")
    print(f"  Unmapped sources: {len(unmapped)}")

    if unmapped:
        print(f"\n  ⚠ Unmapped sources (add to source_mapping.json):")
        for src, count in unmapped.most_common():
            print(f"    {count:4d}  '{src}'")

    print(f"\n  Canonical source distribution:")
    for src, count in canonical_counts.most_common(30):
        print(f"    {count:4d}  {src}")

    if not changes:
        print("\n  No changes needed.")
        return

    print(f"\n  Sample changes (first 20):")
    for ch in changes[:20]:
        print(f"    '{ch['old']}' → '{ch['new']}'  ({ch['name'][:40]})")

    if dry_run:
        print(f"\n  Run with --apply to execute {len(changes)} updates.")
        return

    # Apply changes
    print(f"\n  Applying {len(changes)} updates...")
    success = 0
    failed = 0

    for i, ch in enumerate(changes):
        ed = ch['extra_data'].copy()
        ed['Source'] = ch['new']
        ed['Source_Original'] = ch['old']  # Keep original for reference

        if supabase_helper.update_company(ch['id'], {'extra_data': ed}):
            success += 1
            if success % 50 == 0:
                print(f"    ... {success}/{len(changes)} done")
        else:
            failed += 1

    print(f"\n  Done: {success} updated, {failed} failed")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Promote extra_data JSONB fields to real PostgreSQL columns.

Copies Thier_Group, VIP, Industry, Leverage from extra_data to
the new thier_group, vip, industry, leverage columns.

Usage:
  python3 promote_jsonb_fields.py           # dry-run (default)
  python3 promote_jsonb_fields.py --apply   # actually write to DB
"""

import os
import sys
import argparse
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# Mapping: extra_data key -> new column name
FIELD_MAPPING = {
    'Thier_Group': 'thier_group',
    'VIP': 'vip',
    'Industry': 'industry',
    'Leverage': 'leverage',
}


def main():
    parser = argparse.ArgumentParser(description='Promote extra_data JSONB fields to real columns')
    parser.add_argument('--apply', action='store_true', help='Actually write changes (default: dry-run)')
    args = parser.parse_args()

    dry_run = not args.apply

    if dry_run:
        print("\n  DRY RUN — no changes will be written. Use --apply to write.\n")
    else:
        print("\n  APPLY MODE — writing changes to database.\n")

    # Fetch all companies
    print("  Fetching all companies...")
    companies = supabase_helper.get_all_companies('id, name, extra_data, thier_group, vip, industry, leverage')
    print(f"  Found {len(companies)} companies.\n")

    stats = {field: {'total': 0, 'already_set': 0, 'promoted': 0, 'no_value': 0}
             for field in FIELD_MAPPING.values()}
    update_count = 0
    error_count = 0

    for company in companies:
        extra = company.get('extra_data') or {}
        update_data = {}

        for ed_key, col_name in FIELD_MAPPING.items():
            ed_value = extra.get(ed_key)
            current_value = company.get(col_name)

            if ed_value is not None and str(ed_value).strip():
                stats[col_name]['total'] += 1
                if current_value is not None and str(current_value).strip():
                    stats[col_name]['already_set'] += 1
                else:
                    stats[col_name]['promoted'] += 1
                    update_data[col_name] = str(ed_value).strip()
            else:
                stats[col_name]['no_value'] += 1

        if update_data:
            if dry_run:
                update_count += 1
            else:
                if supabase_helper.update_company(company['id'], update_data):
                    update_count += 1
                    if update_count % 100 == 0:
                        print(f"    ... {update_count} companies updated")
                else:
                    error_count += 1

    # Print summary
    print("\n" + "=" * 60)
    print("  PROMOTION SUMMARY")
    print("=" * 60)
    for col_name, s in stats.items():
        print(f"\n  {col_name}:")
        print(f"    Has value in extra_data: {s['total']}")
        print(f"    Already set in column:   {s['already_set']}")
        print(f"    To promote:              {s['promoted']}")
        print(f"    No value in extra_data:  {s['no_value']}")

    print(f"\n  Companies {'to update' if dry_run else 'updated'}: {update_count}")
    if error_count:
        print(f"  Errors: {error_count}")

    if dry_run:
        print("\n  This was a DRY RUN. Run with --apply to write changes.")
    else:
        print("\n  Done! Changes applied successfully.")

    return 0 if error_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())

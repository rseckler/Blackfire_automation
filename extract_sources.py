#!/usr/bin/env python3
"""Extract and count all distinct Source values from companies extra_data."""

import sys
import os
import json
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supabase_helper import get_all_companies

def main():
    print("Fetching all companies...")
    companies = get_all_companies('id, extra_data')
    print(f"Total companies: {len(companies)}")

    sources = []
    no_source = 0
    for c in companies:
        ed = c.get('extra_data') or {}
        src = ed.get('Source', '')
        if src and str(src).strip():
            sources.append(str(src).strip())
        else:
            no_source += 1

    counter = collections.Counter(sources)
    print(f"Companies with Source: {len(sources)}")
    print(f"Companies without Source: {no_source}")
    print(f"Unique source values: {len(counter)}")
    print()
    print("=" * 80)
    print(f"{'Count':>5}  Source Value")
    print("=" * 80)
    for src, count in sorted(counter.items(), key=lambda x: (-x[1], x[0])):
        print(f"{count:5d}  {src}")

    # Also dump to JSON for analysis
    output = {src: count for src, count in sorted(counter.items(), key=lambda x: (-x[1], x[0]))}
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'source_values_raw.json'), 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nRaw values saved to source_values_raw.json")

if __name__ == '__main__':
    main()

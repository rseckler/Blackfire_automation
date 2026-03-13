#!/usr/bin/env python3
"""
Complete Morning Sync
1. Excel -> Supabase (sync_final.py)
1.5. Normalize data (normalize_data.py) — every sync
2. ISIN/WKN Research (isin_wkn_updater.py)
3. Classify listing status (classify_listing_status.py) — weekly (Mondays)
"""

import subprocess
import sys
import os
from datetime import datetime
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

print("=" * 70)
print("  MORNING SYNC - COMPLETE")
print(f"   Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# Step 1: Excel -> Supabase Sync
print("\n  Step 1: Excel -> Supabase Sync...")
print("-" * 70)
result1 = subprocess.run(
    [sys.executable, os.path.join(SCRIPT_DIR, 'sync_final.py')],
    capture_output=False
)

if result1.returncode != 0:
    print("\n  Excel sync failed!")
    try:
        import supabase_helper
        supabase_helper.send_alert_email(
            "Morning Sync FAILED — Excel Sync",
            f"Excel → Supabase sync failed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.\n"
            f"Exit code: {result1.returncode}\n\n"
            "Check logs: tail -f ~/Blackfire_automation/sync_cron.log"
        )
    except Exception:
        pass
    exit(1)

# Step 1.5: Normalize data (every sync)
print("\n" + "=" * 70)
print("  Step 1.5: Normalize Data...")
print("-" * 70)
result1_5 = subprocess.run(
    [sys.executable, os.path.join(SCRIPT_DIR, 'normalize_data.py'), '--apply'],
    capture_output=False
)
if result1_5.returncode != 0:
    print("\n  Data normalization had issues (non-critical)")

# Step 2: ISIN/WKN Research
print("\n" + "=" * 70)
print("  Step 2: ISIN/WKN Research...")
print("-" * 70)
result2 = subprocess.run(
    [sys.executable, os.path.join(SCRIPT_DIR, 'isin_wkn_updater.py')],
    capture_output=False
)

if result2.returncode != 0:
    print("\n  ISIN/WKN update had issues (non-critical)")

# Step 3: Classify listing status (weekly — Mondays only)
if datetime.now().weekday() == 0:  # Monday
    print("\n" + "=" * 70)
    print("  Step 3: Classify Listing Status (weekly)...")
    print("-" * 70)
    result3 = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, 'classify_listing_status.py'), '--apply'],
        capture_output=False
    )
    if result3.returncode != 0:
        print("\n  Listing status classification had issues (non-critical)")
else:
    print("\n  Step 3: Classify Listing Status — skipped (runs on Mondays)")

print("\n" + "=" * 70)
print("  MORNING SYNC COMPLETE!")
print(f"   Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

exit(0)

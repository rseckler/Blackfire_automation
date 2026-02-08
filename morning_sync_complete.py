#!/usr/bin/env python3
"""
Complete Morning Sync
1. Excel → Notion (sync_final.py)
2. ISIN/WKN Research (isin_wkn_updater.py)
"""

import subprocess
import sys
import os
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

print("=" * 70)
print("🌅 MORNING SYNC - COMPLETE")
print(f"   Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# Step 1: Excel → Notion Sync
print("\n📊 Step 1: Excel → Notion Sync...")
print("-" * 70)
result1 = subprocess.run(
    [sys.executable, os.path.join(SCRIPT_DIR, 'sync_final.py')],
    capture_output=False
)

if result1.returncode != 0:
    print("\n❌ Excel sync failed!")
    exit(1)

# Step 2: ISIN/WKN Research
print("\n" + "=" * 70)
print("🔍 Step 2: ISIN/WKN Research...")
print("-" * 70)
result2 = subprocess.run(
    [sys.executable, os.path.join(SCRIPT_DIR, 'isin_wkn_updater.py')],
    capture_output=False
)

if result2.returncode != 0:
    print("\n⚠️  ISIN/WKN update had issues (non-critical)")

print("\n" + "=" * 70)
print("✅ MORNING SYNC COMPLETE!")
print(f"   Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

exit(0)

#!/usr/bin/env python3
"""
AI-powered data enrichment using Claude Haiku.
Fills missing: Profile, Sector, Sector_Specific, Country, Competitors.
Batches 10 companies per API call to minimize cost (~$1.70 total for ~1600 companies).

Usage:
  python3 ai_data_enrichment.py                    # dry-run, show what's missing
  python3 ai_data_enrichment.py --apply             # enrich and write to Supabase
  python3 ai_data_enrichment.py --apply --limit 50  # enrich first 50 companies only
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

try:
    from anthropic import Anthropic
except ImportError:
    print("Installing anthropic...")
    os.system(f"{sys.executable} -m pip install anthropic")
    from anthropic import Anthropic

# Fields we want to enrich
TARGET_FIELDS = ['Profile', 'Sector', 'Sector_Specific', 'Country', 'Competitors']

BATCH_SIZE = 10  # Companies per API call
RATE_LIMIT_DELAY = 1.0  # Seconds between API calls


def get_missing_companies(companies: list) -> list:
    """Find companies missing at least one target field."""
    missing = []
    for company in companies:
        extra = company.get('extra_data') or {}
        missing_fields = []
        for field in TARGET_FIELDS:
            val = extra.get(field)
            if not val or str(val).strip() in ('', 'None', 'null', 'nan', 'N/A', '-'):
                missing_fields.append(field)
        if missing_fields:
            missing.append({
                'company': company,
                'missing_fields': missing_fields
            })
    return missing


def build_prompt(batch: list) -> str:
    """Build a prompt for Claude Haiku to enrich a batch of companies."""
    companies_text = []
    for i, item in enumerate(batch):
        company = item['company']
        extra = company.get('extra_data') or {}
        name = company.get('name', '?')
        symbol = company.get('symbol') or extra.get('Company Symbol') or ''
        isin = company.get('isin') or ''
        wkn = company.get('wkn') or ''
        industry = extra.get('Industry') or ''
        missing = item['missing_fields']

        companies_text.append(
            f"{i+1}. \"{name}\" (Symbol: {symbol}, ISIN: {isin}, WKN: {wkn}, Industry: {industry})\n"
            f"   Missing: {', '.join(missing)}"
        )

    return f"""Fill in the missing fields for these companies. Return ONLY a JSON array with one object per company.

Fields to fill (only those marked as missing):
- Profile: 1-2 sentence company description (in English)
- Sector: Broad sector (e.g., Technology, Healthcare, Energy, Finance, Consumer, Industrial, Materials)
- Sector_Specific: Specific sub-sector (e.g., Semiconductor, Biotech, Solar Energy, Crypto Exchange)
- Country: Country of headquarters (ISO 2-letter code, e.g., US, DE, CN, IL)
- Competitors: 3-5 main competitors, comma-separated

Companies:
{chr(10).join(companies_text)}

Return a JSON array. Each object must have:
- "index": company number (1-based)
- Only the missing fields that you can confidently fill

Example response:
[
  {{"index": 1, "Profile": "...", "Sector": "Technology", "Sector_Specific": "AI Infrastructure", "Country": "US", "Competitors": "NVIDIA, AMD, Intel"}},
  {{"index": 2, "Sector": "Healthcare", "Country": "DE"}}
]

If you cannot determine a field with reasonable confidence, omit it. Return ONLY the JSON array, no other text."""


def enrich_batch(client: Anthropic, batch: list) -> list:
    """Call Claude Haiku to enrich a batch of companies."""
    prompt = build_prompt(batch)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()

        # Extract JSON from response (handle markdown code blocks)
        if text.startswith('```'):
            text = text.split('\n', 1)[1]
            text = text.rsplit('```', 1)[0]

        results = json.loads(text)
        return results

    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}")
        return []
    except Exception as e:
        print(f"    API error: {e}")
        return []


def apply_enrichment(company_id: str, extra_data: dict, enrichment: dict) -> dict | None:
    """Build update dict from enrichment results."""
    updates = {}
    extra_updates = {}

    for field in TARGET_FIELDS:
        if field in enrichment:
            val = str(enrichment[field]).strip()
            if val and val not in ('', 'None', 'null'):
                extra_updates[field] = val

    if not extra_updates:
        return None

    # Mark enrichment source and timestamp
    extra_updates['AI_Enriched_At'] = datetime.now().isoformat()
    extra_updates['AI_Enriched_Fields'] = list(extra_updates.keys())

    merged = dict(extra_data or {})
    merged.update(extra_updates)
    updates['extra_data'] = merged

    return updates


def main():
    parser = argparse.ArgumentParser(description='AI-powered company data enrichment')
    parser.add_argument('--apply', action='store_true', help='Call API and write to Supabase')
    parser.add_argument('--limit', type=int, default=0, help='Limit number of companies to process')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  AI DATA ENRICHMENT (Claude Haiku)")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (analysis only)'}")
    if args.limit:
        print(f"  Limit: {args.limit} companies")
    print("=" * 70)

    # Check API key
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if args.apply and not api_key:
        print("\n  ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    # Load companies
    print("\n  Loading companies...")
    companies = supabase_helper.get_all_companies(
        'id, name, symbol, isin, wkn, extra_data'
    )
    print(f"  Loaded {len(companies)} companies")

    # Find missing
    missing = get_missing_companies(companies)
    print(f"  Companies with missing fields: {len(missing)}")

    # Stats on what's missing
    from collections import Counter
    field_counts = Counter()
    for item in missing:
        for field in item['missing_fields']:
            field_counts[field] += 1
    print(f"\n  Missing field counts:")
    for field, count in sorted(field_counts.items(), key=lambda x: -x[1]):
        print(f"    {field:20s}: {count}")

    if not args.apply:
        # Cost estimate
        batches = (len(missing) + BATCH_SIZE - 1) // BATCH_SIZE
        est_cost = batches * 0.01  # ~$0.01 per Haiku batch
        print(f"\n  Estimated cost: ~${est_cost:.2f} ({batches} batches × ~$0.01)")
        print(f"  Run with --apply to enrich via Claude Haiku")
        return

    # Apply mode
    client = Anthropic(api_key=api_key)

    if args.limit:
        missing = missing[:args.limit]

    total_enriched = 0
    total_fields = 0
    batches = [missing[i:i + BATCH_SIZE] for i in range(0, len(missing), BATCH_SIZE)]

    print(f"\n  Processing {len(missing)} companies in {len(batches)} batches...")

    for batch_idx, batch in enumerate(batches):
        print(f"\n  Batch {batch_idx + 1}/{len(batches)} ({len(batch)} companies)...")

        results = enrich_batch(client, batch)

        if not results:
            print(f"    No results returned")
            time.sleep(RATE_LIMIT_DELAY)
            continue

        # Map results back to companies
        for result in results:
            idx = result.get('index', 0) - 1
            if 0 <= idx < len(batch):
                company = batch[idx]['company']
                extra = company.get('extra_data') or {}
                updates = apply_enrichment(company['id'], extra, result)
                if updates:
                    if supabase_helper.update_company(company['id'], updates):
                        total_enriched += 1
                        total_fields += len([f for f in TARGET_FIELDS if f in result])

        print(f"    Enriched so far: {total_enriched} companies, {total_fields} fields")
        time.sleep(RATE_LIMIT_DELAY)

    print(f"\n  COMPLETE: Enriched {total_enriched} companies, {total_fields} total fields")


if __name__ == '__main__':
    main()

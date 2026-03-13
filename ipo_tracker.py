#!/usr/bin/env python3
"""
IPO Tracker — Monitor IPO calendars and match against companies in Supabase.

Sources:
  1. Finnhub API (free tier) — structured IPO calendar data
  2. Brave Search API (fallback) — "upcoming IPO 2026" search results
  3. Nasdaq IPO calendar page (additional source)

Matches IPO candidates against companies table by fuzzy name matching,
with special focus on companies with listing_status = 'private' or 'pre_ipo'.
Stores matched events in company_events table (event_type: 'ipo').

Usage:
  python3 ipo_tracker.py              # dry-run (preview only)
  python3 ipo_tracker.py --apply      # write to Supabase
  python3 ipo_tracker.py --days 90    # look ahead 90 days (default: 60)
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Optional
from dotenv import load_dotenv

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', '')
BRAVE_API_KEY = os.getenv('BRAVE_API_KEY', '')

# Minimum fuzzy-match ratio to consider a match (0.0 – 1.0)
MATCH_THRESHOLD = 0.70
# Higher threshold for short names (<=5 chars) to avoid false positives
SHORT_NAME_THRESHOLD = 0.90

# Words to strip before comparing names
STRIP_SUFFIXES = [
    'inc', 'inc.', 'corp', 'corp.', 'ltd', 'ltd.', 'limited',
    'plc', 'ag', 'se', 'gmbh', 'co', 'co.', 'group', 'holdings',
    'holding', 'technologies', 'technology', 'tech', 'therapeutics',
    'pharmaceuticals', 'pharma', 'biosciences', 'biotech',
    'entertainment', 'international', 'global', 'solutions',
    'the', 'de', 'sa', 'nv', 'bv', 'ab', 'oy', 'as',
]


# ---------------------------------------------------------------------------
# Name normalization for fuzzy matching
# ---------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """Normalize a company name for comparison."""
    if not name:
        return ''
    n = name.lower().strip()
    # Remove parenthetical suffixes like "(DE)" or "(US)"
    n = re.sub(r'\s*\(.*?\)\s*', ' ', n)
    # Remove punctuation
    n = re.sub(r'[^\w\s]', ' ', n)
    # Split into words and remove common suffixes
    words = [w for w in n.split() if w not in STRIP_SUFFIXES]
    return ' '.join(words).strip()


def fuzzy_match_score(name_a: str, name_b: str) -> float:
    """Return similarity ratio between two normalized names."""
    na = normalize_name(name_a)
    nb = normalize_name(name_b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


# ---------------------------------------------------------------------------
# Source 1: Finnhub IPO Calendar
# ---------------------------------------------------------------------------
def fetch_finnhub_ipos(days_ahead: int = 60) -> list[dict]:
    """Fetch upcoming IPOs from Finnhub free API."""
    if not FINNHUB_API_KEY:
        print("  [Finnhub] Skipped — FINNHUB_API_KEY not set")
        return []

    today = datetime.now().date()
    # Also look 30 days back to catch recent IPOs
    from_date = (today - timedelta(days=30)).isoformat()
    to_date = (today + timedelta(days=days_ahead)).isoformat()

    url = 'https://finnhub.io/api/v1/calendar/ipo'
    params = {
        'from': from_date,
        'to': to_date,
        'token': FINNHUB_API_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        ipos = data.get('ipoCalendar', [])
        print(f"  [Finnhub] Fetched {len(ipos)} IPO entries ({from_date} to {to_date})")
        results = []
        for ipo in ipos:
            results.append({
                'name': ipo.get('name', ''),
                'symbol': ipo.get('symbol', ''),
                'date': ipo.get('date', ''),
                'exchange': ipo.get('exchange', ''),
                'price_range': f"{ipo.get('priceRangeLow', '')}-{ipo.get('priceRangeHigh', '')}"
                              if ipo.get('priceRangeLow') else '',
                'shares': ipo.get('numberOfShares', ''),
                'status': ipo.get('status', ''),
                'source': 'finnhub',
            })
        return results
    except requests.RequestException as e:
        print(f"  [Finnhub] Error: {e}")
        return []


# ---------------------------------------------------------------------------
# Source 2: Brave Search (fallback)
# ---------------------------------------------------------------------------
def fetch_brave_ipos() -> list[dict]:
    """Search for upcoming IPOs via Brave Search API."""
    if not BRAVE_API_KEY:
        print("  [Brave] Skipped — BRAVE_API_KEY not set")
        return []

    queries = [
        'upcoming IPO 2026 stock market',
        'IPO calendar 2026 new listings',
    ]
    results = []
    seen_names = set()

    for query in queries:
        try:
            resp = requests.get(
                'https://api.search.brave.com/res/v1/web/search',
                params={'q': query, 'count': 10},
                headers={'X-Subscription-Token': BRAVE_API_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get('web', {}).get('results', []):
                title = item.get('title', '')
                description = item.get('description', '')
                url = item.get('url', '')
                # Extract potential company names from snippets
                # Look for patterns like "CompanyName IPO" or "CompanyName plans to go public"
                text = f"{title} {description}"
                ipo_patterns = [
                    r'([A-Z][A-Za-z\s&\.]+?)\s+(?:IPO|goes?\s+public|listing|to\s+list)',
                    r'(?:IPO|listing)\s+(?:of|for|by)\s+([A-Z][A-Za-z\s&\.]+?)(?:\s|,|\.)',
                ]
                for pattern in ipo_patterns:
                    matches = re.findall(pattern, text)
                    for match in matches:
                        name = match.strip()
                        if len(name) > 2 and name.lower() not in seen_names:
                            seen_names.add(name.lower())
                            results.append({
                                'name': name,
                                'symbol': '',
                                'date': '',
                                'exchange': '',
                                'price_range': '',
                                'shares': '',
                                'status': 'rumored',
                                'source': f'brave:{url[:100]}',
                            })

            time.sleep(1)  # Politeness between queries
        except requests.RequestException as e:
            print(f"  [Brave] Error for '{query}': {e}")

    print(f"  [Brave] Extracted {len(results)} IPO candidates from search results")
    return results


# ---------------------------------------------------------------------------
# Source 3: Nasdaq IPO calendar
# ---------------------------------------------------------------------------
def fetch_nasdaq_ipos() -> list[dict]:
    """Scrape Nasdaq IPO calendar page."""
    url = 'https://api.nasdaq.com/api/ipo/calendar'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }

    results = []
    for date_param in ['upcoming', 'priced', 'filed']:
        try:
            params = {'type': date_param}
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                print(f"  [Nasdaq] {date_param}: HTTP {resp.status_code}")
                continue
            data = resp.json()
            rows = data.get('data', {}).get('rows', []) or []
            for row in rows:
                name = row.get('companyName', '') or row.get('dealID', '')
                results.append({
                    'name': name,
                    'symbol': row.get('proposedTickerSymbol', '') or '',
                    'date': row.get('expectedPriceDate', '') or row.get('pricedDate', '') or '',
                    'exchange': row.get('proposedExchange', '') or 'NASDAQ',
                    'price_range': row.get('proposedSharePrice', '') or '',
                    'shares': row.get('sharesOffered', '') or '',
                    'status': date_param,
                    'source': 'nasdaq',
                })
            print(f"  [Nasdaq] {date_param}: {len(rows)} entries")
            time.sleep(0.5)
        except requests.RequestException as e:
            print(f"  [Nasdaq] Error ({date_param}): {e}")
        except (ValueError, KeyError) as e:
            print(f"  [Nasdaq] Parse error ({date_param}): {e}")

    print(f"  [Nasdaq] Total: {len(results)} IPO entries")
    return results


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------
def match_ipos_to_companies(ipo_entries: list[dict], companies: list[dict]) -> list[dict]:
    """Match IPO entries against companies in the database using fuzzy name matching.

    Returns list of dicts with keys: ipo, company, score
    """
    matches = []

    # Build lookup structures
    # Index by normalized name for fast pre-filtering
    company_names = {}
    for c in companies:
        name = c.get('name', '')
        if name:
            company_names[c['id']] = {
                'normalized': normalize_name(name),
                'original': name,
                'company': c,
            }

    # Also index by symbol for exact symbol matching
    company_symbols = {}
    for c in companies:
        sym = (c.get('symbol') or '').upper().strip()
        if sym:
            company_symbols[sym] = c

    for ipo in ipo_entries:
        ipo_name = ipo.get('name', '')
        ipo_symbol = (ipo.get('symbol') or '').upper().strip()
        if not ipo_name and not ipo_symbol:
            continue

        best_match = None
        best_score = 0.0

        # 1. Exact symbol match
        if ipo_symbol and ipo_symbol in company_symbols:
            best_match = company_symbols[ipo_symbol]
            best_score = 1.0
        else:
            # 2. Fuzzy name match
            ipo_normalized = normalize_name(ipo_name)
            if not ipo_normalized:
                continue

            for cid, cdata in company_names.items():
                score = SequenceMatcher(None, ipo_normalized, cdata['normalized']).ratio()

                # Use higher threshold for short names
                threshold = SHORT_NAME_THRESHOLD if len(ipo_normalized) <= 5 else MATCH_THRESHOLD

                if score > best_score and score >= threshold:
                    best_score = score
                    best_match = cdata['company']

        if best_match and best_score >= MATCH_THRESHOLD:
            matches.append({
                'ipo': ipo,
                'company': best_match,
                'score': best_score,
            })

    return matches


def prioritize_private_companies(
    matches: list[dict],
    private_companies: list[dict],
    ipo_entries: list[dict],
) -> list[dict]:
    """Give extra attention to private/pre_ipo companies.

    For private companies that didn't match any IPO entry,
    still report them as 'no match found' for awareness.
    """
    matched_company_ids = {m['company']['id'] for m in matches}

    unmatched_private = []
    for c in private_companies:
        if c['id'] not in matched_company_ids:
            # Check if any IPO entry is a weak match (above 0.5)
            best_ipo = None
            best_score = 0.0
            for ipo in ipo_entries:
                score = fuzzy_match_score(c.get('name', ''), ipo.get('name', ''))
                if score > best_score:
                    best_score = score
                    best_ipo = ipo

            if best_ipo and best_score >= 0.5:
                unmatched_private.append({
                    'company': c,
                    'ipo': best_ipo,
                    'score': best_score,
                    'weak': True,
                })

    return unmatched_private


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def get_existing_ipo_events(company_ids: list[str]) -> set[str]:
    """Get existing IPO events from company_events to avoid duplicates.

    Returns set of 'company_id|event_date' strings for dedup.
    """
    if not company_ids:
        return set()

    client = supabase_helper.get_client()
    existing = set()

    # Paginate in chunks of 50 IDs to avoid URL length issues
    for i in range(0, len(company_ids), 50):
        chunk = company_ids[i:i + 50]
        try:
            resp = client.table('company_events') \
                .select('company_id, event_date') \
                .eq('event_type', 'ipo') \
                .in_('company_id', chunk) \
                .execute()
            for row in resp.data:
                key = f"{row['company_id']}|{row.get('event_date', '')}"
                existing.add(key)
        except Exception as e:
            print(f"  Warning: Could not check existing events: {e}")

    return existing


# ---------------------------------------------------------------------------
# Insert events
# ---------------------------------------------------------------------------
def build_event_row(match: dict) -> dict:
    """Build a company_events insert row from a match."""
    ipo = match['ipo']
    company = match['company']

    # Build description with available details
    parts = []
    if ipo.get('exchange'):
        parts.append(f"Exchange: {ipo['exchange']}")
    if ipo.get('price_range') and ipo['price_range'] != '-':
        parts.append(f"Price range: {ipo['price_range']}")
    if ipo.get('shares'):
        parts.append(f"Shares: {ipo['shares']}")
    if ipo.get('status'):
        parts.append(f"Status: {ipo['status']}")
    parts.append(f"Match score: {match['score']:.0%}")

    description = ' | '.join(parts) if parts else None

    return {
        'company_id': company['id'],
        'event_type': 'ipo',
        'event_date': ipo.get('date') or None,
        'description': description,
        'source': ipo.get('source', 'unknown'),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='IPO Tracker — match IPO calendars to Supabase companies')
    parser.add_argument('--apply', action='store_true', help='Write matches to company_events (default: dry-run)')
    parser.add_argument('--days', type=int, default=60, help='Days to look ahead (default: 60)')
    args = parser.parse_args()

    dry_run = not args.apply
    mode = 'DRY-RUN' if dry_run else 'APPLY'

    print(f"\n{'='*60}")
    print(f"  IPO Tracker — {mode} mode")
    print(f"  Look-ahead: {args.days} days")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    start_time = datetime.now()

    # -----------------------------------------------------------------------
    # Step 1: Fetch IPO data from all sources
    # -----------------------------------------------------------------------
    print("Step 1: Fetching IPO calendars...\n")

    all_ipos = []

    finnhub_ipos = fetch_finnhub_ipos(days_ahead=args.days)
    all_ipos.extend(finnhub_ipos)

    nasdaq_ipos = fetch_nasdaq_ipos()
    all_ipos.extend(nasdaq_ipos)

    brave_ipos = fetch_brave_ipos()
    all_ipos.extend(brave_ipos)

    # Deduplicate IPO entries by normalized name
    seen = {}
    unique_ipos = []
    for ipo in all_ipos:
        key = normalize_name(ipo.get('name', ''))
        if key and key not in seen:
            seen[key] = True
            unique_ipos.append(ipo)

    print(f"\n  Total unique IPO entries: {len(unique_ipos)} "
          f"(Finnhub: {len(finnhub_ipos)}, Nasdaq: {len(nasdaq_ipos)}, Brave: {len(brave_ipos)})\n")

    if not unique_ipos:
        print("  No IPO data found from any source. Check API keys and network.")
        print(f"\n{'='*60}")
        print(f"  Done in {(datetime.now() - start_time).total_seconds():.1f}s")
        print(f"{'='*60}\n")
        return

    # -----------------------------------------------------------------------
    # Step 2: Load companies from Supabase
    # -----------------------------------------------------------------------
    print("Step 2: Loading companies from Supabase...\n")

    companies = supabase_helper.get_all_companies(
        'id, name, symbol, listing_status, extra_data'
    )
    print(f"  Loaded {len(companies)} companies")

    private_companies = [
        c for c in companies
        if (c.get('listing_status') or '').lower() in ('private', 'pre_ipo')
    ]
    print(f"  Private/Pre-IPO companies: {len(private_companies)}")

    # -----------------------------------------------------------------------
    # Step 3: Match IPOs to companies
    # -----------------------------------------------------------------------
    print("\nStep 3: Matching IPOs against companies...\n")

    matches = match_ipos_to_companies(unique_ipos, companies)
    weak_matches = prioritize_private_companies(matches, private_companies, unique_ipos)

    print(f"  Strong matches (>= {MATCH_THRESHOLD:.0%}): {len(matches)}")
    print(f"  Weak matches from private companies (>= 50%): {len(weak_matches)}")

    # -----------------------------------------------------------------------
    # Step 4: Check for duplicates
    # -----------------------------------------------------------------------
    print("\nStep 4: Checking for existing events...\n")

    matched_company_ids = [m['company']['id'] for m in matches]
    existing_events = get_existing_ipo_events(matched_company_ids)

    new_matches = []
    duplicate_count = 0
    for m in matches:
        key = f"{m['company']['id']}|{m['ipo'].get('date', '')}"
        if key in existing_events:
            duplicate_count += 1
        else:
            new_matches.append(m)

    print(f"  Already in DB (duplicates skipped): {duplicate_count}")
    print(f"  New matches to insert: {len(new_matches)}")

    # -----------------------------------------------------------------------
    # Step 5: Display results
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("  RESULTS")
    print(f"{'='*60}\n")

    if new_matches:
        print("  NEW IPO MATCHES:\n")
        for i, m in enumerate(new_matches, 1):
            ipo = m['ipo']
            company = m['company']
            listing = (company.get('listing_status') or 'unknown').upper()
            print(f"  {i:3d}. {ipo['name']}")
            print(f"       -> DB Company: {company['name']} [{listing}]")
            print(f"       Score: {m['score']:.0%} | Date: {ipo.get('date') or 'TBD'} | "
                  f"Exchange: {ipo.get('exchange') or 'N/A'} | Source: {ipo.get('source')}")
            if ipo.get('price_range') and ipo['price_range'] != '-':
                print(f"       Price range: {ipo['price_range']}")
            print()
    else:
        print("  No new IPO matches found.\n")

    if weak_matches:
        print("  WEAK MATCHES (private/pre_ipo companies, below threshold):\n")
        for m in weak_matches:
            company = m['company']
            ipo = m['ipo']
            print(f"       {company['name']} [{company.get('listing_status', '')}]")
            print(f"       ~~ {ipo['name']} (score: {m['score']:.0%})")
            print()

    # -----------------------------------------------------------------------
    # Step 6: Insert if --apply
    # -----------------------------------------------------------------------
    inserted = 0
    if new_matches and not dry_run:
        print(f"\nStep 6: Inserting {len(new_matches)} events into company_events...\n")
        client = supabase_helper.get_client()

        for m in new_matches:
            row = build_event_row(m)
            try:
                client.table('company_events').insert(row).execute()
                inserted += 1
                print(f"  Inserted: {m['company']['name']} — IPO {m['ipo'].get('date', 'TBD')}")
            except Exception as e:
                print(f"  Failed to insert for {m['company']['name']}: {e}")

    elif new_matches and dry_run:
        print(f"  [DRY-RUN] Would insert {len(new_matches)} events. Use --apply to execute.\n")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    elapsed = (datetime.now() - start_time).total_seconds()

    print(f"\n{'='*60}")
    print(f"  SUMMARY — {mode}")
    print(f"{'='*60}")
    print(f"  IPO entries fetched:     {len(unique_ipos)}")
    print(f"    Finnhub:               {len(finnhub_ipos)}")
    print(f"    Nasdaq:                {len(nasdaq_ipos)}")
    print(f"    Brave Search:          {len(brave_ipos)}")
    print(f"  Companies in DB:         {len(companies)}")
    print(f"  Private/Pre-IPO:         {len(private_companies)}")
    print(f"  Strong matches:          {len(matches)}")
    print(f"  Duplicates skipped:      {duplicate_count}")
    print(f"  New matches:             {len(new_matches)}")
    if not dry_run:
        print(f"  Inserted to DB:          {inserted}")
    print(f"  Weak matches (info):     {len(weak_matches)}")
    print(f"  Duration:                {elapsed:.1f}s")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()

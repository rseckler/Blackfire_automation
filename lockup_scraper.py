#!/usr/bin/env python3
"""
Lock-Up Expiration Scraper — Scrapes MarketBeat lock-up expirations and
auto-calculates lockup dates from existing IPO events.

Sources:
  1. MarketBeat lock-up expirations page (scraped HTML table)
  2. Auto-calculation: IPO date + 180 days for companies with IPO events
     but no lockup_expiry event yet

Matches scraped data against companies in Supabase by symbol (exact) then
fuzzy name matching. Creates lockup_expiry events in company_events with
event_metadata JSONB containing lockup details.

Usage:
  python3 lockup_scraper.py              # dry-run (preview only)
  python3 lockup_scraper.py --apply      # write to Supabase
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Optional

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Installing beautifulsoup4...")
    os.system(f"{sys.executable} -m pip install beautifulsoup4")
    from bs4 import BeautifulSoup

from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MARKETBEAT_URL = 'https://www.marketbeat.com/ipos/lockup-expirations/'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

# Default lock-up period (days) for auto-calculation from IPO date
DEFAULT_LOCKUP_DAYS = 180

# Minimum fuzzy-match ratio to consider a match (0.0 - 1.0)
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
# Step 1: Scrape MarketBeat lockup expirations
# ---------------------------------------------------------------------------
def scrape_marketbeat() -> list[dict]:
    """Scrape the MarketBeat lock-up expirations page.

    Expected table columns:
      Company, Symbol, IPO Date, Lock-Up Expiry Date, Shares Subject to Lock-Up

    Returns list of dicts with keys:
      name, symbol, ipo_date, lockup_date, lockup_shares
    """
    print("  Fetching MarketBeat lock-up expirations page...")

    try:
        resp = requests.get(MARKETBEAT_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [MarketBeat] Error fetching page: {e}")
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')

    # Find the data table — MarketBeat uses a <table> with class containing
    # "scroll-table" or similar. We search for the main data table.
    table = None
    for t in soup.find_all('table'):
        # Look for a table that has headers matching our expected columns
        headers = [th.get_text(strip=True).lower() for th in t.find_all('th')]
        header_text = ' '.join(headers)
        if 'symbol' in header_text and ('lock' in header_text or 'expir' in header_text):
            table = t
            break

    if not table:
        # Fallback: try the first table with at least 3 columns
        tables = soup.find_all('table')
        for t in tables:
            rows = t.find_all('tr')
            if len(rows) >= 2:
                cells = rows[0].find_all(['th', 'td'])
                if len(cells) >= 3:
                    table = t
                    break

    if not table:
        print("  [MarketBeat] Could not find lock-up expirations table on page")
        print(f"  [MarketBeat] Page length: {len(resp.text)} chars")
        return []

    # Parse header row to determine column indices
    header_row = table.find('tr')
    if not header_row:
        print("  [MarketBeat] No header row found in table")
        return []

    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(['th', 'td'])]

    # Map column names to indices
    col_map = {}
    for i, h in enumerate(headers):
        h_lower = h.lower()
        if 'company' in h_lower or 'name' in h_lower:
            col_map['name'] = i
        elif 'symbol' in h_lower or 'ticker' in h_lower:
            col_map['symbol'] = i
        elif 'ipo' in h_lower and 'date' in h_lower:
            col_map['ipo_date'] = i
        elif ('lock' in h_lower or 'expir' in h_lower) and 'date' in h_lower:
            col_map['lockup_date'] = i
        elif 'share' in h_lower or 'lock' in h_lower:
            # "Shares Subject to Lock-Up" or similar
            if 'lockup_date' not in col_map or i != col_map.get('lockup_date'):
                col_map['lockup_shares'] = i

    print(f"  [MarketBeat] Detected columns: {col_map}")
    print(f"  [MarketBeat] Headers: {headers}")

    results = []
    data_rows = table.find_all('tr')[1:]  # Skip header row

    for row in data_rows:
        cells = row.find_all(['td', 'th'])
        if len(cells) < 3:
            continue

        cell_texts = [c.get_text(strip=True) for c in cells]

        entry = {
            'name': cell_texts[col_map['name']] if 'name' in col_map and col_map['name'] < len(cell_texts) else '',
            'symbol': cell_texts[col_map['symbol']] if 'symbol' in col_map and col_map['symbol'] < len(cell_texts) else '',
            'ipo_date': cell_texts[col_map['ipo_date']] if 'ipo_date' in col_map and col_map['ipo_date'] < len(cell_texts) else '',
            'lockup_date': cell_texts[col_map['lockup_date']] if 'lockup_date' in col_map and col_map['lockup_date'] < len(cell_texts) else '',
            'lockup_shares': cell_texts[col_map['lockup_shares']] if 'lockup_shares' in col_map and col_map['lockup_shares'] < len(cell_texts) else '',
        }

        # Skip empty rows
        if not entry['name'] and not entry['symbol']:
            continue

        # Clean up symbol (remove whitespace, make uppercase)
        entry['symbol'] = entry['symbol'].strip().upper()

        results.append(entry)

    print(f"  [MarketBeat] Scraped {len(results)} lock-up entries")
    return results


def parse_date(date_str: str) -> Optional[str]:
    """Try to parse a date string into ISO format (YYYY-MM-DD).

    Handles common formats:
      - MM/DD/YYYY
      - YYYY-MM-DD
      - Month DD, YYYY
      - DD.MM.YYYY
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    formats = [
        '%m/%d/%Y',
        '%Y-%m-%d',
        '%B %d, %Y',
        '%b %d, %Y',
        '%d.%m.%Y',
        '%m/%d/%y',
        '%Y/%m/%d',
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue

    # Try to extract date from text like "Jan 15, 2026"
    match = re.search(r'(\w+ \d{1,2},?\s*\d{4})', date_str)
    if match:
        for fmt in ['%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y']:
            try:
                dt = datetime.strptime(match.group(1).replace(',', ''), fmt.replace(',', ''))
                return dt.strftime('%Y-%m-%d')
            except ValueError:
                continue

    return None


def parse_shares(shares_str: str) -> Optional[int]:
    """Parse a shares string like '1,234,567' or '1.2M' into an integer."""
    if not shares_str:
        return None

    s = shares_str.strip().replace(',', '').replace(' ', '')

    # Handle suffixes like M, B, K
    multiplier = 1
    if s.upper().endswith('B'):
        multiplier = 1_000_000_000
        s = s[:-1]
    elif s.upper().endswith('M'):
        multiplier = 1_000_000
        s = s[:-1]
    elif s.upper().endswith('K'):
        multiplier = 1_000
        s = s[:-1]

    try:
        return int(float(s) * multiplier)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Step 2: Load companies from Supabase
# ---------------------------------------------------------------------------
def load_companies() -> list[dict]:
    """Fetch all companies from Supabase."""
    companies = supabase_helper.get_all_companies(
        'id, name, symbol, listing_status, extra_data'
    )
    return companies


# ---------------------------------------------------------------------------
# Step 3: Match scraped data against companies
# ---------------------------------------------------------------------------
def match_lockups_to_companies(
    lockup_entries: list[dict],
    companies: list[dict],
) -> list[dict]:
    """Match lock-up entries against companies by symbol (exact) then fuzzy name.

    Returns list of dicts with keys: lockup, company, score
    """
    matches = []

    # Build lookup structures
    company_symbols = {}
    for c in companies:
        sym = (c.get('symbol') or '').upper().strip()
        if sym:
            company_symbols[sym] = c

    company_names = {}
    for c in companies:
        name = c.get('name', '')
        if name:
            company_names[c['id']] = {
                'normalized': normalize_name(name),
                'original': name,
                'company': c,
            }

    for entry in lockup_entries:
        entry_symbol = (entry.get('symbol') or '').upper().strip()
        entry_name = entry.get('name', '')

        if not entry_name and not entry_symbol:
            continue

        best_match = None
        best_score = 0.0

        # 1. Exact symbol match
        if entry_symbol and entry_symbol in company_symbols:
            best_match = company_symbols[entry_symbol]
            best_score = 1.0
        else:
            # 2. Fuzzy name match
            entry_normalized = normalize_name(entry_name)
            if not entry_normalized:
                continue

            for cid, cdata in company_names.items():
                score = SequenceMatcher(None, entry_normalized, cdata['normalized']).ratio()

                threshold = SHORT_NAME_THRESHOLD if len(entry_normalized) <= 5 else MATCH_THRESHOLD

                if score > best_score and score >= threshold:
                    best_score = score
                    best_match = cdata['company']

        if best_match and best_score >= MATCH_THRESHOLD:
            matches.append({
                'lockup': entry,
                'company': best_match,
                'score': best_score,
            })

    return matches


# ---------------------------------------------------------------------------
# Step 4: Auto-calculate lockup from IPO events
# ---------------------------------------------------------------------------
def get_ipo_events_without_lockup(companies: list[dict]) -> list[dict]:
    """Find companies that have IPO events but no lockup_expiry event.

    Returns list of dicts: { company, ipo_date }
    """
    client = supabase_helper.get_client()

    company_ids = [c['id'] for c in companies]
    if not company_ids:
        return []

    # Build company lookup by ID
    company_by_id = {c['id']: c for c in companies}

    # Fetch all IPO events
    ipo_events = {}  # company_id -> event_date
    for i in range(0, len(company_ids), 200):
        chunk = company_ids[i:i + 200]
        offset = 0
        page_size = 1000
        while True:
            try:
                resp = client.table('company_events') \
                    .select('company_id, event_date') \
                    .eq('event_type', 'ipo') \
                    .in_('company_id', chunk) \
                    .not_.is_('event_date', 'null') \
                    .range(offset, offset + page_size - 1) \
                    .execute()
                for row in resp.data:
                    cid = row['company_id']
                    date = row.get('event_date')
                    if date:
                        ipo_events[cid] = date
                if len(resp.data) < page_size:
                    break
                offset += page_size
            except Exception as e:
                print(f"  Warning: Error fetching IPO events: {e}")
                break

    if not ipo_events:
        return []

    # Fetch existing lockup_expiry events
    lockup_company_ids = set()
    ipo_company_ids = list(ipo_events.keys())
    for i in range(0, len(ipo_company_ids), 200):
        chunk = ipo_company_ids[i:i + 200]
        offset = 0
        page_size = 1000
        while True:
            try:
                resp = client.table('company_events') \
                    .select('company_id') \
                    .eq('event_type', 'lockup_expiry') \
                    .in_('company_id', chunk) \
                    .range(offset, offset + page_size - 1) \
                    .execute()
                for row in resp.data:
                    lockup_company_ids.add(row['company_id'])
                if len(resp.data) < page_size:
                    break
                offset += page_size
            except Exception as e:
                print(f"  Warning: Error fetching lockup events: {e}")
                break

    # Companies with IPO but no lockup
    results = []
    for cid, ipo_date in ipo_events.items():
        if cid not in lockup_company_ids and cid in company_by_id:
            results.append({
                'company': company_by_id[cid],
                'ipo_date': ipo_date,
            })

    return results


def auto_calculate_lockups(ipo_without_lockup: list[dict]) -> list[dict]:
    """Auto-calculate lockup expiry dates as IPO date + 180 days.

    Returns list of dicts matching the same format as match_lockups_to_companies.
    """
    results = []

    for item in ipo_without_lockup:
        ipo_date_str = item['ipo_date']
        company = item['company']

        try:
            ipo_dt = datetime.strptime(ipo_date_str, '%Y-%m-%d')
            lockup_dt = ipo_dt + timedelta(days=DEFAULT_LOCKUP_DAYS)
            lockup_date = lockup_dt.strftime('%Y-%m-%d')
        except (ValueError, TypeError):
            continue

        results.append({
            'lockup': {
                'name': company.get('name', ''),
                'symbol': company.get('symbol', ''),
                'ipo_date': ipo_date_str,
                'lockup_date': lockup_date,
                'lockup_shares': '',
                'source': 'ipo_auto_calc',
            },
            'company': company,
            'score': 1.0,  # Direct match (same company)
        })

    return results


# ---------------------------------------------------------------------------
# Step 5: Deduplicate against existing lockup_expiry events
# ---------------------------------------------------------------------------
def get_existing_lockup_events(company_ids: list[str]) -> set[str]:
    """Get existing lockup_expiry events from company_events to avoid duplicates.

    Returns set of 'company_id|event_date' strings for dedup.
    """
    if not company_ids:
        return set()

    client = supabase_helper.get_client()
    existing = set()

    for i in range(0, len(company_ids), 50):
        chunk = company_ids[i:i + 50]
        try:
            resp = client.table('company_events') \
                .select('company_id, event_date') \
                .eq('event_type', 'lockup_expiry') \
                .in_('company_id', chunk) \
                .execute()
            for row in resp.data:
                key = f"{row['company_id']}|{row.get('event_date', '')}"
                existing.add(key)
        except Exception as e:
            print(f"  Warning: Could not check existing lockup events: {e}")

    return existing


# ---------------------------------------------------------------------------
# Step 6: Build event rows
# ---------------------------------------------------------------------------
def build_event_row(match: dict) -> dict:
    """Build a company_events insert row from a lockup match."""
    lockup = match['lockup']
    company = match['company']

    # Determine source
    source = lockup.get('source', 'marketbeat_scrape')
    if source not in ('marketbeat_scrape', 'ipo_auto_calc'):
        source = 'marketbeat_scrape'

    # Determine confidence
    confidence = 'confirmed' if source == 'marketbeat_scrape' else 'estimated'

    # Parse lockup date
    lockup_date = parse_date(lockup.get('lockup_date', ''))
    ipo_date = parse_date(lockup.get('ipo_date', ''))

    # Calculate lockup days if both dates are available
    lockup_days = DEFAULT_LOCKUP_DAYS
    if lockup_date and ipo_date:
        try:
            ipo_dt = datetime.strptime(ipo_date, '%Y-%m-%d')
            lockup_dt = datetime.strptime(lockup_date, '%Y-%m-%d')
            lockup_days = (lockup_dt - ipo_dt).days
        except ValueError:
            pass

    # Parse shares
    lockup_shares = parse_shares(lockup.get('lockup_shares', ''))

    # Build event_metadata JSONB
    event_metadata = {
        'lockup_days': lockup_days,
        'source': source,
        'confidence': confidence,
    }

    if lockup_shares is not None:
        event_metadata['lockup_shares'] = lockup_shares

    if ipo_date:
        event_metadata['ipo_date'] = ipo_date

    # Try to get percent_of_float from scraped data (if available)
    lockup_percent = lockup.get('lockup_percent_of_float')
    if lockup_percent is not None:
        event_metadata['lockup_percent_of_float'] = lockup_percent

    # Build description
    parts = []
    parts.append(f"Lock-up expiry ({confidence})")
    if lockup_days != DEFAULT_LOCKUP_DAYS:
        parts.append(f"{lockup_days} days")
    else:
        parts.append(f"{DEFAULT_LOCKUP_DAYS} days")
    if lockup_shares is not None:
        parts.append(f"Shares: {lockup_shares:,}")
    if ipo_date:
        parts.append(f"IPO: {ipo_date}")
    parts.append(f"Match: {match['score']:.0%}")

    description = ' | '.join(parts)

    return {
        'company_id': company['id'],
        'event_type': 'lockup_expiry',
        'event_date': lockup_date,
        'description': description,
        'source': source,
        'event_metadata': event_metadata,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Lock-Up Expiration Scraper — scrape MarketBeat + auto-calculate from IPO events'
    )
    parser.add_argument(
        '--apply', action='store_true',
        help='Write events to company_events (default: dry-run)'
    )
    args = parser.parse_args()

    dry_run = not args.apply
    mode = 'DRY-RUN' if dry_run else 'APPLY'

    print(f"\n{'='*60}")
    print(f"  Lock-Up Expiration Scraper — {mode} mode")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    start_time = datetime.now()

    # -------------------------------------------------------------------
    # Step 1: Scrape MarketBeat lockup calendar
    # -------------------------------------------------------------------
    print("Step 1: Scraping MarketBeat lock-up expirations...\n")

    scraped_entries = scrape_marketbeat()
    print(f"\n  Scraped entries: {len(scraped_entries)}\n")

    # -------------------------------------------------------------------
    # Step 2: Load companies from Supabase
    # -------------------------------------------------------------------
    print("Step 2: Loading companies from Supabase...\n")

    companies = load_companies()
    print(f"  Loaded {len(companies)} companies\n")

    # -------------------------------------------------------------------
    # Step 3: Match scraped data by symbol (exact) then fuzzy name
    # -------------------------------------------------------------------
    print("Step 3: Matching scraped lock-ups against companies...\n")

    scraped_matches = match_lockups_to_companies(scraped_entries, companies)
    # Tag source for scraped matches
    for m in scraped_matches:
        m['lockup']['source'] = 'marketbeat_scrape'

    print(f"  Scraped matches: {len(scraped_matches)}\n")

    # -------------------------------------------------------------------
    # Step 4: Auto-calculate lockup for IPO events without lockup
    # -------------------------------------------------------------------
    print("Step 4: Auto-calculating lock-ups from IPO events...\n")

    ipo_without_lockup = get_ipo_events_without_lockup(companies)
    print(f"  IPO events without lockup: {len(ipo_without_lockup)}")

    auto_calc_matches = auto_calculate_lockups(ipo_without_lockup)
    print(f"  Auto-calculated lockup entries: {len(auto_calc_matches)}\n")

    # -------------------------------------------------------------------
    # Combine all matches
    # -------------------------------------------------------------------
    all_matches = scraped_matches + auto_calc_matches

    # Deduplicate within results: if same company appears in both scraped
    # and auto-calc, prefer the scraped version (confirmed > estimated)
    seen_company_ids = set()
    deduped_matches = []
    # Scraped matches first (higher priority)
    for m in scraped_matches:
        cid = m['company']['id']
        if cid not in seen_company_ids:
            seen_company_ids.add(cid)
            deduped_matches.append(m)
    for m in auto_calc_matches:
        cid = m['company']['id']
        if cid not in seen_company_ids:
            seen_company_ids.add(cid)
            deduped_matches.append(m)

    all_matches = deduped_matches
    print(f"  Combined unique matches: {len(all_matches)}\n")

    # -------------------------------------------------------------------
    # Step 5: Deduplicate against existing lockup_expiry events
    # -------------------------------------------------------------------
    print("Step 5: Checking for existing lockup_expiry events...\n")

    matched_company_ids = [m['company']['id'] for m in all_matches]
    existing_events = get_existing_lockup_events(matched_company_ids)

    new_matches = []
    duplicate_count = 0
    for m in all_matches:
        lockup_date = parse_date(m['lockup'].get('lockup_date', ''))
        key = f"{m['company']['id']}|{lockup_date or ''}"
        if key in existing_events:
            duplicate_count += 1
        else:
            new_matches.append(m)

    print(f"  Already in DB (duplicates skipped): {duplicate_count}")
    print(f"  New matches to insert: {len(new_matches)}\n")

    # -------------------------------------------------------------------
    # Display results
    # -------------------------------------------------------------------
    print(f"{'='*60}")
    print("  RESULTS")
    print(f"{'='*60}\n")

    if new_matches:
        # Separate by source
        scraped_new = [m for m in new_matches if m['lockup'].get('source') == 'marketbeat_scrape']
        auto_new = [m for m in new_matches if m['lockup'].get('source') == 'ipo_auto_calc']

        if scraped_new:
            print("  SCRAPED LOCK-UP MATCHES (MarketBeat):\n")
            for i, m in enumerate(scraped_new, 1):
                lockup = m['lockup']
                company = m['company']
                lockup_date = parse_date(lockup.get('lockup_date', '')) or 'TBD'
                ipo_date = parse_date(lockup.get('ipo_date', '')) or 'N/A'
                shares = lockup.get('lockup_shares', 'N/A')
                print(f"  {i:3d}. {lockup['name']} ({lockup.get('symbol', 'N/A')})")
                print(f"       -> DB Company: {company['name']} [ID: {company['id'][:8]}...]")
                print(f"       Score: {m['score']:.0%} | IPO: {ipo_date} | "
                      f"Lock-Up Expiry: {lockup_date} | Shares: {shares}")
                print()

        if auto_new:
            print("  AUTO-CALCULATED LOCK-UPS (IPO + 180 days):\n")
            for i, m in enumerate(auto_new, 1):
                lockup = m['lockup']
                company = m['company']
                lockup_date = lockup.get('lockup_date', 'TBD')
                ipo_date = lockup.get('ipo_date', 'N/A')
                print(f"  {i:3d}. {company['name']} ({company.get('symbol', 'N/A')})")
                print(f"       IPO: {ipo_date} -> Lock-Up Expiry (est.): {lockup_date}")
                print()
    else:
        print("  No new lock-up expiration events found.\n")

    # -------------------------------------------------------------------
    # Step 6: Insert if --apply
    # -------------------------------------------------------------------
    inserted = 0
    if new_matches and not dry_run:
        print(f"\nStep 6: Inserting {len(new_matches)} events into company_events...\n")
        client = supabase_helper.get_client()

        for m in new_matches:
            row = build_event_row(m)
            try:
                client.table('company_events').insert(row).execute()
                inserted += 1
                lockup_date = row.get('event_date') or 'TBD'
                source = row.get('source', 'unknown')
                print(f"  Inserted: {m['company']['name']} — "
                      f"Lock-Up {lockup_date} ({source})")
            except Exception as e:
                print(f"  Failed to insert for {m['company']['name']}: {e}")

    elif new_matches and dry_run:
        print(f"  [DRY-RUN] Would insert {len(new_matches)} events. "
              f"Use --apply to execute.\n")

    # -------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------
    elapsed = (datetime.now() - start_time).total_seconds()

    scraped_match_count = len([m for m in new_matches if m['lockup'].get('source') == 'marketbeat_scrape'])
    auto_match_count = len([m for m in new_matches if m['lockup'].get('source') == 'ipo_auto_calc'])

    print(f"\n{'='*60}")
    print(f"  SUMMARY — {mode}")
    print(f"{'='*60}")
    print(f"  MarketBeat entries scraped: {len(scraped_entries)}")
    print(f"  Companies in DB:            {len(companies)}")
    print(f"  Scraped matches:            {len(scraped_matches)}")
    print(f"  IPO events without lockup:  {len(ipo_without_lockup)}")
    print(f"  Auto-calculated lockups:    {len(auto_calc_matches)}")
    print(f"  Total combined matches:     {len(all_matches)}")
    print(f"  Duplicates skipped:         {duplicate_count}")
    print(f"  New events:                 {len(new_matches)}")
    print(f"    From MarketBeat:          {scraped_match_count}")
    print(f"    From auto-calculation:    {auto_match_count}")
    if not dry_run:
        print(f"  Inserted to DB:             {inserted}")
    print(f"  Duration:                   {elapsed:.1f}s")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()

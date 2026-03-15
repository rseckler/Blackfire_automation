#!/usr/bin/env python3
"""
SPAC Tracker — Monitor SPAC (Special Purpose Acquisition Company) activity
from SEC EDGAR and Brave Search, and match against companies in Supabase.

Sources:
  1. SEC EDGAR EFTS API (free, no API key) — SPAC filings by SIC code 6770
  2. Brave Search API — recent SPAC merger announcements and de-SPAC activity

Matches SPAC candidates against companies table by fuzzy name/symbol matching.
Stores matched events in company_events table with event_types:
  - spac_announced: merger announcements
  - spac_vote: shareholder vote dates
  - spac_closing: expected closing dates
  - spac_deadline: SPAC trust deadlines

Usage:
  python3 spac_tracker.py              # dry-run (preview only)
  python3 spac_tracker.py --apply      # write to Supabase
  python3 spac_tracker.py --days 90    # look back 90 days (default: 90)
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
BRAVE_API_KEY = os.getenv('BRAVE_API_KEY', '')

SEC_USER_AGENT = 'Blackfire-Automation/1.0 (rseckler@gmail.com)'
SEC_RATE_LIMIT_DELAY = 0.12  # 10 req/sec max -> ~120ms between requests
BRAVE_RATE_LIMIT_DELAY = 1.0  # 1 req/sec

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
    'acquisition', 'merger', 'sub', 'parent',
]

# SPAC-specific keywords for classification
SPAC_VOTE_KEYWORDS = [
    'shareholder vote', 'special meeting', 'proxy statement',
    'stockholder approval', 'shareholder approval', 'DEFM14A',
    'vote to approve', 'extraordinary general meeting',
]
SPAC_CLOSING_KEYWORDS = [
    'closing', 'completed merger', 'business combination completed',
    'de-spac', 'completed acquisition', 'merger complete',
    'transaction closed', 'deal closed',
]
SPAC_DEADLINE_KEYWORDS = [
    'trust deadline', 'redemption deadline', 'extension',
    'liquidation deadline', 'trust termination', 'winding down',
    'dissolution', 'must complete',
]
SPAC_ANNOUNCED_KEYWORDS = [
    'definitive agreement', 'letter of intent', 'merger agreement',
    'business combination agreement', 'to merge', 'plans to merge',
    'announces merger', 'to combine', 'spac merger',
    'blank check', 'special purpose acquisition',
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


def classify_spac_event(text: str) -> str:
    """Classify the SPAC event type based on text content.

    Returns one of: spac_vote, spac_closing, spac_deadline, spac_announced
    """
    text_lower = text.lower()

    # Check in order of specificity (most specific first)
    for keyword in SPAC_VOTE_KEYWORDS:
        if keyword.lower() in text_lower:
            return 'spac_vote'

    for keyword in SPAC_CLOSING_KEYWORDS:
        if keyword.lower() in text_lower:
            return 'spac_closing'

    for keyword in SPAC_DEADLINE_KEYWORDS:
        if keyword.lower() in text_lower:
            return 'spac_deadline'

    # Default to spac_announced
    return 'spac_announced'


def extract_date_from_text(text: str) -> Optional[str]:
    """Try to extract a date from text. Returns ISO date string or None."""
    # Patterns: "March 15, 2026", "2026-03-15", "03/15/2026", "Q1 2026"
    patterns = [
        # ISO format: 2026-03-15
        (r'(\d{4}-\d{2}-\d{2})', '%Y-%m-%d'),
        # US format: March 15, 2026 or Mar 15, 2026
        (r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})', '%B %d, %Y'),
        (r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})', None),
        # US numeric: 03/15/2026
        (r'(\d{1,2}/\d{1,2}/\d{4})', '%m/%d/%Y'),
    ]

    for pattern, fmt in patterns:
        match = re.search(pattern, text)
        if match:
            date_str = match.group(1).replace(',', '')
            try:
                if fmt:
                    dt = datetime.strptime(date_str, fmt)
                    return dt.strftime('%Y-%m-%d')
                else:
                    # Try multiple abbreviated month formats
                    for f in ['%b %d %Y', '%B %d %Y']:
                        try:
                            # Clean up abbreviation dots
                            cleaned = date_str.replace('.', '')
                            dt = datetime.strptime(cleaned, f)
                            return dt.strftime('%Y-%m-%d')
                        except ValueError:
                            continue
            except ValueError:
                continue

    return None


def extract_spac_metadata(text: str, source: str) -> dict:
    """Extract SPAC-specific metadata from text."""
    metadata = {'source': source}

    text_lower = text.lower()

    # Try to extract trust size (e.g., "$200 million trust", "$250M")
    trust_patterns = [
        r'\$\s*([\d,.]+)\s*(?:million|mln|mn|m)\s*(?:trust|in trust)',
        r'trust\s*(?:of|worth|valued at|size)?\s*\$\s*([\d,.]+)\s*(?:million|mln|mn|m)',
        r'\$\s*([\d,.]+)\s*(?:billion|bln|bn|b)\s*(?:trust|in trust)',
    ]
    for pattern in trust_patterns:
        match = re.search(pattern, text_lower)
        if match:
            amount = match.group(1).replace(',', '')
            try:
                val = float(amount)
                if 'billion' in pattern or 'bln' in pattern or 'bn' in pattern:
                    val *= 1000
                metadata['spac_trust_size'] = f"${val:.0f}M"
            except ValueError:
                pass
            break

    # Try to extract SPAC sponsor
    sponsor_patterns = [
        r'(?:sponsored by|led by|backed by|managed by)\s+([A-Z][A-Za-z\s&.]+?)(?:\.|,|\s+(?:announced|plans|will|has|is))',
        r'([A-Z][A-Za-z]+\s+(?:Capital|Partners|Management|Ventures|Advisors))\s+(?:spac|blank check|sponsor)',
    ]
    for pattern in sponsor_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            sponsor = match.group(1).strip()
            if len(sponsor) > 3 and len(sponsor) < 80:
                metadata['spac_sponsor'] = sponsor
            break

    # Try to extract exchange
    exchange_patterns = [
        r'(?:list(?:ed|ing)?|trad(?:e|ing|es)?)\s+(?:on\s+)?(?:the\s+)?(NYSE|NASDAQ|Nasdaq|NYSE American|AMEX|OTC)',
        r'(NYSE|NASDAQ|Nasdaq|NYSE American|AMEX):\s*[A-Z]+',
    ]
    for pattern in exchange_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            exchange = match.group(1).upper()
            if exchange == 'NASDAQ':
                exchange = 'NASDAQ'
            metadata['spac_exchange'] = exchange
            break

    # Try to extract pre-merger ticker
    ticker_patterns = [
        r'(?:ticker|symbol|trading as|traded as|under)\s*(?:symbol)?\s*[:\s]?\s*"?([A-Z]{2,5})"?',
        r'(NYSE|NASDAQ|Nasdaq):\s*([A-Z]{2,5})',
    ]
    for pattern in ticker_patterns:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            ticker = groups[-1] if len(groups) > 1 else groups[0]
            if ticker.upper() not in ('NYSE', 'NASDAQ', 'AMEX', 'SPAC', 'THE'):
                metadata['spac_ticker_pre'] = ticker.upper()
            break

    return metadata


# ---------------------------------------------------------------------------
# Source 1: SEC EDGAR EFTS API
# ---------------------------------------------------------------------------
def fetch_sec_edgar_spacs(days_back: int = 90) -> list[dict]:
    """Fetch SPAC-related filings from SEC EDGAR full-text search.

    Uses the EFTS (Electronic Full-Text Search) API which is free
    and requires no API key, just a proper User-Agent header.
    """
    print("  [SEC EDGAR] Fetching SPAC filings...")

    headers = {
        'User-Agent': SEC_USER_AGENT,
        'Accept': 'application/json',
    }

    today = datetime.now().date()
    from_date = (today - timedelta(days=days_back)).isoformat()
    to_date = today.isoformat()

    # Define searches: different query terms and form types
    searches = [
        {
            'label': 'special purpose acquisition (S-1, DEFM14A, 8-K)',
            'params': {
                'q': '"special purpose acquisition"',
                'dateRange': 'custom',
                'startdt': from_date,
                'enddt': to_date,
                'forms': 'S-1,DEFM14A,8-K',
            },
        },
        {
            'label': 'blank check company (8-K)',
            'params': {
                'q': '"blank check"',
                'dateRange': 'custom',
                'startdt': from_date,
                'enddt': to_date,
                'forms': '8-K',
            },
        },
        {
            'label': 'business combination SPAC (8-K, DEFM14A)',
            'params': {
                'q': '"business combination" AND "SPAC"',
                'dateRange': 'custom',
                'startdt': from_date,
                'enddt': to_date,
                'forms': '8-K,DEFM14A',
            },
        },
        {
            'label': 'de-SPAC merger (8-K)',
            'params': {
                'q': '"de-SPAC" OR "deSPAC"',
                'dateRange': 'custom',
                'startdt': from_date,
                'enddt': to_date,
                'forms': '8-K',
            },
        },
    ]

    base_url = 'https://efts.sec.gov/LATEST/search-index'
    all_results = []
    seen_accession_numbers = set()

    for search in searches:
        label = search['label']
        params = search['params']

        try:
            time.sleep(SEC_RATE_LIMIT_DELAY)
            resp = requests.get(base_url, params=params, headers=headers, timeout=20)

            if resp.status_code == 429:
                print(f"    [SEC] Rate limited on '{label}', waiting 5s...")
                time.sleep(5)
                resp = requests.get(base_url, params=params, headers=headers, timeout=20)

            if resp.status_code != 200:
                print(f"    [SEC] HTTP {resp.status_code} for '{label}'")
                continue

            data = resp.json()
            hits = data.get('hits', {}).get('hits', [])
            total = data.get('hits', {}).get('total', {}).get('value', 0)
            print(f"    [SEC] '{label}': {len(hits)} hits (total: {total})")

            for hit in hits:
                source = hit.get('_source', {})
                accession = source.get('file_num', '') or source.get('_id', '')

                # Deduplicate across searches
                filing_id = source.get('_id', accession)
                if filing_id in seen_accession_numbers:
                    continue
                seen_accession_numbers.add(filing_id)

                entity_name = source.get('entity_name', '') or source.get('display_names', [''])[0] if source.get('display_names') else ''
                # Handle display_names as list
                if isinstance(entity_name, list):
                    entity_name = entity_name[0] if entity_name else ''

                filing_date = source.get('file_date', '') or source.get('period_of_report', '')
                form_type = source.get('form_type', '')
                file_description = source.get('file_description', '') or ''

                # Combine text for classification
                combined_text = f"{entity_name} {form_type} {file_description}"

                # Classify event type
                event_type = classify_spac_event(combined_text)

                # Override based on form type
                if form_type == 'DEFM14A':
                    event_type = 'spac_vote'
                elif form_type == 'S-1' and 'blank check' in combined_text.lower():
                    event_type = 'spac_announced'

                # Extract ticker from entity name or description
                ticker = ''
                ticker_match = re.search(r'\(([A-Z]{2,5})\)', entity_name)
                if ticker_match:
                    ticker = ticker_match.group(1)

                # Clean entity name (remove ticker parenthetical)
                clean_name = re.sub(r'\s*\([A-Z]{2,5}\)\s*', ' ', entity_name).strip()
                # Remove common SPAC suffixes for better matching
                clean_name = re.sub(r'\s*(?:Acquisition|Merger Sub|Holdings|Corp|Inc)\s*$', '', clean_name, flags=re.IGNORECASE).strip()

                all_results.append({
                    'name': clean_name or entity_name,
                    'symbol': ticker,
                    'date': filing_date,
                    'form_type': form_type,
                    'event_type': event_type,
                    'description': f"SEC filing: {form_type} — {file_description[:200]}" if file_description else f"SEC filing: {form_type}",
                    'source': f'sec_edgar:{form_type}',
                    'raw_text': combined_text,
                    'confidence': 'high' if form_type in ('DEFM14A', 'S-1') else 'medium',
                })

        except requests.RequestException as e:
            print(f"    [SEC] Error for '{label}': {e}")
        except (ValueError, KeyError) as e:
            print(f"    [SEC] Parse error for '{label}': {e}")

    print(f"  [SEC EDGAR] Total unique filings: {len(all_results)}")
    return all_results


# ---------------------------------------------------------------------------
# Source 2: Brave Search API
# ---------------------------------------------------------------------------
def fetch_brave_spacs() -> list[dict]:
    """Search for recent SPAC merger announcements via Brave Search API."""
    if not BRAVE_API_KEY:
        print("  [Brave] Skipped — BRAVE_API_KEY not set")
        return []

    queries = [
        'SPAC merger 2026 announcement',
        'de-SPAC 2026 completed',
        'SPAC business combination 2026 vote',
        'SPAC trust deadline 2026 extension',
        'blank check company merger 2026',
    ]

    results = []
    seen_urls = set()

    for query in queries:
        try:
            resp = requests.get(
                'https://api.search.brave.com/res/v1/web/search',
                params={'q': query, 'count': 10, 'freshness': 'pm'},  # past month
                headers={'X-Subscription-Token': BRAVE_API_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            web_results = data.get('web', {}).get('results', [])
            print(f"    [Brave] '{query}': {len(web_results)} results")

            for item in web_results:
                url = item.get('url', '')
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title = item.get('title', '')
                description = item.get('description', '')
                text = f"{title} {description}"

                # Extract company names from SPAC-related text
                # Pattern: "CompanyName to merge with SPACName"
                # Pattern: "SPACName announces merger with TargetName"
                spac_patterns = [
                    r'([A-Z][A-Za-z\s&.]+?)\s+(?:to merge|announces? merger|completes? merger|plans merger|enters? (?:into )?(?:definitive|merger) agreement)\s+(?:with\s+)?([A-Z][A-Za-z\s&.]+?)(?:\s*[,.\|]|\s+(?:in|for|to|on))',
                    r'([A-Z][A-Za-z\s&.]+?)\s+(?:SPAC|blank.check)\s+(?:merger|deal|combination)',
                    r'(?:SPAC|blank.check)\s+([A-Z][A-Za-z\s&.]+?)\s+(?:targets?|to (?:merge|combine|acquire))',
                    r'([A-Z][A-Za-z\s&.]+?)\s+(?:de-?SPAC|goes? public via SPAC)',
                    r'([A-Z][A-Za-z\s&.]+?)\s+(?:shareholders? (?:vote|approve)|stockholders? (?:vote|approve))',
                    r'([A-Z][A-Za-z\s&.]+?)\s+(?:trust deadline|extension deadline|redemption)',
                ]

                extracted_names = set()
                for pattern in spac_patterns:
                    matches = re.findall(pattern, text, re.IGNORECASE)
                    for match in matches:
                        if isinstance(match, tuple):
                            for m in match:
                                name = m.strip()
                                if len(name) > 2 and len(name) < 80:
                                    extracted_names.add(name)
                        else:
                            name = match.strip()
                            if len(name) > 2 and len(name) < 80:
                                extracted_names.add(name)

                # Classify event type from the full text
                event_type = classify_spac_event(text)

                # Try to extract a date from the text
                event_date = extract_date_from_text(text)

                # Extract metadata
                metadata = extract_spac_metadata(text, f'brave:{url[:100]}')

                if extracted_names:
                    for name in extracted_names:
                        # Skip generic words that might be false positives
                        if normalize_name(name) in ('', 'spac', 'blank check', 'company'):
                            continue
                        results.append({
                            'name': name,
                            'symbol': metadata.get('spac_ticker_pre', ''),
                            'date': event_date or '',
                            'form_type': '',
                            'event_type': event_type,
                            'description': f"{title[:200]}",
                            'source': f'brave:{url[:100]}',
                            'raw_text': text[:500],
                            'confidence': 'medium',
                            'metadata': metadata,
                        })
                else:
                    # Even without extracted names, store the finding for matching
                    # Use the title as a potential company reference
                    clean_title = re.sub(r'\s*[-|:].+$', '', title).strip()
                    if clean_title and len(clean_title) > 5:
                        results.append({
                            'name': clean_title,
                            'symbol': metadata.get('spac_ticker_pre', ''),
                            'date': event_date or '',
                            'form_type': '',
                            'event_type': event_type,
                            'description': f"{title[:200]}",
                            'source': f'brave:{url[:100]}',
                            'raw_text': text[:500],
                            'confidence': 'low',
                            'metadata': metadata,
                        })

            time.sleep(BRAVE_RATE_LIMIT_DELAY)

        except requests.RequestException as e:
            print(f"    [Brave] Error for '{query}': {e}")

    print(f"  [Brave] Total SPAC candidates extracted: {len(results)}")
    return results


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------
def match_spacs_to_companies(spac_entries: list[dict], companies: list[dict]) -> list[dict]:
    """Match SPAC entries against companies in the database using fuzzy name matching.

    Returns list of dicts with keys: spac, company, score
    """
    matches = []

    # Build lookup structures
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

    for spac in spac_entries:
        spac_name = spac.get('name', '')
        spac_symbol = (spac.get('symbol') or '').upper().strip()
        if not spac_name and not spac_symbol:
            continue

        best_match = None
        best_score = 0.0

        # 1. Exact symbol match
        if spac_symbol and spac_symbol in company_symbols:
            best_match = company_symbols[spac_symbol]
            best_score = 1.0
        else:
            # 2. Fuzzy name match
            spac_normalized = normalize_name(spac_name)
            if not spac_normalized:
                continue

            for cid, cdata in company_names.items():
                score = SequenceMatcher(None, spac_normalized, cdata['normalized']).ratio()

                # Use higher threshold for short names
                threshold = SHORT_NAME_THRESHOLD if len(spac_normalized) <= 5 else MATCH_THRESHOLD

                if score > best_score and score >= threshold:
                    best_score = score
                    best_match = cdata['company']

        if best_match and best_score >= MATCH_THRESHOLD:
            matches.append({
                'spac': spac,
                'company': best_match,
                'score': best_score,
            })

    # Deduplicate: keep highest-score match per company
    best_per_company = {}
    for m in matches:
        cid = m['company']['id']
        if cid not in best_per_company or m['score'] > best_per_company[cid]['score']:
            best_per_company[cid] = m

    return list(best_per_company.values())


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def get_existing_spac_events(company_ids: list[str]) -> set[str]:
    """Get existing SPAC events from company_events to avoid duplicates.

    Returns set of 'company_id|event_type|event_date' strings for dedup.
    """
    if not company_ids:
        return set()

    client = supabase_helper.get_client()
    existing = set()

    spac_event_types = ['spac_announced', 'spac_vote', 'spac_closing', 'spac_deadline']

    # Paginate in chunks of 50 IDs to avoid URL length issues
    for i in range(0, len(company_ids), 50):
        chunk = company_ids[i:i + 50]
        try:
            resp = client.table('company_events') \
                .select('company_id, event_type, event_date') \
                .in_('event_type', spac_event_types) \
                .in_('company_id', chunk) \
                .execute()
            for row in resp.data:
                key = f"{row['company_id']}|{row.get('event_type', '')}|{row.get('event_date', '')}"
                existing.add(key)
        except Exception as e:
            print(f"  Warning: Could not check existing events: {e}")

    return existing


# ---------------------------------------------------------------------------
# Insert events
# ---------------------------------------------------------------------------
def build_event_row(match: dict) -> dict:
    """Build a company_events insert row from a match."""
    spac = match['spac']
    company = match['company']

    # Build description
    parts = []
    if spac.get('form_type'):
        parts.append(f"Filing: {spac['form_type']}")
    if spac.get('description'):
        parts.append(spac['description'][:300])
    parts.append(f"Match score: {match['score']:.0%}")

    description = ' | '.join(parts) if parts else None

    # Build event_metadata JSONB
    event_metadata = {
        'source': spac.get('source', 'unknown'),
        'confidence': spac.get('confidence', 'medium'),
    }

    # Merge any metadata extracted from text
    if spac.get('metadata'):
        event_metadata.update(spac['metadata'])
    else:
        # Extract metadata from raw_text if available
        if spac.get('raw_text'):
            extracted = extract_spac_metadata(spac['raw_text'], spac.get('source', 'unknown'))
            event_metadata.update(extracted)

    # Remove None values from metadata
    event_metadata = {k: v for k, v in event_metadata.items() if v is not None}

    return {
        'company_id': company['id'],
        'event_type': spac.get('event_type', 'spac_announced'),
        'event_date': spac.get('date') or None,
        'description': description,
        'source': spac.get('source', 'unknown'),
        'event_metadata': json.dumps(event_metadata),
    }


# ---------------------------------------------------------------------------
# Update listing_status
# ---------------------------------------------------------------------------
def update_listing_status(matches: list[dict], dry_run: bool) -> int:
    """Update listing_status to 'spac' for matched companies.

    Only updates companies that don't already have listing_status = 'spac'.
    Returns count of updated companies.
    """
    updated = 0

    for m in matches:
        company = m['company']
        current_status = (company.get('listing_status') or '').lower()

        if current_status == 'spac':
            continue

        if dry_run:
            print(f"    [DRY-RUN] Would set listing_status='spac' for {company['name']} "
                  f"(current: {current_status or 'null'})")
            updated += 1
        else:
            success = supabase_helper.update_company(company['id'], {'listing_status': 'spac'})
            if success:
                print(f"    Updated listing_status='spac' for {company['name']}")
                updated += 1
            else:
                print(f"    Failed to update listing_status for {company['name']}")

    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='SPAC Tracker — match SPAC filings and news to Supabase companies'
    )
    parser.add_argument('--apply', action='store_true',
                        help='Write matches to company_events (default: dry-run)')
    parser.add_argument('--days', type=int, default=90,
                        help='Days to look back (default: 90)')
    args = parser.parse_args()

    dry_run = not args.apply
    mode = 'DRY-RUN' if dry_run else 'APPLY'

    print(f"\n{'='*60}")
    print(f"  SPAC Tracker — {mode} mode")
    print(f"  Look-back: {args.days} days")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    start_time = datetime.now()

    # -----------------------------------------------------------------------
    # Step 1: Fetch SPAC data from all sources
    # -----------------------------------------------------------------------
    print("Step 1: Fetching SPAC data...\n")

    all_spacs = []

    sec_spacs = fetch_sec_edgar_spacs(days_back=args.days)
    all_spacs.extend(sec_spacs)

    brave_spacs = fetch_brave_spacs()
    all_spacs.extend(brave_spacs)

    # Deduplicate entries by normalized name + event_type
    seen = {}
    unique_spacs = []
    for spac in all_spacs:
        key = f"{normalize_name(spac.get('name', ''))}|{spac.get('event_type', '')}"
        if key and key not in seen:
            seen[key] = True
            unique_spacs.append(spac)

    print(f"\n  Total unique SPAC entries: {len(unique_spacs)} "
          f"(SEC EDGAR: {len(sec_spacs)}, Brave: {len(brave_spacs)})\n")

    if not unique_spacs:
        print("  No SPAC data found from any source. Check network connectivity.")
        print(f"\n{'='*60}")
        print(f"  Done in {(datetime.now() - start_time).total_seconds():.1f}s")
        print(f"{'='*60}\n")
        return

    # Print breakdown by event type
    type_counts = defaultdict(int)
    for s in unique_spacs:
        type_counts[s.get('event_type', 'unknown')] += 1
    print("  By event type:")
    for etype, count in sorted(type_counts.items()):
        print(f"    {etype}: {count}")
    print()

    # -----------------------------------------------------------------------
    # Step 2: Load companies from Supabase
    # -----------------------------------------------------------------------
    print("Step 2: Loading companies from Supabase...\n")

    companies = supabase_helper.get_all_companies(
        'id, name, symbol, listing_status, extra_data'
    )
    print(f"  Loaded {len(companies)} companies")

    spac_companies = [
        c for c in companies
        if (c.get('listing_status') or '').lower() == 'spac'
    ]
    print(f"  Companies already tagged as SPAC: {len(spac_companies)}")

    # -----------------------------------------------------------------------
    # Step 3: Match SPACs to companies
    # -----------------------------------------------------------------------
    print("\nStep 3: Matching SPACs against companies...\n")

    matches = match_spacs_to_companies(unique_spacs, companies)

    print(f"  Matches found (>= {MATCH_THRESHOLD:.0%}): {len(matches)}")

    # -----------------------------------------------------------------------
    # Step 4: Check for duplicates
    # -----------------------------------------------------------------------
    print("\nStep 4: Checking for existing events...\n")

    matched_company_ids = [m['company']['id'] for m in matches]
    existing_events = get_existing_spac_events(matched_company_ids)

    new_matches = []
    duplicate_count = 0
    for m in matches:
        key = f"{m['company']['id']}|{m['spac'].get('event_type', '')}|{m['spac'].get('date', '')}"
        if key in existing_events:
            duplicate_count += 1
        else:
            new_matches.append(m)

    print(f"  Already in DB (duplicates skipped): {duplicate_count}")
    print(f"  New matches to process: {len(new_matches)}")

    # -----------------------------------------------------------------------
    # Step 5: Display results
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("  RESULTS")
    print(f"{'='*60}\n")

    if new_matches:
        print("  NEW SPAC MATCHES:\n")
        for i, m in enumerate(new_matches, 1):
            spac = m['spac']
            company = m['company']
            listing = (company.get('listing_status') or 'unknown').upper()
            event_type = spac.get('event_type', 'spac_announced')

            print(f"  {i:3d}. {spac['name']}")
            print(f"       -> DB Company: {company['name']} [{listing}]")
            print(f"       Score: {m['score']:.0%} | Type: {event_type} | "
                  f"Date: {spac.get('date') or 'TBD'} | Source: {spac.get('source', '')[:40]}")
            if spac.get('form_type'):
                print(f"       Filing: {spac['form_type']}")
            confidence = spac.get('confidence', 'medium')
            print(f"       Confidence: {confidence}")

            # Show metadata if available
            metadata = spac.get('metadata', {})
            if metadata:
                meta_parts = []
                if metadata.get('spac_sponsor'):
                    meta_parts.append(f"Sponsor: {metadata['spac_sponsor']}")
                if metadata.get('spac_trust_size'):
                    meta_parts.append(f"Trust: {metadata['spac_trust_size']}")
                if metadata.get('spac_exchange'):
                    meta_parts.append(f"Exchange: {metadata['spac_exchange']}")
                if metadata.get('spac_ticker_pre'):
                    meta_parts.append(f"Pre-ticker: {metadata['spac_ticker_pre']}")
                if meta_parts:
                    print(f"       {' | '.join(meta_parts)}")
            print()
    else:
        print("  No new SPAC matches found.\n")

    # -----------------------------------------------------------------------
    # Step 6: Insert events and update listing_status if --apply
    # -----------------------------------------------------------------------
    inserted = 0
    status_updated = 0

    if new_matches and not dry_run:
        print(f"\nStep 6a: Inserting {len(new_matches)} events into company_events...\n")
        client = supabase_helper.get_client()

        for m in new_matches:
            row = build_event_row(m)
            try:
                client.table('company_events').insert(row).execute()
                inserted += 1
                print(f"  Inserted: {m['company']['name']} — {m['spac'].get('event_type', 'spac_announced')} "
                      f"{m['spac'].get('date', 'TBD')}")
            except Exception as e:
                print(f"  Failed to insert for {m['company']['name']}: {e}")

        print(f"\nStep 6b: Updating listing_status for matched companies...\n")
        status_updated = update_listing_status(new_matches, dry_run=False)

    elif new_matches and dry_run:
        print(f"  [DRY-RUN] Would insert {len(new_matches)} events. Use --apply to execute.\n")
        print(f"\n  [DRY-RUN] listing_status updates:\n")
        status_updated = update_listing_status(new_matches, dry_run=True)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    elapsed = (datetime.now() - start_time).total_seconds()

    print(f"\n{'='*60}")
    print(f"  SUMMARY — {mode}")
    print(f"{'='*60}")
    print(f"  SPAC entries fetched:    {len(unique_spacs)}")
    print(f"    SEC EDGAR:             {len(sec_spacs)}")
    print(f"    Brave Search:          {len(brave_spacs)}")
    print(f"  Event type breakdown:")
    for etype, count in sorted(type_counts.items()):
        print(f"    {etype:23s} {count}")
    print(f"  Companies in DB:         {len(companies)}")
    print(f"  Already tagged SPAC:     {len(spac_companies)}")
    print(f"  Matches found:           {len(matches)}")
    print(f"  Duplicates skipped:      {duplicate_count}")
    print(f"  New matches:             {len(new_matches)}")
    if not dry_run:
        print(f"  Inserted to DB:          {inserted}")
        print(f"  Status updated to SPAC:  {status_updated}")
    else:
        print(f"  Would insert:            {len(new_matches)}")
        print(f"  Would update status:     {status_updated}")
    print(f"  Duration:                {elapsed:.1f}s")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()

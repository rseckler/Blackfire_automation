#!/usr/bin/env python3
"""
Fix missing/broken tickers using Claude Haiku + yfinance validation.
Claude suggests likely ticker symbols (20 companies per batch),
then yfinance validates each suggestion.

Valid tickers → update symbol field.
Companies confirmed private → set listing_status = 'private'.

Depends on: classify_listing_status.py (run first to identify unknowns)

Usage:
  python3 fix_tickers.py                    # dry-run, show candidates
  python3 fix_tickers.py --apply            # suggest + validate + write
  python3 fix_tickers.py --apply --limit 50 # process first 50 only
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
    os.system(f"{sys.executable} -m pip install anthropic")
    from anthropic import Anthropic

try:
    import yfinance as yf
except ImportError:
    os.system(f"{sys.executable} -m pip install yfinance")
    import yfinance as yf

BATCH_SIZE = 20  # Companies per AI call
RATE_LIMIT_DELAY = 1.5


def get_candidates(companies: list) -> list:
    """Find companies without a valid symbol that might be public."""
    candidates = []
    for company in companies:
        symbol = company.get('symbol')
        listing = company.get('listing_status')

        # Skip companies already classified as private/acquired
        if listing in ('private', 'acquired'):
            continue

        # Skip companies that already have a symbol
        if symbol and symbol.strip():
            continue

        extra = company.get('extra_data') or {}
        name = company.get('name', '')

        # Must have a name to research
        if not name or name.strip() in ('', 'nan'):
            continue

        candidates.append(company)

    return candidates


def build_prompt(batch: list) -> str:
    """Build prompt for Claude to suggest tickers."""
    lines = []
    for i, company in enumerate(batch):
        extra = company.get('extra_data') or {}
        name = company.get('name', '?')
        isin = company.get('isin') or ''
        wkn = company.get('wkn') or ''
        industry = extra.get('Industry') or ''
        country = extra.get('Country') or ''

        lines.append(
            f"{i+1}. \"{name}\" (ISIN: {isin}, WKN: {wkn}, Industry: {industry}, Country: {country})"
        )

    return f"""For each company below, suggest the most likely stock ticker symbol.
Consider: US exchanges (NYSE, NASDAQ), German exchanges (.DE suffix), and other major exchanges.

If the company is clearly PRIVATE (not publicly traded), respond with "PRIVATE" as the ticker.
If unsure, suggest your best guess — it will be validated.

Companies:
{chr(10).join(lines)}

Return ONLY a JSON array. Each object must have:
- "index": company number (1-based)
- "ticker": suggested ticker symbol (e.g., "AAPL", "SAP.DE", "BABA") or "PRIVATE"
- "confidence": "high", "medium", or "low"
- "exchange": exchange name if known (e.g., "NASDAQ", "XETRA", "NYSE")

Example:
[
  {{"index": 1, "ticker": "PLTR", "confidence": "high", "exchange": "NYSE"}},
  {{"index": 2, "ticker": "PRIVATE", "confidence": "high", "exchange": ""}},
  {{"index": 3, "ticker": "SAP.DE", "confidence": "medium", "exchange": "XETRA"}}
]

Return ONLY the JSON array."""


def validate_ticker(ticker: str) -> bool:
    """Validate a ticker symbol using yfinance."""
    try:
        info = yf.Ticker(ticker).info
        # Check if we got real data (not just an empty/error response)
        return info.get('regularMarketPrice') is not None or info.get('currentPrice') is not None
    except Exception:
        return False


def suggest_tickers(client: Anthropic, batch: list) -> list:
    """Call Claude Haiku to suggest tickers for a batch."""
    prompt = build_prompt(batch)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()

        if text.startswith('```'):
            text = text.split('\n', 1)[1]
            text = text.rsplit('```', 1)[0]

        return json.loads(text)

    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}")
        return []
    except Exception as e:
        print(f"    API error: {e}")
        return []


def main():
    parser = argparse.ArgumentParser(description='Fix missing tickers via AI + yfinance')
    parser.add_argument('--apply', action='store_true', help='Call API, validate, and write')
    parser.add_argument('--limit', type=int, default=0, help='Limit companies to process')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  FIX TICKERS (Claude Haiku + yfinance)")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (analysis only)'}")
    if args.limit:
        print(f"  Limit: {args.limit} companies")
    print("=" * 70)

    api_key = os.getenv('ANTHROPIC_API_KEY')
    if args.apply and not api_key:
        print("\n  ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    # Load companies
    print("\n  Loading companies...")
    try:
        companies = supabase_helper.get_all_companies(
            'id, name, symbol, isin, wkn, extra_data, listing_status'
        )
    except Exception:
        print("  Note: listing_status column not found, fetching without it")
        companies = supabase_helper.get_all_companies(
            'id, name, symbol, isin, wkn, extra_data'
        )
    print(f"  Loaded {len(companies)} companies")

    candidates = get_candidates(companies)
    print(f"  Candidates (no symbol, not private/acquired): {len(candidates)}")

    if not args.apply:
        batches = (len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE
        est_cost = batches * 0.015
        print(f"\n  Estimated cost: ~${est_cost:.2f} ({batches} batches × ~$0.015)")
        print(f"\n  Sample candidates (first 20):")
        for c in candidates[:20]:
            extra = c.get('extra_data') or {}
            print(f"    {c.get('name', '?')[:45]:45s}  ISIN: {c.get('isin') or '-':15s}  Status: {c.get('listing_status') or '-'}")
        print(f"\n  Run with --apply to fix tickers")
        return

    # Apply mode
    client = Anthropic(api_key=api_key)

    # Build set of existing symbols to avoid duplicate key violations
    existing_symbols = set()
    for c in companies:
        s = c.get('symbol')
        if s and s.strip():
            existing_symbols.add(s.strip().upper())
    print(f"  Existing symbols in DB: {len(existing_symbols)}")

    if args.limit:
        candidates = candidates[:args.limit]

    batches = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]
    stats = {'ticker_fixed': 0, 'marked_private': 0, 'validation_failed': 0, 'skipped': 0}

    print(f"\n  Processing {len(candidates)} candidates in {len(batches)} batches...")

    for batch_idx, batch in enumerate(batches):
        print(f"\n  Batch {batch_idx + 1}/{len(batches)} ({len(batch)} companies)...")

        suggestions = suggest_tickers(client, batch)

        if not suggestions:
            print(f"    No suggestions returned")
            time.sleep(RATE_LIMIT_DELAY)
            continue

        for suggestion in suggestions:
            idx = suggestion.get('index', 0) - 1
            if idx < 0 or idx >= len(batch):
                continue

            company = batch[idx]
            ticker = suggestion.get('ticker', '').strip()
            confidence = suggestion.get('confidence', 'low')

            if not ticker:
                stats['skipped'] += 1
                continue

            # Handle PRIVATE classification
            if ticker.upper() == 'PRIVATE':
                if confidence in ('high', 'medium'):
                    if supabase_helper.update_company(company['id'], {'listing_status': 'private'}):
                        stats['marked_private'] += 1
                        print(f"    {company.get('name', '?')[:35]:35s} → PRIVATE")
                continue

            # Check if ticker already exists in DB (avoid unique constraint violation)
            if ticker.upper() in existing_symbols:
                stats['skipped'] += 1
                print(f"    {ticker:12s} for {company.get('name', '?')[:30]:30s} → skipped (symbol already taken)")
                continue

            # Validate ticker via yfinance
            print(f"    Validating {ticker:12s} for {company.get('name', '?')[:30]}...", end=' ')
            if validate_ticker(ticker):
                updates = {
                    'symbol': ticker,
                    'listing_status': 'public'
                }
                if supabase_helper.update_company(company['id'], updates):
                    stats['ticker_fixed'] += 1
                    existing_symbols.add(ticker.upper())
                    print(f"✓ VALID")
                else:
                    print(f"✗ update failed")
            else:
                # Try with .DE suffix for German stocks
                de_ticker = f"{ticker}.DE" if not ticker.endswith('.DE') else None
                if de_ticker and de_ticker.upper() in existing_symbols:
                    stats['skipped'] += 1
                    print(f"✗ skipped ({de_ticker} already taken)")
                elif de_ticker and validate_ticker(de_ticker):
                    updates = {
                        'symbol': de_ticker,
                        'listing_status': 'public'
                    }
                    if supabase_helper.update_company(company['id'], updates):
                        stats['ticker_fixed'] += 1
                        existing_symbols.add(de_ticker.upper())
                        print(f"✓ VALID ({de_ticker})")
                    else:
                        print(f"✗ update failed")
                else:
                    stats['validation_failed'] += 1
                    print(f"✗ invalid")

        time.sleep(RATE_LIMIT_DELAY)

    print(f"\n  RESULTS:")
    print(f"    Tickers fixed:      {stats['ticker_fixed']}")
    print(f"    Marked private:     {stats['marked_private']}")
    print(f"    Validation failed:  {stats['validation_failed']}")
    print(f"    Skipped:            {stats['skipped']}")


if __name__ == '__main__':
    main()

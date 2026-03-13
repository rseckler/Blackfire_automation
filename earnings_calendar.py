#!/usr/bin/env python3
"""
Earnings Calendar — Fetches upcoming earnings dates for public companies.

Usage:
  python3 earnings_calendar.py          # Dry-run: show what would be collected
  python3 earnings_calendar.py --apply  # Actually insert into company_events table

Schedule: Weekly (Sundays) via cron — earnings dates don't change frequently.

Data flow:
  Supabase (companies, listing_status='public', symbol NOT NULL)
    → yfinance (earnings_dates / calendar)
    → Supabase (company_events, event_type='earnings')
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
import yfinance as yf

load_dotenv()

import supabase_helper

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BLACKLIST_FILE = os.path.join(SCRIPT_DIR, 'invalid_companies.json')

# Processing config
BATCH_SIZE = 50          # Companies per batch before a longer pause
BATCH_PAUSE = 5          # Seconds between batches
REQUEST_DELAY = 1.0      # Seconds between individual yfinance calls
MAX_ERRORS_IN_ROW = 10   # Stop if too many consecutive errors (likely rate-limited)


def load_blacklist() -> set:
    """Load company IDs from the shared invalid_companies.json blacklist."""
    try:
        with open(BLACKLIST_FILE, 'r') as f:
            data = json.load(f)
            cutoff = datetime.now().timestamp() - (30 * 86400)
            return {k for k, v in data.items() if v > cutoff}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def get_public_companies_with_symbols() -> list:
    """Fetch all public companies that have a symbol."""
    print("\n  Fetching public companies with symbols from Supabase...")

    client = supabase_helper.get_client()
    all_rows = []
    page_size = 1000
    offset = 0

    while True:
        response = (
            client.table('companies')
            .select('id, symbol, name')
            .eq('listing_status', 'public')
            .neq('symbol', '')
            .not_.is_('symbol', 'null')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = response.data
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    print(f"   Found {len(all_rows)} public companies with symbols")
    return all_rows


def get_existing_earnings_events(company_ids: list) -> dict:
    """Fetch existing earnings events to avoid duplicates.
    Returns dict of {(company_id, event_date_str): event_id}.
    """
    print("  Loading existing earnings events for deduplication...")

    client = supabase_helper.get_client()
    existing = {}
    page_size = 1000

    # Process in chunks since company_ids can be large
    for chunk_start in range(0, len(company_ids), 200):
        chunk = company_ids[chunk_start:chunk_start + 200]
        offset = 0

        while True:
            response = (
                client.table('company_events')
                .select('id, company_id, event_date')
                .eq('event_type', 'earnings')
                .in_('company_id', chunk)
                .range(offset, offset + page_size - 1)
                .execute()
            )
            for row in response.data:
                key = (row['company_id'], row.get('event_date'))
                existing[key] = row['id']

            if len(response.data) < page_size:
                break
            offset += page_size

    print(f"   Found {len(existing)} existing earnings events")
    return existing


def fetch_earnings_for_symbol(symbol: str) -> list:
    """Fetch earnings dates from yfinance for a single symbol.
    Returns list of dicts with: event_date, description (estimated EPS if available).
    """
    results = []

    try:
        ticker = yf.Ticker(symbol)

        # Method 1: ticker.earnings_dates — has historical + upcoming with EPS estimates
        try:
            earnings_dates = ticker.earnings_dates
            if earnings_dates is not None and not earnings_dates.empty:
                for date_idx, row in earnings_dates.iterrows():
                    # date_idx is a Timestamp (the earnings date)
                    event_date = date_idx.strftime('%Y-%m-%d')

                    # Build description with EPS estimates if available
                    parts = []
                    eps_estimate = row.get('EPS Estimate')
                    eps_actual = row.get('Reported EPS')
                    surprise = row.get('Surprise(%)')

                    if eps_estimate is not None and str(eps_estimate) != 'nan':
                        parts.append(f"EPS Est: {eps_estimate}")
                    if eps_actual is not None and str(eps_actual) != 'nan':
                        parts.append(f"EPS Actual: {eps_actual}")
                    if surprise is not None and str(surprise) != 'nan':
                        parts.append(f"Surprise: {surprise}%")

                    description = ' | '.join(parts) if parts else None

                    results.append({
                        'event_date': event_date,
                        'description': description,
                    })
                return results
        except Exception:
            pass

        # Method 2: ticker.calendar — simpler, just upcoming earnings date
        try:
            cal = ticker.calendar
            if cal is not None:
                # calendar can be a dict with 'Earnings Date' key (list of dates)
                if isinstance(cal, dict):
                    earnings_list = cal.get('Earnings Date', [])
                    for ed in earnings_list:
                        if hasattr(ed, 'strftime'):
                            event_date = ed.strftime('%Y-%m-%d')
                        else:
                            event_date = str(ed)[:10]
                        results.append({
                            'event_date': event_date,
                            'description': None,
                        })
        except Exception:
            pass

    except Exception:
        pass

    return results


def run(apply: bool = False):
    """Main function: fetch earnings dates and optionally insert into Supabase."""
    print("\n" + "=" * 70)
    print(f"  EARNINGS CALENDAR {'(APPLY MODE)' if apply else '(DRY-RUN)'}")
    print("=" * 70)

    start_time = datetime.now()

    # 1. Load blacklist
    blacklist = load_blacklist()
    print(f"  Blacklisted companies: {len(blacklist)}")

    # 2. Get public companies with symbols
    companies = get_public_companies_with_symbols()
    if not companies:
        print("  No public companies with symbols found. Exiting.")
        return

    # 3. Filter out blacklisted companies
    companies = [c for c in companies if c['id'] not in blacklist]
    print(f"  Companies to process (after blacklist filter): {len(companies)}")

    # 4. Load existing events for deduplication
    company_ids = [c['id'] for c in companies]
    existing_events = get_existing_earnings_events(company_ids)

    # 5. Process companies
    stats = {
        'processed': 0,
        'with_earnings': 0,
        'events_found': 0,
        'events_new': 0,
        'events_duplicate': 0,
        'events_inserted': 0,
        'errors': 0,
        'skipped_no_data': 0,
    }

    events_to_insert = []
    consecutive_errors = 0

    for i, company in enumerate(companies):
        company_id = company['id']
        symbol = company['symbol']
        name = company.get('name', symbol)

        # Rate limiting
        if i > 0:
            time.sleep(REQUEST_DELAY)

        # Batch pause
        if i > 0 and i % BATCH_SIZE == 0:
            print(f"\n   Batch pause after {i} companies... ({stats['events_new']} new events so far)")
            time.sleep(BATCH_PAUSE)

        # Fetch earnings
        try:
            earnings = fetch_earnings_for_symbol(symbol)
            consecutive_errors = 0
        except Exception as e:
            stats['errors'] += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_ERRORS_IN_ROW:
                print(f"\n  STOPPING: {MAX_ERRORS_IN_ROW} consecutive errors — likely rate-limited")
                print(f"  Last error: {e}")
                break
            continue

        stats['processed'] += 1

        if not earnings:
            stats['skipped_no_data'] += 1
            continue

        stats['with_earnings'] += 1
        stats['events_found'] += len(earnings)

        for event in earnings:
            event_date = event['event_date']

            # Deduplication check
            key = (company_id, event_date)
            if key in existing_events:
                stats['events_duplicate'] += 1
                continue

            stats['events_new'] += 1

            row = {
                'company_id': company_id,
                'event_type': 'earnings',
                'event_date': event_date,
                'description': event.get('description'),
                'source': 'yfinance',
            }
            events_to_insert.append(row)

            # Also mark as "seen" to deduplicate within this run
            existing_events[key] = 'pending'

        # Progress
        if (i + 1) % 100 == 0:
            print(f"   Processed {i + 1}/{len(companies)} — "
                  f"{stats['events_new']} new events, {stats['errors']} errors")

    # 6. Insert events
    if apply and events_to_insert:
        print(f"\n  Inserting {len(events_to_insert)} new earnings events...")
        client = supabase_helper.get_client()

        insert_batch_size = 100
        for batch_start in range(0, len(events_to_insert), insert_batch_size):
            batch = events_to_insert[batch_start:batch_start + insert_batch_size]
            try:
                client.table('company_events').insert(batch).execute()
                stats['events_inserted'] += len(batch)
            except Exception as e:
                print(f"   Insert error (batch {batch_start}): {e}")
                # Try individual inserts for this batch
                for row in batch:
                    try:
                        client.table('company_events').insert(row).execute()
                        stats['events_inserted'] += 1
                    except Exception as e2:
                        print(f"   Failed to insert event for {row['company_id']}: {e2}")

    # 7. Log to sync_history
    end_time = datetime.now()
    duration = int((end_time - start_time).total_seconds())

    if apply:
        supabase_helper.log_sync_history({
            'name': f"Earnings Calendar {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            'start_time': start_time,
            'end_time': end_time,
            'stocks_processed': stats['processed'],
            'stocks_updated': stats['events_inserted'],
            'success': stats['errors'] == 0,
            'error_message': f"Errors: {stats['errors']}" if stats['errors'] > 0 else None,
        })

    # 8. Print summary
    print("\n" + "=" * 70)
    print(f"  EARNINGS CALENDAR {'COMPLETE' if apply else 'DRY-RUN COMPLETE'}")
    print("=" * 70)
    print(f"   Companies processed:    {stats['processed']}")
    print(f"   With earnings data:     {stats['with_earnings']}")
    print(f"   No data available:      {stats['skipped_no_data']}")
    print(f"   Errors:                 {stats['errors']}")
    print(f"   Total events found:     {stats['events_found']}")
    print(f"   Duplicate (skipped):    {stats['events_duplicate']}")
    print(f"   New events:             {stats['events_new']}")
    if apply:
        print(f"   Events inserted:        {stats['events_inserted']}")
    else:
        print(f"   Events to insert:       {stats['events_new']} (use --apply to insert)")
    print(f"   Duration:               {duration} seconds")
    print()

    # Show sample of new events
    if events_to_insert:
        print("  Sample new events (first 10):")
        for event in events_to_insert[:10]:
            desc = f" — {event['description']}" if event.get('description') else ""
            print(f"   {event['event_date']}: {event['company_id'][:8]}...{desc}")
        if len(events_to_insert) > 10:
            print(f"   ... and {len(events_to_insert) - 10} more")


if __name__ == '__main__':
    apply_mode = '--apply' in sys.argv
    run(apply=apply_mode)

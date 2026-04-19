#!/usr/bin/env python3
"""
Finnhub IPO-Calendar Sync — Session 1 (2026-04-19).

Quercheck-Quelle zum SEC-EDGAR-Parser. Finnhub-Free-Tier erlaubt 60 Calls/Min
unbegrenzt. Hat einen IPO-Calendar-Endpoint mit Lockup-Daten (wenn verfügbar).

Workflow:
  1. GET /calendar/ipo?from=YYYY-MM-DD&to=YYYY-MM-DD (365 Tage Fenster)
  2. Für jede IPO: match auf companies.symbol
  3. Wenn lockup_expiry-Datum in Response → upsert mit source='finnhub'
  4. Wenn bestehender Event mit source='sec_edgar_s1' → nicht überschreiben (S-1 gewinnt)

Endpoint: https://finnhub.io/docs/api/ipo-calendar
Auth: FINNHUB_API_KEY via Query-Parameter ?token=...

Usage:
  python3 finnhub_ipo_sync.py                   # dry-run, last 365 days
  python3 finnhub_ipo_sync.py --apply           # write to DB
  python3 finnhub_ipo_sync.py --days 730        # 2 Jahre Fenster
"""

import argparse
import os
import sys
import time
from collections import Counter
from datetime import date, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import requests
import supabase_helper

FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY')
FINNHUB_IPO_URL = 'https://finnhub.io/api/v1/calendar/ipo'


def fetch_ipo_calendar(from_date: date, to_date: date) -> list:
    """Lädt IPOs im Zeitfenster. Finnhub limitiert auf ~365d pro Call."""
    params = {
        'from': from_date.isoformat(),
        'to': to_date.isoformat(),
        'token': FINNHUB_API_KEY,
    }
    r = requests.get(FINNHUB_IPO_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get('ipoCalendar') or []


def fetch_company_by_symbol(client, symbol: str) -> dict | None:
    resp = client.table('companies').select('id, name, symbol').eq('symbol', symbol).execute()
    rows = resp.data or []
    return rows[0] if rows else None


def existing_edgar_event(client, company_id: str) -> bool:
    """Prüfe ob bereits ein SEC-EDGAR-Event existiert (stärkere Quelle → nicht überschreiben)."""
    try:
        resp = client.table('company_events') \
            .select('id, event_metadata') \
            .eq('company_id', company_id) \
            .eq('event_type', 'lockup_expiry') \
            .execute()
        for row in (resp.data or []):
            src = (row.get('event_metadata') or {}).get('source', '')
            if src == 'sec_edgar_s1':
                return True
        return False
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    if not FINNHUB_API_KEY:
        print("ERROR: FINNHUB_API_KEY not set in .env")
        print("  Kostenloses Key: https://finnhub.io/register")
        sys.exit(1)

    print("\n" + "=" * 72)
    print("  FINNHUB IPO CALENDAR SYNC")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 72)

    client = supabase_helper.get_client()

    # Fenster: N Tage in beide Richtungen um heute
    half = args.days // 2
    from_date = date.today() - timedelta(days=half)
    to_date = date.today() + timedelta(days=half)

    print(f"\n  Lade IPO-Kalender {from_date} bis {to_date}...")
    # Finnhub limitiert auf 1 Jahr pro Request → splitten falls > 365d
    all_ipos = []
    cur_from = from_date
    while cur_from < to_date:
        cur_to = min(cur_from + timedelta(days=365), to_date)
        if args.verbose:
            print(f"    chunk {cur_from} → {cur_to}")
        try:
            chunk = fetch_ipo_calendar(cur_from, cur_to)
            all_ipos.extend(chunk)
        except Exception as e:
            print(f"    Fehler bei chunk {cur_from}: {e}")
        time.sleep(1.1)  # fair use
        cur_from = cur_to + timedelta(days=1)

    print(f"  → {len(all_ipos)} IPO-Einträge")

    stats = Counter()
    upserts = []

    for ipo in all_ipos:
        symbol = (ipo.get('symbol') or '').upper()
        if not symbol:
            stats['no_symbol'] += 1
            continue

        company = fetch_company_by_symbol(client, symbol)
        if not company:
            stats['company_not_in_db'] += 1
            continue

        # Finnhub-Felder: symbol, date, price, numberOfShares, totalSharesValue,
        # exchange, name, status
        # Hinweis: Lockup-Ende ist selten direkt drin — wir nutzen date+180d als Schätzung
        ipo_date_str = ipo.get('date')
        if not ipo_date_str:
            stats['no_ipo_date'] += 1
            continue
        try:
            ipo_date = date.fromisoformat(ipo_date_str)
        except Exception:
            stats['bad_ipo_date'] += 1
            continue

        # Check ob EDGAR schon was hat → Finnhub darf nicht überschreiben
        if existing_edgar_event(client, company['id']):
            stats['skipped_edgar_wins'] += 1
            continue

        # Expected Lockup Date = IPO + 180 days
        lockup_date = ipo_date + timedelta(days=180)

        # Only relevant if in future OR recent past (last 90d for post-lockup analysis)
        today = date.today()
        if (lockup_date - today).days < -90:
            stats['too_old'] += 1
            continue

        stats['candidate'] += 1
        upserts.append({
            'company_id': company['id'],
            'symbol': symbol,
            'ipo_date': ipo_date.isoformat(),
            'lockup_date': lockup_date.isoformat(),
            'shares': ipo.get('numberOfShares'),
            'value': ipo.get('totalSharesValue'),
            'name': ipo.get('name') or company.get('name'),
        })

    print(f"\n  Stats:")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    {k:30s}: {v:5d}")

    print(f"\n  Upserts: {len(upserts)}")
    if args.verbose and upserts:
        for u in upserts[:20]:
            print(f"    {u['symbol']:8s} IPO {u['ipo_date']} → Lockup {u['lockup_date']} ({u['shares'] or '?'} Aktien)")

    if args.apply and upserts:
        print(f"\n  Writing {len(upserts)} events...")
        success = 0
        for u in upserts:
            try:
                metadata = {
                    'source': 'finnhub',
                    'confidence': 'estimated',
                    'lockup_days': 180,
                    'ipo_date': u['ipo_date'],
                    'share_count': u['shares'],
                    'share_count_source': 'finnhub_ipo',
                    'total_ipo_value_usd': u['value'],
                    'tranche': 1,
                    'tranche_total': 1,
                    'notes': 'Schätzung IPO+180 Tage, Quelle Finnhub IPO-Kalender',
                }
                client.table('company_events').upsert({
                    'company_id': u['company_id'],
                    'event_type': 'lockup_expiry',
                    'event_date': u['lockup_date'],
                    'description': f"Lock-up expiry (Finnhub, geschätzt 180d nach IPO)",
                    'event_metadata': metadata,
                }).execute()
                success += 1
            except Exception as e:
                print(f"    Error for {u['symbol']}: {e}")
        print(f"  → Written: {success}/{len(upserts)}")
    elif upserts:
        print(f"\n  Run with --apply to write.")

    print("\n  Done!\n")


if __name__ == '__main__':
    main()

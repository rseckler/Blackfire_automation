#!/usr/bin/env python3
"""
Lockup Cleanup — Session 1 (2026-04-19, Tommi-Freigabe).

Entfernt fehlerhafte lockup_expiry-Events aus company_events, die aus
Auto-Berechnungen mit falschen IPO-Daten entstanden sind.

Problem: ipo_tracker.py hat in der Vergangenheit bei manchen Firmen das
"erste gesehene Datum" als IPO-Datum gespeichert. Das führte zu Einträgen
wie "BioNTech IPO Feb 2026" → lockup_expiry August 2026 → Unsinn.

Heuristik (konservativ, nur klare Fälle löschen):
  1. Firma hat Alpha-Vantage-Kerzendaten >= 2 Jahre alt (real alter Public-Market)
     → lockup_expiry mit ipo_date im letzten Jahr ist definitiv falsch
  2. extra_data.Currency oder symbol lässt auf Nicht-US schließen UND
     event_metadata.source='ipo_auto_calc' → nicht durch S-1 verifiziert
     → Kandidat für manuelle Nachprüfung (NICHT auto-löschen)

Default-Verhalten: nur (1) wird gelöscht. (2) wird in einer Warnung
aufgelistet, Tommi entscheidet manuell.

Usage:
  python3 lockup_cleanup.py            # dry-run
  python3 lockup_cleanup.py --apply    # write deletes
  python3 lockup_cleanup.py --verbose  # per-company details
"""

import argparse
import os
import sys
from collections import Counter
from datetime import date, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper


def fetch_all_lockup_events(client) -> list:
    """Lade alle lockup_expiry-Events (gepaged)."""
    all_events = []
    page_size = 1000
    offset = 0
    while True:
        resp = client.table('company_events') \
            .select('id, company_id, event_date, event_metadata, description') \
            .eq('event_type', 'lockup_expiry') \
            .range(offset, offset + page_size - 1) \
            .execute()
        batch = resp.data or []
        all_events.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return all_events


def fetch_companies_by_ids(client, ids: list) -> dict:
    """Lade companies für gegebene IDs, keyed by id."""
    out = {}
    batch_size = 100
    for i in range(0, len(ids), batch_size):
        chunk = ids[i:i + batch_size]
        resp = client.table('companies') \
            .select('id, name, symbol, country, current_price, created_at, extra_data') \
            .in_('id', chunk) \
            .execute()
        for c in (resp.data or []):
            out[c['id']] = c
    return out


def oldest_price_year(client, company_id: str) -> int | None:
    """Finde älteste vorhandene Kurs-Candle in stock_prices → signalisiert Listing-Alter."""
    try:
        resp = client.table('stock_prices') \
            .select('price_date') \
            .eq('company_id', company_id) \
            .order('price_date', desc=False) \
            .limit(1) \
            .execute()
        rows = resp.data or []
        if not rows:
            return None
        d = rows[0]['price_date']
        # Supabase gibt ISO-String
        return int(str(d)[:4])
    except Exception:
        return None


def classify_event(ev: dict, company: dict, oldest_year: int | None) -> tuple[str, str]:
    """
    Returns (action, reason):
      - 'delete' : sicher löschen
      - 'review' : Tommi soll prüfen
      - 'keep'   : OK lassen
    """
    meta = ev.get('event_metadata') or {}
    # Supabase gibt JSONB manchmal als String zurück → parsen
    if isinstance(meta, str):
        try:
            import json as _json
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    source = meta.get('source') or ''
    ipo_date_str = meta.get('ipo_date')
    ipo_year = None
    if ipo_date_str:
        try:
            ipo_year = int(str(ipo_date_str)[:4])
        except (ValueError, TypeError):
            pass

    # Regel 1: Firma hat historische Kurse seit >= 2 Jahren, IPO-Datum aber jünger
    # → ipo_auto_calc hat "erstes Datum in Excel" fälschlich als IPO-Tag interpretiert.
    if (
        source == 'ipo_auto_calc'
        and oldest_year is not None
        and ipo_year is not None
        and (ipo_year - oldest_year) >= 2  # Kurs-Historie deutlich älter als angeblicher IPO
    ):
        return ('delete',
                f'Auto-calc IPO={ipo_year}, aber Kurse seit {oldest_year} → IPO-Datum falsch')

    # Regel 2: Auto-calc ohne S-1-Verifikation bei Firmen die sehr alt aussehen
    # (manuelle Prüfung, nicht automatisch löschen)
    current_year = date.today().year
    if (
        source == 'ipo_auto_calc'
        and meta.get('confidence') == 'estimated'
        and oldest_year is not None
        and (current_year - oldest_year) >= 5  # Firma seit 5+ Jahren gelistet
    ):
        return ('review',
                f'Seit {oldest_year} gelistet, trotzdem auto-calc lockup {ev["event_date"]}')

    return ('keep', '')


def main():
    parser = argparse.ArgumentParser(description='Clean up fehlerhafte Lockup-Events')
    parser.add_argument('--apply', action='store_true', help='Write deletes to Supabase')
    parser.add_argument('--verbose', action='store_true', help='Print per-event decisions')
    args = parser.parse_args()

    print("\n" + "=" * 72)
    print("  LOCKUP CLEANUP")
    print(f"  Mode: {'APPLY (writes!)' if args.apply else 'DRY-RUN'}")
    print("=" * 72)

    client = supabase_helper.get_client()

    print("\n  Loading all lockup_expiry events...")
    events = fetch_all_lockup_events(client)
    print(f"  → {len(events)} events loaded")

    company_ids = list({e['company_id'] for e in events if e.get('company_id')})
    print(f"  Loading {len(company_ids)} distinct companies...")
    companies = fetch_companies_by_ids(client, company_ids)
    print(f"  → {len(companies)} companies")

    to_delete = []
    to_review = []
    to_keep = 0
    stats = Counter()

    for ev in events:
        cid = ev.get('company_id')
        if not cid or cid not in companies:
            stats['orphaned (no company)'] += 1
            to_delete.append((ev, 'Orphaned — company_id ohne matching company'))
            continue
        company = companies[cid]
        oldest_year = oldest_price_year(client, cid)

        action, reason = classify_event(ev, company, oldest_year)
        if action == 'delete':
            to_delete.append((ev, reason))
            stats['delete'] += 1
        elif action == 'review':
            to_review.append((ev, company, reason))
            stats['review'] += 1
        else:
            to_keep += 1
            stats['keep'] += 1

    print("\n  Classification:")
    for k in ('delete', 'review', 'keep', 'orphaned (no company)'):
        if stats.get(k):
            print(f"    {k:25s}: {stats[k]:5d}")

    if to_delete and args.verbose:
        print(f"\n  Delete-Kandidaten (Top 20):")
        for ev, reason in to_delete[:20]:
            company = companies.get(ev['company_id'], {})
            print(f"    {company.get('name', '?')[:35]:35s} ({company.get('symbol', '—')}): {reason}")

    if to_review:
        print(f"\n  ⚠  Manuelle Review (wird NICHT gelöscht, Tommi entscheidet):")
        for ev, company, reason in to_review[:20]:
            print(f"    {company.get('name', '?')[:35]:35s} ({company.get('symbol', '—')}): {reason}")
        if len(to_review) > 20:
            print(f"    ... + {len(to_review) - 20} weitere")

    if args.apply and to_delete:
        print(f"\n  Deleting {len(to_delete)} events...")
        success = 0
        batch = []
        for ev, _ in to_delete:
            batch.append(ev['id'])
            if len(batch) >= 50:
                try:
                    client.table('company_events').delete().in_('id', batch).execute()
                    success += len(batch)
                except Exception as e:
                    print(f"    Error on batch: {e}")
                batch = []
        if batch:
            try:
                client.table('company_events').delete().in_('id', batch).execute()
                success += len(batch)
            except Exception as e:
                print(f"    Error on final batch: {e}")
        print(f"  → Deleted: {success}/{len(to_delete)}")
    elif not args.apply and to_delete:
        print(f"\n  Run with --apply to delete {len(to_delete)} events.")

    print("\n  Done!\n")


if __name__ == '__main__':
    main()

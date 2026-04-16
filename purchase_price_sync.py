#!/usr/bin/env python3
"""
Purchase-Price Sync — v1.4 Etappe 1 (Tommi 2026-04-16).

Liest die währungs-spezifischen Purchase-Spalten aus extra_data (stammen aus
Excel BF–BM Spalten, gesynct von sync_final.py) und schreibt sie in Tommi's
user_entry_prices Rows. Per (User, Company).

Geschäftsregeln (per Tommi's Antworten zum v1.4-Plan):
  - Frage 1 Option A: Jede Purchase-Spalte = Kauf in Währung je Handelsplatz
  - Frage 2: Purchase_2_$ = veraltet, IGNORIEREN
  - Frage 3a: Wähle die Spalte passend zur Company-Handelsplatz-Währung
  - Frage 4 Option B: Nur Tommi pflegt Purchase (Tommi's user_id hardcoded)
  - Frage 5 Option A: Purchase + Entry getrennte Felder
  - Purchased_Amount: laut Tommi "momentan nicht berücksichtigen"

Verhalten:
  - Wenn Excel-Purchase-Wert vorhanden (> 0, non-whitespace) → upsert in user_entry_prices
  - Wenn Excel-Zelle leer/0/whitespace → NICHT löschen (könnte Tommi's manuellen
    Edit nicht überschreiben; Purchase bleibt bis explizit gecleared)
  - Dubletten nicht möglich (UNIQUE user_id × company_id)

Usage:
  python3 purchase_price_sync.py            # dry-run (preview only)
  python3 purchase_price_sync.py --apply    # write to Supabase
  python3 purchase_price_sync.py --verbose  # print per-company details
"""

import argparse
import os
import sys
from collections import Counter
from datetime import datetime
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# Tommi's user_id — er pflegt das Excel, ihm gehören die Purchase-Preise
TOMMI_USER_ID = '092aac8f-b80d-4de3-b091-e35a908df11b'

# Mapping: Excel-Spalten-Name → ISO-Währungscode.
# Purchase_2_$ und Purchased_Amount werden bewusst NICHT gelistet (veraltet/irrelevant).
CURRENCY_COLUMN_MAP = {
    'Purchase_$':        'USD',
    'Purchase_€':        'EUR',
    'Purchase_UK Pounds': 'GBP',
    'Purchase_CNY':      'CNY',
    'Purchase_JPY':      'JPY',
    'Purchase_HK$':      'HKD',
    'Purchase_AS$':      'AUD',
    'Purchase_Korea':    'KRW',
}

# Fallback-Mapping von Länder-Code auf Default-Währung (Symbolic,
# falls extra_data.Currency nicht gesetzt). Hält Parität zu src/lib/buy-zone.ts.
EUR_COUNTRIES = {'DE', 'AT', 'FR', 'IT', 'ES', 'NL', 'BE', 'IE', 'FI', 'PT', 'GR', 'LU'}


def default_currency_for_country(country: str) -> str:
    if not country:
        return 'USD'
    c = country.strip().upper()
    if c in EUR_COUNTRIES:
        return 'EUR'
    if c == 'CH':
        return 'CHF'
    if c in ('GB', 'UK'):
        return 'GBP'
    if c == 'CN':
        return 'CNY'
    if c == 'JP':
        return 'JPY'
    if c == 'HK':
        return 'HKD'
    if c == 'AU':
        return 'AUD'
    if c in ('KR', 'KOR'):
        return 'KRW'
    return 'USD'


def determine_expected_currency(company: dict) -> str:
    """Bestimme die Währung, in der der Kurs dieser Company erwartet wird."""
    extra = company.get('extra_data') or {}
    currency = (extra.get('Currency') or '').strip().upper()
    if currency:
        return currency
    return default_currency_for_country(company.get('country') or '')


def parse_purchase_value(raw) -> float | None:
    """Parse ein Zell-Value zu einem positiven Float, oder None wenn ungültig/leer."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Excel oft mit Komma statt Punkt
    s = s.replace(',', '.').replace(' ', '')
    try:
        v = float(s)
    except (ValueError, TypeError):
        return None
    if v <= 0:
        return None
    return v


def extract_purchase_price(company: dict, expected_currency: str) -> tuple:
    """
    Returns (price, currency, source_column) oder (None, None, None).

    Strategie:
      1. Primär: Spalte die zur expected_currency passt
      2. Fallback: Falls Primär leer, erste non-empty Spalte nehmen (und
         mit deren Währung arbeiten — warn-case)

    Beide Strategien filtern Purchase_2_$ und Purchased_Amount aus (veraltet).
    """
    extra = company.get('extra_data') or {}

    # 1. Primär: Spalte zur erwarteten Währung
    primary_column = None
    for col, curr in CURRENCY_COLUMN_MAP.items():
        if curr == expected_currency:
            primary_column = col
            break

    if primary_column and primary_column in extra:
        price = parse_purchase_value(extra[primary_column])
        if price is not None:
            return price, expected_currency, primary_column

    # 2. Fallback: irgendeine non-empty Currency-Spalte (Warn-Case)
    for col, curr in CURRENCY_COLUMN_MAP.items():
        if col == primary_column:
            continue  # primary schon geprüft, war leer
        if col not in extra:
            continue
        price = parse_purchase_value(extra[col])
        if price is not None:
            return price, curr, col

    return None, None, None


def load_companies(client) -> list:
    """Lädt alle companies mit extra_data. Wir filtern dann lokal auf die mit Purchase-Spalten."""
    return supabase_helper.get_all_companies(
        'id, name, symbol, country, extra_data'
    )


def load_existing_entries(client) -> dict:
    """Lädt bestehende user_entry_prices für Tommi.
    Returns {company_id: {entry_price, entry_currency, purchase_price, purchase_currency}}"""
    try:
        resp = client.table('user_entry_prices') \
            .select('company_id, entry_price, entry_currency, purchase_price, purchase_currency') \
            .eq('user_id', TOMMI_USER_ID) \
            .execute()
        return {row['company_id']: row for row in (resp.data or [])}
    except Exception as e:
        print(f"  Warning: Could not load existing entries: {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(description='Sync Tommi Excel Purchase-Preise zu user_entry_prices')
    parser.add_argument('--apply', action='store_true', help='Write to Supabase')
    parser.add_argument('--verbose', action='store_true', help='Print per-company decisions')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  PURCHASE PRICE SYNC")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print(f"  Target user: Tommi ({TOMMI_USER_ID[:8]}…)")
    print("=" * 70)

    client = supabase_helper.get_client()

    print("\n  Loading companies...")
    companies = load_companies(client)
    print(f"  Loaded {len(companies)} companies")

    print(f"\n  Loading Tommi's existing user_entry_prices...")
    existing = load_existing_entries(client)
    print(f"  Found {len(existing)} existing rows for Tommi")

    stats = Counter()
    updates = []       # new or changed purchase_price
    unchanged = 0      # Excel value same as DB
    source_cols = Counter()
    fallback_hits = []  # companies using fallback column (warn-case)

    for c in companies:
        extra = c.get('extra_data') or {}

        # Skip companies without ANY Purchase-Spalte
        has_any_purchase_col = any(col in extra for col in CURRENCY_COLUMN_MAP.keys())
        if not has_any_purchase_col:
            stats['no_purchase_columns'] += 1
            continue

        expected_currency = determine_expected_currency(c)
        price, currency, source = extract_purchase_price(c, expected_currency)

        if price is None:
            stats['all_purchase_cols_empty'] += 1
            continue

        source_cols[source] += 1
        if currency != expected_currency:
            fallback_hits.append({
                'name': c.get('name'),
                'expected': expected_currency,
                'got': currency,
                'source': source,
            })
            stats['fallback_used'] += 1

        existing_row = existing.get(c['id'])
        if existing_row:
            existing_price = existing_row.get('purchase_price')
            existing_curr = existing_row.get('purchase_currency')
            # Supabase NUMERIC wird manchmal als String zurückgegeben
            try:
                existing_price_f = float(existing_price) if existing_price is not None else None
            except (ValueError, TypeError):
                existing_price_f = None
            if existing_price_f == price and existing_curr == currency:
                unchanged += 1
                continue
            stats['updated'] += 1
        else:
            stats['new'] += 1

        updates.append({
            'company_id': c['id'],
            'name': c.get('name'),
            'price': price,
            'currency': currency,
            'source_column': source,
            'had_existing': existing_row is not None,
        })

    # Report
    print(f"\n  Stats:")
    for key, count in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    {key:30s}: {count:5d}")
    print(f"    {'unchanged (already synced)':30s}: {unchanged:5d}")

    if source_cols:
        print(f"\n  Source-Spalten-Verteilung:")
        for col, count in sorted(source_cols.items(), key=lambda x: -x[1]):
            print(f"    {col:22s}: {count:5d}")

    if fallback_hits:
        print(f"\n  ⚠  Fallback genutzt (Currency-Mismatch, erste non-empty Spalte):")
        for f in fallback_hits[:10]:
            print(f"    {f['name'][:30]:30s}  expected={f['expected']}  got={f['got']}  from={f['source']}")
        if len(fallback_hits) > 10:
            print(f"    ... + {len(fallback_hits) - 10} weitere")

    print(f"\n  Updates to apply: {len(updates)}")
    if updates and args.verbose:
        print(f"\n  Sample (first 20):")
        for u in updates[:20]:
            action = 'UPDATE' if u['had_existing'] else 'NEW   '
            print(f"    [{action}] {u['name'][:30]:30s}  {u['currency']} {u['price']:>10.2f}  ← {u['source_column']}")

    if args.apply and updates:
        print(f"\n  Writing {len(updates)} upserts...")
        now_iso = datetime.now().isoformat()
        success = 0
        for u in updates:
            try:
                client.table('user_entry_prices').upsert(
                    {
                        'user_id': TOMMI_USER_ID,
                        'company_id': u['company_id'],
                        'purchase_price': u['price'],
                        'purchase_currency': u['currency'],
                        'purchase_imported_at': now_iso,
                        # entry_price / entry_currency bleiben wie sie sind (nicht überschreiben)
                    },
                    on_conflict='user_id,company_id',
                ).execute()
                success += 1
            except Exception as e:
                print(f"    Error upserting for {u['name']}: {e}")
        print(f"  Written: {success}/{len(updates)}")
    elif not args.apply and updates:
        print(f"\n  Run with --apply to write changes to Supabase")

    print("\n  Done!\n")


if __name__ == '__main__':
    main()

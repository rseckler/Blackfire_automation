#!/usr/bin/env python3
"""
Excel Entry-Price Sync — v1.4 Rollback (Tommi 2026-04-17).

HISTORIE:
  v1.4 Etappe 1 (16.04.): Excel Purchase_* → user_entry_prices.purchase_price
                          → löste Portfolio-Modus im Basket aus (Einstand/Perf%).
  v1.4 Rollback  (17.04.): Tommi's Feedback — Basket soll REIN Selektion/Buy-Zone
                          sein. Excel Purchase_* sind laut Tommi = Entry-Preise
                          (Wunsch-Kaufkurse, nicht Einstandspreise).
                          Einstand/Performance kommt später in /portfolio.

Was dieses Script jetzt tut:
  • Liest Excel Purchase_*-Spalten aus company.extra_data
  • Schreibt Wert in user_entry_prices.entry_price (NICHT mehr purchase_price)
  • Setzt entry_source='excel' auf neu angelegten/aktualisierten Zeilen
  • ÜBERSCHREIBT NIEMALS Zeilen mit entry_source='manual' (User-UI-Edits)
  • Alle 2h via morning_sync. User behält UI-Kontrolle.

Konflikt-Regel (Kern):
  entry_source='manual'  → Excel-Sync skipt diese Zeile komplett
  entry_source='excel' oder NULL → Excel-Sync darf upsert machen

User bekommt im UI einen "Zurück auf Excel-Wert"-Button (später umsetzbar),
falls er einen manuellen Entry wieder auf Excel tracken lassen will.

Usage:
  python3 purchase_price_sync.py            # dry-run (preview only)
  python3 purchase_price_sync.py --apply    # write to Supabase
  python3 purchase_price_sync.py --verbose  # per-company Details
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

# Tommi's user_id — er pflegt das Excel, ihm gehören die Entry-Preise aus Excel
TOMMI_USER_ID = '092aac8f-b80d-4de3-b091-e35a908df11b'

# Mapping: Excel-Spalten-Name → ISO-Währungscode.
# Purchase_2_$ und Purchased_Amount werden bewusst NICHT gelistet (veraltet/irrelevant).
# Reihenfolge = Priorität bei Mehrfach-Füllung (USD zuerst, dann EUR, ...).
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


def parse_purchase_value(raw) -> float | None:
    """Parse ein Zell-Value zu positivem Float, oder None wenn ungültig/leer."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace(',', '.').replace(' ', '')
    try:
        v = float(s)
    except (ValueError, TypeError):
        return None
    if v <= 0:
        return None
    return v


def extract_entry_price(company: dict) -> tuple:
    """
    Returns (price, currency, source_column, other_filled_columns).

    Priorität = Insertion-Order der CURRENCY_COLUMN_MAP. Wenn mehrere
    Purchase-Spalten gefüllt sind, wird die erste genommen + Warning.
    """
    extra = company.get('extra_data') or {}

    filled = []
    for col, curr in CURRENCY_COLUMN_MAP.items():
        if col not in extra:
            continue
        price = parse_purchase_value(extra[col])
        if price is not None:
            filled.append((col, curr, price))

    if not filled:
        return None, None, None, []

    primary_col, primary_curr, primary_price = filled[0]
    others = [(c, cu, p) for (c, cu, p) in filled[1:]]
    return primary_price, primary_curr, primary_col, others


def load_companies(client) -> list:
    return supabase_helper.get_all_companies(
        'id, name, symbol, country, extra_data'
    )


def load_existing_entries(client) -> dict:
    """Lädt bestehende user_entry_prices für Tommi (mit entry_source)."""
    try:
        resp = client.table('user_entry_prices') \
            .select('company_id, entry_price, entry_currency, entry_source') \
            .eq('user_id', TOMMI_USER_ID) \
            .execute()
        return {row['company_id']: row for row in (resp.data or [])}
    except Exception as e:
        print(f"  Warning: Could not load existing entries: {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(description='Sync Tommi Excel Purchase-Preise → user_entry_prices.entry_price')
    parser.add_argument('--apply', action='store_true', help='Write to Supabase')
    parser.add_argument('--verbose', action='store_true', help='Print per-company decisions')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  EXCEL ENTRY-PRICE SYNC  (v1.4 Rollback)")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print(f"  Target user: Tommi ({TOMMI_USER_ID[:8]}…)")
    print(f"  Regel: Excel Purchase_* → entry_price (source='excel')")
    print(f"         Manuelle UI-Edits (source='manual') werden NIE überschrieben")
    print("=" * 70)

    client = supabase_helper.get_client()

    print("\n  Loading companies...")
    companies = load_companies(client)
    print(f"  Loaded {len(companies)} companies")

    print(f"\n  Loading Tommi's existing user_entry_prices...")
    existing = load_existing_entries(client)
    print(f"  Found {len(existing)} existing rows for Tommi")

    stats = Counter()
    updates = []
    unchanged = 0
    skipped_manual = []   # rows where user has manual override (we skip)
    source_cols = Counter()
    multi_filled = []

    for c in companies:
        extra = c.get('extra_data') or {}

        has_any_purchase_col = any(col in extra for col in CURRENCY_COLUMN_MAP.keys())
        if not has_any_purchase_col:
            stats['no_purchase_columns'] += 1
            continue

        price, currency, source, others = extract_entry_price(c)

        if price is None:
            stats['no_purchase_value'] += 1
            continue

        if others:
            multi_filled.append({
                'name': c.get('name'),
                'primary': f'{source}={price} {currency}',
                'others': [f'{col}={p} {cu}' for col, cu, p in others],
            })
            stats['multiple_columns_filled'] += 1

        source_cols[source] += 1

        existing_row = existing.get(c['id'])

        # KERN-REGEL: Manual Overrides bleiben unberührt
        if existing_row and existing_row.get('entry_source') == 'manual':
            skipped_manual.append({
                'name': c.get('name'),
                'excel_price': f'{currency} {price}',
                'manual_price': f"{existing_row.get('entry_currency')} {existing_row.get('entry_price')}",
            })
            stats['skipped_manual_override'] += 1
            continue

        if existing_row and existing_row.get('entry_price') is not None:
            try:
                existing_price_f = float(existing_row['entry_price'])
            except (ValueError, TypeError):
                existing_price_f = None
            existing_curr = existing_row.get('entry_currency')
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
        print(f"    {key:32s}: {count:5d}")
    print(f"    {'unchanged (already synced)':32s}: {unchanged:5d}")

    if source_cols:
        print(f"\n  Source-Spalten-Verteilung:")
        for col, count in sorted(source_cols.items(), key=lambda x: -x[1]):
            print(f"    {col:22s}: {count:5d}")

    if multi_filled:
        print(f"\n  ⚠  Datenqualität: mehrere Purchase-Spalten gefüllt ({len(multi_filled)} Firmen)")
        print(f"     Genommen wird jeweils die erste (USD/EUR/GBP/...).")
        for f in multi_filled[:5]:
            print(f"    {f['name'][:35]:35s}  → {f['primary']}  ignoriert: {', '.join(f['others'])}")
        if len(multi_filled) > 5:
            print(f"    ... + {len(multi_filled) - 5} weitere")

    if skipped_manual:
        print(f"\n  🔒 Manuell gesetzt (skipped, {len(skipped_manual)}):")
        for s in skipped_manual[:10]:
            print(f"    {s['name'][:35]:35s}  Excel: {s['excel_price']}   Manual: {s['manual_price']}")
        if len(skipped_manual) > 10:
            print(f"    ... + {len(skipped_manual) - 10} weitere")

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
                        'entry_price': u['price'],
                        'entry_currency': u['currency'],
                        'entry_set_at': now_iso,
                        'entry_source': 'excel',
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

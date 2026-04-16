#!/usr/bin/env python3
"""
Buy-Alert Checker — v1.3 (Tommi 2026-04-14).

Iterates user_entry_prices and creates a `buy_zone_reached` alert when a
company's current_price is in the green buy zone (<=10% above the user's
target entry price). Dedups per (user_id, company_id) within 24h so users
don't get spammed when the price hovers around the threshold.

Alert rules (matches src/lib/buy-zone.ts):
  diff_pct = (current - entry) / entry * 100
  green: diff_pct <= 10%

Usage:
  python3 buy_alert_checker.py            # dry-run (preview only)
  python3 buy_alert_checker.py --apply    # write alerts to Supabase

VPS cron (suggested): hourly 07-23 UTC, parallel to stock_price_updater.
"""

import argparse
import os
from collections import Counter
from datetime import datetime, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

ALERT_TYPE = 'buy_zone_reached'
DEDUP_WINDOW_HOURS = 24
GREEN_ZONE_THRESHOLD_PCT = 10.0

# Fallback currency mapping by country code (matches defaultCurrencyForCountry
# in src/lib/buy-zone.ts — keep in sync).
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
    return 'USD'


def load_entry_prices(client) -> list:
    """Load user_entry_prices rows for WATCHLIST-phase companies only.

    Per Tommi-Regel (v1.4 Frage 9): Buy-Zone only für Firmen die noch nicht
    gekauft wurden — also purchase_price IS NULL. Portfolio-Firmen (mit
    gesetztem purchase_price) sind für Stop-Loss/Stop-Win via sell_alert_checker.
    """
    try:
        resp = client.table('user_entry_prices') \
            .select('user_id, company_id, entry_price, entry_currency, entry_set_at') \
            .is_('purchase_price', 'null') \
            .not_.is_('entry_price', 'null') \
            .execute()
        return resp.data or []
    except Exception as e:
        print(f"  Error loading user_entry_prices: {e}")
        return []


def load_companies_by_id(client, company_ids: list) -> dict:
    """Fetch companies keyed by id. Batches to keep request sizes reasonable."""
    companies = {}
    if not company_ids:
        return companies
    batch_size = 100
    for i in range(0, len(company_ids), batch_size):
        batch = company_ids[i:i + batch_size]
        try:
            resp = client.table('companies') \
                .select('id, name, symbol, current_price, country, extra_data') \
                .in_('id', batch) \
                .execute()
            for row in resp.data:
                companies[row['id']] = row
        except Exception as e:
            print(f"  Error loading companies batch: {e}")
    return companies


def load_recent_buy_alerts(client) -> dict:
    """Load recent buy_zone_reached alerts for dedup.
    Keyed by (user_id, company_id) → created_at iso string."""
    cutoff = (datetime.now() - timedelta(hours=DEDUP_WINDOW_HOURS)).isoformat()
    try:
        resp = client.table('alerts') \
            .select('user_id, company_id, created_at') \
            .eq('alert_type', ALERT_TYPE) \
            .gte('created_at', cutoff) \
            .execute()
        existing = {}
        for row in resp.data:
            key = (row.get('user_id'), row['company_id'])
            # Keep the most recent if duplicates
            if key not in existing or row['created_at'] > existing[key]:
                existing[key] = row['created_at']
        return existing
    except Exception as e:
        print(f"  Warning: Could not load existing buy alerts: {e}")
        return {}


def calculate_diff_pct(current: float, entry: float) -> float:
    return ((current - entry) / entry) * 100.0


def format_price(value: float, currency: str) -> str:
    symbols = {
        'USD': '$', 'EUR': '€', 'GBP': '£',
        'CHF': 'CHF ', 'CNY': '¥', 'JPY': '¥', 'HKD': 'HK$',
    }
    return f"{symbols.get(currency, currency + ' ')}{value:.2f}"


def build_buy_zone_alerts(entry_prices: list, companies: dict, existing: dict) -> tuple:
    """Compute buy-zone alerts. Returns (alerts_to_insert, stats_counter)."""
    alerts = []
    stats = Counter()

    for ep in entry_prices:
        user_id = ep['user_id']
        company_id = ep['company_id']
        try:
            entry_price = float(ep['entry_price'])
        except (ValueError, TypeError):
            stats['bad_entry_price'] += 1
            continue
        entry_currency = ep.get('entry_currency') or 'USD'

        company = companies.get(company_id)
        if not company:
            stats['company_not_found'] += 1
            continue

        current_price = company.get('current_price')
        if current_price is None:
            stats['no_current_price'] += 1
            continue
        try:
            current_price = float(current_price)
        except (ValueError, TypeError):
            stats['bad_current_price'] += 1
            continue
        if current_price <= 0 or entry_price <= 0:
            stats['non_positive_price'] += 1
            continue

        # Determine current currency — extra_data.Currency takes precedence,
        # else fall back to country-based guess.
        extra = company.get('extra_data') or {}
        current_currency = (extra.get('Currency') or '').strip() or \
            default_currency_for_country(company.get('country') or '')

        if current_currency != entry_currency:
            stats['currency_mismatch'] += 1
            continue

        diff_pct = calculate_diff_pct(current_price, entry_price)
        if diff_pct > GREEN_ZONE_THRESHOLD_PCT:
            stats['not_in_green_zone'] += 1
            continue

        # Dedup
        if (user_id, company_id) in existing:
            stats['duplicate_within_24h'] += 1
            continue

        name = company.get('name') or '?'
        title = f"Buy-Zone erreicht: {name}"
        entry_str = format_price(entry_price, entry_currency)
        current_str = format_price(current_price, current_currency)
        sign = '+' if diff_pct >= 0 else ''
        message = f"Entry {entry_str}, now {current_str} ({sign}{diff_pct:.1f}%)"

        alerts.append({
            'user_id': user_id,
            'company_id': company_id,
            'alert_type': ALERT_TYPE,
            'priority': 'high',
            'title': title,
            'message': message,
            'condition': {
                'entry_price': entry_price,
                'current_price': current_price,
                'diff_pct': round(diff_pct, 2),
                'entry_currency': entry_currency,
                'current_currency': current_currency,
            },
            'is_active': True,
            'is_read': False,
        })
        stats['green_zone_new_alert'] += 1

    return alerts, stats


def main():
    parser = argparse.ArgumentParser(description='Generate buy-zone reached alerts')
    parser.add_argument('--apply', action='store_true', help='Write alerts to Supabase')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  BUY ALERT CHECKER")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print(f"  Green zone threshold: diff_pct <= {GREEN_ZONE_THRESHOLD_PCT}%")
    print(f"  Dedup window: {DEDUP_WINDOW_HOURS}h per (user, company)")
    print("=" * 70)

    client = supabase_helper.get_client()

    print("\n  Loading entry prices...")
    entry_prices = load_entry_prices(client)
    print(f"  Loaded {len(entry_prices)} entry price rows")

    if not entry_prices:
        print("\n  No entry prices set — nothing to do.")
        return

    company_ids = list({ep['company_id'] for ep in entry_prices})
    print(f"  Fetching {len(company_ids)} unique companies...")
    companies = load_companies_by_id(client, company_ids)
    print(f"  Loaded {len(companies)} companies")

    print(f"\n  Loading existing buy alerts from last {DEDUP_WINDOW_HOURS}h...")
    existing = load_recent_buy_alerts(client)
    print(f"  Found {len(existing)} recent alerts")

    print("\n  Computing buy-zone alerts...")
    alerts, stats = build_buy_zone_alerts(entry_prices, companies, existing)

    print(f"\n  Stats:")
    for key, count in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    {key:30s}: {count:5d}")

    print(f"\n  New alerts to create: {len(alerts)}")
    if alerts:
        print(f"\n  Sample (first 10):")
        for a in alerts[:10]:
            print(f"    [{a['priority']:6s}] user={a['user_id'][:8]}…  {a['title']}")
            print(f"             {a['message']}")

    if args.apply and alerts:
        print(f"\n  Writing {len(alerts)} alerts...")
        success = 0
        for a in alerts:
            try:
                client.table('alerts').insert(a).execute()
                success += 1
            except Exception as e:
                print(f"    Error inserting alert for company {a['company_id']}: {e}")
        print(f"  Written: {success}/{len(alerts)}")
    elif not args.apply and alerts:
        print(f"\n  Run with --apply to write alerts to Supabase")

    print("\n  Done!\n")


if __name__ == '__main__':
    main()

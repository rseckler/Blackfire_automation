#!/usr/bin/env python3
"""
Thesis Checker — monitors active investment theses against current conditions.

Runs hourly, checks all theses with status IN ('watching', 'ready', 'invested')
and generates alerts when conditions are met.

Alert types:
  - thesis_entry_reached:       current_price <= entry_price AND status = 'watching'
  - thesis_exit_reached:        current_price >= exit_target_price AND status = 'invested'
  - thesis_stop_loss:           current_price <= stop_loss_price AND status = 'invested'
  - thesis_catalyst_approaching: expected_date within 7 days
  - thesis_catalyst_matched:    News in last 24h matches thesis catalyst_type

Usage:
  python3 thesis_checker.py                # dry-run (preview only)
  python3 thesis_checker.py --apply        # write alerts + update theses in Supabase
"""

import argparse
import os
from datetime import datetime, timedelta
from collections import Counter
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

ALERT_TYPES = [
    'thesis_entry_reached', 'thesis_exit_reached', 'thesis_stop_loss',
    'thesis_catalyst_approaching', 'thesis_catalyst_matched',
]

# Deduplication windows (hours)
DEDUP_WINDOWS = {
    'thesis_entry_reached': 24,
    'thesis_exit_reached': 24,
    'thesis_stop_loss': 24,
    'thesis_catalyst_approaching': 168,   # 7 days
    'thesis_catalyst_matched': 168,       # 7 days
}

ACTIVE_STATUSES = ['watching', 'ready', 'invested']


def load_theses(client) -> list:
    """Load all active investment theses. Returns empty list if table doesn't exist."""
    try:
        all_theses = []
        page_size = 1000
        offset = 0

        while True:
            resp = client.table('investment_theses') \
                .select('*') \
                .in_('status', ACTIVE_STATUSES) \
                .range(offset, offset + page_size - 1) \
                .execute()

            batch = resp.data
            all_theses.extend(batch)

            if len(batch) < page_size:
                break
            offset += page_size

        return all_theses
    except Exception as e:
        error_str = str(e).lower()
        if 'relation' in error_str and 'does not exist' in error_str:
            print("  WARNING: Table 'investment_theses' does not exist yet.")
            print("  Migration pending — skipping thesis checks.")
            return []
        if '404' in error_str or 'not found' in error_str or 'undefined' in error_str:
            print("  WARNING: Table 'investment_theses' not found.")
            print("  Migration pending — skipping thesis checks.")
            return []
        print(f"  WARNING: Could not load theses: {e}")
        return []


def load_recent_alerts(client, hours: int = 720) -> dict:
    """Load recent thesis alerts for deduplication. Returns {(alert_type, company_id): created_at}."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        resp = client.table('alerts') \
            .select('alert_type, company_id, created_at') \
            .gte('created_at', cutoff) \
            .like('alert_type', 'thesis_%') \
            .execute()
        existing = {}
        for row in resp.data:
            key = (row['alert_type'], row['company_id'])
            existing[key] = row['created_at']
        return existing
    except Exception as e:
        print(f"  Warning: Could not load existing alerts: {e}")
        return {}


def is_duplicate(alert_type: str, company_id: str, existing: dict) -> bool:
    """Check if alert already exists within dedup window."""
    key = (alert_type, company_id)
    if key not in existing:
        return False

    created = existing[key]
    try:
        created_dt = datetime.fromisoformat(created.replace('Z', '+00:00')).replace(tzinfo=None)
        window_hours = DEDUP_WINDOWS.get(alert_type, 24)
        return (datetime.now() - created_dt).total_seconds() < window_hours * 3600
    except (ValueError, TypeError):
        return False


def load_company_prices(client, company_ids: list) -> dict:
    """Load current prices for given company IDs. Returns {company_id: {name, current_price}}."""
    if not company_ids:
        return {}

    prices = {}
    batch_size = 50
    unique_ids = list(set(company_ids))

    for i in range(0, len(unique_ids), batch_size):
        batch = unique_ids[i:i + batch_size]
        try:
            resp = client.table('companies') \
                .select('id, name, current_price') \
                .in_('id', batch) \
                .execute()
            for row in resp.data:
                prices[row['id']] = {
                    'name': row.get('name', '?'),
                    'current_price': row.get('current_price'),
                }
        except Exception as e:
            print(f"  Warning: Could not load company prices: {e}")

    return prices


def load_recent_news(client, company_ids: list) -> dict:
    """Load news from last 24h for given companies. Returns {company_id: [news_rows]}."""
    if not company_ids:
        return {}

    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    news_by_company = {}
    batch_size = 50

    for i in range(0, len(company_ids), batch_size):
        batch = company_ids[i:i + batch_size]
        try:
            resp = client.table('company_news') \
                .select('company_id, title, category, published_at') \
                .in_('company_id', batch) \
                .gte('published_at', cutoff) \
                .execute()
            for row in resp.data:
                cid = row['company_id']
                if cid not in news_by_company:
                    news_by_company[cid] = []
                news_by_company[cid].append(row)
        except Exception as e:
            print(f"  Warning: Could not load recent news: {e}")

    return news_by_company


def check_entry_reached(theses: list, prices: dict, existing: dict) -> tuple:
    """Check if current_price <= entry_price for watching theses.
    Returns (alerts, status_updates)."""
    alerts = []
    updates = []

    for t in theses:
        if t.get('status') != 'watching':
            continue

        entry_price = t.get('entry_price')
        if not entry_price:
            continue

        cid = t.get('company_id')
        company = prices.get(cid, {})
        current_price = company.get('current_price')
        if not current_price:
            continue

        try:
            entry_price = float(entry_price)
            current_price = float(current_price)
        except (ValueError, TypeError):
            continue

        if current_price > entry_price:
            continue

        if is_duplicate('thesis_entry_reached', cid, existing):
            continue

        name = company.get('name', '?')
        alerts.append({
            'company_id': cid,
            'alert_type': 'thesis_entry_reached',
            'priority': 'high',
            'title': f"Entry-Preis erreicht: {name}",
            'message': f"Entry-Preis erreicht: {name} bei ${current_price:.2f} (Ziel war ${entry_price:.2f})",
            'condition': {
                'type': 'thesis_entry_reached',
                'thesis_id': t.get('id'),
                'entry_price': entry_price,
                'current_price': current_price,
            },
            'is_active': True,
            'is_read': False,
        })

        # Status update: watching -> ready
        updates.append({
            'thesis_id': t['id'],
            'new_status': 'ready',
            'old_status': 'watching',
        })

    return alerts, updates


def check_exit_reached(theses: list, prices: dict, existing: dict) -> list:
    """Check if current_price >= exit_target_price for invested theses."""
    alerts = []

    for t in theses:
        if t.get('status') != 'invested':
            continue

        exit_price = t.get('exit_target_price')
        if not exit_price:
            continue

        cid = t.get('company_id')
        company = prices.get(cid, {})
        current_price = company.get('current_price')
        if not current_price:
            continue

        try:
            exit_price = float(exit_price)
            current_price = float(current_price)
        except (ValueError, TypeError):
            continue

        if current_price < exit_price:
            continue

        if is_duplicate('thesis_exit_reached', cid, existing):
            continue

        name = company.get('name', '?')
        alerts.append({
            'company_id': cid,
            'alert_type': 'thesis_exit_reached',
            'priority': 'high',
            'title': f"Exit-Ziel erreicht: {name}",
            'message': f"Exit-Ziel erreicht: {name} bei ${current_price:.2f} (Ziel war ${exit_price:.2f})",
            'condition': {
                'type': 'thesis_exit_reached',
                'thesis_id': t.get('id'),
                'exit_target_price': exit_price,
                'current_price': current_price,
            },
            'is_active': True,
            'is_read': False,
        })

    return alerts


def check_stop_loss(theses: list, prices: dict, existing: dict) -> list:
    """Check if current_price <= stop_loss_price for invested theses."""
    alerts = []

    for t in theses:
        if t.get('status') != 'invested':
            continue

        stop_loss = t.get('stop_loss_price')
        if not stop_loss:
            continue

        cid = t.get('company_id')
        company = prices.get(cid, {})
        current_price = company.get('current_price')
        if not current_price:
            continue

        try:
            stop_loss = float(stop_loss)
            current_price = float(current_price)
        except (ValueError, TypeError):
            continue

        if current_price > stop_loss:
            continue

        if is_duplicate('thesis_stop_loss', cid, existing):
            continue

        name = company.get('name', '?')
        alerts.append({
            'company_id': cid,
            'alert_type': 'thesis_stop_loss',
            'priority': 'high',
            'title': f"Stop-Loss ausgeloest: {name}",
            'message': f"Stop-Loss ausgeloest: {name} bei ${current_price:.2f} (Stop bei ${stop_loss:.2f})",
            'condition': {
                'type': 'thesis_stop_loss',
                'thesis_id': t.get('id'),
                'stop_loss_price': stop_loss,
                'current_price': current_price,
            },
            'is_active': True,
            'is_read': False,
        })

    return alerts


def check_catalyst_approaching(theses: list, prices: dict, existing: dict) -> list:
    """Check if expected_date is within 7 days."""
    alerts = []
    today = datetime.now().date()

    for t in theses:
        expected_date = t.get('expected_date')
        if not expected_date:
            continue

        try:
            if isinstance(expected_date, str):
                exp_date = datetime.fromisoformat(expected_date.replace('Z', '+00:00')).date()
            else:
                exp_date = expected_date
        except (ValueError, TypeError):
            continue

        days_until = (exp_date - today).days
        if days_until < 0 or days_until > 7:
            continue

        cid = t.get('company_id')
        if is_duplicate('thesis_catalyst_approaching', cid, existing):
            continue

        company = prices.get(cid, {})
        name = company.get('name', '?')
        catalyst_type = t.get('catalyst_type', 'event')

        alerts.append({
            'company_id': cid,
            'alert_type': 'thesis_catalyst_approaching',
            'priority': 'medium',
            'title': f"Katalysator in {days_until} Tagen: {catalyst_type}",
            'message': f"Katalysator in {days_until} Tagen: {catalyst_type} fuer {name}",
            'condition': {
                'type': 'thesis_catalyst_approaching',
                'thesis_id': t.get('id'),
                'catalyst_type': catalyst_type,
                'expected_date': str(exp_date),
                'days_until': days_until,
            },
            'is_active': True,
            'is_read': False,
        })

    return alerts


def check_catalyst_matched(theses: list, prices: dict, news_by_company: dict, existing: dict) -> list:
    """Check if recent news matches thesis catalyst_type (WP-10.2)."""
    alerts = []

    for t in theses:
        catalyst_type = t.get('catalyst_type')
        if not catalyst_type:
            continue

        cid = t.get('company_id')
        news_list = news_by_company.get(cid, [])
        if not news_list:
            continue

        if is_duplicate('thesis_catalyst_matched', cid, existing):
            continue

        # Match: news category or title contains catalyst_type (case-insensitive)
        catalyst_lower = catalyst_type.lower()
        matched_news = None
        for news in news_list:
            category = (news.get('category') or '').lower()
            title = (news.get('title') or '').lower()
            if catalyst_lower in category or catalyst_lower in title:
                matched_news = news
                break

        if not matched_news:
            continue

        company = prices.get(cid, {})
        name = company.get('name', '?')
        news_title = matched_news.get('title', '?')

        alerts.append({
            'company_id': cid,
            'alert_type': 'thesis_catalyst_matched',
            'priority': 'high',
            'title': f"Katalysator eingetreten: {catalyst_type}",
            'message': f"Katalysator eingetreten: {catalyst_type} fuer {name} — '{news_title}'",
            'condition': {
                'type': 'thesis_catalyst_matched',
                'thesis_id': t.get('id'),
                'catalyst_type': catalyst_type,
                'news_title': news_title,
                'news_published_at': matched_news.get('published_at'),
            },
            'is_active': True,
            'is_read': False,
        })

    return alerts


def main():
    parser = argparse.ArgumentParser(description='Check investment theses against current conditions')
    parser.add_argument('--apply', action='store_true', help='Write alerts to Supabase and update thesis statuses')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  THESIS CHECKER")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print("=" * 70)

    client = supabase_helper.get_client()

    # Load active theses
    print("\n  Loading active investment theses...")
    theses = load_theses(client)
    if not theses:
        print("  No active theses found. Exiting.")
        print("\n  Done!")
        return

    print(f"  Loaded {len(theses)} active theses")
    by_status = Counter(t.get('status', '?') for t in theses)
    print(f"  By status: {dict(by_status)}")

    # Load existing alerts for deduplication
    print("\n  Loading existing alerts for deduplication...")
    existing = load_recent_alerts(client)
    print(f"  Existing thesis alerts (last 30 days): {len(existing)}")

    # Load company prices for all thesis companies
    company_ids = list(set(t['company_id'] for t in theses if t.get('company_id')))
    print(f"\n  Loading prices for {len(company_ids)} companies...")
    prices = load_company_prices(client, company_ids)
    print(f"  Prices loaded: {len(prices)}")

    # Load recent news for catalyst matching
    invested_or_watching_ids = list(set(
        t['company_id'] for t in theses
        if t.get('company_id') and t.get('catalyst_type')
    ))
    print(f"  Loading recent news for {len(invested_or_watching_ids)} companies with catalysts...")
    news_by_company = load_recent_news(client, invested_or_watching_ids)
    companies_with_news = sum(1 for v in news_by_company.values() if v)
    print(f"  Companies with news in last 24h: {companies_with_news}")

    # Run all checks
    all_alerts = []
    status_updates = []

    # 1. Entry reached (watching -> ready)
    print("\n  Checking entry prices (watching theses)...")
    entry_alerts, entry_updates = check_entry_reached(theses, prices, existing)
    print(f"  Found: {len(entry_alerts)} entry-reached alerts")
    all_alerts.extend(entry_alerts)
    status_updates.extend(entry_updates)

    # 2. Exit target reached
    print("\n  Checking exit targets (invested theses)...")
    exit_alerts = check_exit_reached(theses, prices, existing)
    print(f"  Found: {len(exit_alerts)} exit-reached alerts")
    all_alerts.extend(exit_alerts)

    # 3. Stop loss triggered
    print("\n  Checking stop losses (invested theses)...")
    sl_alerts = check_stop_loss(theses, prices, existing)
    print(f"  Found: {len(sl_alerts)} stop-loss alerts")
    all_alerts.extend(sl_alerts)

    # 4. Catalyst approaching
    print("\n  Checking approaching catalysts (next 7 days)...")
    cat_alerts = check_catalyst_approaching(theses, prices, existing)
    print(f"  Found: {len(cat_alerts)} catalyst-approaching alerts")
    all_alerts.extend(cat_alerts)

    # 5. Catalyst matched from news (WP-10.2)
    print("\n  Checking catalyst matches from recent news...")
    match_alerts = check_catalyst_matched(theses, prices, news_by_company, existing)
    print(f"  Found: {len(match_alerts)} catalyst-matched alerts")
    all_alerts.extend(match_alerts)

    # Summary
    by_priority = Counter(a['priority'] for a in all_alerts)
    by_type = Counter(a['alert_type'] for a in all_alerts)

    print(f"\n  Total alerts: {len(all_alerts)}")
    print(f"  By priority: high={by_priority.get('high', 0)}, medium={by_priority.get('medium', 0)}, low={by_priority.get('low', 0)}")
    print(f"  By type: {dict(by_type)}")
    print(f"  Status updates: {len(status_updates)}")

    if all_alerts:
        print(f"\n  Sample alerts:")
        for a in all_alerts[:15]:
            print(f"    [{a['priority']:6s}] [{a['alert_type']:30s}] {a['title']}")

    if status_updates:
        print(f"\n  Status updates:")
        for u in status_updates:
            print(f"    Thesis {u['thesis_id']}: {u['old_status']} -> {u['new_status']}")

    # Apply
    if args.apply and (all_alerts or status_updates):
        # Write alerts
        if all_alerts:
            print(f"\n  Writing {len(all_alerts)} alerts...")
            success = 0
            for a in all_alerts:
                try:
                    client.table('alerts').insert(a).execute()
                    success += 1
                except Exception as e:
                    print(f"    Error writing alert: {e}")
            print(f"  Written: {success}/{len(all_alerts)}")

        # Update thesis statuses
        if status_updates:
            print(f"\n  Updating {len(status_updates)} thesis statuses...")
            updated = 0
            for u in status_updates:
                try:
                    client.table('investment_theses') \
                        .update({'status': u['new_status']}) \
                        .eq('id', u['thesis_id']) \
                        .execute()
                    updated += 1
                except Exception as e:
                    print(f"    Error updating thesis {u['thesis_id']}: {e}")
            print(f"  Updated: {updated}/{len(status_updates)}")

    elif not args.apply and (all_alerts or status_updates):
        print(f"\n  Run with --apply to write alerts and update statuses in Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Alert Generator — creates alerts for important company events.

Alert types:
  - price_jump:        >5% price change in 24h
  - ipo_announced:     New IPO date in company_events
  - earnings_surprise: Strong price move after earnings
  - score_change:      Score changed by >10 points
  - new_company:       Company added in last 24h

Usage:
  python3 alert_generator.py                      # dry-run (preview only)
  python3 alert_generator.py --apply              # write alerts to Supabase
  python3 alert_generator.py --apply --type price_jump  # only one type
"""

import argparse
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

ALERT_TYPES = ['price_jump', 'ipo_announced', 'earnings_surprise', 'score_change', 'new_company']

# Deduplication windows (hours)
DEDUP_WINDOWS = {
    'price_jump': 24,
    'ipo_announced': 168,  # 7 days
    'earnings_surprise': 168,
    'score_change': 168,
    'new_company': 720,  # 30 days
}


def load_recent_alerts(client, hours: int = 720) -> dict:
    """Load recent alerts for deduplication. Returns {(alert_type, company_id): created_at}."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        resp = client.table('alerts') \
            .select('alert_type, company_id, created_at') \
            .gte('created_at', cutoff) \
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


def detect_price_jumps(companies: list, existing: dict) -> list:
    """Detect >5% price changes."""
    alerts = []
    for c in companies:
        extra = c.get('extra_data') or {}
        price = c.get('current_price')
        if not price:
            continue
        price = float(price)
        if price <= 0:
            continue

        # Get previous price
        prev = extra.get('Previous_Close') or extra.get('Previous_Price')
        if not prev:
            continue
        try:
            prev = float(str(prev).replace(',', '.'))
        except (ValueError, TypeError):
            continue
        if prev <= 0:
            continue

        pct = ((price - prev) / prev) * 100

        if abs(pct) < 5:
            continue

        if is_duplicate('price_jump', c['id'], existing):
            continue

        priority = 'high' if abs(pct) >= 10 else 'medium'
        direction = 'up' if pct > 0 else 'down'

        alerts.append({
            'company_id': c['id'],
            'alert_type': 'price_jump',
            'priority': priority,
            'title': f"Kurssprung {pct:+.1f}%",
            'message': f"{c.get('name', '?')}: ${prev:.2f} → ${price:.2f} ({pct:+.1f}%)",
            'condition': {'type': 'price_jump', 'threshold': 5, 'actual': round(pct, 1)},
            'metadata': {
                'price_before': prev,
                'price_after': price,
                'percentage': round(pct, 1),
                'direction': direction,
            },
            'is_active': True,
            'is_read': False,
        })

    return alerts


def detect_ipo_announced(client, existing: dict) -> list:
    """Detect new IPO events."""
    alerts = []
    today = datetime.now().date().isoformat()
    try:
        resp = client.table('company_events') \
            .select('company_id, event_date, description, source') \
            .eq('event_type', 'ipo') \
            .gte('event_date', today) \
            .execute()

        # Get company names
        company_ids = list(set(r['company_id'] for r in resp.data))
        names = {}
        if company_ids:
            for cid in company_ids:
                try:
                    cr = client.table('companies').select('name').eq('id', cid).single().execute()
                    if cr.data:
                        names[cid] = cr.data['name']
                except Exception:
                    pass

        for event in resp.data:
            cid = event['company_id']
            if is_duplicate('ipo_announced', cid, existing):
                continue

            name = names.get(cid, '?')
            alerts.append({
                'company_id': cid,
                'alert_type': 'ipo_announced',
                'priority': 'high',
                'title': f"IPO Termin: {event.get('event_date', '?')}",
                'message': f"{name}: IPO am {event.get('event_date', '?')}. {event.get('description', '')}",
                'condition': {'type': 'ipo_announced'},
                'metadata': {
                    'event_date': event.get('event_date'),
                    'description': event.get('description'),
                    'source': event.get('source'),
                },
                'is_active': True,
                'is_read': False,
            })
    except Exception as e:
        print(f"  Warning: Could not check IPO events: {e}")

    return alerts


def detect_score_changes(client, existing: dict) -> list:
    """Detect score changes >10 points."""
    alerts = []
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()

    try:
        # Get trend_7d scores (already calculated by score_history.py)
        resp = client.table('company_scores') \
            .select('company_id, score_value, details') \
            .eq('score_type', 'trend_7d') \
            .execute()

        company_ids = [r['company_id'] for r in resp.data if abs(float(r['score_value'])) >= 10]
        names = {}
        for cid in company_ids:
            try:
                cr = client.table('companies').select('name').eq('id', cid).single().execute()
                if cr.data:
                    names[cid] = cr.data['name']
            except Exception:
                pass

        for row in resp.data:
            delta = float(row['score_value'])
            if abs(delta) < 10:
                continue

            cid = row['company_id']
            if is_duplicate('score_change', cid, existing):
                continue

            name = names.get(cid, '?')
            direction = 'gestiegen' if delta > 0 else 'gefallen'

            alerts.append({
                'company_id': cid,
                'alert_type': 'score_change',
                'priority': 'medium',
                'title': f"Score {direction}: {delta:+.0f} Punkte",
                'message': f"{name}: Blackfire Score {delta:+.0f} Punkte in 7 Tagen",
                'condition': {'type': 'score_change', 'threshold': 10, 'actual': delta},
                'metadata': {
                    'delta': delta,
                    'direction': 'up' if delta > 0 else 'down',
                },
                'is_active': True,
                'is_read': False,
            })
    except Exception as e:
        print(f"  Warning: Could not check score changes: {e}")

    return alerts


def detect_new_companies(companies: list, existing: dict) -> list:
    """Detect companies added in last 24h."""
    alerts = []
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()

    for c in companies:
        created = c.get('created_at', '')
        if not created or created < cutoff:
            continue

        if is_duplicate('new_company', c['id'], existing):
            continue

        extra = c.get('extra_data') or {}
        alerts.append({
            'company_id': c['id'],
            'alert_type': 'new_company',
            'priority': 'low',
            'title': "Neues Unternehmen hinzugefügt",
            'message': f"{c.get('name', '?')} wurde zur Datenbank hinzugefügt. Sektor: {extra.get('Sector', 'N/A')}",
            'condition': {'type': 'new_company'},
            'metadata': {
                'sector': extra.get('Sector'),
                'country': extra.get('Country'),
                'listing_status': c.get('listing_status'),
            },
            'is_active': True,
            'is_read': False,
        })

    return alerts


def main():
    parser = argparse.ArgumentParser(description='Generate alerts for company events')
    parser.add_argument('--apply', action='store_true', help='Write alerts to Supabase')
    parser.add_argument('--type', choices=ALERT_TYPES, help='Run only one alert type')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  ALERT GENERATOR")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    if args.type:
        print(f"  Type: {args.type} only")
    print("=" * 70)

    client = supabase_helper.get_client()

    # Load existing alerts for deduplication
    print("\n  Loading existing alerts for deduplication...")
    existing = load_recent_alerts(client)
    print(f"  Existing alerts (last 30 days): {len(existing)}")

    # Load companies
    print("  Loading companies...")
    companies = supabase_helper.get_all_companies(
        'id, name, symbol, current_price, listing_status, created_at, extra_data'
    )
    print(f"  Loaded {len(companies)} companies")

    # Generate alerts
    all_alerts = []
    types_to_run = [args.type] if args.type else ALERT_TYPES

    if 'price_jump' in types_to_run:
        print("\n  Checking price jumps...")
        price_alerts = detect_price_jumps(companies, existing)
        print(f"  Found: {len(price_alerts)} price jump alerts")
        all_alerts.extend(price_alerts)

    if 'ipo_announced' in types_to_run:
        print("\n  Checking IPO announcements...")
        ipo_alerts = detect_ipo_announced(client, existing)
        print(f"  Found: {len(ipo_alerts)} IPO alerts")
        all_alerts.extend(ipo_alerts)

    if 'score_change' in types_to_run:
        print("\n  Checking score changes...")
        score_alerts = detect_score_changes(client, existing)
        print(f"  Found: {len(score_alerts)} score change alerts")
        all_alerts.extend(score_alerts)

    if 'new_company' in types_to_run:
        print("\n  Checking new companies...")
        new_alerts = detect_new_companies(companies, existing)
        print(f"  Found: {len(new_alerts)} new company alerts")
        all_alerts.extend(new_alerts)

    # Summary
    from collections import Counter
    by_priority = Counter(a['priority'] for a in all_alerts)
    by_type = Counter(a['alert_type'] for a in all_alerts)

    print(f"\n  Total alerts: {len(all_alerts)}")
    print(f"  By priority: high={by_priority.get('high', 0)}, medium={by_priority.get('medium', 0)}, low={by_priority.get('low', 0)}")
    print(f"  By type: {dict(by_type)}")

    if all_alerts:
        print(f"\n  Sample alerts:")
        for a in all_alerts[:10]:
            print(f"    [{a['priority']:6s}] [{a['alert_type']:18s}] {a['title']}")

    # Apply
    if args.apply and all_alerts:
        print(f"\n  Writing {len(all_alerts)} alerts...")
        success = 0
        for a in all_alerts:
            try:
                client.table('alerts').insert(a).execute()
                success += 1
            except Exception as e:
                print(f"    Error: {e}")
        print(f"  Written: {success}/{len(all_alerts)}")
    elif not args.apply and all_alerts:
        print(f"\n  Run with --apply to write alerts to Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

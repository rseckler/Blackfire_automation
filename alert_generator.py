#!/usr/bin/env python3
"""
Alert Generator — creates alerts for important company events.

Alert types:
  - price_jump:            >=2% price change in 24h (low 2-5%, medium 5-8%, high >8%)
  - ipo_announced:         New IPO date in company_events
  - earnings_surprise:     Strong price move after earnings
  - score_change:          Score changed by >=3 points (low 3-5, medium 5-8, high >8)
  - new_company:           Company added in last 24h
  - watchlist_price:       >1% price change on watchlist company (low)
  - newsletter_mention:    New news article for watchlist company (low)
  - approaching_catalyst:  Event within next 7 days (medium)
  - stale_watchlist:       Watchlist company with no activity in 90 days (low)

Usage:
  python3 alert_generator.py                      # dry-run (preview only)
  python3 alert_generator.py --apply              # write alerts to Supabase
  python3 alert_generator.py --apply --type price_jump  # only one type
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
    'price_jump', 'ipo_announced', 'earnings_surprise', 'score_change',
    'new_company', 'watchlist_price', 'newsletter_mention',
    'approaching_catalyst', 'stale_watchlist',
]

# Deduplication windows (hours)
DEDUP_WINDOWS = {
    'price_jump': 24,
    'ipo_announced': 168,       # 7 days
    'earnings_surprise': 168,
    'score_change': 168,        # 7 days
    'new_company': 720,         # 30 days
    'watchlist_price': 24,
    'newsletter_mention': 168,  # 7 days
    'approaching_catalyst': 168,  # 7 days (1 per event per week)
    'stale_watchlist': 720,     # 30 days (1 per company per month)
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


def load_recent_alerts_with_key(client, hours: int = 720) -> dict:
    """Load recent alerts keyed by (alert_type, company_id, condition_key).
    Used for approaching_catalyst dedup where we need event-level dedup."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        resp = client.table('alerts') \
            .select('alert_type, company_id, condition, created_at') \
            .gte('created_at', cutoff) \
            .execute()
        existing = {}
        for row in resp.data:
            cond = row.get('condition') or {}
            event_id = cond.get('event_id', '')
            key = (row['alert_type'], row['company_id'], event_id)
            existing[key] = row['created_at']
        return existing
    except Exception as e:
        print(f"  Warning: Could not load existing alerts (keyed): {e}")
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


def is_duplicate_keyed(alert_type: str, company_id: str, event_id: str, existing_keyed: dict) -> bool:
    """Check dedup with event-level key."""
    key = (alert_type, company_id, event_id)
    if key not in existing_keyed:
        return False
    created = existing_keyed[key]
    try:
        created_dt = datetime.fromisoformat(created.replace('Z', '+00:00')).replace(tzinfo=None)
        window_hours = DEDUP_WINDOWS.get(alert_type, 168)
        return (datetime.now() - created_dt).total_seconds() < window_hours * 3600
    except (ValueError, TypeError):
        return False


def load_watchlist_company_ids(client) -> set:
    """Get all company_ids on any user's watchlist."""
    try:
        resp = client.table('watchlist') \
            .select('company_id') \
            .execute()
        return set(row['company_id'] for row in resp.data)
    except Exception as e:
        print(f"  Warning: Could not load watchlist: {e}")
        return set()


def detect_price_jumps(companies: list, existing: dict) -> list:
    """Detect >=2% price changes. Priority: high >8%, medium 5-8%, low 2-5%."""
    alerts = []
    for c in companies:
        extra = c.get('extra_data') or {}
        price = c.get('current_price')
        if not price:
            continue
        price = float(price)
        if price <= 0:
            continue

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

        if abs(pct) < 2:
            continue

        if is_duplicate('price_jump', c['id'], existing):
            continue

        if abs(pct) >= 8:
            priority = 'high'
        elif abs(pct) >= 5:
            priority = 'medium'
        else:
            priority = 'low'

        direction = 'up' if pct > 0 else 'down'

        alerts.append({
            'company_id': c['id'],
            'alert_type': 'price_jump',
            'priority': priority,
            'title': f"Kurssprung {pct:+.1f}%",
            'message': f"{c.get('name', '?')}: ${prev:.2f} -> ${price:.2f} ({pct:+.1f}%)",
            'condition': {
                'type': 'price_jump',
                'threshold': 2,
                'actual': round(pct, 1),
                'price_before': prev,
                'price_after': price,
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
                'condition': {
                    'type': 'ipo_announced',
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
    """Detect score changes >=3 points. Priority: high >8, medium 5-8, low 3-5."""
    alerts = []

    try:
        resp = client.table('company_scores') \
            .select('company_id, score_value, details') \
            .eq('score_type', 'trend_7d') \
            .execute()

        company_ids = [r['company_id'] for r in resp.data if abs(float(r['score_value'])) >= 3]
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
            if abs(delta) < 3:
                continue

            cid = row['company_id']
            if is_duplicate('score_change', cid, existing):
                continue

            name = names.get(cid, '?')
            direction = 'gestiegen' if delta > 0 else 'gefallen'

            if abs(delta) > 8:
                priority = 'high'
            elif abs(delta) >= 5:
                priority = 'medium'
            else:
                priority = 'low'

            alerts.append({
                'company_id': cid,
                'alert_type': 'score_change',
                'priority': priority,
                'title': f"Score {direction}: {delta:+.0f} Punkte",
                'message': f"{name}: Blackfire Score {delta:+.0f} Punkte in 7 Tagen",
                'condition': {
                    'type': 'score_change',
                    'threshold': 3,
                    'actual': delta,
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
            'title': "Neues Unternehmen hinzugefuegt",
            'message': f"{c.get('name', '?')} wurde zur Datenbank hinzugefuegt. Sektor: {extra.get('Sector', 'N/A')}",
            'condition': {
                'type': 'new_company',
                'sector': extra.get('Sector'),
                'country': extra.get('Country'),
                'listing_status': c.get('listing_status'),
            },
            'is_active': True,
            'is_read': False,
        })

    return alerts


def detect_watchlist_price(companies: list, watchlist_ids: set, existing: dict) -> list:
    """Detect >1% price change on watchlist companies."""
    alerts = []
    for c in companies:
        if c['id'] not in watchlist_ids:
            continue

        extra = c.get('extra_data') or {}
        price = c.get('current_price')
        if not price:
            continue
        price = float(price)
        if price <= 0:
            continue

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

        if abs(pct) < 1:
            continue

        # Skip if already covered by a price_jump alert (>=2%)
        if abs(pct) >= 2:
            continue

        if is_duplicate('watchlist_price', c['id'], existing):
            continue

        direction = 'up' if pct > 0 else 'down'
        alerts.append({
            'company_id': c['id'],
            'alert_type': 'watchlist_price',
            'priority': 'low',
            'title': f"Watchlist: {c.get('name', '?')} {pct:+.1f}%",
            'message': f"{c.get('name', '?')} (Watchlist): ${prev:.2f} -> ${price:.2f} ({pct:+.1f}%)",
            'condition': {
                'type': 'watchlist_price',
                'threshold': 1,
                'actual': round(pct, 1),
                'price_before': prev,
                'price_after': price,
                'direction': direction,
            },
            'is_active': True,
            'is_read': False,
        })

    return alerts


def detect_newsletter_mention(client, watchlist_ids: set, existing: dict) -> list:
    """Detect new news articles for watchlist companies in last 24h."""
    alerts = []
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()

    if not watchlist_ids:
        return alerts

    try:
        # Fetch recent news for watchlist companies
        # Supabase .in_() has limits, batch if needed
        wl_list = list(watchlist_ids)
        all_news = []
        batch_size = 50
        for i in range(0, len(wl_list), batch_size):
            batch = wl_list[i:i + batch_size]
            resp = client.table('company_news') \
                .select('company_id, title, sentiment, published_at') \
                .in_('company_id', batch) \
                .gte('published_at', cutoff) \
                .execute()
            all_news.extend(resp.data)

        # Get company names
        news_company_ids = list(set(n['company_id'] for n in all_news))
        names = {}
        for cid in news_company_ids:
            try:
                cr = client.table('companies').select('name').eq('id', cid).single().execute()
                if cr.data:
                    names[cid] = cr.data['name']
            except Exception:
                pass

        for news in all_news:
            cid = news['company_id']
            if is_duplicate('newsletter_mention', cid, existing):
                continue

            name = names.get(cid, '?')
            sentiment = news.get('sentiment', 'neutral')
            alerts.append({
                'company_id': cid,
                'alert_type': 'newsletter_mention',
                'priority': 'low',
                'title': f"Watchlist: Neuer Artikel zu {name}",
                'message': f"{name} (Watchlist): \"{news.get('title', '?')}\" (Sentiment: {sentiment})",
                'condition': {
                    'type': 'newsletter_mention',
                    'news_title': news.get('title'),
                    'sentiment': sentiment,
                },
                'is_active': True,
                'is_read': False,
            })
    except Exception as e:
        print(f"  Warning: Could not check newsletter mentions: {e}")

    return alerts


def detect_approaching_catalyst(client, existing_keyed: dict) -> list:
    """Detect events within next 7 days. Medium priority. Dedup: 1 per event per week."""
    alerts = []
    today = datetime.now().date()
    end_date = (today + timedelta(days=7)).isoformat()
    today_str = today.isoformat()

    try:
        resp = client.table('company_events') \
            .select('id, company_id, event_type, event_date, description') \
            .gte('event_date', today_str) \
            .lte('event_date', end_date) \
            .execute()

        company_ids = list(set(r['company_id'] for r in resp.data))
        names = {}
        for cid in company_ids:
            try:
                cr = client.table('companies').select('name').eq('id', cid).single().execute()
                if cr.data:
                    names[cid] = cr.data['name']
            except Exception:
                pass

        for event in resp.data:
            cid = event['company_id']
            event_id = event.get('id', '')

            if is_duplicate_keyed('approaching_catalyst', cid, event_id, existing_keyed):
                continue

            name = names.get(cid, '?')
            event_date = event.get('event_date', '')
            try:
                days_until = (datetime.fromisoformat(event_date).date() - today).days
            except (ValueError, TypeError):
                days_until = 0
            event_type = event.get('event_type', 'event')

            alerts.append({
                'company_id': cid,
                'alert_type': 'approaching_catalyst',
                'priority': 'medium',
                'title': f"Event in {days_until} Tagen: {event_type}",
                'message': f"Event in {days_until} Tagen: {event_type} fuer {name}. {event.get('description', '')}",
                'condition': {
                    'type': 'approaching_catalyst',
                    'event_id': event_id,
                    'event_type': event_type,
                    'event_date': event_date,
                    'days_until': days_until,
                    'description': event.get('description'),
                },
                'is_active': True,
                'is_read': False,
            })
    except Exception as e:
        print(f"  Warning: Could not check approaching catalysts: {e}")

    return alerts


def detect_stale_watchlist(client, watchlist_ids: set, existing: dict) -> list:
    """Detect watchlist companies with no news and no score change in 90 days."""
    alerts = []
    if not watchlist_ids:
        return alerts

    cutoff_90d = (datetime.now() - timedelta(days=90)).isoformat()
    wl_list = list(watchlist_ids)

    try:
        # Find companies with recent news
        companies_with_news = set()
        batch_size = 50
        for i in range(0, len(wl_list), batch_size):
            batch = wl_list[i:i + batch_size]
            resp = client.table('company_news') \
                .select('company_id') \
                .in_('company_id', batch) \
                .gte('published_at', cutoff_90d) \
                .execute()
            companies_with_news.update(r['company_id'] for r in resp.data)

        # Find companies with recent score changes
        companies_with_scores = set()
        for i in range(0, len(wl_list), batch_size):
            batch = wl_list[i:i + batch_size]
            resp = client.table('company_scores') \
                .select('company_id') \
                .in_('company_id', batch) \
                .gte('computed_at', cutoff_90d) \
                .execute()
            companies_with_scores.update(r['company_id'] for r in resp.data)

        # Stale = on watchlist but no news AND no score in 90 days
        stale_ids = watchlist_ids - companies_with_news - companies_with_scores

        # Get company names and watchlist added_at dates
        for cid in stale_ids:
            if is_duplicate('stale_watchlist', cid, existing):
                continue

            try:
                cr = client.table('companies').select('name').eq('id', cid).single().execute()
                name = cr.data['name'] if cr.data else '?'
            except Exception:
                name = '?'

            # Get earliest watchlist add date for this company
            try:
                wr = client.table('watchlist') \
                    .select('added_at') \
                    .eq('company_id', cid) \
                    .order('added_at') \
                    .limit(1) \
                    .execute()
                if wr.data:
                    added_at = wr.data[0]['added_at']
                    try:
                        added_dt = datetime.fromisoformat(added_at.replace('Z', '+00:00')).replace(tzinfo=None)
                        days_on_watchlist = (datetime.now() - added_dt).days
                    except (ValueError, TypeError):
                        days_on_watchlist = 0
                else:
                    days_on_watchlist = 0
            except Exception:
                days_on_watchlist = 0

            alerts.append({
                'company_id': cid,
                'alert_type': 'stale_watchlist',
                'priority': 'low',
                'title': f"Watchlist inaktiv: {name}",
                'message': f"{name} auf Watchlist seit {days_on_watchlist} Tagen ohne neue Aktivitaet",
                'condition': {
                    'type': 'stale_watchlist',
                    'days_on_watchlist': days_on_watchlist,
                    'last_activity_check_days': 90,
                },
                'is_active': True,
                'is_read': False,
            })
    except Exception as e:
        print(f"  Warning: Could not check stale watchlist: {e}")

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
    existing_keyed = load_recent_alerts_with_key(client)
    print(f"  Existing alerts (last 30 days): {len(existing)}")

    # Load companies
    print("  Loading companies...")
    companies = supabase_helper.get_all_companies(
        'id, name, symbol, current_price, listing_status, created_at, extra_data'
    )
    print(f"  Loaded {len(companies)} companies")

    # Load watchlist
    print("  Loading watchlist...")
    watchlist_ids = load_watchlist_company_ids(client)
    print(f"  Watchlist companies: {len(watchlist_ids)}")

    # Generate alerts
    all_alerts = []
    types_to_run = [args.type] if args.type else ALERT_TYPES

    if 'price_jump' in types_to_run:
        print("\n  Checking price jumps (>=2%)...")
        price_alerts = detect_price_jumps(companies, existing)
        print(f"  Found: {len(price_alerts)} price jump alerts")
        all_alerts.extend(price_alerts)

    if 'ipo_announced' in types_to_run:
        print("\n  Checking IPO announcements...")
        ipo_alerts = detect_ipo_announced(client, existing)
        print(f"  Found: {len(ipo_alerts)} IPO alerts")
        all_alerts.extend(ipo_alerts)

    if 'score_change' in types_to_run:
        print("\n  Checking score changes (>=3pt)...")
        score_alerts = detect_score_changes(client, existing)
        print(f"  Found: {len(score_alerts)} score change alerts")
        all_alerts.extend(score_alerts)

    if 'new_company' in types_to_run:
        print("\n  Checking new companies...")
        new_alerts = detect_new_companies(companies, existing)
        print(f"  Found: {len(new_alerts)} new company alerts")
        all_alerts.extend(new_alerts)

    if 'watchlist_price' in types_to_run:
        print("\n  Checking watchlist price changes (>1%)...")
        wp_alerts = detect_watchlist_price(companies, watchlist_ids, existing)
        print(f"  Found: {len(wp_alerts)} watchlist price alerts")
        all_alerts.extend(wp_alerts)

    if 'newsletter_mention' in types_to_run:
        print("\n  Checking watchlist newsletter mentions...")
        nl_alerts = detect_newsletter_mention(client, watchlist_ids, existing)
        print(f"  Found: {len(nl_alerts)} newsletter mention alerts")
        all_alerts.extend(nl_alerts)

    if 'approaching_catalyst' in types_to_run:
        print("\n  Checking approaching catalysts (next 7 days)...")
        cat_alerts = detect_approaching_catalyst(client, existing_keyed)
        print(f"  Found: {len(cat_alerts)} approaching catalyst alerts")
        all_alerts.extend(cat_alerts)

    if 'stale_watchlist' in types_to_run:
        print("\n  Checking stale watchlist companies (90 days inactive)...")
        stale_alerts = detect_stale_watchlist(client, watchlist_ids, existing)
        print(f"  Found: {len(stale_alerts)} stale watchlist alerts")
        all_alerts.extend(stale_alerts)

    # Summary
    by_priority = Counter(a['priority'] for a in all_alerts)
    by_type = Counter(a['alert_type'] for a in all_alerts)

    print(f"\n  Total alerts: {len(all_alerts)}")
    print(f"  By priority: high={by_priority.get('high', 0)}, medium={by_priority.get('medium', 0)}, low={by_priority.get('low', 0)}")
    print(f"  By type: {dict(by_type)}")

    if all_alerts:
        print(f"\n  Sample alerts:")
        for a in all_alerts[:15]:
            print(f"    [{a['priority']:6s}] [{a['alert_type']:22s}] {a['title']}")

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

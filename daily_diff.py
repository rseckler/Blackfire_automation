#!/usr/bin/env python3
"""
Daily Diff — generates a structured daily summary of all changes.

Collects:
  - Price changes >1% from last 24h
  - New news articles from last 24h (count + top 5 by sentiment)
  - Score changes from last 24h
  - Upcoming events in next 7 days
  - New companies added in last 24h

Stores result as alert_type='daily_diff' in the alerts table with full
summary in the condition JSONB field.

Watchlist companies are prioritized (shown first in each category).

Designed to run at 06:13 UTC, after score_history and before alert_generator.

Usage:
  python3 daily_diff.py                   # dry-run (preview only)
  python3 daily_diff.py --apply           # write daily diff to Supabase
"""

import argparse
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# Sentinel company_id for non-company-specific alerts
# Uses a fixed UUID so we can dedup daily_diff entries
DAILY_DIFF_COMPANY_ID = '00000000-0000-0000-0000-000000000000'


def load_watchlist_ids(client) -> set:
    """Get all company_ids on any user's watchlist."""
    try:
        resp = client.table('watchlist') \
            .select('company_id') \
            .execute()
        return set(row['company_id'] for row in resp.data)
    except Exception as e:
        print(f"  Warning: Could not load watchlist: {e}")
        return set()


def sort_watchlist_first(items: list, watchlist_ids: set, key: str = 'company_id') -> list:
    """Sort list so watchlist items come first, then by original order."""
    wl_items = [i for i in items if i.get(key) in watchlist_ids]
    other_items = [i for i in items if i.get(key) not in watchlist_ids]
    return wl_items + other_items


def collect_price_moves(companies: list, watchlist_ids: set) -> list:
    """Collect price changes >1% from companies data."""
    moves = []
    for c in companies:
        extra = c.get('extra_data') or {}
        price = c.get('current_price')
        if not price:
            continue
        try:
            price = float(price)
        except (ValueError, TypeError):
            continue
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

        moves.append({
            'company_id': c['id'],
            'name': c.get('name', '?'),
            'change_pct': round(pct, 2),
            'price': round(price, 2),
            'previous_price': round(prev, 2),
            'on_watchlist': c['id'] in watchlist_ids,
        })

    # Sort by absolute change descending, watchlist first
    moves.sort(key=lambda x: (not x['on_watchlist'], -abs(x['change_pct'])))
    return moves


def collect_new_news(client, watchlist_ids: set) -> list:
    """Collect new news articles from last 24h, top 5 by sentiment relevance."""
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    all_news = []

    try:
        resp = client.table('company_news') \
            .select('company_id, title, sentiment, published_at, source') \
            .gte('published_at', cutoff) \
            .order('published_at', desc=True) \
            .limit(500) \
            .execute()
        all_news = resp.data
    except Exception as e:
        print(f"  Warning: Could not load news: {e}")
        return []

    if not all_news:
        return []

    # Get company names
    company_ids = list(set(n['company_id'] for n in all_news))
    names = {}
    batch_size = 50
    for i in range(0, len(company_ids), batch_size):
        batch = company_ids[i:i + batch_size]
        for cid in batch:
            try:
                cr = client.table('companies').select('name').eq('id', cid).single().execute()
                if cr.data:
                    names[cid] = cr.data['name']
            except Exception:
                pass

    # Sentiment priority: positive/negative first (more relevant), then neutral
    sentiment_priority = {'positive': 0, 'negative': 1, 'neutral': 2}

    news_items = []
    for n in all_news:
        cid = n['company_id']
        news_items.append({
            'company_id': cid,
            'name': names.get(cid, '?'),
            'title': n.get('title', '?'),
            'sentiment': n.get('sentiment', 'neutral'),
            'source': n.get('source', '?'),
            'on_watchlist': cid in watchlist_ids,
        })

    # Sort: watchlist first, then by sentiment relevance
    news_items.sort(key=lambda x: (
        not x['on_watchlist'],
        sentiment_priority.get(x['sentiment'], 2),
    ))

    return news_items


def collect_score_changes(client, watchlist_ids: set) -> list:
    """Collect score changes from company_scores trend_7d."""
    changes = []

    try:
        resp = client.table('company_scores') \
            .select('company_id, score_value, details') \
            .eq('score_type', 'trend_7d') \
            .execute()

        # Filter to meaningful changes (>=1 point)
        significant = [r for r in resp.data if abs(float(r['score_value'])) >= 1]

        # Get names
        company_ids = [r['company_id'] for r in significant]
        names = {}
        for cid in company_ids:
            try:
                cr = client.table('companies').select('name').eq('id', cid).single().execute()
                if cr.data:
                    names[cid] = cr.data['name']
            except Exception:
                pass

        # Also get current overall scores
        current_scores = {}
        try:
            score_resp = client.table('company_scores') \
                .select('company_id, score_value') \
                .eq('score_type', 'overall') \
                .execute()
            for s in score_resp.data:
                cid = s['company_id']
                if cid not in current_scores:
                    current_scores[cid] = float(s['score_value'])
        except Exception:
            pass

        for row in significant:
            cid = row['company_id']
            delta = float(row['score_value'])
            current = current_scores.get(cid)
            old_score = round(current - delta, 1) if current is not None else None

            changes.append({
                'company_id': cid,
                'name': names.get(cid, '?'),
                'old_score': old_score,
                'new_score': round(current, 1) if current is not None else None,
                'delta': round(delta, 1),
                'on_watchlist': cid in watchlist_ids,
            })

        # Sort: watchlist first, then by absolute delta descending
        changes.sort(key=lambda x: (not x['on_watchlist'], -abs(x['delta'])))

    except Exception as e:
        print(f"  Warning: Could not load score changes: {e}")

    return changes


def collect_upcoming_events(client, watchlist_ids: set) -> list:
    """Collect events in next 7 days."""
    today = datetime.now().date()
    end_date = (today + timedelta(days=7)).isoformat()
    today_str = today.isoformat()
    events = []

    try:
        resp = client.table('company_events') \
            .select('company_id, event_type, event_date, description') \
            .gte('event_date', today_str) \
            .lte('event_date', end_date) \
            .order('event_date') \
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
            event_date = event.get('event_date', '')
            try:
                days_until = (datetime.fromisoformat(event_date).date() - today).days
            except (ValueError, TypeError):
                days_until = 0

            events.append({
                'company_id': cid,
                'name': names.get(cid, '?'),
                'event_type': event.get('event_type', 'event'),
                'event_date': event_date,
                'days_until': days_until,
                'description': event.get('description', ''),
                'on_watchlist': cid in watchlist_ids,
            })

        # Sort: watchlist first, then by days_until ascending
        events.sort(key=lambda x: (not x['on_watchlist'], x['days_until']))

    except Exception as e:
        print(f"  Warning: Could not load upcoming events: {e}")

    return events


def collect_new_companies(companies: list, watchlist_ids: set) -> list:
    """Collect companies added in last 24h."""
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    new = []

    for c in companies:
        created = c.get('created_at', '')
        if not created or created < cutoff:
            continue

        extra = c.get('extra_data') or {}
        new.append({
            'company_id': c['id'],
            'name': c.get('name', '?'),
            'sector': extra.get('Sector', 'N/A'),
            'listing_status': c.get('listing_status', 'unknown'),
            'on_watchlist': c['id'] in watchlist_ids,
        })

    # Sort: watchlist first
    new.sort(key=lambda x: not x['on_watchlist'])
    return new


def check_existing_diff(client) -> bool:
    """Check if a daily_diff already exists for today."""
    today_start = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
    try:
        resp = client.table('alerts') \
            .select('id', count='exact') \
            .eq('alert_type', 'daily_diff') \
            .gte('created_at', today_start) \
            .execute()
        return (resp.count or 0) > 0
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description='Generate daily diff summary')
    parser.add_argument('--apply', action='store_true', help='Write daily diff to Supabase')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  DAILY DIFF GENERATOR")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    client = supabase_helper.get_client()

    # Check for existing daily diff today
    if check_existing_diff(client):
        print("\n  Daily diff already exists for today. Skipping.")
        print("  Done!")
        return

    # Load watchlist
    print("\n  Loading watchlist...")
    watchlist_ids = load_watchlist_ids(client)
    print(f"  Watchlist companies: {len(watchlist_ids)}")

    # Load companies
    print("  Loading companies...")
    companies = supabase_helper.get_all_companies(
        'id, name, symbol, current_price, listing_status, created_at, extra_data'
    )
    print(f"  Loaded {len(companies)} companies")

    # Collect all data
    print("\n  Collecting price moves (>1%)...")
    price_moves = collect_price_moves(companies, watchlist_ids)
    wl_price_count = sum(1 for m in price_moves if m['on_watchlist'])
    print(f"  Found: {len(price_moves)} price moves ({wl_price_count} watchlist)")

    print("  Collecting new news (last 24h)...")
    new_news = collect_new_news(client, watchlist_ids)
    wl_news_count = sum(1 for n in new_news if n['on_watchlist'])
    print(f"  Found: {len(new_news)} articles ({wl_news_count} watchlist)")

    print("  Collecting score changes...")
    score_changes = collect_score_changes(client, watchlist_ids)
    wl_score_count = sum(1 for s in score_changes if s['on_watchlist'])
    print(f"  Found: {len(score_changes)} score changes ({wl_score_count} watchlist)")

    print("  Collecting upcoming events (next 7 days)...")
    upcoming_events = collect_upcoming_events(client, watchlist_ids)
    wl_event_count = sum(1 for e in upcoming_events if e['on_watchlist'])
    print(f"  Found: {len(upcoming_events)} events ({wl_event_count} watchlist)")

    print("  Collecting new companies (last 24h)...")
    new_companies = collect_new_companies(companies, watchlist_ids)
    print(f"  Found: {len(new_companies)} new companies")

    # Build summary
    watchlist_changes = wl_price_count + wl_news_count + wl_score_count
    total_changes = len(price_moves) + len(new_news) + len(score_changes) + len(new_companies)

    summary = {
        'total_changes': total_changes,
        'watchlist_changes': watchlist_changes,
        'price_moves_count': len(price_moves),
        'news_count': len(new_news),
        'score_changes_count': len(score_changes),
        'events_count': len(upcoming_events),
        'new_companies_count': len(new_companies),
    }

    # Strip on_watchlist flag from items before storage (not needed in DB)
    def strip_wl(items):
        return [{k: v for k, v in i.items() if k != 'on_watchlist'} for i in items]

    # Build the daily diff payload
    # Limit lists to keep JSONB manageable
    diff_data = {
        'price_moves': strip_wl(price_moves[:50]),
        'new_news': strip_wl(new_news[:50]),
        'score_changes': strip_wl(score_changes[:50]),
        'upcoming_events': strip_wl(upcoming_events[:30]),
        'new_companies': strip_wl(new_companies[:30]),
        'summary': summary,
        'generated_at': datetime.now().isoformat(),
    }

    # Print summary
    print(f"\n  " + "-" * 50)
    print(f"  DAILY DIFF SUMMARY")
    print(f"  " + "-" * 50)
    print(f"  Total changes:      {total_changes}")
    print(f"  Watchlist changes:   {watchlist_changes}")
    print(f"  Price moves (>1%):   {len(price_moves)}")
    print(f"  New news articles:   {len(new_news)}")
    print(f"  Score changes:       {len(score_changes)}")
    print(f"  Upcoming events:     {len(upcoming_events)}")
    print(f"  New companies:       {len(new_companies)}")

    # Show top price movers
    if price_moves:
        print(f"\n  Top price movers:")
        for m in price_moves[:5]:
            wl_tag = " [WL]" if m.get('on_watchlist') else ""
            print(f"    {m['change_pct']:+6.1f}%  {m['name']}{wl_tag}  ${m['price']:.2f}")

    # Show top news
    if new_news:
        print(f"\n  Top news articles:")
        for n in new_news[:5]:
            wl_tag = " [WL]" if n.get('on_watchlist') else ""
            print(f"    [{n['sentiment']:8s}] {n['name']}{wl_tag}: {n['title'][:60]}")

    # Show upcoming events
    if upcoming_events:
        print(f"\n  Upcoming events:")
        for e in upcoming_events[:5]:
            wl_tag = " [WL]" if e.get('on_watchlist') else ""
            print(f"    In {e['days_until']}d: {e['event_type']} - {e['name']}{wl_tag}")

    # Write to Supabase
    if args.apply:
        # We need a valid company_id for the FK constraint.
        # Use the first company in the DB as a sentinel.
        sentinel_id = companies[0]['id'] if companies else None
        if not sentinel_id:
            print("\n  Error: No companies found, cannot write daily diff")
            return

        alert_row = {
            'company_id': sentinel_id,
            'alert_type': 'daily_diff',
            'priority': 'low',
            'title': f"Daily Diff {datetime.now().strftime('%Y-%m-%d')}",
            'message': (
                f"Tagesueberblick: {len(price_moves)} Kursbewegungen, "
                f"{len(new_news)} Nachrichten, {len(score_changes)} Score-Aenderungen, "
                f"{len(upcoming_events)} Events, {len(new_companies)} neue Unternehmen"
            ),
            'condition': diff_data,
            'is_active': True,
            'is_read': False,
        }

        print(f"\n  Writing daily diff to alerts table...")
        try:
            client.table('alerts').insert(alert_row).execute()
            print(f"  Written successfully!")
        except Exception as e:
            print(f"  Error writing daily diff: {e}")
    else:
        print(f"\n  Run with --apply to write daily diff to Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

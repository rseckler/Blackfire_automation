#!/usr/bin/env python3
"""
Score History — retention cleanup and trend analysis.

Manages company_scores history:
  - Deletes scores older than retention window (default 90 days)
  - Calculates 7-day and 30-day score trends per company
  - Saves trend data as score_type='trend_7d' / 'trend_30d'

Designed to run after scoring_engine.py in the cron chain.

Usage:
  python3 score_history.py                          # dry-run
  python3 score_history.py --apply                  # cleanup + trends
  python3 score_history.py --apply --cleanup-only   # only retention cleanup
  python3 score_history.py --apply --trends-only    # only trend calculation
  python3 score_history.py --retention-days 60      # custom retention
"""

import argparse
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper


def cleanup_old_scores(client, retention_days: int, apply: bool) -> int:
    """Delete scores older than retention window."""
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()

    print(f"\n  Retention cleanup (>{retention_days} days)...")
    print(f"  Cutoff: {cutoff[:10]}")

    try:
        # Count old scores
        resp = client.table('company_scores') \
            .select('id', count='exact') \
            .lt('computed_at', cutoff) \
            .execute()
        count = resp.count or 0
        print(f"  Scores to delete: {count}")

        if apply and count > 0:
            client.table('company_scores') \
                .delete() \
                .lt('computed_at', cutoff) \
                .execute()
            print(f"  Deleted: {count} old scores")
        elif count > 0:
            print(f"  Would delete {count} scores (dry-run)")

        return count
    except Exception as e:
        print(f"  Error during cleanup: {e}")
        return 0


def calculate_trends(client, apply: bool) -> dict:
    """Calculate 7-day and 30-day score trends."""
    print(f"\n  Calculating score trends...")

    today = datetime.now()
    date_7d = (today - timedelta(days=7)).isoformat()
    date_30d = (today - timedelta(days=30)).isoformat()

    # Get current overall scores (most recent per company)
    try:
        current_resp = client.table('company_scores') \
            .select('company_id, score_value, computed_at') \
            .eq('score_type', 'overall') \
            .order('computed_at', desc=True) \
            .execute()
    except Exception as e:
        print(f"  Error loading current scores: {e}")
        return {}

    # Deduplicate: keep latest per company
    current_scores = {}
    for row in current_resp.data:
        cid = row['company_id']
        if cid not in current_scores:
            current_scores[cid] = float(row['score_value'])

    print(f"  Companies with current scores: {len(current_scores)}")

    # Get scores from 7 days ago
    try:
        resp_7d = client.table('company_scores') \
            .select('company_id, score_value, computed_at') \
            .eq('score_type', 'overall') \
            .lt('computed_at', date_7d) \
            .order('computed_at', desc=True) \
            .execute()
    except Exception as e:
        print(f"  Error loading 7d scores: {e}")
        resp_7d = type('obj', (object,), {'data': []})()

    scores_7d = {}
    for row in resp_7d.data:
        cid = row['company_id']
        if cid not in scores_7d:
            scores_7d[cid] = float(row['score_value'])

    # Get scores from 30 days ago
    try:
        resp_30d = client.table('company_scores') \
            .select('company_id, score_value, computed_at') \
            .eq('score_type', 'overall') \
            .lt('computed_at', date_30d) \
            .order('computed_at', desc=True) \
            .execute()
    except Exception as e:
        print(f"  Error loading 30d scores: {e}")
        resp_30d = type('obj', (object,), {'data': []})()

    scores_30d = {}
    for row in resp_30d.data:
        cid = row['company_id']
        if cid not in scores_30d:
            scores_30d[cid] = float(row['score_value'])

    # Calculate trends
    trends = {'7d': {}, '30d': {}}
    stats = {'up_7d': 0, 'down_7d': 0, 'stable_7d': 0, 'up_30d': 0, 'down_30d': 0, 'stable_30d': 0}

    for cid, current in current_scores.items():
        # 7-day trend
        if cid in scores_7d:
            delta = round(current - scores_7d[cid], 1)
            direction = 'up' if delta > 5 else ('down' if delta < -5 else 'stable')
            trends['7d'][cid] = {'delta': delta, 'direction': direction}
            stats[f'{direction}_7d'] += 1

        # 30-day trend
        if cid in scores_30d:
            delta = round(current - scores_30d[cid], 1)
            direction = 'up' if delta > 5 else ('down' if delta < -5 else 'stable')
            trends['30d'][cid] = {'delta': delta, 'direction': direction}
            stats[f'{direction}_30d'] += 1

    print(f"\n  7-day trends: {len(trends['7d'])} companies")
    print(f"    Up: {stats['up_7d']}, Down: {stats['down_7d']}, Stable: {stats['stable_7d']}")
    print(f"\n  30-day trends: {len(trends['30d'])} companies")
    print(f"    Up: {stats['up_30d']}, Down: {stats['down_30d']}, Stable: {stats['stable_30d']}")

    # Top movers
    if trends['7d']:
        movers_7d = sorted(trends['7d'].items(), key=lambda x: abs(x[1]['delta']), reverse=True)
        print(f"\n  Top 7-day movers:")
        for cid, t in movers_7d[:5]:
            print(f"    {t['delta']:+6.1f}  {cid[:8]}...")

    # Write trend scores
    if apply:
        written = 0
        for period, period_trends in trends.items():
            score_type = f'trend_{period}'
            for cid, trend in period_trends.items():
                try:
                    client.table('company_scores').upsert({
                        'company_id': cid,
                        'score_type': score_type,
                        'score_value': trend['delta'],
                        'details': {'direction': trend['direction'], 'period_days': 7 if period == '7d' else 30},
                        'computed_at': datetime.now().isoformat(),
                    }, on_conflict='company_id,score_type').execute()
                    written += 1
                except Exception:
                    pass

        print(f"\n  Written: {written} trend scores")
    else:
        total = len(trends['7d']) + len(trends['30d'])
        print(f"\n  Would write {total} trend scores (dry-run)")

    return stats


def main():
    parser = argparse.ArgumentParser(description='Score history management')
    parser.add_argument('--apply', action='store_true', help='Apply changes to Supabase')
    parser.add_argument('--retention-days', type=int, default=90, help='Days to retain scores (default: 90)')
    parser.add_argument('--cleanup-only', action='store_true', help='Only run retention cleanup')
    parser.add_argument('--trends-only', action='store_true', help='Only calculate trends')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  SCORE HISTORY MANAGEMENT")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print(f"  Retention: {args.retention_days} days")
    if args.cleanup_only:
        print(f"  Scope: Cleanup only")
    elif args.trends_only:
        print(f"  Scope: Trends only")
    print("=" * 70)

    client = supabase_helper.get_client()

    if not args.trends_only:
        cleanup_old_scores(client, args.retention_days, args.apply)

    if not args.cleanup_only:
        calculate_trends(client, args.apply)

    if not args.apply:
        print(f"\n  Run with --apply to write changes to Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

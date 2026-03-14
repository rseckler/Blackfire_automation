#!/usr/bin/env python3
"""
Blackfire Scoring Engine v2 — calculates a composite score (0-100) per company.

Score Components:
  - Valuation Gap    (25%): Analyst target / purchase price vs current price
  - Conviction Signal(25%): Thier_Group + VIP + Prio_Buy
  - Price Momentum   (20%): Price vs 52W range
  - News Sentiment   (15%): Positive/negative news ratio
  - Catalyst Prox.   (15%): Upcoming events proximity

Score Labels:
  80-100  Strong Buy
  60-79   Accumulate
  40-59   Hold
  20-39   Review
   0-19   Remove

Usage:
  python3 scoring_engine.py                    # dry-run (preview only)
  python3 scoring_engine.py --apply            # write scores to Supabase
  python3 scoring_engine.py --apply --limit 50 # score first 50 companies
"""

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

# ── Score weights ──
WEIGHTS = {
    'valuation_gap': 0.25,
    'conviction_signal': 0.25,
    'price_momentum': 0.20,
    'news_sentiment': 0.15,
    'catalyst_proximity': 0.15,
}

# ── Conviction Signal mappings ──
THIER_GROUP_MAP = {
    '2026***': 100, '2026**': 80, '2026*': 60, '2026': 40,
    '2025***': 35, '2025**': 30, '2025*': 25, '2025': 20,
}
VIP_MAP = {
    'Defcon 1': 100, 'Defcon 2': 75, 'Defcon 3': 50,
}
PRIO_BUY_MAP = {1: 100, 2: 80, 3: 60, 4: 40, 5: 20}


def get_score_label(score: float) -> str:
    """Map overall score to a human-readable label."""
    if score >= 80:
        return 'Strong Buy'
    if score >= 60:
        return 'Accumulate'
    if score >= 40:
        return 'Hold'
    if score >= 20:
        return 'Review'
    return 'Remove'


def _get_price(company: dict) -> float | None:
    """Extract current price from company row."""
    price = company.get('current_price')
    if not price:
        extra = company.get('extra_data') or {}
        price_str = extra.get('Current_Price')
        if price_str:
            try:
                price = float(str(price_str).replace(',', '.'))
            except (ValueError, TypeError):
                return None
    if not price or float(price) <= 0:
        return None
    return float(price)


def score_valuation_gap(company: dict) -> float:
    """Score valuation gap (0-100).

    Priority:
      1. Analyst_Target_Mean vs current_price
      2. Purchase_$ vs current_price
      3. Forward_PE < 15 AND Revenue_Growth > 20% → 75
      4. Neutral → 50
    """
    extra = company.get('extra_data') or {}
    price = _get_price(company)

    def _map_gap_pct(gap_pct: float) -> float:
        """Map gap percentage to score.
        -50% → 0, 0% → 50, +100% → 85, +200% → 100
        """
        if gap_pct <= -50:
            return 0.0
        if gap_pct <= 0:
            # Linear from 0 (-50%) to 50 (0%)
            return (gap_pct + 50) / 50 * 50
        if gap_pct <= 100:
            # Linear from 50 (0%) to 85 (+100%)
            return 50 + (gap_pct / 100) * 35
        if gap_pct <= 200:
            # Linear from 85 (+100%) to 100 (+200%)
            return 85 + ((gap_pct - 100) / 100) * 15
        return 100.0

    # 1. Analyst target vs current price
    if price:
        target_str = extra.get('Analyst_Target_Mean')
        if target_str is not None:
            try:
                target = float(str(target_str).replace(',', '.'))
                if target > 0:
                    gap_pct = (target - price) / price * 100
                    return round(_map_gap_pct(gap_pct), 1)
            except (ValueError, TypeError):
                pass

        # 2. Purchase price as target proxy
        purchase_str = extra.get('Purchase_$')
        if purchase_str is not None:
            try:
                purchase = float(str(purchase_str).replace(',', '.'))
                if purchase > 0:
                    gap_pct = (purchase - price) / price * 100
                    return round(_map_gap_pct(gap_pct), 1)
            except (ValueError, TypeError):
                pass

    # 3. Cheap growth heuristic
    fwd_pe_str = extra.get('Forward_PE')
    rev_growth_str = extra.get('Revenue_Growth')
    if fwd_pe_str is not None and rev_growth_str is not None:
        try:
            fwd_pe = float(str(fwd_pe_str).replace(',', '.'))
            rev_growth = float(str(rev_growth_str).replace(',', '.'))
            if fwd_pe < 15 and rev_growth > 0.2:
                return 75.0
        except (ValueError, TypeError):
            pass

    # 4. Neutral
    return 50.0


def score_conviction_signal(company: dict) -> float:
    """Score from Thier_Group + VIP + Prio_Buy (0-100)."""
    extra = company.get('extra_data') or {}

    # Thier_Group (40% of this component)
    tg = (company.get('thier_group') or extra.get('Thier_Group') or '').strip()
    tg_score = THIER_GROUP_MAP.get(tg, 20)

    # VIP (30% of this component)
    vip = (company.get('vip') or extra.get('VIP') or '').strip()
    vip_score = VIP_MAP.get(vip, 25)

    # Prio_Buy (30% of this component)
    pb = company.get('prio_buy')
    if pb is None:
        pb_raw = extra.get('Prio_Buy')
        if pb_raw:
            try:
                pb = int(float(str(pb_raw)))
            except (ValueError, TypeError):
                pb = None
    pb_score = PRIO_BUY_MAP.get(pb, 40) if pb else 40

    return round(tg_score * 0.4 + vip_score * 0.3 + pb_score * 0.3, 1)


def score_price_momentum(company: dict) -> float:
    """Score price momentum (0-100). No data = 50 (neutral)."""
    extra = company.get('extra_data') or {}
    price = _get_price(company)

    if not price:
        return 50.0

    # Check 52-week range
    high_52w = extra.get('52W_High') or extra.get('52_Week_High')
    low_52w = extra.get('52W_Low') or extra.get('52_Week_Low')

    if high_52w and low_52w:
        try:
            high_52w = float(str(high_52w).replace(',', '.'))
            low_52w = float(str(low_52w).replace(',', '.'))
            if high_52w > low_52w > 0:
                position = (price - low_52w) / (high_52w - low_52w)
                return round(min(max(position * 100, 0), 100), 1)
        except (ValueError, TypeError):
            pass

    # Check daily change
    change_str = extra.get('Change_%') or extra.get('Change_Percent')
    if change_str:
        try:
            change = float(str(change_str).replace(',', '.').replace('%', ''))
            # Map -20%..+20% to 0..100
            return round(min(max((change + 20) / 40 * 100, 0), 100), 1)
        except (ValueError, TypeError):
            pass

    return 50.0


def score_news_sentiment(company_id: str, news_cache: dict) -> float:
    """Score news sentiment from last 30 days (0-100). No news = 50."""
    news_list = news_cache.get(company_id, [])
    if not news_list:
        return 50.0

    pos = sum(1 for n in news_list if n.get('sentiment') == 'positive')
    neg = sum(1 for n in news_list if n.get('sentiment') == 'negative')
    total = pos + neg

    if total == 0:
        return 50.0

    # Ratio of positive to total sentiment news
    ratio = pos / total
    return round(ratio * 100, 1)


def score_catalyst_proximity(company_id: str, events_cache: dict) -> float:
    """Score based on upcoming events (0-100). No event = 30."""
    events = events_cache.get(company_id, [])
    if not events:
        return 30.0

    today = datetime.now().date()
    best_score = 30.0

    for event in events:
        event_date = event.get('event_date')
        if not event_date:
            continue

        try:
            if isinstance(event_date, str):
                ed = datetime.strptime(event_date, '%Y-%m-%d').date()
            else:
                ed = event_date
        except (ValueError, TypeError):
            continue

        days_until = (ed - today).days
        if days_until < 0:
            continue  # past event

        # Score by event type
        event_type = (event.get('event_type') or '').lower()
        if event_type == 'ipo':
            type_score = 100
        elif event_type in ('earnings', 'earnings_report'):
            type_score = 80
        else:
            type_score = 60

        # Closer events score higher (within 30 days)
        if days_until <= 30:
            proximity = 1.0 - (days_until / 30)
            score = 30 + proximity * (type_score - 30)
            best_score = max(best_score, score)

    return round(best_score, 1)


def load_news_cache(client) -> dict:
    """Load recent news grouped by company_id."""
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    cache = {}
    try:
        resp = client.table('company_news') \
            .select('company_id, sentiment') \
            .gte('published_at', cutoff) \
            .execute()
        for row in resp.data:
            cid = row['company_id']
            cache.setdefault(cid, []).append(row)
    except Exception as e:
        print(f"  Warning: Could not load news: {e}")
    return cache


def load_events_cache(client) -> dict:
    """Load upcoming events grouped by company_id."""
    today = datetime.now().date().isoformat()
    cache = {}
    try:
        resp = client.table('company_events') \
            .select('company_id, event_type, event_date') \
            .gte('event_date', today) \
            .execute()
        for row in resp.data:
            cid = row['company_id']
            cache.setdefault(cid, []).append(row)
    except Exception as e:
        print(f"  Warning: Could not load events: {e}")
    return cache


def main():
    parser = argparse.ArgumentParser(description='Calculate Blackfire Scores v2')
    parser.add_argument('--apply', action='store_true', help='Write scores to Supabase')
    parser.add_argument('--limit', type=int, default=0, help='Limit number of companies')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  BLACKFIRE SCORING ENGINE v2")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    if args.limit:
        print(f"  Limit: {args.limit} companies")
    print("=" * 70)

    # Load data
    print("\n  Loading companies...")
    companies = supabase_helper.get_all_companies(
        'id, name, symbol, isin, wkn, current_price, thier_group, vip, prio_buy, extra_data'
    )
    print(f"  Loaded {len(companies)} companies")

    if args.limit:
        companies = companies[:args.limit]

    client = supabase_helper.get_client()

    print("  Loading news cache (last 30 days)...")
    news_cache = load_news_cache(client)
    print(f"  News entries: {sum(len(v) for v in news_cache.values())}")

    print("  Loading events cache (upcoming)...")
    events_cache = load_events_cache(client)
    print(f"  Event entries: {sum(len(v) for v in events_cache.values())}")

    # Calculate scores
    results = []
    score_dist = Counter()
    label_dist = Counter()

    for company in companies:
        cid = company['id']

        components = {
            'valuation_gap': score_valuation_gap(company),
            'conviction_signal': score_conviction_signal(company),
            'price_momentum': score_price_momentum(company),
            'news_sentiment': score_news_sentiment(cid, news_cache),
            'catalyst_proximity': score_catalyst_proximity(cid, events_cache),
        }

        overall = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)
        overall = round(overall, 1)
        label = get_score_label(overall)

        # Bucket for distribution
        if overall >= 70:
            score_dist['good (>=70)'] += 1
        elif overall >= 40:
            score_dist['medium (40-69)'] += 1
        else:
            score_dist['low (<40)'] += 1

        label_dist[label] += 1

        results.append({
            'company_id': cid,
            'name': company.get('name', '?'),
            'overall': overall,
            'label': label,
            'components': components,
        })

    # Sort by overall score descending
    results.sort(key=lambda x: -x['overall'])

    # Report
    print(f"\n  Score distribution:")
    for bucket, count in sorted(score_dist.items()):
        print(f"    {bucket:20s}: {count:5d}")

    print(f"\n  Label distribution:")
    for label in ['Strong Buy', 'Accumulate', 'Hold', 'Review', 'Remove']:
        count = label_dist.get(label, 0)
        print(f"    {label:20s}: {count:5d}")

    print(f"\n  Top 10:")
    for r in results[:10]:
        print(f"    {r['overall']:5.1f} [{r['label']:11s}]  {r['name'][:50]}")

    print(f"\n  Bottom 10:")
    for r in results[-10:]:
        print(f"    {r['overall']:5.1f} [{r['label']:11s}]  {r['name'][:50]}")

    # Apply
    if args.apply:
        print(f"\n  Clearing old scores...")
        try:
            client.table('company_scores').delete().neq('id', '00000000-0000-0000-0000-000000000000').execute()
            print(f"  Old scores cleared")
        except Exception as e:
            print(f"  Warning: Could not clear old scores: {e}")

        print(f"  Writing {len(results)} scores to company_scores...")
        now = datetime.now().isoformat()
        success = 0
        errors = 0

        # Batch insert in chunks of 50
        batch = []
        for i, r in enumerate(results):
            # Overall score with label in details
            details_with_label = dict(r['components'])
            details_with_label['score_label'] = r['label']

            batch.append({
                'company_id': r['company_id'],
                'score_type': 'overall',
                'score_value': r['overall'],
                'details': details_with_label,
                'computed_at': now,
            })

            # Component scores
            for comp_name, comp_value in r['components'].items():
                batch.append({
                    'company_id': r['company_id'],
                    'score_type': comp_name,
                    'score_value': comp_value,
                    'details': {'weight': WEIGHTS[comp_name]},
                    'computed_at': now,
                })

            # Flush batch every 50 companies (300 rows)
            if len(batch) >= 300 or i == len(results) - 1:
                try:
                    client.table('company_scores').insert(batch).execute()
                    success += len(batch)
                except Exception as e:
                    errors += len(batch)
                    if errors <= 5:
                        print(f"    Batch error: {e}")
                batch = []

                if (i + 1) % 200 == 0:
                    print(f"    ... {i + 1}/{len(results)} companies")

        print(f"  Written: {success} rows ({errors} errors)")
    else:
        print(f"\n  Run with --apply to write scores to Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Portfolio Exit Signal Monitor — checks holdings for exit conditions.

Runs hourly during market hours. For each holding, calculates P&L
and generates alerts for take-profit, partial-profit, stop-loss,
and stale positions.

Alert type: portfolio_exit_signal
Dedup: 24h window per company per signal subtype

Usage:
  python3 portfolio_monitor.py              # dry-run (preview only)
  python3 portfolio_monitor.py --apply      # write alerts to Supabase
"""

import argparse
import os
from datetime import datetime, timedelta
from collections import Counter
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

ALERT_TYPE = 'portfolio_exit_signal'

# Signal subtypes
SIGNAL_TAKE_PROFIT = 'take_profit'
SIGNAL_PARTIAL_PROFIT = 'partial_profit'
SIGNAL_STOP_LOSS = 'stop_loss'
SIGNAL_STALE = 'stale_position'

DEDUP_WINDOW_HOURS = 24


def load_recent_exit_alerts(client) -> dict:
    """Load recent portfolio_exit_signal alerts for deduplication.
    Returns {(company_id, subtype): created_at}."""
    cutoff = (datetime.now() - timedelta(hours=DEDUP_WINDOW_HOURS)).isoformat()
    try:
        resp = client.table('alerts') \
            .select('company_id, condition, created_at') \
            .eq('alert_type', ALERT_TYPE) \
            .gte('created_at', cutoff) \
            .execute()
        existing = {}
        for row in resp.data:
            cond = row.get('condition') or {}
            subtype = cond.get('subtype', '')
            key = (row['company_id'], subtype)
            existing[key] = row['created_at']
        return existing
    except Exception as e:
        print(f"  Warning: Could not load existing exit alerts: {e}")
        return {}


def is_duplicate(company_id: str, subtype: str, existing: dict) -> bool:
    """Check if alert already exists within dedup window."""
    key = (company_id, subtype)
    if key not in existing:
        return False

    created = existing[key]
    try:
        created_dt = datetime.fromisoformat(created.replace('Z', '+00:00')).replace(tzinfo=None)
        return (datetime.now() - created_dt).total_seconds() < DEDUP_WINDOW_HOURS * 3600
    except (ValueError, TypeError):
        return False


def load_all_holdings(client) -> list:
    """Load all holdings across all portfolios with company data."""
    try:
        resp = client.table('holdings') \
            .select('id, portfolio_id, company_id, quantity, average_purchase_price, purchase_date, created_at') \
            .execute()
        return resp.data or []
    except Exception as e:
        print(f"  Warning: Could not load holdings: {e}")
        return []


def load_company_prices(client, company_ids: list) -> dict:
    """Load current prices for given company IDs. Returns {company_id: {name, current_price}}."""
    if not company_ids:
        return {}

    prices = {}
    batch_size = 50
    for i in range(0, len(company_ids), batch_size):
        batch = company_ids[i:i + batch_size]
        try:
            resp = client.table('companies') \
                .select('id, name, symbol, current_price') \
                .in_('id', batch) \
                .execute()
            for row in resp.data:
                prices[row['id']] = row
        except Exception as e:
            print(f"  Warning: Could not load prices for batch: {e}")

    return prices


def detect_exit_signals(holdings: list, company_data: dict, existing: dict) -> list:
    """Analyze holdings and generate exit signal alerts."""
    alerts = []
    now = datetime.now()

    for h in holdings:
        company_id = h['company_id']
        company = company_data.get(company_id)
        if not company:
            continue

        current_price = company.get('current_price')
        if not current_price:
            continue

        try:
            current_price = float(current_price)
        except (ValueError, TypeError):
            continue

        if current_price <= 0:
            continue

        avg_price = h.get('average_purchase_price')
        if not avg_price:
            continue

        try:
            avg_price = float(avg_price)
        except (ValueError, TypeError):
            continue

        if avg_price <= 0:
            continue

        pnl_pct = ((current_price - avg_price) / avg_price) * 100
        name = company.get('name', '?')
        symbol = company.get('symbol', '')

        # Calculate days held
        purchase_date = h.get('purchase_date') or h.get('created_at', '')
        days_held = 0
        if purchase_date:
            try:
                pd_dt = datetime.fromisoformat(purchase_date.replace('Z', '+00:00')).replace(tzinfo=None)
                days_held = (now - pd_dt).days
            except (ValueError, TypeError):
                days_held = 0

        # --- Take Profit: P&L >= +100% ---
        if pnl_pct >= 100:
            if not is_duplicate(company_id, SIGNAL_TAKE_PROFIT, existing):
                alerts.append({
                    'company_id': company_id,
                    'alert_type': ALERT_TYPE,
                    'priority': 'high',
                    'title': f"Take-Profit prufen: {name} bei +{pnl_pct:.0f}%",
                    'message': f"{name} ({symbol}): Einstand ${avg_price:.2f} -> Aktuell ${current_price:.2f} ({pnl_pct:+.1f}%). Position hat sich verdoppelt.",
                    'condition': {
                        'type': ALERT_TYPE,
                        'subtype': SIGNAL_TAKE_PROFIT,
                        'pnl_percent': round(pnl_pct, 1),
                        'avg_price': avg_price,
                        'current_price': current_price,
                        'days_held': days_held,
                        'threshold': 100,
                    },
                    'is_active': True,
                    'is_read': False,
                })

        # --- Partial Profit: P&L >= +50% (but < 100%, otherwise take_profit fires) ---
        elif pnl_pct >= 50:
            if not is_duplicate(company_id, SIGNAL_PARTIAL_PROFIT, existing):
                alerts.append({
                    'company_id': company_id,
                    'alert_type': ALERT_TYPE,
                    'priority': 'medium',
                    'title': f"Teilverkauf erwaegen: {name} bei +{pnl_pct:.0f}%",
                    'message': f"{name} ({symbol}): Einstand ${avg_price:.2f} -> Aktuell ${current_price:.2f} ({pnl_pct:+.1f}%). Teilverkauf erwaegen.",
                    'condition': {
                        'type': ALERT_TYPE,
                        'subtype': SIGNAL_PARTIAL_PROFIT,
                        'pnl_percent': round(pnl_pct, 1),
                        'avg_price': avg_price,
                        'current_price': current_price,
                        'days_held': days_held,
                        'threshold': 50,
                    },
                    'is_active': True,
                    'is_read': False,
                })

        # --- Stop Loss: P&L <= -20% ---
        elif pnl_pct <= -20:
            if not is_duplicate(company_id, SIGNAL_STOP_LOSS, existing):
                alerts.append({
                    'company_id': company_id,
                    'alert_type': ALERT_TYPE,
                    'priority': 'high',
                    'title': f"Stop-Loss Warnung: {name} bei {pnl_pct:.0f}%",
                    'message': f"{name} ({symbol}): Einstand ${avg_price:.2f} -> Aktuell ${current_price:.2f} ({pnl_pct:+.1f}%). Verlustbegrenzung pruefen.",
                    'condition': {
                        'type': ALERT_TYPE,
                        'subtype': SIGNAL_STOP_LOSS,
                        'pnl_percent': round(pnl_pct, 1),
                        'avg_price': avg_price,
                        'current_price': current_price,
                        'days_held': days_held,
                        'threshold': -20,
                    },
                    'is_active': True,
                    'is_read': False,
                })

        # --- Stale Position: Held >180 days AND -10% < P&L < +20% ---
        elif days_held > 180 and -10 < pnl_pct < 20:
            if not is_duplicate(company_id, SIGNAL_STALE, existing):
                alerts.append({
                    'company_id': company_id,
                    'alert_type': ALERT_TYPE,
                    'priority': 'low',
                    'title': f"Stagnierende Position: {name} seit {days_held} Tagen",
                    'message': f"{name} ({symbol}): {pnl_pct:+.1f}% nach {days_held} Tagen. Position stagniert — Kapital evtl. besser einsetzbar.",
                    'condition': {
                        'type': ALERT_TYPE,
                        'subtype': SIGNAL_STALE,
                        'pnl_percent': round(pnl_pct, 1),
                        'avg_price': avg_price,
                        'current_price': current_price,
                        'days_held': days_held,
                        'threshold_days': 180,
                    },
                    'is_active': True,
                    'is_read': False,
                })

    return alerts


def main():
    parser = argparse.ArgumentParser(description='Portfolio exit signal monitor')
    parser.add_argument('--apply', action='store_true', help='Write alerts to Supabase')
    parser.add_argument('--dry-run', action='store_true', help='Preview only (default)')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  PORTFOLIO EXIT SIGNAL MONITOR")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    client = supabase_helper.get_client()

    # Load existing alerts for dedup
    print("\n  Loading existing exit alerts for deduplication...")
    existing = load_recent_exit_alerts(client)
    print(f"  Existing alerts (last {DEDUP_WINDOW_HOURS}h): {len(existing)}")

    # Load holdings
    print("  Loading portfolio holdings...")
    holdings = load_all_holdings(client)
    print(f"  Holdings found: {len(holdings)}")

    if not holdings:
        print("\n  No holdings found. Nothing to check.")
        print("  Done!")
        return

    # Load company prices
    company_ids = list(set(h['company_id'] for h in holdings))
    print(f"  Loading prices for {len(company_ids)} companies...")
    company_data = load_company_prices(client, company_ids)
    print(f"  Companies with price data: {len(company_data)}")

    # Detect exit signals
    print("\n  Checking exit conditions...")
    alerts = detect_exit_signals(holdings, company_data, existing)

    # Summary
    by_subtype = Counter(
        (a.get('condition') or {}).get('subtype', 'unknown') for a in alerts
    )
    by_priority = Counter(a['priority'] for a in alerts)

    print(f"\n  Total exit signals: {len(alerts)}")
    print(f"  By priority: high={by_priority.get('high', 0)}, medium={by_priority.get('medium', 0)}, low={by_priority.get('low', 0)}")
    print(f"  By subtype: take_profit={by_subtype.get('take_profit', 0)}, partial_profit={by_subtype.get('partial_profit', 0)}, stop_loss={by_subtype.get('stop_loss', 0)}, stale={by_subtype.get('stale_position', 0)}")

    if alerts:
        print(f"\n  Signals:")
        for a in alerts[:20]:
            print(f"    [{a['priority']:6s}] {a['title']}")

    # Apply
    if args.apply and alerts:
        print(f"\n  Writing {len(alerts)} alerts...")
        success = 0
        for a in alerts:
            try:
                client.table('alerts').insert(a).execute()
                success += 1
            except Exception as e:
                print(f"    Error: {e}")
        print(f"  Written: {success}/{len(alerts)}")
    elif not args.apply and alerts:
        print(f"\n  Run with --apply to write alerts to Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

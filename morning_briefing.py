#!/usr/bin/env python3
"""
Morning Briefing — AI-generated daily briefing using Claude Sonnet.

Collects alerts, news, score changes, and personal data (watchlist + holdings)
from the last 24 hours, sends them to Claude Sonnet, and stores the generated
personalized briefing.

Schedule: Daily 06:30 UTC (after Morning Sync at 06:00, Alert Generator at 06:15)

Usage:
  python3 morning_briefing.py              # dry-run (print to console)
  python3 morning_briefing.py --apply      # save briefing to Supabase
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import supabase_helper

try:
    from anthropic import Anthropic
except ImportError:
    print("Installing anthropic...")
    os.system(f"{sys.executable} -m pip install anthropic")
    from anthropic import Anthropic

MODEL = "claude-sonnet-4-20250514"


def collect_data(client) -> dict:
    """Collect alerts, news, and score changes from last 24h."""
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    data = {'alerts': [], 'news': [], 'movers': [], 'companies': {}}

    # 1. Recent alerts
    try:
        resp = client.table('alerts') \
            .select('company_id, alert_type, priority, title, message, metadata, created_at') \
            .gte('created_at', cutoff) \
            .order('created_at', desc=True) \
            .limit(50) \
            .execute()
        data['alerts'] = resp.data or []
    except Exception as e:
        print(f"  Warning: Could not load alerts: {e}")

    # 2. Recent news
    try:
        resp = client.table('company_news') \
            .select('company_id, title, summary, sentiment, source, published_at') \
            .gte('published_at', cutoff) \
            .order('published_at', desc=True) \
            .limit(20) \
            .execute()
        data['news'] = resp.data or []
    except Exception as e:
        print(f"  Warning: Could not load news: {e}")

    # 3. Score movers (top changes)
    try:
        resp = client.table('company_scores') \
            .select('company_id, score_value, details') \
            .eq('score_type', 'trend_7d') \
            .execute()
        # Sort by absolute delta
        movers = sorted(resp.data or [], key=lambda x: abs(float(x.get('score_value', 0))), reverse=True)
        data['movers'] = movers[:10]
    except Exception as e:
        print(f"  Warning: Could not load score movers: {e}")

    # 4. Load company details for all mentioned companies
    company_ids = set()
    for a in data['alerts']:
        if a.get('company_id'):
            company_ids.add(a['company_id'])
    for n in data['news']:
        if n.get('company_id'):
            company_ids.add(n['company_id'])
    for m in data['movers']:
        if m.get('company_id'):
            company_ids.add(m['company_id'])

    for cid in company_ids:
        try:
            resp = client.table('companies') \
                .select('id, name, symbol, thier_group, vip, current_price, listing_status') \
                .eq('id', cid) \
                .single() \
                .execute()
            if resp.data:
                data['companies'][cid] = resp.data
        except Exception:
            pass

    return data


def collect_personal_data(client) -> dict:
    """Collect user-specific data: watchlist, holdings, watchlist news, stale companies."""
    personal = {
        'watchlist_companies': [],
        'holdings_with_pnl': [],
        'watchlist_news': [],
        'stale_companies': [],
    }

    watchlist_ids = []

    # --- a. Watchlist companies ---
    try:
        resp = client.table('watchlist') \
            .select('company_id') \
            .execute()
        watchlist_rows = resp.data or []
        watchlist_ids = [r['company_id'] for r in watchlist_rows if r.get('company_id')]

        if watchlist_ids:
            for cid in watchlist_ids:
                try:
                    c_resp = client.table('companies') \
                        .select('id, name, symbol, current_price, extra_data') \
                        .eq('id', cid) \
                        .single() \
                        .execute()
                    if c_resp.data:
                        c = c_resp.data
                        current = float(c.get('current_price') or 0)
                        extra = c.get('extra_data') or {}
                        previous = float(extra.get('Previous_Close') or 0)
                        change_pct = ((current - previous) / previous * 100) if previous > 0 else None
                        personal['watchlist_companies'].append({
                            'id': c['id'],
                            'name': c.get('name', '?'),
                            'symbol': c.get('symbol', ''),
                            'current_price': current,
                            'previous_close': previous,
                            'change_pct': round(change_pct, 2) if change_pct is not None else None,
                        })
                except Exception:
                    pass
    except Exception as e:
        print(f"  Warning: Could not load watchlist: {e}")

    # --- b. Holdings with P&L ---
    try:
        # Get first portfolio
        port_resp = client.table('portfolios') \
            .select('id') \
            .limit(1) \
            .execute()
        portfolios = port_resp.data or []

        if portfolios:
            portfolio_id = portfolios[0]['id']
            hold_resp = client.table('holdings') \
                .select('company_id, quantity, average_cost') \
                .eq('portfolio_id', portfolio_id) \
                .execute()
            holdings = hold_resp.data or []

            for h in holdings:
                cid = h.get('company_id')
                if not cid:
                    continue
                try:
                    c_resp = client.table('companies') \
                        .select('id, name, symbol, current_price') \
                        .eq('id', cid) \
                        .single() \
                        .execute()
                    if c_resp.data:
                        c = c_resp.data
                        qty = float(h.get('quantity') or 0)
                        avg_cost = float(h.get('average_cost') or 0)
                        current = float(c.get('current_price') or 0)
                        cost_basis = avg_cost * qty
                        market_value = current * qty
                        pnl = market_value - cost_basis
                        pnl_pct = ((current - avg_cost) / avg_cost * 100) if avg_cost > 0 else None
                        personal['holdings_with_pnl'].append({
                            'name': c.get('name', '?'),
                            'symbol': c.get('symbol', ''),
                            'quantity': qty,
                            'average_cost': avg_cost,
                            'current_price': current,
                            'cost_basis': round(cost_basis, 2),
                            'market_value': round(market_value, 2),
                            'pnl': round(pnl, 2),
                            'pnl_pct': round(pnl_pct, 2) if pnl_pct is not None else None,
                        })
                except Exception:
                    pass
    except Exception as e:
        print(f"  Warning: Could not load holdings: {e}")

    # --- c. Watchlist news (last 24h) ---
    if watchlist_ids:
        cutoff_24h = (datetime.now() - timedelta(hours=24)).isoformat()
        try:
            # Supabase .in_() filter for watchlist company IDs
            resp = client.table('company_news') \
                .select('company_id, title, summary, sentiment, source, published_at') \
                .in_('company_id', watchlist_ids) \
                .gte('published_at', cutoff_24h) \
                .order('published_at', desc=True) \
                .limit(30) \
                .execute()
            personal['watchlist_news'] = resp.data or []
        except Exception as e:
            print(f"  Warning: Could not load watchlist news: {e}")

    # --- d. Stale watchlist (no news in 90 days) ---
    if watchlist_ids:
        cutoff_90d = (datetime.now() - timedelta(days=90)).isoformat()
        try:
            # Find watchlist companies WITH recent news
            resp = client.table('company_news') \
                .select('company_id') \
                .in_('company_id', watchlist_ids) \
                .gte('published_at', cutoff_90d) \
                .execute()
            active_ids = set(r['company_id'] for r in (resp.data or []))
            stale_ids = [cid for cid in watchlist_ids if cid not in active_ids]

            # Get names for stale companies
            for cid in stale_ids:
                wc = next((w for w in personal['watchlist_companies'] if w['id'] == cid), None)
                if wc:
                    personal['stale_companies'].append({
                        'name': wc['name'],
                        'symbol': wc['symbol'],
                    })
                else:
                    try:
                        c_resp = client.table('companies') \
                            .select('name, symbol') \
                            .eq('id', cid) \
                            .single() \
                            .execute()
                        if c_resp.data:
                            personal['stale_companies'].append({
                                'name': c_resp.data.get('name', '?'),
                                'symbol': c_resp.data.get('symbol', ''),
                            })
                    except Exception:
                        pass
        except Exception as e:
            print(f"  Warning: Could not determine stale watchlist: {e}")

    return personal


def build_prompt(data: dict, personal: dict) -> str:
    """Build the personalized prompt for Claude Sonnet."""
    companies = data['companies']

    # --- Format holdings ---
    if personal['holdings_with_pnl']:
        holdings_lines = []
        for h in personal['holdings_with_pnl']:
            pnl_str = f"{h['pnl']:+.2f} EUR ({h['pnl_pct']:+.1f}%)" if h['pnl_pct'] is not None else "N/A"
            holdings_lines.append(
                f"- {h['name']} ({h['symbol']}): {h['quantity']:.0f} Stück, "
                f"EK {h['average_cost']:.2f} EUR, Aktuell {h['current_price']:.2f} EUR, "
                f"P&L: {pnl_str}"
            )
        holdings_text = "\n".join(holdings_lines)
    else:
        holdings_text = "Noch kein Portfolio angelegt."

    # --- Format watchlist ---
    if personal['watchlist_companies']:
        wl_lines = []
        for w in personal['watchlist_companies']:
            change = f"{w['change_pct']:+.2f}%" if w['change_pct'] is not None else "N/A"
            wl_lines.append(
                f"- {w['name']} ({w['symbol']}): {w['current_price']:.2f} EUR, "
                f"Vortag: {w['previous_close']:.2f} EUR, Veränderung: {change}"
            )
        watchlist_text = "\n".join(wl_lines)
    else:
        watchlist_text = "Noch keine Watchlist eingerichtet."

    # --- Format watchlist news ---
    if personal['watchlist_news']:
        wn_lines = []
        # Build a lookup from watchlist companies
        wl_lookup = {w['id']: w for w in personal['watchlist_companies']}
        for n in personal['watchlist_news']:
            wc = wl_lookup.get(n.get('company_id'), {})
            name = wc.get('name', '?')
            sentiment = n.get('sentiment') or 'neutral'
            wn_lines.append(
                f"- [{sentiment.upper()}] {n.get('title', '?')} — {name}\n"
                f"  Quelle: {n.get('source', '?')}"
            )
        watchlist_news_text = "\n".join(wn_lines)
    else:
        watchlist_news_text = "Keine neuen News für Watchlist-Unternehmen."

    # --- Format alerts ---
    if data['alerts']:
        alerts_lines = []
        for a in data['alerts']:
            c = companies.get(a.get('company_id'), {})
            name = c.get('name', '?')
            symbol = c.get('symbol', '')
            vip = c.get('vip', '')
            alerts_lines.append(
                f"- [{a.get('priority', '?').upper()}] {a.get('title', '?')} — {name}"
                f" ({symbol}) [VIP: {vip}]\n  {a.get('message', '')}"
            )
        alerts_text = "\n".join(alerts_lines)
    else:
        alerts_text = "Keine Alerts in den letzten 24 Stunden."

    # --- Format movers ---
    if data['movers']:
        movers_lines = []
        for m in data['movers']:
            c = companies.get(m.get('company_id'), {})
            delta = float(m.get('score_value', 0))
            direction = '\u2191' if delta > 0 else '\u2193'
            movers_lines.append(f"- {c.get('name', '?')}: {direction} {delta:+.0f} Punkte (7 Tage)")
        movers_text = "\n".join(movers_lines)
    else:
        movers_text = "Keine signifikanten Score-\u00c4nderungen."

    # --- Format stale watchlist ---
    if personal['stale_companies']:
        stale_names = [f"{s['name']} ({s['symbol']})" for s in personal['stale_companies']]
        stale_text = f"{len(personal['stale_companies'])} Unternehmen: " + ", ".join(stale_names)
    else:
        stale_text = "Keine inaktiven Watchlist-Einträge."

    return f"""Du bist der persönliche Blackfire Investment Analyst. Erstelle ein Morning Briefing auf Deutsch, personalisiert für den User.

PORTFOLIO DES USERS:
{holdings_text}

WATCHLIST DES USERS:
{watchlist_text}

WATCHLIST-NEWS (letzte 24h):
{watchlist_news_text}

GLOBALE ALERTS ({len(data['alerts'])} Stück):
{alerts_text}

SCORE-BEWEGUNGEN (Top 10):
{movers_text}

INAKTIVE WATCHLIST (>90 Tage ohne News):
{stale_text}

ANWEISUNGEN:
Erstelle ein strukturiertes Morning Briefing mit maximal 600 Wörtern in sauberem HTML.

Struktur:
1. <h3>📊 Portfolio Updates</h3> — Wie haben sich die Holdings entwickelt? Signifikante Moves (>3%), P&L-Warnungen (>20% Gewinn = Take-Profit prüfen, >15% Verlust = Stop-Loss prüfen). Falls kein Portfolio: "Noch kein Portfolio angelegt."

2. <h3>👁 Watchlist Signals</h3> — Welche Watchlist-Companies haben sich bewegt? Neue News? Score-Änderungen? Klare Empfehlung pro Company (Kaufen/Beobachten/Abwarten).

3. <h3>🔍 Neue Chancen</h3> — Top 3-5 Companies NICHT auf der Watchlist aber mit hohem Score oder frischen Katalysatoren. Defcon 1 priorisieren.

4. <h3>🧹 Überprüfen</h3> — Stale Watchlist-Companies (>90 Tage ohne Aktivität). "Prüfe ob diese noch relevant sind oder entferne sie."

Pro Erwähnung: <strong>Unternehmensname</strong> (Symbol), konkretes Ereignis, und Empfehlung.
Sei direkt, konkret, actionable. Keine Floskeln. Der User will wissen: Was muss ich HEUTE tun?
Antworte NUR mit dem HTML-Inhalt, keine Erklärungen drumherum."""


def strip_html(html: str) -> str:
    """Strip HTML tags for plain text version."""
    text = re.sub(r'<[^>]+>', '', html)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


def generate_briefing(api_key: str, data: dict, personal: dict) -> dict | None:
    """Call Claude Sonnet to generate the briefing."""
    client = Anthropic(api_key=api_key)
    prompt = build_prompt(data, personal)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )

        content_html = response.content[0].text.strip()

        # Clean up markdown code blocks if present
        if content_html.startswith('```'):
            content_html = content_html.split('\n', 1)[1]
            content_html = content_html.rsplit('```', 1)[0]

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        return {
            'content_html': content_html,
            'content_text': strip_html(content_html),
            'summary_stats': {
                'alerts_count': len(data['alerts']),
                'high_priority_count': sum(1 for a in data['alerts'] if a.get('priority') == 'high'),
                'news_count': len(data['news']),
                'companies_mentioned': len(data['companies']),
                'movers_count': len(data['movers']),
                'watchlist_count': len(personal['watchlist_companies']),
                'holdings_count': len(personal['holdings_with_pnl']),
                'watchlist_news_count': len(personal['watchlist_news']),
                'stale_count': len(personal['stale_companies']),
            },
            'model_used': MODEL,
            'generated_at': datetime.now().isoformat(),
            'tokens': {'input': input_tokens, 'output': output_tokens},
        }

    except Exception as e:
        print(f"  API Error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description='Generate AI Morning Briefing')
    parser.add_argument('--apply', action='store_true', help='Save briefing to Supabase')
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  MORNING BRIEFING GENERATOR (Personalized)")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print(f"  Model: {MODEL}")
    print("=" * 70)

    # Check API key
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        print("\n  ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    # Collect data
    client = supabase_helper.get_client()

    print("\n  Collecting global data from last 24h...")
    data = collect_data(client)
    print(f"  Alerts: {len(data['alerts'])}")
    print(f"  News: {len(data['news'])}")
    print(f"  Score movers: {len(data['movers'])}")
    print(f"  Companies referenced: {len(data['companies'])}")

    print("\n  Collecting personal data (watchlist + holdings)...")
    personal = collect_personal_data(client)
    print(f"  Watchlist companies: {len(personal['watchlist_companies'])}")
    print(f"  Holdings: {len(personal['holdings_with_pnl'])}")
    print(f"  Watchlist news (24h): {len(personal['watchlist_news'])}")
    print(f"  Stale watchlist (>90d): {len(personal['stale_companies'])}")

    # P&L summary
    if personal['holdings_with_pnl']:
        total_pnl = sum(h['pnl'] for h in personal['holdings_with_pnl'])
        total_value = sum(h['market_value'] for h in personal['holdings_with_pnl'])
        total_cost = sum(h['cost_basis'] for h in personal['holdings_with_pnl'])
        total_pnl_pct = ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0
        print(f"  Portfolio P&L: {total_pnl:+.2f} EUR ({total_pnl_pct:+.1f}%)")

    # Check if there's anything to report
    total_items = (len(data['alerts']) + len(data['news']) + len(data['movers'])
                   + len(personal['watchlist_companies']) + len(personal['holdings_with_pnl']))
    if total_items == 0:
        print("\n  No data to report. Generating minimal briefing...")

    # Generate briefing
    print(f"\n  Generating personalized briefing via {MODEL}...")
    briefing = generate_briefing(api_key, data, personal)

    if not briefing:
        print("  ERROR: Briefing generation failed")
        sys.exit(1)

    print(f"  Tokens used: {briefing['tokens']['input']} input + {briefing['tokens']['output']} output")
    est_cost = (briefing['tokens']['input'] * 3 + briefing['tokens']['output'] * 15) / 1_000_000
    print(f"  Estimated cost: ${est_cost:.4f}")

    # Preview
    print(f"\n  --- BRIEFING PREVIEW ---")
    print(briefing['content_text'][:500])
    if len(briefing['content_text']) > 500:
        print(f"  ... ({len(briefing['content_text'])} chars total)")
    print(f"  --- END PREVIEW ---")

    # Apply
    if args.apply:
        print(f"\n  Saving briefing to Supabase...")
        try:
            row = {
                'content_html': briefing['content_html'],
                'content_text': briefing['content_text'],
                'summary_stats': briefing['summary_stats'],
                'model_used': briefing['model_used'],
                'generated_at': briefing['generated_at'],
            }
            client.table('briefings').insert(row).execute()
            print("  Saved successfully!")
        except Exception as e:
            print(f"  ERROR saving briefing: {e}")
            sys.exit(1)
    else:
        print(f"\n  Run with --apply to save briefing to Supabase")

    print("\n  Done!")


if __name__ == '__main__':
    main()

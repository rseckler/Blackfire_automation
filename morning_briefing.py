#!/usr/bin/env python3
"""
Morning Briefing — AI-generated daily briefing using Claude Sonnet.

Collects alerts, news, and score changes from the last 24 hours,
sends them to Claude Sonnet, and stores the generated briefing.

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


def build_prompt(data: dict) -> str:
    """Build the prompt for Claude Sonnet."""
    companies = data['companies']

    # Format alerts
    alerts_text = ""
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

    # Format news
    news_text = ""
    if data['news']:
        news_lines = []
        for n in data['news']:
            c = companies.get(n.get('company_id'), {})
            name = c.get('name', '?')
            sentiment = n.get('sentiment') or 'neutral'
            news_lines.append(
                f"- [{sentiment.upper()}] {n.get('title', '?')} — {name}\n"
                f"  Quelle: {n.get('source', '?')}"
            )
        news_text = "\n".join(news_lines)
    else:
        news_text = "Keine neuen News."

    # Format movers
    movers_text = ""
    if data['movers']:
        movers_lines = []
        for m in data['movers']:
            c = companies.get(m.get('company_id'), {})
            delta = float(m.get('score_value', 0))
            direction = '↑' if delta > 0 else '↓'
            movers_lines.append(f"- {c.get('name', '?')}: {direction} {delta:+.0f} Punkte (7 Tage)")
        movers_text = "\n".join(movers_lines)
    else:
        movers_text = "Keine signifikanten Score-Änderungen."

    return f"""Du bist der Blackfire Investment Analyst. Erstelle ein Morning Briefing auf Deutsch.

DATEN DER LETZTEN 24 STUNDEN:

## Alerts ({len(data['alerts'])} Stück):
{alerts_text}

## News ({len(data['news'])} Stück):
{news_text}

## Score-Bewegungen (Top 10):
{movers_text}

## Statistiken:
- Unternehmen erwähnt: {len(companies)}
- Alerts total: {len(data['alerts'])}
- High-Priority Alerts: {sum(1 for a in data['alerts'] if a.get('priority') == 'high')}

ANWEISUNGEN:
Erstelle ein strukturiertes Morning Briefing mit maximal 500 Wörtern in sauberem HTML.

Struktur:
1. <h3>🔴 Sofort handeln</h3> — High-Priority Alerts mit klarer Empfehlung (Kaufen/Halten/Verkaufen). Defcon 1 Unternehmen priorisieren.
2. <h3>🟡 Beobachten</h3> — Medium-Priority Alerts und Trends. Was sich entwickelt.
3. <h3>🔵 Marktüberblick</h3> — Zusammenfassung der Lage. Wie viele Unternehmen erwähnt, Score-Trends, allgemeine Einschätzung.

Pro Erwähnung: <strong>Unternehmensname</strong>, Ereignis, und eine knappe Empfehlung.
Falls keine High-Priority Alerts: Sektion 1 mit "Keine dringenden Aktionen" füllen.
Antworte NUR mit dem HTML-Inhalt, keine Erklärungen drumherum."""


def strip_html(html: str) -> str:
    """Strip HTML tags for plain text version."""
    text = re.sub(r'<[^>]+>', '', html)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


def generate_briefing(api_key: str, data: dict) -> dict | None:
    """Call Claude Sonnet to generate the briefing."""
    client = Anthropic(api_key=api_key)
    prompt = build_prompt(data)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
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
    print("  MORNING BRIEFING GENERATOR")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN (preview only)'}")
    print(f"  Model: {MODEL}")
    print("=" * 70)

    # Check API key
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        print("\n  ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    # Collect data
    print("\n  Collecting data from last 24h...")
    client = supabase_helper.get_client()
    data = collect_data(client)

    print(f"  Alerts: {len(data['alerts'])}")
    print(f"  News: {len(data['news'])}")
    print(f"  Score movers: {len(data['movers'])}")
    print(f"  Companies referenced: {len(data['companies'])}")

    # Check if there's anything to report
    total_items = len(data['alerts']) + len(data['news']) + len(data['movers'])
    if total_items == 0:
        print("\n  No data to report. Generating minimal briefing...")

    # Generate briefing
    print(f"\n  Generating briefing via {MODEL}...")
    briefing = generate_briefing(api_key, data)

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

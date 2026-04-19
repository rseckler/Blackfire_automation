#!/usr/bin/env python3
"""
SEC Form 144 Monitor — Session 1 (2026-04-19).

Pollt SEC EDGAR stündlich (09-20 UTC) nach neuen Form-144-Filings. Form 144
ist die Pflicht-Meldung vor Insider-Aktienverkäufen (> $50k oder 5000 Aktien).
Post-Lockup-Ausverkauf wird hier sichtbar — oft *bevor* der Kurs reagiert.

Workflow:
  1. EDGAR RSS Feed: https://www.sec.gov/cgi-bin/browse-edgar?type=144&action=getcurrent
     → neueste Form-144-Filings letzten 24h
  2. Jedes Filing: CIK → match auf companies (über stored CIK oder Symbol-Lookup)
  3. Wenn Watchlist-Relevanz (Firma in basket, watchlist oder user_entry_prices):
     → insert in form_144_filings
     → Alert vom Typ 'form_144_filed' erzeugen
  4. Dedup per accession_number UNIQUE

Usage:
  python3 form_144_monitor.py              # dry-run letzte 24h
  python3 form_144_monitor.py --apply      # write + create alerts
  python3 form_144_monitor.py --days 7     # 7 Tage Backlog
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import requests
import supabase_helper

SEC_USER_AGENT = os.getenv('SEC_USER_AGENT', 'Blackfire Research (rseckler@gmail.com)')

# SEC EDGAR Full-Text Search für Form 144
# Alternative: RSS Feed
SEC_FORM144_RSS = (
    'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=144&output=atom&count=100'
)
SEC_SUBMISSION_INDEX = 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=144&dateb={dateb}&datea={datea}&output=atom&count=100'


def sec_headers():
    return {'User-Agent': SEC_USER_AGENT, 'Accept': 'application/atom+xml,text/xml'}


def fetch_form144_feed(datea: date, dateb: date) -> list:
    """Hol ATOM feed von Form-144-Filings im Zeitraum."""
    url = SEC_SUBMISSION_INDEX.format(
        datea=datea.strftime('%Y%m%d'),
        dateb=dateb.strftime('%Y%m%d'),
    )
    r = requests.get(url, headers=sec_headers(), timeout=60)
    if r.status_code != 200:
        print(f"    Feed fetch error {r.status_code}")
        return []
    return parse_atom(r.text)


def fetch_recent_form144() -> list:
    """Hol neueste Form-144-Filings (letzte ~24h, aber kein exakter Datums-Filter)."""
    r = requests.get(SEC_FORM144_RSS, headers=sec_headers(), timeout=60)
    if r.status_code != 200:
        print(f"    Feed fetch error {r.status_code}")
        return []
    return parse_atom(r.text)


def parse_atom(xml_text: str) -> list:
    """Minimaler Atom-Parser für SEC-Feeds (ohne lxml-Dependency)."""
    entries = []
    # Entry-Blöcke
    for entry_match in re.finditer(r'<entry>(.*?)</entry>', xml_text, re.DOTALL):
        body = entry_match.group(1)
        title = _extract(body, 'title')
        link_match = re.search(r'<link[^>]+href="([^"]+)"', body)
        link = link_match.group(1) if link_match else None
        updated = _extract(body, 'updated')
        # SEC gibt typically: <title>144 - CompanyName (CIK) (Filer)</title>
        # oder: <title>144 - Filer Name (CIK)</title>
        m = re.match(r'144\s*[-–]\s*(.+?)\s*\((\d{10})\)', (title or '').strip())
        company_name = None
        cik = None
        if m:
            company_name = m.group(1).strip()
            cik = str(int(m.group(2)))  # Remove leading zeros
        # Accession from link: /cgi-bin/browse-edgar?action=getcompany&CIK=...&filenum=...&type=144
        # Or: /Archives/edgar/data/<cik>/<accession_nodash>/<accession>-index.htm
        accession = None
        if link:
            am = re.search(r'/(\d{18})-index', link)
            if am:
                raw = am.group(1)
                accession = f'{raw[:10]}-{raw[10:12]}-{raw[12:]}'
            else:
                am2 = re.search(r'accession[_-]?number=([\d-]+)', link, re.IGNORECASE)
                if am2:
                    accession = am2.group(1)
        entries.append({
            'title': title,
            'company_name': company_name,
            'cik': cik,
            'link': link,
            'accession': accession,
            'updated': updated,
        })
    return entries


def _extract(xml: str, tag: str) -> str | None:
    m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', xml, re.DOTALL)
    return m.group(1).strip() if m else None


def fetch_form144_details(cik: str, accession: str) -> dict | None:
    """Lade Form-144-XML für Detail-Parsing (shares_to_sell, relationship, etc.)."""
    if not accession:
        return None
    nodash = accession.replace('-', '')
    # Index-Datei geht nicht direkt, aber Primary Doc suchen
    idx_url = f'https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/'
    # Für jetzt: nur High-Level-Metadaten speichern, Detail-Parsing kommt später
    return {'accession': accession, 'cik': cik, 'url': idx_url}


def match_company(client, cik: str, company_name: str | None) -> dict | None:
    """Versuche Firma in DB zu finden via CIK oder Fuzzy-Name."""
    # CIK first (wenn wir sie gespeichert haben) — aktuell nicht in companies, skippen
    # → Name-Fuzzy
    if not company_name:
        return None
    # Normalize
    short = re.sub(r'\b(inc|corp|ltd|llc|plc|sa|ag|nv|se|gmbh|co)\b\.?', '', company_name, flags=re.IGNORECASE).strip().strip(',.')
    short = short[:25]  # Top 25 chars for ILIKE
    if not short:
        return None
    try:
        resp = client.table('companies').select('id, name, symbol').ilike('name', f'%{short}%').limit(5).execute()
        rows = resp.data or []
        # Exact name match prefer
        for r in rows:
            if r['name'].upper() == company_name.upper():
                return r
        if rows:
            return rows[0]
    except Exception:
        pass
    return None


def is_watchlist_relevant(client, company_id: str) -> bool:
    """Ist diese Firma relevant für einen User? (Basket, Watchlist, Entry-Preis)."""
    try:
        # Basket?
        resp = client.table('tree_basket_members').select('id').eq('company_id', company_id).limit(1).execute()
        if resp.data:
            return True
        # Watchlist?
        resp = client.table('watchlist').select('id').eq('company_id', company_id).limit(1).execute()
        if resp.data:
            return True
        # Entry-Preis?
        resp = client.table('user_entry_prices').select('id').eq('company_id', company_id).limit(1).execute()
        if resp.data:
            return True
    except Exception:
        pass
    return False


def get_watchlist_users(client, company_id: str) -> list[str]:
    """Liste aller user_ids die diese Firma im Blick haben."""
    users = set()
    try:
        r1 = client.table('tree_basket_members').select('tree_basket_categories!inner(tree_baskets!inner(user_id))').eq('company_id', company_id).execute()
        for row in (r1.data or []):
            uid = row.get('tree_basket_categories', {}).get('tree_baskets', {}).get('user_id')
            if uid: users.add(uid)
    except Exception:
        pass
    try:
        r2 = client.table('watchlist').select('user_id').eq('company_id', company_id).execute()
        for row in (r2.data or []):
            if row.get('user_id'): users.add(row['user_id'])
    except Exception:
        pass
    try:
        r3 = client.table('user_entry_prices').select('user_id').eq('company_id', company_id).execute()
        for row in (r3.data or []):
            if row.get('user_id'): users.add(row['user_id'])
    except Exception:
        pass
    return list(users)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    print("\n" + "=" * 72)
    print("  SEC FORM 144 MONITOR")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 72)

    client = supabase_helper.get_client()

    if args.days == 1:
        print("\n  Fetching RSS feed (recent ~24h)...")
        entries = fetch_recent_form144()
    else:
        print(f"\n  Fetching {args.days}-day backlog...")
        entries = fetch_form144_feed(date.today() - timedelta(days=args.days), date.today())

    print(f"  → {len(entries)} raw entries")

    stats = Counter()
    to_insert = []
    to_alert = []

    for e in entries:
        if not e['cik']:
            stats['no_cik'] += 1
            continue

        company = match_company(client, e['cik'], e['company_name'])
        if not company:
            stats['company_not_in_db'] += 1
            continue

        relevant = is_watchlist_relevant(client, company['id'])
        stats['matched_to_company'] += 1
        if relevant:
            stats['watchlist_relevant'] += 1

        # Dedup check
        if e['accession']:
            try:
                dup = client.table('form_144_filings').select('id').eq('accession_number', e['accession']).limit(1).execute()
                if dup.data:
                    stats['already_in_db'] += 1
                    continue
            except Exception:
                pass

        row = {
            'company_id': company['id'],
            'filer_name': None,  # aus detailed XML später
            'filer_relationship': None,
            'shares_to_sell': None,
            'estimated_value_usd': None,
            'broker': None,
            'sale_date': None,
            'filing_date': (e['updated'] or '')[:10] or date.today().isoformat(),
            'filing_url': e['link'],
            'cik': e['cik'],
            'accession_number': e['accession'] or f"unknown-{company['id']}-{e['updated'][:10]}",
            'raw_metadata': {'title': e['title'], 'feed_updated': e['updated']},
        }
        to_insert.append(row)

        if relevant:
            to_alert.append({
                'company_id': company['id'],
                'company_name': company['name'],
                'symbol': company.get('symbol'),
                'filing_url': e['link'],
                'users': get_watchlist_users(client, company['id']),
            })

        if args.verbose:
            flag = '⚠ WATCHLIST' if relevant else ''
            print(f"    {company['name'][:40]:40s} ({company.get('symbol', '—')})  {flag}")

    print(f"\n  Stats:")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    {k:30s}: {v:5d}")

    print(f"\n  → {len(to_insert)} filings to insert")
    print(f"  → {len(to_alert)} watchlist-relevant alerts")

    if args.apply and to_insert:
        print(f"\n  Writing {len(to_insert)} filings...")
        success = 0
        for row in to_insert:
            try:
                client.table('form_144_filings').insert(row).execute()
                success += 1
            except Exception as e:
                if 'duplicate key' in str(e).lower() or '23505' in str(e):
                    stats['dup_skipped'] += 1
                    continue
                print(f"    Error: {e}")
        print(f"  → Inserted: {success}")

        # Create alerts
        print(f"\n  Writing {len(to_alert)} alerts...")
        alert_success = 0
        for al in to_alert:
            for uid in al['users']:
                try:
                    client.table('alerts').insert({
                        'user_id': uid,
                        'company_id': al['company_id'],
                        'alert_type': 'form_144_filed',
                        'priority': 'high',
                        'title': f'Insider-Sale gemeldet: {al["company_name"]}',
                        'message': f'Form 144 Filing für {al["symbol"] or al["company_name"]} auf SEC. Ein Insider plant einen Aktienverkauf nach Lock-up-Ende.',
                        'condition': {'filing_url': al['filing_url']},
                        'is_read': False,
                    }).execute()
                    alert_success += 1
                except Exception as e:
                    print(f"    Alert error: {e}")
        print(f"  → Alerts: {alert_success}")

    elif to_insert:
        print(f"\n  Run with --apply to write.")

    print("\n  Done!\n")


if __name__ == '__main__':
    main()

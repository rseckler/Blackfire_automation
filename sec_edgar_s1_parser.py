#!/usr/bin/env python3
"""
SEC EDGAR S-1 Parser — Session 1 (2026-04-19, Tommi-Freigabe).

Zieht Lock-up-Agreement-Details aus SEC-S-1/S-1A/424B-Prospekten. Primärquelle
für US-IPO-Lockups. Free, keine Rate-Limits (SEC Fair Access, User-Agent Pflicht).

Workflow pro Firma:
  1. Firmen-Symbol → CIK lookup (SEC Company Tickers JSON)
  2. CIK → Filings List (S-1, S-1/A, 424B1/B4, 10-K für bestätigte IPO-Dates)
  3. Neustes relevantes Filing → Full-Text-Index oder Full Submission
  4. Text um "lock-up" / "Lock-Up Agreement" / "lockup period" durchsuchen
  5. 2–3 Absätze um den Match als Claude-Haiku-Input packen
  6. Haiku extrahiert JSON: {days, share_count, tranches[], ipo_effective_date, …}
  7. Upsert in company_events mit source='sec_edgar_s1', confidence='verified'

Rate-Limits: SEC erlaubt 10 Requests/Sekunde pro User-Agent, aber fair-use
behandlung ist Pflicht. User-Agent muss Identifizierung enthalten
(z.B. "Blackfire-Research rseckler@gmail.com").

Usage:
  python3 sec_edgar_s1_parser.py --symbols AAPL,NVDA         # Liste
  python3 sec_edgar_s1_parser.py --test-set                   # 25-Firmen Test
  python3 sec_edgar_s1_parser.py --symbols OKLO --apply       # write to DB
  python3 sec_edgar_s1_parser.py --all-public --apply         # Mass run
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

import requests
import supabase_helper

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: anthropic package not installed. Run: pip install anthropic")
    sys.exit(1)

SEC_USER_AGENT = os.getenv('SEC_USER_AGENT', 'Blackfire Research (rseckler@gmail.com)')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# SEC endpoints
SEC_TICKERS_URL = 'https://www.sec.gov/files/company_tickers.json'
SEC_SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik:010d}.json'
SEC_FULL_TEXT_SEARCH = 'https://efts.sec.gov/LATEST/search-index?q={q}&ciks={cik:010d}&forms={forms}'
SEC_ARCHIVE_URL = 'https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{doc}'

# Filing types to inspect. Priority-Order: S-1-Varianten enthalten Lockup
# sehr zuverlässig, 424B-Varianten der finale Prospekt. 10-K/424B3 bewusst
# ausgeschlossen — die enthalten typischerweise keinen Lockup-Abschnitt
# und sorgen nur für falsche-Positiv-Haiku-Calls.
LOCKUP_FILING_TYPES = ['S-1/A', 'S-1', '424B4', '424B1']

# Test-Set aus PLAN-LOCKUP-SYSTEM-v1.md Abschnitt 8
TEST_SET_SYMBOLS = [
    # Recent IPOs / SPACs
    'OKLO', 'LUNR', 'BKSY', 'SMR', 'AMPX', 'NVTS', 'FLNC', 'EOSE', 'XNDU',
    'EXOD', 'CRDO', 'CLSK', 'HIVE', 'MARA', 'WOLF',
    # Etablierte
    'NVDA', 'IBM', 'TXN', 'AMAT', 'MRVL',
    # Edge-Cases (Altlasten / Nicht-US / Dublette-gemergt)
    'BNTX', 'MSTR', 'LUMN', 'ENR', 'CEVA',
]


def sec_headers() -> dict:
    return {'User-Agent': SEC_USER_AGENT, 'Accept': 'application/json'}


def load_ticker_to_cik() -> dict:
    """Lädt die SEC Ticker → CIK Mapping-Datei (ca. 1 MB)."""
    r = requests.get(SEC_TICKERS_URL, headers=sec_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    # Format: { "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ... }
    mapping = {}
    for entry in data.values():
        mapping[entry['ticker'].upper()] = {
            'cik': int(entry['cik_str']),
            'title': entry['title'],
        }
    return mapping


def fetch_submissions(cik: int) -> dict | None:
    """Fetch Firmen-Submissions-Index (alle Filings)."""
    url = SEC_SUBMISSIONS_URL.format(cik=cik)
    try:
        r = requests.get(url, headers=sec_headers(), timeout=30)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def find_recent_lockup_filings(submissions: dict) -> list:
    """Durchsuche submissions.json nach S-1/424B-Filings (inkl. older files-Archive)."""
    out = []
    filings = submissions.get('filings', {}) if submissions else {}

    # 1. Recent (letzte ~1000 Filings)
    recent = filings.get('recent', {})
    forms = recent.get('form', [])
    dates = recent.get('filingDate', [])
    accession_numbers = recent.get('accessionNumber', [])
    primary_docs = recent.get('primaryDocument', [])
    for i, form in enumerate(forms):
        if form in LOCKUP_FILING_TYPES:
            out.append({
                'form': form,
                'filing_date': dates[i],
                'accession_number': accession_numbers[i],
                'primary_document': primary_docs[i],
            })

    # 2. Ältere Filings aus files[] (für Firmen die schon 10+ Jahre gelistet sind)
    if not out:
        for archive in filings.get('files', []):
            url = f"https://data.sec.gov/submissions/{archive.get('name')}"
            try:
                r = requests.get(url, headers=sec_headers(), timeout=30)
                if r.status_code != 200:
                    continue
                arch_data = r.json()
                af = arch_data.get('form', [])
                ad = arch_data.get('filingDate', [])
                aa = arch_data.get('accessionNumber', [])
                ap = arch_data.get('primaryDocument', [])
                for i, form in enumerate(af):
                    if form in LOCKUP_FILING_TYPES:
                        out.append({
                            'form': form,
                            'filing_date': ad[i],
                            'accession_number': aa[i],
                            'primary_document': ap[i],
                        })
                time.sleep(0.15)  # rate-limit
                if out:
                    break  # one archive with hits reicht
            except Exception:
                continue

    return out


def fetch_filing_text(cik: int, filing: dict) -> str | None:
    """Lade Primary Document eines Filings als Text."""
    accession_nodash = filing['accession_number'].replace('-', '')
    doc = filing['primary_document']
    url = SEC_ARCHIVE_URL.format(cik=cik, accession_nodash=accession_nodash, doc=doc)
    headers = {**sec_headers(), 'Accept': '*/*'}
    try:
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code != 200:
            return None
        text = r.text
        # HTML-Tags stripen wenn HTML (S-1s sind meistens HTML)
        if '<html' in text.lower() or '<body' in text.lower():
            text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'&nbsp;', ' ', text)
            text = re.sub(r'&amp;', '&', text)
            text = re.sub(r'&#\d+;', ' ', text)
            text = re.sub(r'\s+', ' ', text)
        return text
    except Exception as e:
        print(f"    Error fetching filing: {e}")
        return None


# Regex für Lockup-Abschnitte (erweitert für bessere Coverage)
LOCKUP_PATTERNS = [
    re.compile(r'lock[-\s]?up\s+agreement', re.IGNORECASE),
    re.compile(r'lock[-\s]?up\s+period', re.IGNORECASE),
    re.compile(r'\b\d{2,3}[-\s]?day\s+lock[-\s]?up', re.IGNORECASE),
    re.compile(r'lock[-\s]?up\s+restrictions?', re.IGNORECASE),
    re.compile(r'lock[-\s]?up\s+provisions?', re.IGNORECASE),
    re.compile(r'lock[-\s]?up\s+expir', re.IGNORECASE),
    re.compile(r'subject\s+to\s+(a\s+)?lock[-\s]?up', re.IGNORECASE),
    re.compile(r'without\s+the\s+prior\s+written\s+consent.*lock[-\s]?up', re.IGNORECASE),
]


def extract_lockup_blocks(text: str, max_blocks: int = 3, chars_context: int = 2500) -> list[str]:
    """Extrahiere bis max_blocks Text-Blöcke um Lockup-Treffer herum."""
    matches = []
    seen_positions = set()
    for pat in LOCKUP_PATTERNS:
        for m in pat.finditer(text):
            start = max(0, m.start() - chars_context // 2)
            end = min(len(text), m.end() + chars_context // 2)
            # Overlap-Detection
            if any(abs(start - sp) < chars_context for sp in seen_positions):
                continue
            seen_positions.add(start)
            matches.append(text[start:end])
            if len(matches) >= max_blocks:
                return matches
    return matches


def haiku_extract(blocks: list[str], company_name: str, filing_meta: dict) -> dict | None:
    """Claude Haiku extrahiert strukturierte Lockup-Details aus Text-Blöcken."""
    if not ANTHROPIC_API_KEY:
        print("    WARN: ANTHROPIC_API_KEY not set — skipping Haiku extraction")
        return None

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    joined_blocks = "\n\n---\n\n".join(blocks)

    prompt = f"""Du bist ein Finanz-Prospekt-Analyst. Dein Auftrag ist präzise Extraktion des PRIMÄR-Lockups aus dem S-1/424B-Prospekt.

FIRMA: "{company_name}"
FILING: {filing_meta.get('form')} vom {filing_meta.get('filing_date')}

WICHTIG — Unterscheide diese Lockup-Typen:

1. PRIMÄR-Lockup (der interessante):
   - Gilt für "directors, executive officers and holders of substantially all of our outstanding common stock"
   - Typische Dauer: 90, 180 oder 365 Tage
   - Betrifft in der Regel 60-95% aller ausstehenden Aktien
   - DAS ist was wir wollen.

2. Sponsor-Lockup (bei SPACs, NICHT der primäre):
   - Nur für den SPAC-Sponsor / Founders-Aktien
   - Oft 3-7 Jahre (1095-2555 Tage)
   - Betrifft nur Founder-Shares, kleiner Anteil
   - Falls nur Sponsor-Lockup vorhanden: found=false eintragen (nicht verwechseln!)

3. Earn-out / Performance-Lockups:
   - Aktien die erst bei Kurs-Triggern (+30%) freiwerden
   - Nicht der Primär-Lockup, ignorieren

REGELN:
- Wenn mehrere Lockup-Typen erwähnt: extrahiere NUR den Primär-Lockup
- Wenn Sponsor-Lockup mit Jahreszahl (z.B. "3-year lockup") als EINZIGER Lockup: return found=false
- shares_total muss in Stück stehen (z.B. 50000000 für 50 Millionen)
- Fordere shares_total nur ein wenn wörtlich im Text genannt (z.B. "aggregate of X shares")
- lockup_days als Zahl, keine Beschreibung

TEXTBLÖCKE:
{joined_blocks[:15000]}

Antworte AUSSCHLIESSLICH mit diesem JSON:
{{
  "found": true/false,
  "is_primary_lockup": true/false (false wenn nur Sponsor- oder Performance-Lockup gefunden),
  "lockup_type": "insider" | "sponsor" | "performance" | "mixed",
  "lockup_days": Zahl oder null,
  "shares_total": Zahl oder null,
  "shares_citation": "wörtliches Zitat mit Zahl aus Prospekt" oder null,
  "effective_date": "YYYY-MM-DD" des Prospekts oder null,
  "tranches": [{{"days": 180, "shares": 50000000, "note": "Beschreibung"}}] oder [],
  "insider_filer_relationships": ["directors", "officers", "10% holders"] oder [],
  "release_events": ["quarterly earnings", ...] oder [],
  "confidence": {{"days": "high"|"medium"|"low", "shares": "high"|"medium"|"low"}},
  "summary": "Ein Satz max 150 Zeichen"
}}

Wenn kein Primär-Lockup gefunden: {{"found": false, ...}}."""

    try:
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = resp.content[0].text.strip()
        # JSON aus Antwort extrahieren (manchmal ```json ``` wrap)
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as e:
        print(f"    Haiku error: {e}")
        return None


def process_symbol(
    symbol: str,
    ticker_map: dict,
    db_client,
    apply_changes: bool,
    verbose: bool,
) -> dict:
    """Volle Pipeline für ein Symbol. Returns Status-Dict."""
    status = {'symbol': symbol, 'stage': 'start', 'result': None, 'error': None}

    # 1) Symbol → CIK
    entry = ticker_map.get(symbol.upper())
    if not entry:
        status['stage'] = 'ticker_lookup'
        status['error'] = 'Nicht in SEC Ticker-Liste (wahrscheinlich kein US-Listing)'
        return status
    cik = entry['cik']
    company_name = entry['title']
    if verbose:
        print(f"    {symbol} → CIK {cik} ({company_name})")

    # 2) Submissions holen
    submissions = fetch_submissions(cik)
    if not submissions:
        status['stage'] = 'submissions_fetch'
        status['error'] = 'SEC submissions.json nicht abrufbar'
        return status

    time.sleep(0.15)  # fair-use rate limit

    # 3) Relevante Filings finden
    filings = find_recent_lockup_filings(submissions)
    if not filings:
        status['stage'] = 'filings_search'
        status['error'] = f'Keine {", ".join(LOCKUP_FILING_TYPES)} Filings gefunden'
        return status

    # Prio: S-1/A > S-1 > 424B4 ...
    filings.sort(key=lambda f: LOCKUP_FILING_TYPES.index(f['form']))

    # Track wenn wir sponsor-only-Lockups gesehen haben (für besseren Fehlermode)
    seen_sponsor_only = False

    # 4) Bis zu 2 Filings durchgehen, beim ersten Treffer stoppen
    for filing in filings[:2]:
        if verbose:
            print(f"    Trying {filing['form']} from {filing['filing_date']}")
        text = fetch_filing_text(cik, filing)
        time.sleep(0.15)
        if not text:
            continue

        # 5) Lockup-Blöcke extrahieren
        blocks = extract_lockup_blocks(text)
        if not blocks:
            if verbose:
                print(f"    No lockup-match blocks in {filing['form']}")
            continue

        # 6) Haiku-Extraktion
        if verbose:
            print(f"    → {len(blocks)} blocks found, calling Haiku...")
        extracted = haiku_extract(blocks, company_name, filing)
        if not extracted:
            continue
        if not extracted.get('found'):
            if verbose:
                print(f"    Haiku: no lockup found in blocks")
            continue

        # Reject: NUR Sponsor-/Performance-Lockup gefunden (nicht primary)
        if extracted.get('is_primary_lockup') is False:
            if verbose:
                print(f"    Haiku: only {extracted.get('lockup_type')}-Lockup found, skipping")
            seen_sponsor_only = True
            continue

        # Erfolg!
        status['stage'] = 'extracted'
        status['result'] = {
            'cik': cik,
            'company_name': company_name,
            'filing_form': filing['form'],
            'filing_date': filing['filing_date'],
            'accession': filing['accession_number'],
            'extracted': extracted,
        }

        # 7) In DB schreiben
        if apply_changes:
            save_to_db(db_client, symbol, status['result'])

        return status

    if seen_sponsor_only:
        status['stage'] = 'only_sponsor_lockup'
        status['error'] = 'Nur Sponsor-/Performance-Lockup — kein Primary-Insider-Lockup gefunden'
    else:
        status['stage'] = 'no_lockup_found'
        status['error'] = f'Checked {min(2, len(filings))} filings, no lockup extracted'
    return status


def save_to_db(db_client, symbol: str, result: dict):
    """Upsert lockup_expiry-Event in company_events."""
    # Finde Firma per Symbol
    resp = db_client.table('companies').select('id, name').eq('symbol', symbol).execute()
    rows = resp.data or []
    if not rows:
        print(f"    WARN: Company for symbol={symbol} not in DB — skipping save")
        return
    company_id = rows[0]['id']

    ex = result['extracted']
    # Berechne event_date: effective_date + lockup_days, fallback: filing_date + days
    try:
        from datetime import date, timedelta
        base_date_str = ex.get('effective_date') or result.get('filing_date')
        base_date = datetime.strptime(base_date_str, '%Y-%m-%d').date()
        days = ex.get('lockup_days') or 180
        event_date = base_date + timedelta(days=days)
    except Exception as e:
        print(f"    WARN: Could not compute event_date: {e}")
        return

    # Confidence-Struktur v2: per-Feld (days/shares)
    conf = ex.get('confidence') or {}
    if isinstance(conf, str):  # Alt-Format Kompat
        conf = {'days': conf, 'shares': conf}
    days_conf = conf.get('days') or 'low'

    metadata = {
        'source': 'sec_edgar_s1',
        'confidence': 'verified' if days_conf in ('high', 'medium') else 'estimated',
        'confidence_detail': conf,
        'lockup_type': ex.get('lockup_type') or 'insider',
        'lockup_days': ex.get('lockup_days'),
        'share_count': ex.get('shares_total'),
        'share_count_source': 'sec_edgar_s1',
        'shares_citation': ex.get('shares_citation'),
        'insider_relationships': ex.get('insider_filer_relationships') or [],
        'release_events': ex.get('release_events') or [],
        'ipo_date': ex.get('effective_date'),
        'filing_form': result['filing_form'],
        'filing_accession': result['accession'],
        'tranche': 1,
        'tranche_total': max(1, len(ex.get('tranches') or [])),
        'notes': ex.get('summary'),
    }

    # Wenn mehrere Tranchen: zusätzliche Events anlegen, jeweils mit tranche=N
    tranches = ex.get('tranches') or []
    if tranches:
        # Haupt-Event für Tranche 1
        for idx, tranche in enumerate(tranches, start=1):
            try:
                t_days = tranche.get('days', ex.get('lockup_days', 180))
                t_date = base_date + timedelta(days=t_days)
                t_meta = {**metadata, 'tranche': idx, 'tranche_total': len(tranches),
                          'share_count': tranche.get('shares') or ex.get('shares_total'),
                          'notes': tranche.get('note') or ex.get('summary')}
                db_client.table('company_events').upsert(
                    {
                        'company_id': company_id,
                        'event_type': 'lockup_expiry',
                        'event_date': t_date.isoformat(),
                        'description': f'Lock-up expiry (SEC EDGAR S-1, Tranche {idx}/{len(tranches)})',
                        'event_metadata': t_meta,
                    }
                ).execute()
            except Exception as e:
                print(f"    Error upsert tranche {idx}: {e}")
    else:
        try:
            db_client.table('company_events').upsert(
                {
                    'company_id': company_id,
                    'event_type': 'lockup_expiry',
                    'event_date': event_date.isoformat(),
                    'description': f'Lock-up expiry (SEC EDGAR {result["filing_form"]})',
                    'event_metadata': metadata,
                }
            ).execute()
        except Exception as e:
            print(f"    Error upsert event: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbols', help='Kommagetrennte Symbole')
    parser.add_argument('--test-set', action='store_true', help='Nutze Test-Set aus PLAN')
    parser.add_argument('--all-public', action='store_true', help='Alle US public Companies')
    parser.add_argument('--apply', action='store_true', help='Write to Supabase')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    print("\n" + "=" * 72)
    print("  SEC EDGAR S-1 PARSER")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 72)

    db_client = supabase_helper.get_client()

    # Symbol-Liste aufbauen
    if args.test_set:
        symbols = TEST_SET_SYMBOLS
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    elif args.all_public:
        resp = db_client.table('companies') \
            .select('symbol') \
            .eq('listing_status', 'public') \
            .not_.is_('symbol', 'null') \
            .execute()
        symbols = [r['symbol'].upper() for r in (resp.data or []) if r['symbol'] and '.' not in r['symbol']]
        print(f"  Loaded {len(symbols)} public symbols from DB")
    else:
        print("  ERROR: --symbols, --test-set, or --all-public required")
        sys.exit(1)

    print(f"\n  Loading SEC ticker→CIK mapping...")
    ticker_map = load_ticker_to_cik()
    print(f"  → {len(ticker_map)} tickers")

    stats = Counter()
    results = []
    for i, sym in enumerate(symbols, 1):
        print(f"\n[{i}/{len(symbols)}] {sym}")
        try:
            status = process_symbol(sym, ticker_map, db_client, args.apply, args.verbose)
        except Exception as e:
            status = {'symbol': sym, 'stage': 'exception', 'error': str(e), 'result': None}

        if status['result']:
            ex = status['result']['extracted']
            days = ex.get('lockup_days')
            shares = ex.get('shares_total')
            days_str = f"{days}d" if days else "—"
            shares_str = f"{shares:,}" if shares else "—"
            conf = ex.get('confidence')
            if isinstance(conf, dict):
                conf_str = f"days={conf.get('days', '?')}/shares={conf.get('shares', '?')}"
            else:
                conf_str = str(conf)
            lt = ex.get('lockup_type', '?')
            print(f"    ✓ {lt}-Lockup: {days_str}, {shares_str} shares  [{conf_str}]")
            stats['success'] += 1
        else:
            print(f"    ✗ {status['stage']}: {status['error']}")
            stats[status['stage']] += 1
        results.append(status)
        time.sleep(0.15)  # SEC rate limit

    # Report
    print("\n" + "=" * 72)
    print("  ZUSAMMENFASSUNG")
    print("=" * 72)
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {k:35s}: {v:4d}")

    # Speichere JSON-Report
    report_path = os.path.join(SCRIPT_DIR, 'logs', f'sec_s1_report_{datetime.now():%Y%m%d_%H%M%S}.json')
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w') as f:
        # reduziere extracted blocks für Report
        clean = []
        for r in results:
            clean.append({
                'symbol': r['symbol'],
                'stage': r['stage'],
                'error': r['error'],
                'extracted': r['result']['extracted'] if r.get('result') else None,
                'filing': (
                    {k: r['result'][k] for k in ('filing_form', 'filing_date', 'accession')}
                    if r.get('result') else None
                ),
            })
        json.dump(clean, f, indent=2, ensure_ascii=False)
    print(f"\n  Report: {report_path}")
    print()


if __name__ == '__main__':
    main()

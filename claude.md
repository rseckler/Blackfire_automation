# Blackfire Automation - Systemdokumentation

**Status:** Production Ready - Deployed auf Hostinger VPS
**Datum:** 15. März 2026
**Version:** 3.4 (SPAC & Lock-up Tracking)

---

## Übersicht

8 automatisierte Workflows laufen 24/7 auf Hostinger VPS:

### 1. Passive Income Generator (06:00 MEZ)
- **Repo:** https://github.com/rseckler/Passive-Income-Generator
- **Path:** `/root/Passive-Income-Generator`
- **Funktion:** Generiert täglich 10 Passive Income Ideen via GPT-4o
- **Output:** Notion Database + E-Mail
- **Status:** Aktiv

### 2. Blackfire Morning Sync (07:00 MEZ)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** Excel -> Supabase Sync (~977 Aktien) + ISIN/WKN Recherche
- **Status:** Aktiv

### 3. Blackfire Stock Updates (08:00-00:00 MEZ, stündlich)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** Live Aktienkurse via yfinance -> Supabase (~300-400 Updates/h)
- **Status:** Aktiv

### 4. News Collector (alle 2 Stunden)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** RSS-Feeds (5 Quellen) + Brave Search → company_news Tabelle
- **Output:** 39+ Artikel/Run, Duplikat-Erkennung via URL
- **Status:** Aktiv

### 5. IPO Tracker (täglich 07:00 UTC)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** Finnhub (90 IPOs) + Nasdaq + Brave Search → company_events (event_type: 'ipo') + auto Lock-up Events (IPO+180d)
- **Matching:** Fuzzy-Matching gegen 518 Private/Pre-IPO Companies (70% Threshold)
- **Status:** Aktiv

### 6. Earnings Calendar (wöchentlich Sonntag 06:00 UTC)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** yfinance Earnings-Termine → company_events (event_type: 'earnings')
- **Scope:** ~1193 Public Companies mit gültigem Symbol → 399 Termine
- **Status:** Aktiv

### 7. SPAC Tracker (täglich 07:30 UTC)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** SEC EDGAR EFTS API (SIC 6770) + Brave Search → company_events (event_type: spac_announced/vote/closing/deadline)
- **Daten:** SPAC Sponsor, Trust Size, Pre-Merger Ticker, Exchange in event_metadata JSONB
- **Matching:** Fuzzy-Matching gegen alle Companies (70% Threshold), Updates listing_status → 'spac'
- **Status:** Aktiv (2026-03-15)

### 8. Lock-up Scraper (wöchentlich Montag 07:00 UTC)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** MarketBeat Lock-up Calendar Scraping + Auto-Berechnung (IPO+180d) → company_events (event_type: lockup_expiry)
- **Daten:** lockup_days, lockup_shares, lockup_percent_of_float, confidence (confirmed/estimated) in event_metadata JSONB
- **Status:** Aktiv (2026-03-15)

---

## Datenbank: Supabase (PostgreSQL)

**URL:** https://lglvuiuwbrhiqvxcriwa.supabase.co
**Dashboard:** https://supabase.com/dashboard/project/lglvuiuwbrhiqvxcriwa

### Tabellen

| Tabelle | Zweck |
|---------|-------|
| `companies` | Haupttabelle (~1732 Aktien), Core-Felder + `extra_data` JSONB |
| `sync_history` | Automatisches Logging aller Sync/Update-Runs |
| `stock_prices` | Historische Kursdaten (von Blackfire_service) |
| `company_news` | Automatisch gesammelte News (news_collector.py) |
| `company_events` | IPO-Termine + Earnings-Termine (ipo_tracker.py, earnings_calendar.py) |
| `company_scores` | Blackfire Score Historie (Phase 3, noch nicht befüllt) |
| `alerts` | Automatische Alerts (Phase 3, noch nicht befüllt) |

### Companies Schema (relevante Felder)

```
id UUID (PK)
name TEXT
symbol TEXT (UNIQUE)
satellog TEXT (UNIQUE) -- Excel-Identifier
wkn TEXT
isin TEXT
current_price DECIMAL(20,4)
price_change_percent DECIMAL(10,4)
price_update TIMESTAMPTZ
market_status TEXT
day_high DECIMAL(20,4)
day_low DECIMAL(20,4)
volume BIGINT
market_cap BIGINT
exchange TEXT
currency TEXT
extra_data JSONB -- alle weiteren Excel-Spalten
last_synced_at TIMESTAMPTZ
```

### Shared Module: `supabase_helper.py`

Alle Scripts nutzen dieses Modul für Supabase-Zugriff:
- `get_client()` - Singleton Supabase Client
- `get_all_companies(select)` - Paginierter Abruf (1000er Batches)
- `update_company(id, data)` - Update mit Retry (3 Versuche)
- `update_company_safe(id, data, expected_updated_at)` - Update mit Optimistic Locking (verhindert Race Conditions zwischen Scripts)
- `insert_companies(list)` - Batch Insert mit Retry
- `log_sync_history(stats)` - Sync-Log in `sync_history` Tabelle
- `send_alert_email(subject, body)` - E-Mail Alert via Gmail SMTP (benötigt `ALERT_EMAIL_FROM`, `ALERT_EMAIL_PASSWORD`, `ALERT_EMAIL_TO` in `.env`)

---

## VPS Production Environment

**Server:** 72.62.148.205 (Hostinger)
**OS:** Ubuntu 24.04.3 LTS
**Python:** 3.12.3 (separate venvs pro Projekt)
**SSH:** Key-based Authentication (kein Password)
**User:** root

### Projektstruktur

```
/root/
├── Blackfire_automation/
│   ├── venv/
│   ├── .env                       # Supabase + Dropbox Credentials
│   ├── supabase_helper.py         # Shared Supabase Client
│   ├── sync_final.py              # Excel -> Supabase Sync
│   ├── morning_sync_complete.py   # Orchestrator (sync + ISIN)
│   ├── stock_price_updater.py     # Stündliche Kursupdates
│   ├── isin_wkn_updater.py        # ISIN/WKN Recherche
│   ├── isin_ticker_mapper.py      # Hybrid ISIN->Ticker (OpenFIGI + ChatGPT)
│   ├── normalize_sources.py       # Source-Feld Normalisierung (222→40 kanonische Namen)
│   ├── source_mapping.json        # Mapping raw Source → canonical (248 Einträge)
│   ├── harvest_symbols.py         # Symbole aus extra_data in core symbol-Feld kopieren
│   ├── normalize_data.py          # Daten-Normalisierung (läuft bei jedem Sync)
│   ├── classify_listing_status.py # AI Listing-Status Klassifikation (wöchentlich Mo)
│   ├── ai_data_enrichment.py      # Claude AI Datenanreicherung (manuell --apply)
│   ├── fix_tickers.py             # Ticker-Korrektur via OpenFIGI (manuell --apply)
│   ├── extract_sources.py         # Source-Extraktion Utility
│   ├── promote_jsonb_fields.py    # JSONB→Real Columns Migration
│   ├── news_collector.py          # RSS + Brave Search News-Sammlung (alle 2h)
│   ├── ipo_tracker.py             # IPO-Kalender Finnhub+Nasdaq+Brave (täglich)
│   ├── earnings_calendar.py       # Earnings-Termine via yfinance (wöchentlich)
│   ├── spac_tracker.py            # SPAC Tracking SEC EDGAR + Brave (täglich)
│   ├── lockup_scraper.py          # Lock-up Scraping MarketBeat + auto-calc (wöchentlich)
│   ├── invalid_companies.json     # Blacklist (Supabase UUIDs, TTL 30d)
│   ├── sync_cron.log
│   ├── stock_prices.log
│   ├── news.log                   # News Collector Output
│   ├── ipo.log                    # IPO Tracker Output
│   ├── earnings.log               # Earnings Calendar Output
│   ├── spac.log                   # SPAC Tracker Output
│   └── lockup.log                 # Lock-up Scraper Output
│
└── Passive-Income-Generator/
    ├── venv/
    ├── .env
    ├── passive_income_generator.py
    └── cron.log
```

---

## Cronjob Schedule (VPS)

```cron
# Passive Income Generator (05:00 UTC = 06:00 MEZ)
0 5 * * * /root/Passive-Income-Generator/venv/bin/python3 /root/Passive-Income-Generator/passive_income_generator.py >> /root/Passive-Income-Generator/cron.log 2>&1

# Blackfire Morning Sync (06:00 UTC = 07:00 MEZ)
0 6 * * * /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/morning_sync_complete.py >> /root/Blackfire_automation/sync_cron.log 2>&1

# Blackfire Stock Updates (07-23 UTC = 08-00 MEZ, stündlich)
0 7-23 * * * /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/stock_price_updater.py >> /root/Blackfire_automation/stock_prices.log 2>&1

# News Collector (alle 2 Stunden)
0 */2 * * * /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/news_collector.py --apply >> /root/Blackfire_automation/news.log 2>&1

# IPO Tracker (täglich 07:00 UTC) — now also creates auto Lock-up events (IPO+180d)
0 7 * * * /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/ipo_tracker.py --apply >> /root/Blackfire_automation/ipo.log 2>&1

# SPAC Tracker (täglich 07:30 UTC) — SEC EDGAR + Brave Search
30 7 * * * /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/spac_tracker.py --apply >> /root/Blackfire_automation/spac.log 2>&1

# Lock-up Scraper (wöchentlich Montag 07:00 UTC) — MarketBeat + auto-calc
0 7 * * 1 /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/lockup_scraper.py --apply >> /root/Blackfire_automation/lockup.log 2>&1

# Earnings Calendar (wöchentlich Sonntag 06:00 UTC)
0 6 * * 0 /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/earnings_calendar.py --apply >> /root/Blackfire_automation/earnings.log 2>&1
```

---

## Credentials

### .env (Blackfire_automation)

```bash
# Supabase (PostgreSQL)
SUPABASE_URL=https://lglvuiuwbrhiqvxcriwa.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...  # Service Role Key aus Supabase Dashboard

# Dropbox
DROPBOX_URL=...  # Direct Download Link (?dl=1)

# E-Mail Alerts (Gmail SMTP)
ALERT_EMAIL_FROM=...@gmail.com
ALERT_EMAIL_PASSWORD=...  # Gmail App Password (16 Zeichen)
ALERT_EMAIL_TO=...@gmail.com

# AI Data Enrichment (Claude)
ANTHROPIC_API_KEY=...  # Für ai_data_enrichment.py, classify_listing_status.py, fix_tickers.py

# News & IPO Tracking
BRAVE_API_KEY=...        # Für news_collector.py, ipo_tracker.py
FINNHUB_API_KEY=...      # Für ipo_tracker.py (finnhub.io Free Tier)
```

Notion-Credentials werden nicht mehr benötigt (Migration abgeschlossen).

---

## Systemarchitektur

### Morning Sync (06:00 UTC)

```
┌──────────────────────────────────────────┐
│ 1. Dropbox -> Excel Download (3.8 MB)    │
│ 2. Parse ~1672 rows, 85 columns          │
│ 3. Get existing companies from Supabase  │
│ 4. Match by satellog, then by name       │
│ 5. Update ~977 companies                 │
│    - Core fields -> direkte Spalten      │
│    - Rest -> extra_data JSONB            │
│    - Skip Protected Fields!              │
│ 6. Create new companies                  │
│ 7. ISIN/WKN Research (new symbols)       │
│ 8. Log to sync_history                   │
└──────────────────────────────────────────┘
Duration: ~70 seconds
```

### Stock Price Update (07-23 UTC, hourly)

```
┌──────────────────────────────────────────┐
│ 1. Get all companies from Supabase       │
│ 2. Skip blacklisted companies            │
│ 3. Validate ticker (4-tier strategy)     │
│    - Try Symbol (US)                     │
│    - Try Symbol.DE (German)              │
│    - Map ISIN -> Ticker (OpenFIGI)       │
│    - Map WKN -> Ticker                   │
│ 4. Fetch prices via yfinance             │
│ 5. Update Supabase directly              │
│ 6. Save blacklist for next run           │
│ 7. Log to sync_history                   │
└──────────────────────────────────────────┘
Duration: ~20 minutes (with blacklist)
Success Rate: ~300-400 updated / ~1200 skipped
```

---

## Protected Properties

`sync_final.py` überschreibt diese Felder NIEMALS:

```python
PROTECTED_PROPERTIES = {
    # Stock prices (managed by stock_price_updater.py)
    'Current_Price', 'Currency', 'Price_Change_Percent',
    'Price_Update', 'Exchange', 'Market_Status',
    'Day_High', 'Day_Low', 'Volume', 'Market_Cap',
    # Identifiers (managed by isin_wkn_updater.py)
    'ISIN', 'WKN'
}
```

---

## Management & Updates

### SSH Zugriff
```bash
ssh root@72.62.148.205
```

### Logs ansehen
```bash
tail -f ~/Blackfire_automation/sync_cron.log
tail -f ~/Blackfire_automation/stock_prices.log
tail -f ~/Blackfire_automation/news.log
tail -f ~/Blackfire_automation/ipo.log
tail -f ~/Blackfire_automation/earnings.log
tail -f ~/Passive-Income-Generator/cron.log
```

### Code Updates deployen
```bash
# Lokal: pushen
git add . && git commit -m "Update XYZ" && git push

# VPS: pullen + Dependencies
ssh root@72.62.148.205
cd ~/Blackfire_automation
git pull
source venv/bin/activate
pip3 install -r requirements.txt  # nur bei neuen Dependencies
```

### Manuell testen
```bash
cd ~/Blackfire_automation
source venv/bin/activate
python3 sync_final.py              # Excel -> Supabase
python3 stock_price_updater.py     # Kurse updaten (nur 7-23 Uhr)
python3 isin_wkn_updater.py        # ISIN/WKN recherchieren
python3 morning_sync_complete.py   # Vollständiger Morning Sync
```

---

## Kosten & Performance

### Monthly Costs
| Service | Cost |
|---------|------|
| Hostinger VPS | Already paid |
| Supabase | $0 (Free Tier) |
| yfinance | $0 |
| OpenFIGI | $0 (Free Tier) |
| OpenAI (GPT-4o) | ~$4 |
| **Total** | **~$4/month** |

### API Limits
- **Supabase:** Kein Rate-Limit (Service Role Key)
- **yfinance:** Unlimited
- **OpenFIGI:** 10 req/min
- **OpenAI:** Pay-as-you-go

### Performance
| Script | Duration |
|--------|----------|
| Passive Income Generator | ~50 seconds |
| Blackfire Morning Sync (Excel -> Supabase) | ~70 seconds |
| Blackfire Stock Update | ~20 minutes (with blacklist) |

---

## Troubleshooting

### Problem: Cronjob läuft nicht
```bash
cd ~/Blackfire_automation
source venv/bin/activate
python3 [script].py
systemctl status cron
grep CRON /var/log/syslog | grep Blackfire | tail -20
```

### Problem: Supabase Connection Error
- `.env` prüfen: `SUPABASE_URL` und `SUPABASE_SERVICE_ROLE_KEY`
- Service Role Key im Supabase Dashboard -> Project Settings -> API

### Problem: Git Pull Conflicts
```bash
git reset --hard origin/main
git pull
```

---

## Deployment Changelog

### 15. März 2026 - SPAC & Lock-up Tracking (v3.4)
- **2 neue Scripts:**
  - `spac_tracker.py`: SEC EDGAR EFTS API (SIC 6770, 4 Suchbegriffe) + Brave Search (5 Queries). Erkennt SPAC-Filings (S-1, DEFM14A, 8-K), klassifiziert in 4 Event-Types (spac_announced/vote/closing/deadline). Fuzzy-Matching gegen alle Companies. Speichert Sponsor, Trust Size, Ticker, Exchange in event_metadata JSONB. Updates listing_status → 'spac'.
  - `lockup_scraper.py`: Scrapt MarketBeat Lock-up Expirations Calendar (BeautifulSoup). Auto-berechnet Lock-up für IPO-Events ohne lockup_expiry (IPO+180d). Speichert lockup_days, lockup_shares, lockup_percent_of_float, confidence in event_metadata JSONB.
- **3 erweiterte Scripts:**
  - `ipo_tracker.py`: Neuer Step 7 — auto-erstellt lockup_expiry Events für jeden IPO-Match (IPO+180d, confidence: estimated). Dedup gegen bestehende Lock-up Events.
  - `news_collector.py`: Neue catalyst_types 'spac' und 'lockup'. Neue Phase 5 — auto-erstellt SPAC/Lock-up Events aus News mit Relevanz >= 3 (confidence: rumored). Dedup gegen bestehende Events.
  - `alert_generator.py`: 2 neue Alert-Types: lockup_approaching (Lock-up <14d, HIGH priority) und spac_milestone (SPAC Event <14d, HIGH priority).
- **2 neue Cronjobs geplant:**
  - SPAC Tracker: täglich 07:30 UTC
  - Lock-up Scraper: wöchentlich Montag 07:00 UTC
- **Neue Dependencies:** `beautifulsoup4>=4.12.0` (für lockup_scraper.py)
- **DB Migration:** `event_metadata JSONB` Spalte auf `company_events` (via Supabase SQL Editor)

### 13. März 2026 - Phase 2 News-Automatisierung (v3.3)
- **3 neue Scripts deployed:**
  - `news_collector.py`: RSS-Feeds (TechCrunch, Reuters, Yahoo Finance, MarketWatch, Seeking Alpha) + Brave Search API für company-spezifische News. Company-Matching by Symbol + Name. Duplikat-Erkennung via URL. 39+ Artikel/Run.
  - `ipo_tracker.py`: Multi-Source IPO-Tracking (Finnhub API: 90 IPOs, Nasdaq Calendar, Brave Search Fallback). Fuzzy-Matching (difflib, 70% Threshold) gegen 518 Private/Pre-IPO Companies.
  - `earnings_calendar.py`: yfinance Earnings-Termine für ~1193 Public Companies. 399 Termine gefunden. Respektiert invalid_companies.json Blacklist. Rate-Limiting: 1s/Request.
- **3 neue Cronjobs auf VPS:**
  - News Collector: alle 2 Stunden mit --apply
  - IPO Tracker: täglich 07:00 UTC mit --apply
  - Earnings Calendar: wöchentlich Sonntag 06:00 UTC mit --apply
- **Neue Credentials auf VPS:** FINNHUB_API_KEY, BRAVE_API_KEY
- **Dependencies:** `feedparser>=6.0.0` hinzugefügt
- **Linear Issues:** RSE-170, RSE-174–177 → Done

### 13. März 2026 - Data Quality Phase 1 & Full Hardening (v3.2)
- **Data Quality Scripts (VPS deployed):**
  - `normalize_data.py`: Normalisiert Key-Felder (läuft automatisch bei jedem Morning Sync)
  - `classify_listing_status.py`: AI-basierte Listing-Status Klassifikation (wöchentlich Montags im Morning Sync)
  - `ai_data_enrichment.py`: Claude Haiku Datenanreicherung für fehlende Felder (Country, Competitors, Profile, Sector) — manuell mit `--apply`
  - `fix_tickers.py`: Ticker-Korrektur via OpenFIGI für Companies ohne Symbol — manuell mit `--apply`
  - `extract_sources.py`: Source-Extraktion Utility
- **JSONB → Real Columns:**
  - `thier_group`, `vip`, `industry`, `leverage` als echte PostgreSQL-Spalten (vorher nur in extra_data JSONB)
  - B-tree Indexes auf thier_group, vip, industry
  - `sync_final.py` schreibt jetzt in beide (real columns + extra_data)
  - `promote_jsonb_fields.py`: Migrations-Script + SQL Migration
  - Daten: thier_group 1715/1732, vip 1715/1732, industry 1450/1732, leverage 1583/1732
- **Neue DB-Spalten:** `listing_status`, `prio_buy` in companies Tabelle
- **4 neue Tabellen:** company_news, company_events, company_scores, alerts
- **Morning Sync erweitert:** normalize_data bei jedem Sync, classify_listing_status montags
- **ANTHROPIC_API_KEY** auf VPS konfiguriert
- **Dependencies:** `anthropic>=0.40.0` hinzugefügt (v0.84.0 installiert)
- **Google OAuth:** Supabase Auth Provider für Blackfire_service
- **Sentry:** Error Tracking für Blackfire_service (@sentry/nextjs)

### 13. März 2026 - Hardening & Source Normalization (v3.1)
- **8 Schwachstellen behoben:**
  1. TypeScript-Fehler in Blackfire_service behoben (52 Errors → 0)
  2. `ignoreBuildErrors`/`ignoreDuringBuilds` aus next.config.ts entfernt
  3. Cron Auth für Buy Radar (Vercel cron header + CRON_SECRET)
  4. RLS Policies für Notes (INSERT/UPDATE/DELETE mit auth.uid() Check)
  5. Alpha Vantage Fallback-Chain (Redis → PostgreSQL → API)
  6. Optimistic Locking in `update_company_safe()` (verhindert Race Conditions)
  7. E-Mail Alerts bei Fehlern (`send_alert_email()` in supabase_helper.py)
  8. Unused Dependencies entfernt (bullmq, drizzle-orm, recharts, etc.)
- **Source Normalization:** 222 raw Source-Varianten → ~40 kanonische Namen
  - `source_mapping.json`: 248 Mapping-Einträge
  - `normalize_sources.py`: Dry-run + Apply Mode, preserviert Originale in `Source_Original`
  - 811/811 Records erfolgreich normalisiert
  - Top Sources: The Information (367), CB Insights (192), Motley Fool (160), Insider Monkey (101)
- **Supabase TypeScript Types:** Vollständige 1031-Zeilen Type-Definition für alle 17 Tabellen generiert (ersetzt `Database = any`)
- **ISIN/WKN Coverage analysiert:** Symbol 68%, ISIN 37%, WKN ~0% (korrekt, da meist US-Firmen)
- **Symbol Harvest:** extra_data → core `symbol` Feld geprüft — bereits vollständig, 0 Änderungen nötig
- **Systemcheck verifiziert (13.03.2026):**
  - Supabase: 1732 Companies, 1189 mit Symbol (69%), 656 mit ISIN (38%), 581 mit Preis (34%)
  - Stock Updates stündlich aktiv (letztes 14:14 UTC)
  - Buy Radar: 4007 Analysen, täglich um 06:00 UTC
  - Notes: 3544 aktiv mit RLS Policies
  - Vercel Production: READY (alle Commits deployed)
  - Blackfire_automation VPS: git pulled, alle Scripts aktuell

### 10. Februar 2026 - Migration Notion -> Supabase (v3.0)
- Komplett-Migration aller Scripts von Notion API auf Supabase/PostgreSQL
- Neues Shared Module `supabase_helper.py` mit Retry-Logik und Pagination
- `sync_final.py`: Excel -> Supabase statt Notion (977 Updates in 68s)
- `stock_price_updater.py`: Kurse direkt in Supabase (v3, Blacklist mit UUIDs)
- `isin_wkn_updater.py`: ISIN/WKN direkt in Supabase
- `morning_sync_complete.py`: Print-Texte aktualisiert
- Neue `sync_history` Tabelle in Supabase für Logging
- `.env` aktualisiert: Supabase statt Notion Credentials
- Notion-Abhängigkeit vollständig entfernt
- Performance: Morning Sync von 6-8 Min auf ~70 Sek (kein Rate-Limiting mehr)
- VPS Deployment: git pull, pip install, .env mit Supabase-Credentials
- VPS Tests erfolgreich: test_complete_system.py + stock_price_updater.py
- Alte Notion-Backup-Dateien aufgeräumt (18 lokal, 11 auf VPS)
- `test_complete_system.py` für Supabase aktualisiert

### 8. Februar 2026 - Reliability & Performance Fixes (v2.1)
- Persistent Invalid-Ticker Blacklist
- Retry-Logik für Notion API
- PID-Lock für Stock-Updater
- Log-Rotation auf VPS

### 28. Januar 2026 - Intelligente Duplikatserkennung
- ChatGPT-basierte semantische Duplikatserkennung (Passive Income)

### 27. Januar 2026 - Initiales VPS Deployment
- Alle 3 Cronjobs auf VPS deployed
- GitHub Repositories erstellt

---

## Production Checklist

- [x] VPS Setup & SSH Access
- [x] GitHub Repositories
- [x] Supabase Migration (Notion entfernt)
- [x] sync_history Tabelle erstellt
- [x] supabase_helper.py mit Retry + Pagination
- [x] sync_final.py getestet (977 Updates)
- [x] stock_price_updater.py getestet (221+ Kurse lokal, VPS verifiziert)
- [x] isin_wkn_updater.py getestet (199 ISINs)
- [x] Credentials konfiguriert (.env lokal + VPS)
- [x] Cronjobs (gleiche Skript-Namen, keine Änderung nötig)
- [x] PID-Lock für Stock-Updater
- [x] Invalid-Companies Blacklist (30d TTL)
- [x] Log-Rotation konfiguriert
- [x] Service Overview Dashboard
- [x] VPS Deployment (git pull + pip install + .env)
- [x] VPS Test: test_complete_system.py (3/10 Stocks erfolgreich)
- [x] VPS Test: stock_price_updater.py (Full Run verifiziert)
- [x] Alte Notion-Backup-Dateien aufgeräumt (lokal + VPS)
- [x] Optimistic Locking für concurrent Updates (v3.1)
- [x] E-Mail Alerts bei Script-Fehlern (Gmail SMTP)
- [x] Source Normalization (222 → 40 kanonische Namen)
- [x] Source Mapping JSON (248 Einträge)
- [x] Data Quality Scripts deployed (normalize, classify, AI enrich, fix tickers)
- [x] JSONB → Real Columns (thier_group, vip, industry, leverage)
- [x] listing_status + prio_buy Spalten
- [x] 4 neue Tabellen (company_news, company_events, company_scores, alerts)
- [x] ANTHROPIC_API_KEY auf VPS konfiguriert
- [x] Morning Sync erweitert (normalize + classify integriert)
- [x] Google OAuth (Blackfire_service)
- [x] Sentry Error Tracking (Blackfire_service)
- [x] news_collector.py deployed (RSS + Brave Search, alle 2h)
- [x] ipo_tracker.py deployed (Finnhub + Nasdaq + Brave, täglich 07:00)
- [x] earnings_calendar.py deployed (yfinance, wöchentlich So 06:00)
- [x] FINNHUB_API_KEY + BRAVE_API_KEY auf VPS konfiguriert
- [x] Phase 2 Cronjobs eingerichtet
- [x] Linear Issues RSE-170, RSE-174–177 → Done

- [x] spac_tracker.py created (SEC EDGAR + Brave Search)
- [x] lockup_scraper.py created (MarketBeat + auto-calc)
- [x] ipo_tracker.py erweitert (auto Lock-up bei IPO Events)
- [x] news_collector.py erweitert (SPAC/Lock-up catalyst + auto-events)
- [x] alert_generator.py erweitert (lockup_approaching + spac_milestone)
- [x] VPS Deploy: git pull + pip install beautifulsoup4
- [x] VPS: 2 neue Cronjobs eingerichtet (spac_tracker daily 07:30, lockup_scraper Mon 07:00)
- [x] Supabase: event_metadata JSONB Migration ausgeführt
- [x] Initial Run: lockup_scraper --apply (20 lockup_expiry events), spac_tracker --apply, ipo_tracker --apply, news_collector --apply, alert_generator --apply

**Status:** All Systems Operational - VPS Production Deployed (v3.4)

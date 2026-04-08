# Changelog

All notable changes to this project will be documented in this file.

## [1.5.0] - 2026-04-08

### Added
- **Fuzzy Name-Matching im Excel-Sync** (`sync_final.py`)
  - 4-Tier Matching-Kaskade: Satellog exakt → Name exakt → Name case-insensitiv → Name normalisiert
  - `normalize_company_name()` Helper: strippt Legal-Suffixe (INC., CORP., LTD., PLC, AG, SE, GMBH, etc.) und lowercased
  - Name-Spalte (`Company_Name`) wird jetzt immer gesucht, auch wenn `york` leer ist
  - Fuzzy-Match Logging im Sync-Output (Tier 3 + Tier 4 Counter)
  - Create-Deduplizierung per Satellog-Key verhindert doppelte Inserts aus Excel-Duplikaten
  - Einzel-Inserts statt Batch: ein Fehler blockt nicht mehr den gesamten Batch
  - Satellog-Schutz bei Updates: leere york-Werte ueberschreiben nicht den existierenden Satellog

### Changed
- Excel-Sync matcht jetzt **1.824 von 2.167** Excel-Zeilen (vorher 1.069)
- 106 neue Companies automatisch angelegt die vorher nicht gematcht wurden
- Nur noch 343 leere Zeilen werden uebersprungen (vorher 1.098)

### Fixed
- 120 bekannte boersennotierte Companies (PFIZER, CATERPILLAR, SALESFORCE, TSMC, etc.) wurden wegen Case-Sensitivity und Legal-Suffix-Abweichung nicht gesynct
- Batch-Insert Fehler bei Duplikat-Satellog-Constraint (23505) blockierte alle Creates

## [1.4.0] - 2026-03-22

### Added
- SPAC Tracker (`spac_tracker.py`): SEC EDGAR EFTS API + Brave Search
- Lock-up Scraper (`lockup_scraper.py`): MarketBeat + auto-calculation (IPO+180d)
- News Collector auto-event creation from catalyst detection

## [1.3.0] - 2026-03-14

### Added
- Scoring Engine v2 (`scoring_engine.py`): 5 weighted components, Valuation Gap
- Alert Generator (`alert_generator.py`): 9 alert types
- Morning Briefing (`morning_briefing.py`): Claude Sonnet, 4-section format
- Thesis Checker (`thesis_checker.py`): hourly condition monitoring
- Portfolio Monitor (`portfolio_monitor.py`): exit signal detection
- News Collector (`news_collector.py`): RSS + Brave + sentiment + relevance scoring

## [1.2.0] - 2026-03-13

### Added
- Source normalization: 222 raw Source variants → ~40 canonical names
- `listing_status`, `prio_buy` real columns with B-tree indexes
- `classify_listing_status.py`, `normalize_data.py`

## [1.1.0] - 2026-02-10

### Added
- ISIN/WKN Updater (`isin_wkn_updater.py`): yfinance + OpenFIGI
- Morning Sync orchestrator (`morning_sync_complete.py`)

## [1.0.0] - 2026-01-30

### Added
- Initial Excel → Supabase sync (`sync_final.py`)
- Stock Price Updater (`stock_price_updater.py`): yfinance hourly
- Supabase Helper (`supabase_helper.py`): singleton client, pagination, retries

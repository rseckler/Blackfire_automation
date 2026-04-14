# Blackfire Automation — TODO

Operative Aufgabenliste. Single Source of Truth für laufende Arbeit.
**Letzte Aktualisierung:** 2026-04-14 (v1.3 Entry-Price System: classify_listing_status Fix + buy_alert_checker deployed)

## Arbeitslogik

- `CLAUDE.md` enthält nur Fokus, Top-3 Aktionen und Verweis hierher.
- Diese Datei enthält alle operativen Aufgaben, gruppiert nach Workstreams.
- Linear enthält nur Epics, externe Blocker und mehrwöchige Themen.
- `[ ]` offen | `[x]` erledigt (Datum) | `[-]` entfällt

---

## Now

Aktuell aktive Workstreams. Maximal 2-3 gleichzeitig.

1. **v1.3 Buy-Alert System (Python-Seite)** — `buy_alert_checker.py` läuft stündlich (`15 7-23 * * *`), classify_listing_status auf alle 2h umgestellt. Plan-Doku: `../Blackfire_service/docs/PLAN-v1.3-ENTRY-PRICE-SYSTEM.md`
2. **Stock Price Updates & Ticker-Validation** — Laufend, ~561 Stocks mit Blacklist
3. **SPAC Tracker & Lock-up Scraper** — Aktiv, MarketBeat 403-Problem offen

## Next

Kommt dran sobald ein Now-Slot frei wird oder ein Blocker sich löst.

3. **Newsletter Processing** — Gmail IMAP + Claude Haiku Parser geplant
4. **Alert & News System** — Verfeinerung der 9 Alert-Typen

## Later

Bewusst geparkt. Wird bei Bedarf nach Next gezogen.

5. Stock Price Updater Performance-Optimierung (aktuell ~20 Min)
6. Blacklist-Management UI (aktuell nur JSON-File)

---

## Workstreams

---

### 1. v1.3 Buy-Alert System & Listing-Status-Fix (2026-04-14)

**Ziel:** Tommi-Feedback 2026-04-14 — Lancium/MetaX falsch klassifiziert + Buy-Zone-Alerts automatisieren
**Status:** ✅ Komplett deployed — classify-Fix angewandt (514 Firmen korrigiert), buy_alert_checker läuft stündlich
**Blocker:** Keiner
**Nächste Aktion:** Tommi-Testfeedback abwarten

Vollständiger Plan (Cross-Repo): `../Blackfire_service/docs/PLAN-v1.3-ENTRY-PRICE-SYSTEM.md`

#### Erledigt

- [x] `classify_listing_status.py` — Excel-Status als führende Quelle vor Preis/Symbol-Heuristik (Lancium-Bug-Root-Cause). Commits `46f6620`, `cd62334`, `6baeca5` (2026-04-14)
- [x] Typo-Keywords `aquired` + `rebranded` für ACQUIRED_KEYWORDS (Tommi schreibt manchmal mit Tippfehler) (2026-04-14)
- [x] Whitespace-only Werte ignorieren (MetaX hatte `IPO_expected=' '` → triggerte fälschlich pre_ipo) (2026-04-14)
- [x] `morning_sync_complete.py` — Monday-Only-Gate entfernt, classify läuft jetzt bei jedem Sync (alle 2h) (2026-04-14)
- [x] `--apply` auf VPS — 514 Firmen korrigiert (Lancium: public→private, Cruise, TAE, BigID, …) (2026-04-14)
- [x] `buy_alert_checker.py` — neues Python-Script, iteriert `user_entry_prices`, erstellt `buy_zone_reached` Alerts bei diff_pct ≤ 10%. Dedup 24h pro (user, company). Currency-Mismatch-Guard. Commit `33641e8` (2026-04-14)
- [x] VPS-Crontab: `15 7-23 * * * buy_alert_checker.py --apply >> buy_alerts.log 2>&1` (2026-04-14)

#### Offene Aufgaben

- [ ] MetaX-Excel pflegen (Symbol+Kurs oder `Status=public` setzen) — dann auto-korrekt
- [ ] Unrecognized Excel-Status-Werte bereinigen: `income` (152×), `value`, `bet`, `p`, `reit`

---

### 2. Morning Sync & Excel-Pipeline

**Ziel:** Täglicher zuverlässiger Excel→Supabase Sync für ~977 Companies
**Status:** Stabil in Production (06:00 UTC), ~70 Sekunden
**Blocker:** Keiner
**Nächste Aktion:** Monitoring bei Bedarf

#### Offene Aufgaben

- [ ] Keine aktuellen offenen Aufgaben — läuft stabil

#### Erledigt

- [x] Morning Sync auf VPS deployed (2026-01)
- [x] Dropbox Excel Download + Parse (2026-01)
- [x] Protected Properties (Stock prices + ISIN/WKN geschützt) (2026-01)
- [x] ISIN/WKN Research für neue ISINs (2026-01)
- [x] Retry-Logik + Rate-Limiting für Supabase API (2026-02)
- [x] Pfad-Fix für VPS (os.path.join statt relativ) (2026-02)

---

### 3. Stock Price Updates & Ticker-Validation

**Ziel:** Stündliche Aktienkurse für alle Stocks mit gültigem Ticker
**Status:** Läuft stündlich (07-23 UTC), ~561 Stocks, ~20 Min mit Blacklist
**Blocker:** Keiner
**Nächste Aktion:** Performance beobachten

#### Offene Aufgaben

- [ ] ETH/Crypto-Ticker Handling verbessern (gelegentliche Fehlzuordnungen)
- [ ] Blacklist-Cleanup: Alte Einträge nach TTL (7 Tage) prüfen

#### Erledigt

- [x] 4-Tier Ticker Validation (Symbol, Symbol.DE, ISIN→OpenFIGI, WKN→Ticker) (2026-01)
- [x] Persistent Invalid-Ticker Blacklist (invalid_tickers.json, TTL 7d) (2026-02)
- [x] PID-Lock gegen parallele Runs (fcntl.flock) (2026-02)
- [x] Fundamentals Fetch (14 yfinance Felder: P/E, Revenue, Analyst Targets) (2026-03)

---

### 4. SPAC Tracker & Lock-up Scraper

**Ziel:** Automatische Erkennung von SPAC-Events und Lock-up-Expiries
**Status:** SPAC Tracker aktiv (07:30 UTC), Lock-up Scraper aktiv (Mo 07:00 UTC)
**Blocker:** MarketBeat gibt 403 (Bot-Protection) — Lock-up nutzt Auto-Berechnung (IPO+180d)
**Nächste Aktion:** Alternative Datenquelle für Lock-up-Daten evaluieren

#### Offene Aufgaben

- [ ] Alternative zu MarketBeat für Lock-up-Daten finden
- [ ] SPAC Tracker: Mehr Event-Typen (SPAC Liquidation, Extension)

#### Erledigt

- [x] SPAC Tracker via SEC EDGAR EFTS API (SIC 6770) + Brave Search (2026-03)
- [x] Lock-up Scraper via MarketBeat + Auto-Calculation (IPO+180d) (2026-03)
- [x] company_events Tabelle mit event_metadata JSONB (2026-03)
- [x] Auto-Event-Erstellung aus News (catalyst detection, relevance >= 3) (2026-03)

---

### 5. Alert Generator & News Collector

**Ziel:** Automatische Alerts bei relevanten Events + News-Pipeline
**Status:** Alert Generator (06:15 UTC, 9 Typen), News Collector (alle 2h, 17 RSS Feeds)
**Blocker:** Keiner
**Nächste Aktion:** Newsletter Processing hinzufügen (Phase 8.6)

#### Offene Aufgaben

- [ ] Newsletter Processing: Gmail IMAP + Claude Haiku Parser (~$4.50/Monat)
- [ ] Gmail Account erstellen: blackfire.feeds@gmail.com
- [ ] News Relevance Scoring verfeinern (aktuell 1-5 Scale)

#### Erledigt

- [x] Alert Generator mit 9 Typen deployed (2026-03)
- [x] News Collector mit Sentiment-Analyse + Relevance Scoring (2026-03)
- [x] Lockup-Approaching + SPAC-Milestone Alert-Typen (2026-03)
- [x] News-basierte Event-Erstellung (catalyst detection) (2026-03)

---

## Linear-Themen (Management-Ebene)

Reine Code-Tasks (RSE-45, RSE-46) wurden am 2026-04-12 nach TODO.md verschoben und in Linear geschlossen.

| Issue | Thema | Status | Blocker |
|---|---|---|---|
| — | Keine offenen Linear-Issues. Newsletter Processing lebt in TODO.md bis Gmail Account extern erstellt wird. |

---

## Erledigte Meilensteine

| Datum | Meilenstein |
|---|---|
| 2026-01-27 | VPS Deployment (Morning Sync + Stock Updates) |
| 2026-01-28 | Intelligente Duplikatserkennung (Passive Income) |
| 2026-02-08 | Reliability Fixes (Retry, Blacklist, Log-Rotation, PID-Lock) |
| 2026-03 | SPAC Tracker + Lock-up Scraper + Alert Generator deployed |
| 2026-03 | News Collector mit Sentiment + Relevance Scoring |
| 2026-03 | Scoring Engine + Morning Briefing |

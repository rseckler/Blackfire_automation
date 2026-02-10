# Blackfire Automation - Systemdokumentation

**Status:** Production Ready - Deployed auf Hostinger VPS
**Datum:** 10. Februar 2026
**Version:** 3.0 (Migration Notion -> Supabase)

---

## Übersicht

3 automatisierte Workflows laufen 24/7 auf Hostinger VPS:

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

---

## Datenbank: Supabase (PostgreSQL)

**URL:** https://lglvuiuwbrhiqvxcriwa.supabase.co
**Dashboard:** https://supabase.com/dashboard/project/lglvuiuwbrhiqvxcriwa

### Tabellen

| Tabelle | Zweck |
|---------|-------|
| `companies` | Haupttabelle (~1644 Aktien), Core-Felder + `extra_data` JSONB |
| `sync_history` | Automatisches Logging aller Sync/Update-Runs |
| `stock_prices` | Historische Kursdaten (von Blackfire_service) |

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
- `insert_companies(list)` - Batch Insert mit Retry
- `log_sync_history(stats)` - Sync-Log in `sync_history` Tabelle

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
│   ├── invalid_companies.json     # Blacklist (Supabase UUIDs, TTL 30d)
│   ├── sync_cron.log
│   └── stock_prices.log
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

# Optional
BRAVE_API_KEY=...
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
- [x] stock_price_updater.py getestet (221+ Kurse)
- [x] isin_wkn_updater.py getestet (199 ISINs)
- [x] Credentials konfiguriert (.env)
- [x] Cronjobs (gleiche Skript-Namen, keine Änderung nötig)
- [x] PID-Lock für Stock-Updater
- [x] Invalid-Companies Blacklist (30d TTL)
- [x] Log-Rotation konfiguriert
- [x] Service Overview Dashboard
- [ ] VPS Deployment (git pull + pip install)

**Status:** All Systems Operational (lokal getestet, VPS-Deployment ausstehend)

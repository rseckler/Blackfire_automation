# Blackfire Automation - Systemdokumentation

## Übersicht

Automatisiertes System zur Synchronisation von Excel-Daten mit Notion und Live-Aktualisierung von Aktienkursen.

**Version:** 2.0 (mit ISIN/WKN-Support)
**Status:** ✅ Produktiv auf Hostinger VPS
**Erstellt:** Januar 2026
**Letzte Aktualisierung:** 27. Januar 2026

---

## 🚀 Deployment Status

### Production Environment
- **Server:** Hostinger VPS (72.62.148.205)
- **OS:** Ubuntu 24.04.3 LTS
- **Python:** 3.12.3 (in venv)
- **Verzeichnis:** `/root/Blackfire_automation`
- **GitHub:** https://github.com/rseckler/Blackfire_automation (public)

### Aktive Services
- ✅ **Morning Sync:** Täglich 06:00 UTC (07:00 MEZ)
- ✅ **Stock Updates:** Stündlich 07:00-23:00 UTC
- ✅ **Notion API:** Verbunden und funktionsfähig
- ✅ **~1600 Aktien:** In Datenbank

---

## 🎯 Funktionen

### 1. Excel → Notion Synchronisation
- Tägliche Synchronisation um 06:00 UTC
- Download von Dropbox
- Automatische Erkennung von Updates und neuen Einträgen
- **Protected Properties:** Überschreibt keine Stock-Daten oder ISIN/WKN

### 2. ISIN/WKN-Recherche
- Automatische Recherche fehlender ISIN/WKN-Daten
- Nutzt yfinance und OpenFIGI APIs
- Läuft direkt nach dem Morning Sync

### 3. Live-Aktienkurse
- Stündliche Updates von 07:00-23:00 UTC
- Unterstützt US- und deutsche Börsen
- Hybrid-Mapping: ISIN/WKN → Ticker
- 10 Kursdaten-Eigenschaften
- ~300-400 erfolgreiche Updates pro Stunde

---

## 📁 Projektstruktur

```
Blackfire_automation/
├── .env                          # Credentials (NICHT auf GitHub!)
├── .env.example                  # Template für Credentials
├── .gitignore                    # Git-Schutz für sensible Daten
├── requirements.txt              # Python-Dependencies
│
├── sync_final.py                 # Excel → Notion Sync (Hauptskript)
├── isin_wkn_updater.py          # ISIN/WKN-Recherche
├── stock_price_updater.py       # Stündliche Kursaktualisierung
├── isin_ticker_mapper.py        # Hybrid ISIN → Ticker Mapping
├── morning_sync_complete.py     # Orchestriert Morning Workflow
│
├── test_complete_system.py      # System-Test (erste 10 Aktien)
│
├── install_stock_cron.sh        # Cronjob-Installation (Stock)
├── update_morning_cron.sh       # Cronjob-Installation (Morning)
│
├── CLAUDE.md                     # Claude Code Guidance (EN)
├── SYSTEMDOKU.md                 # Diese Datei (DE)
├── GITHUB_DEPLOYMENT_GUIDE.md   # VPS Deployment Guide
├── README.md                     # GitHub Projekt-Übersicht
└── venv/                         # Python Virtual Environment (VPS)
```

---

## ⚙️ Systemarchitektur

### Zeitplan (Cronjobs auf VPS)

| Zeit (UTC) | Zeit (MEZ) | Script | Funktion |
|------------|------------|--------|----------|
| 06:00 | 07:00 | `morning_sync_complete.py` | Excel Sync + ISIN/WKN |
| 07:00-23:00 | 08:00-00:00 | `stock_price_updater.py` | Live-Kurse (stündlich) |

### Datenfluss

```
1. MORNING SYNC (06:00 UTC / 07:00 MEZ)
   ┌─────────────────────────────────────────────────────┐
   │ Dropbox                                             │
   │    ↓ Download Excel (~3.7 MB)                      │
   │ sync_final.py                                       │
   │   - Parse 1610 rows                                 │
   │   - Compare with Notion                             │
   │   - Update ~900 Pages                               │
   │   - Create ~2-3 neue Pages                          │
   │   - SKIP Protected Properties!                      │
   │    ↓                                                │
   │ isin_wkn_updater.py                                 │
   │   - Hole Aktien ohne ISIN/WKN                       │
   │   - Recherche via yfinance                          │
   │   - Recherche via OpenFIGI                          │
   │   - Update ISIN/WKN in Notion                       │
   │    ↓                                                │
   │ Log to Notion Sync History                          │
   └─────────────────────────────────────────────────────┘
   Dauer: 6-8 Minuten

2. STOCK PRICE UPDATE (07:00-23:00 UTC, stündlich)
   ┌─────────────────────────────────────────────────────┐
   │ Notion Database                                     │
   │    ↓ Query alle Aktien                             │
   │ stock_price_updater.py                              │
   │   - Hole Symbol, ISIN, WKN                          │
   │   - Validiere Ticker (4-Stufen):                    │
   │     1. Try Company_Symbol (US: AAPL)                │
   │     2. Try Company_Symbol.DE (DE: SAP.DE)           │
   │     3. Map ISIN → Ticker (OpenFIGI)                 │
   │     4. Map WKN → Ticker                             │
   │   - Fetch Price via yfinance                        │
   │   - Update 10 Stock Properties                      │
   │   - Sleep 1 sec (Rate Limiting)                     │
   │    ↓                                                │
   │ Log to Notion Sync History                          │
   └─────────────────────────────────────────────────────┘
   Dauer: 20-30 Minuten
   Erfolgsrate: ~300-400 Updates / ~1200-1300 Skipped
```

---

## 🔑 Credentials (.env)

### Auf VPS
```bash
# Speicherort: /root/Blackfire_automation/.env
# Permissions: 600 (nur root kann lesen)
```

### Auf Mac
```bash
# Speicherort: /Users/robin/Documents/4_AI/Blackfire_automation/.env
# Wird NICHT in Git committed!
```

### Erforderliche Variablen

```bash
# Notion API
NOTION_API_KEY=ntn_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
NOTION_DATABASE_ID=2f3708a3-de95-807b-88c4-ca0463fd07fb
SYNC_HISTORY_DB_ID=2f4708a3-de95-81f2-b551-f06244e000e9

# Excel-Quelle
DROPBOX_URL=https://www.dropbox.com/scl/fi/.../file.xlsx?dl=1

# OpenAI API (für ISIN-Mapping Fallback, optional)
OPENAI_API_KEY=sk-proj-...

# Finnhub (nicht verwendet, legacy)
FINNHUB_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**WICHTIG:**
- `.env` ist in `.gitignore` geschützt
- Notion API Key wurde am 27.01.2026 aktualisiert
- Gleicher Key wird für "Passive Income Generator" verwendet

---

## 🗄️ Notion-Datenbank-Schema

### Haupt-Database: Aktien_Blackfire
**Database ID:** `2f3708a3-de95-807b-88c4-ca0463fd07fb`

#### Basis-Properties (von Excel)
- `Name` (Title) - Firmenname
- `satellog` (Text) - **Eindeutiger Identifier** (Primary Key)
- `Company_Symbol` (Text) - Ticker-Symbol
- 80+ weitere Excel-Spalten

#### Stock Price Properties (managed by stock_price_updater.py)
| Property | Type | Beschreibung |
|----------|------|--------------|
| `Current_Price` | Number | Aktueller Kurs |
| `Currency` | Select | EUR/USD |
| `Price_Change_Percent` | Number | Tagesänderung in % |
| `Price_Update` | Date | Letztes Update (Timestamp) |
| `Exchange` | Select | XETRA/NASDAQ/NYSE/etc |
| `Market_Status` | Select | 🟢 Open / 🔴 Closed |
| `Day_High` | Number | Tageshoch |
| `Day_Low` | Number | Tagestief |
| `Volume` | Number | Handelsvolumen |
| `Market_Cap` | Number | Marktkapitalisierung |

#### ISIN/WKN Properties (managed by isin_wkn_updater.py)
| Property | Type | Beschreibung |
|----------|------|--------------|
| `ISIN` | Text | International Securities ID (12 Zeichen) |
| `WKN` | Text | Wertpapierkennnummer (6 Zeichen, nur DE) |

#### Protected Properties
**Diese Properties werden von `sync_final.py` NIEMALS überschrieben:**
```python
PROTECTED_PROPERTIES = {
    # Stock prices
    'Current_Price', 'Currency', 'Price_Change_Percent', 'Price_Update',
    'Exchange', 'Market_Status', 'Day_High', 'Day_Low', 'Volume', 'Market_Cap',
    # Identifiers
    'ISIN', 'WKN'
}
```

### Sync History Database
**Database ID:** `2f4708a3-de95-81f2-b551-f06244e000e9`

Loggt alle Sync-Operationen:
- Timestamp
- Status (Success/Error)
- Pages Updated
- Pages Created
- Error Messages

---

## 📜 Script-Referenz

### 1. sync_final.py
**Funktion:** Excel → Notion Synchronisation

**Workflow:**
1. Download Excel von Dropbox
2. Parse alle Zeilen mit pandas
3. Query alle Notion Pages
4. Compare by `satellog` (unique identifier)
5. Update existierende Pages (Skip Protected Properties!)
6. Create neue Pages
7. Log zu Sync History

**Usage (auf VPS):**
```bash
ssh root@72.62.148.205
cd ~/Blackfire_automation
source venv/bin/activate
python3 sync_final.py
```

**Erwartete Ausgabe:**
```
🔄 FINAL EXCEL → NOTION SYNC
📊 Updates: 927
📊 Creates: 3
✅ Updated: 926
✅ Created: 3
⏱️ Duration: 4m 23s
```

---

### 2. isin_wkn_updater.py
**Funktion:** ISIN/WKN-Recherche für Aktien ohne diese Daten

**Strategie:**
1. Query Notion: alle Pages ohne ISIN/WKN
2. Try yfinance: `ticker.info.get('isin')`
3. Try OpenFIGI: Reverse Lookup via Ticker
4. Extract WKN: Letzte 6 Zeichen von DE-ISINs

**APIs:**
- yfinance (kostenlos, begrenzte ISIN-Coverage)
- OpenFIGI (kostenlos, 10 req/min, sehr gut für DE-Aktien)

**Usage:**
```bash
python3 isin_wkn_updater.py
```

---

### 3. stock_price_updater.py
**Funktion:** Stündliche Aktienkurs-Updates (nur 07-23 UTC)

**Smart Ticker Validation (4-Stufen):**
```python
1. ticker = symbol                    # AAPL, MSFT
2. ticker = symbol + '.DE'            # SAP.DE, BMW.DE
3. ticker = map_isin(isin)            # DE0007164600 → SAP.DE
4. ticker = map_wkn(wkn)              # 716460 → SAP.DE
```

**Features:**
- Market Hours Check (skip außerhalb 7-23 UTC)
- Rate Limiting (1 sec/Aktie = 3600 Aktien/h max)
- Caching (valid_tickers Set vermeidet Re-Checks)
- Skip ungültige Ticker (private companies, falsche Symbols)

**Usage:**
```bash
python3 stock_price_updater.py
```

**Erwartete Ausgabe:**
```
📈 STOCK PRICE UPDATE v2
⏰ Market Hours: 7-23 UTC ✅
📊 Total Stocks: 1602
✅ Updated: 342
⏩ Skipped: 1260 (no valid ticker)
⏱️ Duration: 28m 14s
```

---

### 4. isin_ticker_mapper.py
**Funktion:** Hybrid ISIN → Ticker Mapping Library

**Klasse:** `HybridISINMapper`

**Strategie:**
1. **Primary:** OpenFIGI API (präzise, rate-limited)
2. **Fallback:** ChatGPT gpt-4o-mini (für unbekannte ISINs)
3. **Validation:** yfinance Price Check (validiert Existenz)

**Methods:**
```python
from isin_ticker_mapper import HybridISINMapper

mapper = HybridISINMapper()

# Single ISIN
ticker = mapper.map_isin_openfigi('DE0007164600')
# Returns: 'SAP.DE'

# Batch (efficient)
results = mapper.map_batch_openfigi(['DE0007164600', 'US0378331005'])
# Returns: {'DE0007164600': 'SAP.DE', 'US0378331005': 'AAPL'}

# Validate
is_valid = mapper.validate_ticker('SAP.DE')
# Returns: True (price > 0)
```

**Besonderheit:** Bevorzugt deutsche Börsen (GY, GR, GF) für ISIN-Mappings

---

### 5. morning_sync_complete.py
**Funktion:** Orchestriert Morning Workflow

**Workflow:**
```python
1. sync_final.py           # Excel → Notion (6-8 min)
2. isin_wkn_updater.py    # ISIN/WKN enrichment (2-3 min)
```

**Usage:**
```bash
python3 morning_sync_complete.py
```

**Cronjob (auf VPS):**
```bash
0 6 * * * /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/morning_sync_complete.py >> /root/Blackfire_automation/sync_cron.log 2>&1
```

---

### 6. test_complete_system.py
**Funktion:** System-Test mit ersten 10 Aktien (safe, non-destructive)

**Test-Flow:**
1. Fetch 20 Stocks from Notion
2. Process first 10
3. Try Ticker Validation (US → .DE → ISIN)
4. Fetch Prices via yfinance
5. Update Notion (real updates!)

**Usage:**
```bash
python3 test_complete_system.py
```

**Erwartete Ausgabe:**
```
🧪 COMPLETE SYSTEM TEST - First 10 Stocks
📊 Step 1: Getting first 20 stocks...
   ✅ Found 20 stocks

🔍 Step 2: Processing first 10...
   [1/10] KYNDRYL HOLDINGS: $23.83 ✅
   [2/10] DIGITAL TURBINE: $5.46 ✅
   ...
   [5/10] Inflection: ⏩ Skipped (private)

✅ TEST COMPLETE!
   Processed: 10
   Updated: 5
   Failed: 5
```

---

## 🚀 VPS Management

### SSH Zugriff
```bash
# Von Mac aus
ssh root@72.62.148.205

# SSH Key ist konfiguriert (kein Password nötig)
```

### Logs ansehen
```bash
# Live-Logs (Ctrl+C zum beenden)
tail -f ~/Blackfire_automation/sync_cron.log
tail -f ~/Blackfire_automation/stock_prices.log

# Letzte 50 Zeilen
tail -50 ~/Blackfire_automation/sync_cron.log

# Fehler suchen
grep -i error ~/Blackfire_automation/*.log

# Log-Größe prüfen
du -h ~/Blackfire_automation/*.log
```

### Manuell ausführen
```bash
cd ~/Blackfire_automation
source venv/bin/activate

# Morning Sync (jederzeit testbar)
python3 morning_sync_complete.py

# Stock Update (nur 07-23 UTC, sonst skip)
python3 stock_price_updater.py

# Test (safe, nur 10 Aktien)
python3 test_complete_system.py
```

### Cronjobs verwalten
```bash
# Anzeigen
crontab -l

# Editieren
crontab -e

# Cronjob-Logs (System)
grep CRON /var/log/syslog | grep Blackfire | tail -20
```

### Code updaten
```bash
# Auf Mac: Code ändern & pushen
cd ~/Documents/4_AI/Blackfire_automation
git add .
git commit -m "Fix: XYZ"
git push origin main

# Auf VPS: Updates holen
ssh root@72.62.148.205
cd ~/Blackfire_automation
git pull

# Fertig! Cronjobs nutzen automatisch neuen Code
```

### System Status
```bash
# Disk Space
df -h

# RAM
free -h

# CPU Load
uptime

# Python Prozesse
ps aux | grep python3 | grep Blackfire

# Log Rotation (automatisch via logrotate)
ls -lh /var/log/blackfire/
```

---

## 🔧 Troubleshooting

### Problem: Cronjob läuft nicht
**Debug:**
```bash
# Manuell ausführen
cd ~/Blackfire_automation
source venv/bin/activate
python3 morning_sync_complete.py

# Falls "Module not found"
pip install -r requirements.txt

# Cron Service prüfen
systemctl status cron
```

**Lösung:** Cronjob muss vollständigen Python-Path nutzen:
```bash
/root/Blackfire_automation/venv/bin/python3  # ✅ Richtig
python3                                       # ❌ Falsch (System-Python)
```

---

### Problem: Stock Updates finden keine Kurse
**Mögliche Ursachen:**
1. **Ungültiger Ticker** → Company_Symbol ist Firmenname, kein Ticker
2. **Private Company** → Keine börsennotierte Aktie (z.B. Inflection)
3. **Außerhalb Marktzeiten** → Script skipped automatisch (nur 07-23 UTC)
4. **ISIN fehlt** → Kann nicht gemappt werden

**Debug:**
```bash
# Einzelne Aktie testen
python3
>>> import yfinance as yf
>>> ticker = yf.Ticker('SAP.DE')
>>> print(ticker.info.get('currentPrice'))
# Sollte Preis zeigen

# Falls None:
>>> ticker = yf.Ticker('SAP')  # Versuche ohne .DE
>>> print(ticker.info.get('currentPrice'))
```

---

### Problem: OpenFIGI Rate Limit
**Symptom:** Viele ISIN-Mappings schlagen fehl

**Ursache:** OpenFIGI Free Tier = 10 req/min

**Lösung:**
- Script hat 0.3 sec delay → ~200 req/hour
- Für 1600 neue ISINs: ~8 Stunden
- Danach: Caching + nur neue Aktien
- Falls zu langsam: OpenAI Fallback aktivieren (bereits konfiguriert)

---

### Problem: Notion API 401 Unauthorized
**Symptom:** `{"code":"unauthorized","message":"API token is invalid"}`

**Lösung:**
1. Neuen API Key erstellen: https://www.notion.so/my-integrations
2. Integration mit Database verbinden (⋮ → Add connections)
3. `.env` aktualisieren:
   ```bash
   nano .env
   # NOTION_API_KEY=ntn_NEU_HIER_EINFÜGEN
   ```
4. Auf VPS hochladen:
   ```bash
   scp .env root@72.62.148.205:~/Blackfire_automation/.env
   ```

**API Key:** Gespeichert in `.env` (NICHT in Git!)

---

### Problem: Git Pull Merge Conflicts
**Symptom:** `error: Your local changes would be overwritten`

**Ursache:** Lokale Änderungen auf VPS kollidieren mit GitHub

**Lösung:**
```bash
# Option 1: GitHub-Version übernehmen (empfohlen)
git reset --hard origin/main
git pull

# Option 2: Lokale Änderungen behalten
git stash
git pull
git stash pop
# Conflicts manuell lösen
```

**Best Practice:** ❌ Niemals auf VPS Code editieren! Immer auf Mac entwickeln → GitHub pushen → VPS pullen

---

## 📊 Performance & Kosten

### API Limits

| Service | Free Tier | Usage/Tag | Kosten |
|---------|-----------|-----------|--------|
| **yfinance** | Unlimited | ~1600 calls/hour | 🟢 $0 |
| **OpenFIGI** | 10 req/min | ~30 calls/Tag | 🟢 $0 |
| **OpenAI** | Pay-as-you-go | ~10 calls/Tag (Fallback) | 🟡 ~$0.01/Tag |
| **Notion API** | 3 req/sec | ~2000 calls/Tag | 🟢 $0 |
| **Dropbox** | - | 1 download/Tag (3.7 MB) | 🟢 $0 |

**Total Kosten:** ~$0.30/Monat (nur OpenAI Fallback)

### Laufzeiten

| Script | Durchschnitt | Peak |
|--------|--------------|------|
| `sync_final.py` | 3-5 min | 10 min (erste Ausführung) |
| `isin_wkn_updater.py` | 2-3 min | 15 min (viele neue ISINs) |
| `stock_price_updater.py` | 20-30 min | 45 min (alle 1600 Aktien) |
| **Morning Sync Total** | 6-8 min | 25 min |

### Datenvolumen

- **Excel Download:** 3.7 MB/Tag
- **Notion API:** ~50 MB/Tag
- **Total Traffic:** ~60 MB/Tag = 1.8 GB/Monat

---

## 🔒 Sicherheit

### Protected Files (.gitignore)
```
.env
*.env
client_secret*.json
credentials.json
*.log
*.xlsx
*.xls
__pycache__/
venv/
```

### API Key Rotation
**Empfehlung:** Alle 90 Tage rotieren

**Letzter Wechsel:** 27. Januar 2026

**Next Rotation:** ~April 2026

**Betroffene Keys:**
- Notion API Key (beide Projects: Blackfire + Passive Income)
- OpenAI API Key (optional, nur für ISIN-Fallback)

### VPS Security
```bash
# SSH Key Authentication (✅ konfiguriert)
# Root Login mit Password (❌ deaktivieren empfohlen)

# Firewall (optional, noch nicht konfiguriert)
sudo ufw allow 22/tcp
sudo ufw enable
```

### Backup
**Kritische Daten:**
- `.env` (lokal auf Mac gesichert, verschlüsselt speichern!)
- Notion Database (Auto-Backup durch Notion)
- GitHub Repository (Source Code)

**Backup-Strategie:**
```bash
# .env Backup erstellen (lokal auf Mac)
cp .env .env.backup.$(date +%Y%m%d)
# NIEMALS in Git committen!

# VPS .env Backup holen
scp root@72.62.148.205:~/Blackfire_automation/.env ~/Desktop/.env.vps.backup
```

---

## 📚 Weiterführende Dokumentation

- **CLAUDE.md** - Guidance für Claude Code (EN, technisch)
- **GITHUB_DEPLOYMENT_GUIDE.md** - VPS Deployment Guide
- **README.md** - GitHub Projekt-Übersicht
- **.env.example** - Credentials Template

---

## 🎉 Changelog

### v2.0 - VPS Production Deployment (27. Januar 2026)
- ✅ VPS Deployment auf Hostinger (72.62.148.205)
- ✅ GitHub Repository public gemacht
- ✅ SSH Key Authentication konfiguriert
- ✅ Virtual Environment auf VPS
- ✅ Cronjobs migriert (Mac → VPS)
- ✅ Notion API Key aktualisiert
- ✅ Test mit 10 Aktien erfolgreich (5/10 updated)
- ✅ Production läuft 24/7

### v2.0 - ISIN/WKN Support (27. Januar 2026)
- ✅ ISIN/WKN-Properties hinzugefügt
- ✅ Hybrid ISIN-Mapper (OpenFIGI + ChatGPT)
- ✅ Protected Properties in sync_final.py
- ✅ Smart Ticker Validation (US → .DE → ISIN)
- ✅ morning_sync_complete.py orchestriert beide Syncs

### v1.0 - Initial Release (24. Januar 2026)
- ✅ Excel → Notion Sync
- ✅ Stock Price Updater (nur Symbol-basiert)
- ✅ Cronjobs konfiguriert (Mac)
- ✅ Logging zu Sync History

---

## 🆘 Support & Kontakt

### Bei Problemen

1. **Logs prüfen:**
   ```bash
   ssh root@72.62.148.205
   tail -100 ~/Blackfire_automation/sync_cron.log
   tail -100 ~/Blackfire_automation/stock_prices.log
   ```

2. **Manuell testen:**
   ```bash
   cd ~/Blackfire_automation
   source venv/bin/activate
   python3 test_complete_system.py
   ```

3. **Notion Sync History prüfen:**
   - Öffne Notion
   - Database: "Sync History - Excel → Notion"
   - Letzte Einträge prüfen

### GitHub
- **Repository:** https://github.com/rseckler/Blackfire_automation
- **Issues:** https://github.com/rseckler/Blackfire_automation/issues

### VPS Access
- **Host:** 72.62.148.205
- **User:** root
- **Auth:** SSH Key (konfiguriert)
- **Provider:** Hostinger

---

**Erstellt mit Claude Code** 🤖
**Powered by:** Notion API • yfinance • OpenFIGI • OpenAI
**Deployment:** GitHub → Hostinger VPS

**Status:** ✅ Production Ready • 24/7 Active

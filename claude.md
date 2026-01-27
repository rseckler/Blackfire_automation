# Blackfire Automation - Finale Systemdokumentation

**Status:** ✅ Production Ready - Deployed auf Hostinger VPS  
**Datum:** 27. Januar 2026 (22:45 Uhr)  
**Version:** 2.0 (mit ISIN/WKN-Support + Passive Income Integration)

---

## 🎯 Übersicht

3 automatisierte Workflows laufen 24/7 auf Hostinger VPS:

### 1. Passive Income Generator (06:00 MEZ)
- **Repo:** https://github.com/rseckler/Passive-Income-Generator
- **Path:** `/root/Passive-Income-Generator`
- **Funktion:** Generiert täglich 10 Passive Income Ideen via GPT-4o
- **Output:** Notion Database + E-Mail
- **Status:** ✅ Getestet & Funktioniert

### 2. Blackfire Morning Sync (07:00 MEZ)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** Excel → Notion Sync (~900 Aktien) + ISIN/WKN Recherche
- **Status:** ✅ Aktiv

### 3. Blackfire Stock Updates (08:00-00:00 MEZ, stündlich)
- **Repo:** https://github.com/rseckler/Blackfire_automation
- **Path:** `/root/Blackfire_automation`
- **Funktion:** Live Aktienkurse (~300-400 Updates/h)
- **Status:** ✅ Aktiv

---

## 🚀 VPS Production Environment

**Server:** 72.62.148.205 (Hostinger)  
**OS:** Ubuntu 24.04.3 LTS  
**Python:** 3.12.3 (separate venvs pro Projekt)  
**SSH:** Key-based Authentication (kein Password)  
**User:** root  

### Projektstruktur

```
/root/
├── Blackfire_automation/          # Stock & Excel Automation
│   ├── venv/                      # Python 3.12.3
│   ├── .env                       # Credentials (chmod 600)
│   ├── sync_final.py
│   ├── morning_sync_complete.py
│   ├── stock_price_updater.py
│   ├── isin_wkn_updater.py
│   ├── isin_ticker_mapper.py
│   ├── sync_cron.log             # Morning Sync Logs
│   └── stock_prices.log          # Stock Update Logs
│
└── Passive-Income-Generator/      # Daily Ideas Generator
    ├── venv/                      # Python 3.12.3
    ├── .env                       # Credentials (chmod 600)
    ├── passive_income_generator.py
    └── cron.log                   # Generator Logs
```

---

## ⏰ Cronjob Schedule (VPS)

```cron
# Passive Income Generator (05:00 UTC = 06:00 MEZ)
0 5 * * * /root/Passive-Income-Generator/venv/bin/python3 /root/Passive-Income-Generator/passive_income_generator.py >> /root/Passive-Income-Generator/cron.log 2>&1

# Blackfire Morning Sync (06:00 UTC = 07:00 MEZ)
0 6 * * * /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/morning_sync_complete.py >> /root/Blackfire_automation/sync_cron.log 2>&1

# Blackfire Stock Updates (07-23 UTC = 08-00 MEZ, stündlich)
0 7-23 * * * /root/Blackfire_automation/venv/bin/python3 /root/Blackfire_automation/stock_price_updater.py >> /root/Blackfire_automation/stock_prices.log 2>&1
```

**Mac:** ❌ Alle Cronjobs entfernt (keine lokalen Jobs mehr)

---

## 🔑 Credentials & Notion Integration

### Notion API
- **API Key:** In `.env` auf VPS (nicht in Git!)
- **Gültig für:** Beide Projekte (Blackfire + Passive Income)
- **Letzte Aktualisierung:** 27. Januar 2026

### Notion Databases

| Database | ID | Integration |
|----------|-----|-------------|
| **Aktien_Blackfire** | `2f3708a3-de95-807b-88c4-ca0463fd07fb` | ✅ Verbunden |
| **Sync History** | `2f4708a3-de95-81f2-b551-f06244e000e9` | ✅ Verbunden (404 fixed!) |
| **Passive Income Ideen** | `2f5708a3-de95-8180-b6a7-dd163de77ea8` | ✅ Verbunden |

**Wichtig:** Sync History 404-Problem wurde behoben durch Verbindung der Integration!

### OpenAI API
- **Verwendet von:** Passive Income Generator (GPT-4o)
- **Kosten:** ~$0.10-0.15 pro Tag = ~$4/Monat

---

## 📊 Systemarchitektur

### Blackfire Automation

```
Morning Workflow (06:00 UTC):
┌─────────────────────────────────────────┐
│ 1. Dropbox → Excel Download (3.7 MB)   │
│ 2. Parse 1610 rows                      │
│ 3. Compare with Notion                  │
│ 4. Update ~900 pages (Skip Protected!)  │
│ 5. Create ~2-3 new pages                │
│ 6. ISIN/WKN Research (new ISINs)        │
│ 7. Log to Sync History                  │
└─────────────────────────────────────────┘
Duration: 6-8 minutes

Stock Price Update (07-23 UTC, hourly):
┌─────────────────────────────────────────┐
│ 1. Query all stocks (~1600)             │
│ 2. Validate ticker (4-tier strategy)    │
│    - Try Symbol (US)                    │
│    - Try Symbol.DE (German)             │
│    - Map ISIN → Ticker (OpenFIGI)       │
│    - Map WKN → Ticker                   │
│ 3. Fetch prices via yfinance            │
│ 4. Update 10 stock properties           │
│ 5. Log to Sync History                  │
└─────────────────────────────────────────┘
Duration: 20-30 minutes
Success Rate: ~300-400 / ~1200-1300 skipped
```

### Passive Income Generator

```
Daily Workflow (05:00 UTC):
┌─────────────────────────────────────────┐
│ 1. Generate 10 ideas via GPT-4o         │
│    - Title, Description                 │
│    - Implementation Guide (5 steps)     │
│    - Difficulty, Start Capital          │
│    - Tools, Potential Score (1-10)      │
│ 2. Save to Notion Database              │
│ 3. Send Email Summary                   │
└─────────────────────────────────────────┘
Duration: ~50 seconds
Success Rate: 100%
```

---

## 🔧 Protected Properties (Blackfire)

**Kritisch:** `sync_final.py` überschreibt diese Properties NIEMALS:

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

Dies verhindert, dass der Excel-Sync die stündlich aktualisierten Kursdaten überschreibt!

---

## 🛠️ Management & Updates

### SSH Zugriff
```bash
ssh root@72.62.148.205
```

### Logs ansehen
```bash
# Passive Income
tail -f ~/Passive-Income-Generator/cron.log

# Blackfire Morning Sync
tail -f ~/Blackfire_automation/sync_cron.log

# Blackfire Stock Updates
tail -f ~/Blackfire_automation/stock_prices.log

# Alle Logs gleichzeitig
tail -f ~/*/*.log
```

### Code Updates deployen
```bash
# Auf Mac: Entwickeln & Pushen
cd ~/Documents/4_AI/[Projekt]
# Code ändern...
git add .
git commit -m "Update XYZ"
git push origin main

# Auf VPS: Updates holen
ssh root@72.62.148.205
cd ~/[Projekt]
git pull

# Fertig! Cronjob nutzt automatisch neuen Code
```

### Manuell testen
```bash
# Passive Income
cd ~/Passive-Income-Generator
source venv/bin/activate
python3 passive_income_generator.py

# Blackfire
cd ~/Blackfire_automation
source venv/bin/activate
python3 test_complete_system.py        # Safe test (10 stocks)
python3 morning_sync_complete.py       # Full morning sync
python3 stock_price_updater.py         # Stock updates (only 7-23)
```

### System Status
```bash
# Cronjobs prüfen
crontab -l

# Disk Space
df -h

# RAM
free -h

# Python Prozesse
ps aux | grep python3
```

---

## 💰 Kosten & Performance

### Monthly Costs
| Service | Cost |
|---------|------|
| Hostinger VPS | Already paid |
| Notion API | $0 (Free Tier) |
| yfinance | $0 |
| OpenFIGI | $0 (Free Tier) |
| OpenAI (GPT-4o) | ~$4 |
| **Total** | **~$4/month** |

### API Limits
- **Notion:** 3 req/sec → ~2000 req/day ✅
- **yfinance:** Unlimited → ~1600 calls/hour ✅
- **OpenFIGI:** 10 req/min → ~30 req/day ✅
- **OpenAI:** Pay-as-you-go → 1 call/day ✅

### Performance
| Script | Duration |
|--------|----------|
| Passive Income Generator | ~50 seconds |
| Blackfire Morning Sync | 6-8 minutes |
| Blackfire Stock Update | 20-30 minutes |

---

## 🐛 Troubleshooting

### Problem: Cronjob läuft nicht
```bash
# Manuell testen
cd ~/[Projekt]
source venv/bin/activate
python3 [script].py

# Cron Service prüfen
systemctl status cron

# Cronjob Logs (System)
grep CRON /var/log/syslog | grep Blackfire | tail -20
```

### Problem: Notion API 404
**Gelöst!** Integration wurde mit Sync History verbunden.

Falls erneut:
1. Notion öffnen → Database öffnen
2. ⋮ (Menü) → Connections → Add Integration
3. Integration auswählen

### Problem: Git Pull Conflicts
```bash
# GitHub-Version übernehmen (empfohlen)
git reset --hard origin/main
git pull
```

---

## 📚 Dokumentation

### Blackfire Automation
- **GitHub:** https://github.com/rseckler/Blackfire_automation
- **README.md** - Projekt-Übersicht
- **SYSTEMDOKU.md** - Diese Datei
- **CLAUDE.md** - Guidance für Claude Code (EN)
- **GITHUB_DEPLOYMENT_GUIDE.md** - VPS Deployment

### Passive Income Generator
- **GitHub:** https://github.com/rseckler/Passive-Income-Generator
- **README.md** - Quick Start
- **ANLEITUNG.md** - Detaillierte Anleitung (DE)
- **NOTION-SETUP.md** - Database Setup

---

## 🎉 Deployment Changelog

### 27. Januar 2026 (22:45) - Finale Production Version
- ✅ Passive Income Generator auf VPS deployed
- ✅ GitHub Repository erstellt & public
- ✅ Sync History 404 Problem behoben
- ✅ Alle 3 Cronjobs aktiv auf VPS
- ✅ Mac Cronjobs entfernt
- ✅ Test erfolgreich: 10 Ideen generiert
- ✅ Beide Projekte vollständig dokumentiert

### 27. Januar 2026 (21:00) - Blackfire VPS Deployment
- ✅ Blackfire auf VPS deployed
- ✅ SSH Key Authentication konfiguriert
- ✅ GitHub Repository public gemacht
- ✅ Virtual Environments eingerichtet
- ✅ Notion API Key aktualisiert
- ✅ 2 Cronjobs migriert (Mac → VPS)

### 27. Januar 2026 (Vormittag) - ISIN/WKN Support
- ✅ ISIN/WKN Properties hinzugefügt
- ✅ Hybrid ISIN-Mapper implementiert (OpenFIGI + ChatGPT)
- ✅ Protected Properties in sync_final.py
- ✅ Smart Ticker Validation (4-tier)

---

## ✅ Production Checklist

- [x] VPS Setup & SSH Access
- [x] GitHub Repositories erstellt (beide public)
- [x] Code deployed & getestet
- [x] Credentials konfiguriert (.env)
- [x] Notion Integrations verbunden
- [x] Cronjobs installiert & verifiziert
- [x] Logs funktionieren
- [x] Mac Cronjobs entfernt
- [x] Dokumentation vollständig
- [x] Test-Runs erfolgreich

**Status:** 🟢 All Systems Operational

---

**Erstellt mit Claude Code** 🤖  
**Deployment:** GitHub → Hostinger VPS  
**Maintenance:** Git Pull für Updates  
**Monitoring:** Logs + Notion Sync History

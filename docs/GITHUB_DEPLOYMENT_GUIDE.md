# 🚀 GitHub → Hostinger VPS Deployment

## Warum GitHub statt lokalem Upload?

### ✅ GitHub (Empfohlen für Production)
```
Mac → GitHub → VPS
     git push   git clone/pull
```

**Vorteile:**
- ✅ **Sauberer Code** - keine Test-Files, Backups
- ✅ **Einfache Updates** - `git pull` statt SCP
- ✅ **Versionskontrolle** - Rollback möglich
- ✅ **Best Practice** - Industry Standard
- ✅ **Kein manuelles Kopieren**

### ❌ Lokaler Upload (setup_vps.sh + upload_to_vps.sh)
- 35+ untracked Files
- Manueller Upload jedes Mal
- Fehleranfällig
- Keine History

---

## 📋 Voraussetzung

### 1. GitHub Repository Status
```bash
# Auf deinem Mac prüfen:
cd /Users/robin/Documents/4_AI/Blackfire_automation
git remote -v
```

**Output sollte sein:**
```
origin  https://github.com/rseckler/Blackfire_automation.git (fetch)
origin  https://github.com/rseckler/Blackfire_automation.git (push)
```

✅ **Repository ist öffentlich** → Kein Token nötig für `git clone`

### 2. Produktive Files auf GitHub
Prüfe was committed ist:
```bash
git status
git log --oneline -5
```

**Wichtig:** Nur produktive Files sollten auf GitHub sein:
- ✅ `sync_final.py`
- ✅ `stock_price_updater.py`
- ✅ `isin_wkn_updater.py`
- ✅ `isin_ticker_mapper.py`
- ✅ `morning_sync_complete.py`
- ✅ `requirements.txt`
- ✅ `.env.example` (Template, KEINE echten Credentials!)
- ❌ `.env` (NIEMALS committen!)
- ❌ `test_*.py` (optional, für Dev)

---

## 🚀 Deployment auf Hostinger VPS

### Schritt 1: Auf VPS einloggen
```bash
ssh root@72.62.148.205
# oder
ssh robin@72.62.148.205
```

### Schritt 2: System vorbereiten
```bash
# System updaten
apt-get update
apt-get upgrade -y

# Python 3.9+ installieren
apt-get install -y python3 python3-pip python3-venv git curl

# Python-Version prüfen
python3 --version  # Sollte 3.9+ sein
```

### Schritt 3: Repository klonen
```bash
# Nach Home wechseln
cd ~

# Repository klonen (öffentlich, kein Token nötig)
git clone https://github.com/rseckler/Blackfire_automation.git

# Ins Verzeichnis wechseln
cd Blackfire_automation

# Status prüfen
ls -la
git log --oneline -3
```

### Schritt 4: Python Dependencies installieren
```bash
# Direkt im Repository-Ordner
cd ~/Blackfire_automation

# Option A: System Python (einfach)
pip3 install -r requirements.txt

# Option B: Virtual Environment (sauberer, empfohlen)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Schritt 5: .env Datei erstellen
**⚠️ WICHTIG:** `.env` ist NICHT auf GitHub!

```bash
# Template kopieren
cp .env.example .env

# Editieren
nano .env
```

**Füge echte Credentials ein:**
```bash
NOTION_API_KEY=ntn_xxxxxxxxxxxxxxxx
NOTION_DATABASE_ID=2f3708a3-de95-807b-88c4-ca0463fd07fb
SYNC_HISTORY_DB_ID=2f4708a3-de95-81f2-b551-f06244e000e9
DROPBOX_URL=https://www.dropbox.com/scl/fi/.../file.xlsx?rlkey=xxx&dl=1
OPENAI_API_KEY=sk-proj-xxxxxxxxxxxxx
```

**Sichern:**
```bash
chmod 600 .env  # Nur Owner kann lesen
```

### Schritt 6: Test-Run
```bash
# Test mit 10 Aktien (sicher)
python3 test_complete_system.py
```

**Erwartete Ausgabe:**
```
🧪 COMPLETE SYSTEM TEST - First 10 Stocks
✅ Updated: 5-8
❌ Failed: 2-5
```

Falls erfolgreich, voller Sync:
```bash
python3 morning_sync_complete.py
```

### Schritt 7: Cronjobs installieren

#### Option A: Automatisch (empfohlen)
```bash
cd ~/Blackfire_automation

# Scripts ausführbar machen
chmod +x *.sh

# Cronjobs installieren
./update_morning_cron.sh    # Morning Sync (06:00)
./install_stock_cron.sh      # Stock Updates (07-23)
```

#### Option B: Manuell
```bash
crontab -e
```

**Füge ein (System Python):**
```
# Blackfire Automation
0 6 * * * /usr/bin/python3 ~/Blackfire_automation/morning_sync_complete.py >> ~/Blackfire_automation/sync_cron.log 2>&1
0 7-23 * * * /usr/bin/python3 ~/Blackfire_automation/stock_price_updater.py >> ~/Blackfire_automation/stock_prices.log 2>&1
```

**Falls Virtual Environment:**
```
0 6 * * * ~/Blackfire_automation/venv/bin/python3 ~/Blackfire_automation/morning_sync_complete.py >> ~/Blackfire_automation/sync_cron.log 2>&1
0 7-23 * * * ~/Blackfire_automation/venv/bin/python3 ~/Blackfire_automation/stock_price_updater.py >> ~/Blackfire_automation/stock_prices.log 2>&1
```

**Verifizieren:**
```bash
crontab -l
```

---

## 🔄 Code Updates deployen (der große Vorteil!)

### Auf Mac: Entwickeln & Pushen
```bash
# Auf deinem Mac
cd /Users/robin/Documents/4_AI/Blackfire_automation

# Code ändern
nano sync_final.py

# Lokal testen
python3 test_complete_system.py

# Committen
git add sync_final.py
git commit -m "Fix: Verbesserte Ticker-Validierung"
git push origin main
```

### Auf VPS: Updaten
```bash
# SSH auf VPS
ssh root@72.62.148.205

# Zum Projekt
cd ~/Blackfire_automation

# Updates holen
git pull

# FERTIG! 🎉
# Cronjobs nutzen automatisch den neuen Code
```

**Das war's!** Kein SCP, kein manuelles Kopieren, keine Fehler.

---

## 📊 Monitoring

### Logs ansehen
```bash
# Live-Logs (Ctrl+C zum beenden)
tail -f ~/Blackfire_automation/sync_cron.log
tail -f ~/Blackfire_automation/stock_prices.log

# Letzte 50 Zeilen
tail -50 ~/Blackfire_automation/sync_cron.log

# Fehler suchen
grep -i error ~/Blackfire_automation/*.log
```

### Cronjob Status
```bash
# Ist Python-Prozess aktiv?
ps aux | grep python3 | grep Blackfire

# Letzte Cron-Ausführungen
grep CRON /var/log/syslog | grep Blackfire | tail -10
```

### System Status
```bash
# Disk Space
df -h

# RAM Usage
free -h

# System Load
uptime

# Logs Größe
du -h ~/Blackfire_automation/*.log
```

---

## 🔧 Troubleshooting

### Problem: `git clone` schlägt fehl
**Symptom:** `Repository not found`

**Lösung:**
```bash
# URL prüfen
curl -I https://github.com/rseckler/Blackfire_automation

# Falls 404: Repository ist privat
# → Entweder public machen ODER mit Token klonen:
git clone https://YOUR_GITHUB_TOKEN@github.com/rseckler/Blackfire_automation.git
```

### Problem: Module nicht gefunden im Cronjob
**Symptom:** Cronjob läuft nicht, manuell funktioniert

**Ursache:** Cronjob nutzt falsches Python

**Debug:**
```bash
# Welches Python nutzt Cronjob?
which python3  # /usr/bin/python3

# Packages dort installiert?
/usr/bin/python3 -c "import yfinance"

# Falls Fehler: Packages fehlen
/usr/bin/python3 -m pip install -r requirements.txt
```

**Lösung (wenn venv genutzt):**
```bash
# In crontab vollständigen Path nutzen:
~/Blackfire_automation/venv/bin/python3 ~/Blackfire_automation/morning_sync_complete.py
```

### Problem: `.env` fehlt nach `git pull`
**Das ist normal!** `.env` ist in `.gitignore`

**Lösung:**
```bash
# .env bleibt immer auf dem Server
ls -la ~/Blackfire_automation/.env

# Falls weg: von Backup wiederherstellen
# oder neu erstellen aus .env.example
```

### Problem: Merge Conflicts nach `git pull`
**Symptom:**
```
error: Your local changes to the following files would be overwritten by merge
```

**Ursache:** Lokale Änderungen auf VPS kollidieren mit GitHub

**Lösung 1 (empfohlen):** GitHub-Version übernehmen
```bash
git reset --hard origin/main
git pull
```

**Lösung 2:** Lokale Änderungen committen
```bash
git stash         # Änderungen sichern
git pull          # Updates holen
git stash pop     # Änderungen zurück
# Conflicts manuell lösen
```

**Best Practice:** ❌ Niemals auf VPS Code editieren!
- Immer auf Mac entwickeln
- Über GitHub deployen

---

## 🔒 Sicherheit

### GitHub Repository
**Prüfe `.gitignore`:**
```bash
cat .gitignore
```

**Sollte enthalten:**
```
.env
*.env
.DS_Store
*.log
*.xlsx
*.xls
__pycache__/
venv/
```

**Verify:**
```bash
git ls-files | grep .env  # Sollte NICHTS zeigen!
```

Falls `.env` in Git:
```bash
# SOFORT entfernen:
git rm --cached .env
git commit -m "Remove .env from tracking"
git push origin main

# Und GitHub-Token rotieren!
```

### VPS .env Backup
```bash
# Backup auf Mac ziehen
scp root@72.62.148.205:~/Blackfire_automation/.env ~/Desktop/.env.vps_backup

# NICHT in Git committen!
# Sicher aufbewahren (verschlüsselt)
```

---

## 📈 Vorher/Nachher Vergleich

| Aspekt | Lokaler Upload | GitHub Deployment |
|--------|----------------|-------------------|
| **Setup** | `scp` + manuell kopieren | `git clone` |
| **Updates** | `./upload_to_vps.sh` + scp | `git pull` |
| **Fehlerrate** | ⚠️ Mittel (Files vergessen) | ✅ Niedrig |
| **Versionskontrolle** | ❌ Keine | ✅ Vollständig |
| **Rollback** | ❌ Nicht möglich | ✅ `git checkout` |
| **CI/CD Ready** | ❌ Nein | ✅ Ja |
| **Team-Workflow** | ❌ Schwierig | ✅ Einfach |

---

## 🎯 Empfohlener Workflow

```
┌─────────────────────────────────────────────────┐
│ 1. MAC: Code entwickeln & testen                │
│    cd ~/Documents/4_AI/Blackfire_automation     │
│    nano sync_final.py                           │
│    python3 test_complete_system.py              │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ 2. MAC: Git Push                                │
│    git add .                                    │
│    git commit -m "Feature: XYZ"                 │
│    git push origin main                         │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ 3. VPS: Git Pull                                │
│    ssh root@72.62.148.205                       │
│    cd ~/Blackfire_automation                    │
│    git pull                                     │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ 4. VPS: Cronjobs laufen automatisch             │
│    (kein Neustart nötig)                        │
└─────────────────────────────────────────────────┘
```

---

## 🚀 Quick Reference

```bash
# === AUF MAC ===
cd ~/Documents/4_AI/Blackfire_automation
git add .
git commit -m "Update"
git push

# === AUF VPS ===
ssh root@72.62.148.205
cd ~/Blackfire_automation
git pull

# Logs ansehen
tail -f sync_cron.log

# Manueller Test
python3 morning_sync_complete.py

# Cronjobs prüfen
crontab -l
```

---

## ✅ Migration Checklist

- [ ] VPS eingeloggt
- [ ] Git installiert (`apt-get install git`)
- [ ] Python 3.9+ installiert
- [ ] Repository geklont (`git clone`)
- [ ] Dependencies installiert (`pip install -r requirements.txt`)
- [ ] `.env` erstellt und gesichert (`chmod 600`)
- [ ] Test-Run erfolgreich (`test_complete_system.py`)
- [ ] Cronjobs installiert (`crontab -l`)
- [ ] Logs funktionieren (`tail -f *.log`)
- [ ] Mac Cronjobs deaktiviert

---

**GitHub Deployment Setup erfolgreich! 🎉**

Jetzt kannst du einfach Code auf Mac entwickeln, `git push`, und auf VPS `git pull` - fertig!

Bei Fragen: Logs prüfen oder manuell testen.

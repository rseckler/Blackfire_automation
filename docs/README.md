# 🔄 Excel → Notion Sync Automation

Automatische tägliche Synchronisation von Excel-Daten (Dropbox) zu Notion Database mit vollständigem Logging.

## ✨ Features

- ✅ **Automatischer Download** von Excel-Datei aus Dropbox
- ✅ **Intelligentes Sync** - Updates + Creates in Notion
- ✅ **Property Type Detection** - Automatische Erkennung von Notion Property Types
- ✅ **Column Mapping** - Flexible Spalten-Zuordnung (Company_Name → Name)
- ✅ **Sync History** - Alle Syncs werden in Notion geloggt mit Statistiken
- ✅ **Cronjob Integration** - Tägliche Ausführung um 6:00 Uhr
- ✅ **Production-Ready** - Getestet mit 927 erfolgreichen Updates

## 📊 Sync Statistics

Typischer erfolgreicher Sync:
- **Excel Rows:** 1,608
- **Notion Pages:** 1,598
- **Updates:** ~927
- **Creates:** ~2
- **Duration:** ~10 Minuten
- **Success Rate:** 100%

## 🚀 Quick Start

### 1. Installation

```bash
# Repository klonen
git clone <your-repo-url>
cd Blackfire_automation

# Dependencies installieren
pip3 install -r requirements.txt
```

### 2. Konfiguration

```bash
# .env Datei erstellen (von Template kopieren)
cp .env.example .env

# .env mit deinen Credentials befüllen
nano .env
```

**Benötigte Credentials:**
- Notion API Key
- Notion Database ID (Haupt-Datenbank)
- Sync History Database ID
- Dropbox Share Link zur Excel-Datei

### 3. Manueller Test

```bash
# Ersten Sync manuell ausführen
python3 sync_final.py
```

### 4. Cronjob installieren

```bash
# Cronjob für tägliche Ausführung um 6:00 Uhr
bash install_cron.sh
```

## 📋 Dokumentation

- **[README_FINAL.md](README_FINAL.md)** - Vollständige Setup-Anleitung
- **[SYNC_HISTORY_GUIDE.md](SYNC_HISTORY_GUIDE.md)** - Sync History & Monitoring

## 🔐 Sicherheit

**WICHTIG:** Die `.env` Datei enthält sensitive Credentials und ist NICHT im Repository enthalten!

- Alle Credentials werden in `.env` gespeichert (git-ignored)
- Template: `.env.example` (ohne echte Credentials)
- Nie `.env` zu Git hinzufügen oder committen!

## 📊 Notion Databases

Das Projekt nutzt zwei Notion Databases:

1. **Haupt-Database** (`Aktien_Blackfire`) - Aktien/Buy Orders
2. **Sync History** (`Sync History - Excel → Notion`) - Automatisches Logging

## 🛠️ Technologie

- **Python 3.x** - Hauptsprache
- **pandas** - Excel-Verarbeitung
- **requests** - Notion API & Dropbox
- **python-dotenv** - Environment Variables

## 📈 Monitoring

### Notion (Empfohlen)
Öffne die **Sync History** Database in Notion:
- ✅ Status: Success?
- ✅ Updates: ~900+?
- ✅ Success_Rate: 100%?

### Log-Datei
```bash
tail -30 sync_cron.log
```

## ⚙️ Column Mapping

Excel → Notion Mapping:
```python
COLUMN_MAPPING = {
    'Company_Name': 'Name'
}
```

## 🐛 Troubleshooting

### Cronjob funktioniert nicht
```bash
# Cronjob prüfen
crontab -l

# Log-Datei prüfen
tail -30 sync_cron.log
```

### Sync-Fehler
```bash
# Manuell testen
python3 sync_final.py

# Notion API Zugriff prüfen
python3 test_notion_access.py
```

### Credentials-Probleme
```bash
# .env Datei prüfen
cat .env

# Alle Environment Variables vorhanden?
# - NOTION_API_KEY
# - NOTION_DATABASE_ID
# - SYNC_HISTORY_DB_ID
# - DROPBOX_URL
```

Siehe **[README_FINAL.md](README_FINAL.md)** für detaillierte Troubleshooting-Anleitung.

## 📝 License

Private Project

## 👤 Author

Robin Seckler

---

**Viel Erfolg mit der Automation! 🚀**

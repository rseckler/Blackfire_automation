# Lock-up System — VPS Cron-Setup

**Stand:** 2026-04-19 · Session 1 Deployment

## Neue Cron-Jobs für Lock-up-System

Hinzufügen zu `crontab -e` auf VPS (`72.62.148.205`):

```cron
# --- LOCK-UP SYSTEM v1 (2026-04-19) ---

# Einmalig: Altlasten-Cleanup (vor Session 1 ausgeführt, kann monatlich wiederholt werden)
0 4 1 * *   cd /root/Blackfire_automation && . venv/bin/activate && python3 lockup_cleanup.py --apply >> /root/Blackfire_automation/lockup_cleanup.log 2>&1

# SEC EDGAR S-1 Parser: 2x/Woche für neue Public-Firmen
0 8 * * 2,5 cd /root/Blackfire_automation && . venv/bin/activate && python3 sec_edgar_s1_parser.py --all-public --apply >> /root/Blackfire_automation/lockup_edgar.log 2>&1

# Finnhub IPO-Kalender Sync: täglich
15 8 * * *  cd /root/Blackfire_automation && . venv/bin/activate && python3 finnhub_ipo_sync.py --apply >> /root/Blackfire_automation/lockup_finnhub.log 2>&1

# SEC Form 144 Monitor: stündlich 09-20 UTC (US-Marktzeiten)
30 9-20 * * * cd /root/Blackfire_automation && . venv/bin/activate && python3 form_144_monitor.py --apply >> /root/Blackfire_automation/form144.log 2>&1
```

## Alte `lockup_scraper.py` bleibt bestehen

Der bestehende Mo-7-Uhr-Scraper via MarketBeat läuft weiter (als Fallback-Quelle via Auto-Calc). Neue Quellen ergänzen sich, überschreiben nicht: `source='sec_edgar_s1'` > `source='finnhub'` > `source='ipo_auto_calc'`.

## Benötigte .env-Variablen

```bash
# In /root/Blackfire_automation/.env
SEC_USER_AGENT="Blackfire Research (rseckler@gmail.com)"  # SEC fordert Identifizierung
FINNHUB_API_KEY=...                                        # Free Tier: https://finnhub.io/register
# ANTHROPIC_API_KEY bereits gesetzt
```

## Log-Rotation

Empfehlung: logrotate für die neuen Logs:

```bash
# /etc/logrotate.d/blackfire-lockup
/root/Blackfire_automation/lockup_*.log /root/Blackfire_automation/form144.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
```

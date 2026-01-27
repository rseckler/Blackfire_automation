#!/bin/bash

# Update morning cronjob to use complete morning sync

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SCRIPT_PATH="$SCRIPT_DIR/morning_sync_complete.py"
LOG_FILE="$SCRIPT_DIR/sync_cron.log"

echo "🔄 Updating morning cronjob..."
echo ""
echo "New script: morning_sync_complete.py"
echo "  - Step 1: Excel → Notion sync"
echo "  - Step 2: ISIN/WKN research"
echo ""

# Remove old sync_final cronjob
crontab -l 2>/dev/null | grep -v "sync_final.py" | crontab -

# Add new morning_sync_complete cronjob
CRON_ENTRY="0 6 * * * export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin && cd $SCRIPT_DIR && python3 $SCRIPT_PATH >> $LOG_FILE 2>&1"

(crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -

echo "✅ Cronjob updated successfully!"
echo ""
echo "Schedule: Daily at 6:00 AM"
echo "Log file: $LOG_FILE"
echo ""
echo "📋 View cronjob:"
echo "   crontab -l | grep morning_sync"
echo ""

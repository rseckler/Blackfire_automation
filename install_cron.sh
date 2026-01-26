#!/bin/bash
# Install Cronjob for daily sync at 6 AM

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTHON_PATH=$(which python3)
CRON_COMMAND="0 6 * * * cd $SCRIPT_DIR && $PYTHON_PATH sync_final.py >> $SCRIPT_DIR/sync_cron.log 2>&1"

echo "Installing cronjob..."
echo "Script directory: $SCRIPT_DIR"
echo "Python path: $PYTHON_PATH"
echo ""
echo "Cronjob command:"
echo "$CRON_COMMAND"
echo ""

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -q "sync_final.py"; then
    echo "⚠️  Cronjob already exists. Removing old one..."
    crontab -l 2>/dev/null | grep -v "sync_final.py" | crontab -
fi

# Add new cron job
(crontab -l 2>/dev/null; echo "$CRON_COMMAND") | crontab -

echo "✅ Cronjob installed!"
echo ""
echo "Verify with: crontab -l"
echo ""
echo "The sync will run daily at 6:00 AM"
echo "Log file: $SCRIPT_DIR/sync_cron.log"

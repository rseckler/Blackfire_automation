#!/bin/bash

# Install Stock Price Updater Cronjob
# Runs hourly from 7 AM to 11 PM

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SCRIPT_PATH="$SCRIPT_DIR/stock_price_updater.py"
LOG_FILE="$SCRIPT_DIR/stock_prices.log"

echo "📈 Installing Stock Price Updater Cronjob..."
echo ""
echo "Script: $SCRIPT_PATH"
echo "Log: $LOG_FILE"
echo ""

# Check if script exists
if [ ! -f "$SCRIPT_PATH" ]; then
    echo "❌ Error: stock_price_updater.py not found"
    exit 1
fi

# Check if .env exists
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "❌ Error: .env file not found"
    echo "   Please create .env file"
    exit 1
fi

# Make script executable
chmod +x "$SCRIPT_PATH"

# Create cronjob entry with full PATH and explicit shell
# Runs at minute 0 of every hour from 7 AM to 11 PM
CRON_ENTRY="0 7-23 * * * export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin && cd $SCRIPT_DIR && python3 $SCRIPT_PATH >> $LOG_FILE 2>&1"

# Check if cronjob already exists
if crontab -l 2>/dev/null | grep -q "stock_price_updater.py"; then
    echo "⚠️  Cronjob already exists. Updating..."
    # Remove old entry
    crontab -l 2>/dev/null | grep -v "stock_price_updater.py" | crontab -
fi

# Add new cronjob
(crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -

echo ""
echo "✅ Cronjob installed successfully!"
echo ""
echo "Schedule: Hourly from 7 AM to 11 PM (17 times per day)"
echo "Times: 07:00, 08:00, 09:00, ..., 22:00, 23:00"
echo ""
echo "📋 View cronjob:"
echo "   crontab -l | grep stock_price"
echo ""
echo "📊 Monitor updates:"
echo "   tail -f $LOG_FILE"
echo ""
echo "🧪 Test manually:"
echo "   python3 $SCRIPT_PATH"
echo ""
echo "🗑️  Remove cronjob:"
echo "   crontab -l | grep -v stock_price_updater.py | crontab -"
echo ""

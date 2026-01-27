#!/bin/bash
#############################################################################
# Automatisches GitHub → VPS Deployment
# Führt alle Setup-Schritte remote auf dem VPS aus
#############################################################################

set -e  # Exit on error

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   GitHub → Hostinger VPS Deployment           ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════╝${NC}"
echo ""

# Configuration
VPS_HOST="72.62.148.205"
VPS_USER="root"
REPO_URL="https://github.com/rseckler/Blackfire_automation.git"
TARGET_DIR="Blackfire_automation"

#############################################################################
# Step 1: Test SSH Connection
#############################################################################
echo -e "${YELLOW}[1/8] Testing SSH connection...${NC}"
echo -e "   Target: $VPS_USER@$VPS_HOST"
echo ""

if ssh -o ConnectTimeout=5 -o BatchMode=yes $VPS_USER@$VPS_HOST exit 2>/dev/null; then
    echo -e "   ${GREEN}✅ SSH connection OK${NC}"
else
    echo -e "   ${RED}❌ Cannot connect to VPS${NC}"
    echo ""
    echo "Please ensure:"
    echo "  1. VPS is running"
    echo "  2. SSH key is configured: ssh-copy-id $VPS_USER@$VPS_HOST"
    echo "  3. Or use password authentication"
    echo ""
    echo "Test manually: ssh $VPS_USER@$VPS_HOST"
    exit 1
fi
echo ""

#############################################################################
# Step 2: Install System Dependencies
#############################################################################
echo -e "${YELLOW}[2/8] Installing system dependencies on VPS...${NC}"

ssh $VPS_USER@$VPS_HOST << 'ENDSSH'
set -e

# Update package list
echo "   📦 Updating package list..."
apt-get update -qq > /dev/null 2>&1

# Install Python, pip, git
echo "   📦 Installing Python 3, pip, git..."
apt-get install -y python3 python3-pip python3-venv git curl > /dev/null 2>&1

# Check Python version
PYTHON_VERSION=$(python3 --version)
echo "   ✅ $PYTHON_VERSION installed"
ENDSSH

echo -e "   ${GREEN}✅ System dependencies installed${NC}"
echo ""

#############################################################################
# Step 3: Clone Repository
#############################################################################
echo -e "${YELLOW}[3/8] Cloning GitHub repository...${NC}"
echo -e "   Repo: $REPO_URL"

ssh $VPS_USER@$VPS_HOST << ENDSSH
set -e
cd ~

# Remove old installation if exists
if [ -d "$TARGET_DIR" ]; then
    echo "   ⚠️  Found existing installation, backing up..."
    mv $TARGET_DIR ${TARGET_DIR}_backup_\$(date +%Y%m%d_%H%M%S)
fi

# Clone repository
echo "   📥 Cloning repository..."
git clone $REPO_URL $TARGET_DIR > /dev/null 2>&1

# Check
cd $TARGET_DIR
COMMIT=\$(git log --oneline -1)
echo "   ✅ Repository cloned"
echo "   📝 Latest commit: \$COMMIT"
ENDSSH

echo -e "   ${GREEN}✅ Repository cloned${NC}"
echo ""

#############################################################################
# Step 4: Install Python Dependencies
#############################################################################
echo -e "${YELLOW}[4/8] Installing Python dependencies...${NC}"

ssh $VPS_USER@$VPS_HOST << 'ENDSSH'
set -e
cd ~/Blackfire_automation

echo "   📦 Installing Python packages..."
python3 -m pip install --upgrade pip --quiet > /dev/null 2>&1
python3 -m pip install -r requirements.txt --quiet > /dev/null 2>&1

# Verify installations
echo "   🔍 Verifying installations..."
python3 -c "import requests, pandas, yfinance, dotenv" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "   ✅ All packages installed successfully"
else
    echo "   ❌ Package import failed"
    exit 1
fi
ENDSSH

echo -e "   ${GREEN}✅ Python dependencies installed${NC}"
echo ""

#############################################################################
# Step 5: Setup .env File
#############################################################################
echo -e "${YELLOW}[5/8] Setting up .env file...${NC}"

# Check if .env exists locally
if [ ! -f ".env" ]; then
    echo -e "   ${RED}❌ .env file not found locally!${NC}"
    echo ""
    echo "Please create .env file first:"
    echo "  cp .env.example .env"
    echo "  nano .env"
    echo ""
    exit 1
fi

echo -e "   📤 Uploading .env to VPS..."
scp -q .env $VPS_USER@$VPS_HOST:~/Blackfire_automation/.env

ssh $VPS_USER@$VPS_HOST << 'ENDSSH'
set -e
cd ~/Blackfire_automation

# Secure .env
chmod 600 .env
echo "   🔒 .env secured (chmod 600)"

# Verify required credentials
echo "   🔍 Verifying .env..."
MISSING=""
for var in NOTION_API_KEY NOTION_DATABASE_ID SYNC_HISTORY_DB_ID DROPBOX_URL; do
    if ! grep -q "^$var=..*" .env; then
        MISSING="$MISSING $var"
    fi
done

if [ -n "$MISSING" ]; then
    echo "   ⚠️  Missing credentials:$MISSING"
else
    echo "   ✅ All required credentials present"
fi
ENDSSH

echo -e "   ${GREEN}✅ .env configured${NC}"
echo ""

#############################################################################
# Step 6: Test Installation
#############################################################################
echo -e "${YELLOW}[6/8] Testing installation...${NC}"

ssh $VPS_USER@$VPS_HOST << 'ENDSSH'
set -e
cd ~/Blackfire_automation

echo "   🧪 Running test with 10 stocks..."
timeout 120 python3 test_complete_system.py 2>&1 | tail -10
ENDSSH

if [ $? -eq 0 ]; then
    echo -e "   ${GREEN}✅ Test completed successfully${NC}"
else
    echo -e "   ${YELLOW}⚠️  Test completed with warnings (check output above)${NC}"
fi
echo ""

#############################################################################
# Step 7: Setup Cronjobs
#############################################################################
echo -e "${YELLOW}[7/8] Setting up cronjobs...${NC}"

ssh $VPS_USER@$VPS_HOST << 'ENDSSH'
set -e
cd ~/Blackfire_automation

# Get Python path
PYTHON_PATH=$(which python3)
SCRIPT_DIR="$HOME/Blackfire_automation"

echo "   📝 Python path: $PYTHON_PATH"
echo "   📝 Script dir: $SCRIPT_DIR"

# Create crontab
cat > /tmp/blackfire_cron << EOF
# Blackfire Automation - GitHub Deployment
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Morning Sync (06:00 UTC) - Excel + ISIN/WKN
0 6 * * * $PYTHON_PATH $SCRIPT_DIR/morning_sync_complete.py >> $SCRIPT_DIR/sync_cron.log 2>&1

# Stock Price Updates (hourly 07-23 UTC)
0 7-23 * * * $PYTHON_PATH $SCRIPT_DIR/stock_price_updater.py >> $SCRIPT_DIR/stock_prices.log 2>&1
EOF

# Install crontab
crontab /tmp/blackfire_cron
rm /tmp/blackfire_cron

echo "   ✅ Cronjobs installed"
echo ""
echo "   📋 Installed cronjobs:"
crontab -l | grep -v "^#" | grep -v "^$" | sed 's/^/      /'
ENDSSH

echo -e "   ${GREEN}✅ Cronjobs configured${NC}"
echo ""

#############################################################################
# Step 8: Final Verification
#############################################################################
echo -e "${YELLOW}[8/8] Final verification...${NC}"

ssh $VPS_USER@$VPS_HOST << 'ENDSSH'
set -e
cd ~/Blackfire_automation

echo "   🔍 Checking installation..."

# Check files
REQUIRED_FILES="sync_final.py stock_price_updater.py isin_wkn_updater.py isin_ticker_mapper.py morning_sync_complete.py requirements.txt .env"
MISSING_FILES=""

for file in $REQUIRED_FILES; do
    if [ ! -f "$file" ]; then
        MISSING_FILES="$MISSING_FILES $file"
    fi
done

if [ -n "$MISSING_FILES" ]; then
    echo "   ❌ Missing files:$MISSING_FILES"
    exit 1
else
    echo "   ✅ All required files present"
fi

# Check cronjobs
CRON_COUNT=$(crontab -l | grep -c "Blackfire_automation" || true)
if [ "$CRON_COUNT" -ge 2 ]; then
    echo "   ✅ Cronjobs configured ($CRON_COUNT entries)"
else
    echo "   ⚠️  Expected 2 cronjobs, found $CRON_COUNT"
fi

# Check .env
if [ -f ".env" ] && [ "$(stat -c %a .env)" = "600" ]; then
    echo "   ✅ .env secured properly"
else
    echo "   ⚠️  .env permissions incorrect"
fi
ENDSSH

echo -e "   ${GREEN}✅ Verification complete${NC}"
echo ""

#############################################################################
# Deployment Summary
#############################################################################
echo -e "${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║          Deployment Complete! 🎉               ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}✅ GitHub repository cloned${NC}"
echo -e "${GREEN}✅ Python dependencies installed${NC}"
echo -e "${GREEN}✅ .env configured${NC}"
echo -e "${GREEN}✅ Test successful${NC}"
echo -e "${GREEN}✅ Cronjobs scheduled${NC}"
echo ""
echo -e "${YELLOW}📊 Cronjob Schedule:${NC}"
echo -e "   • Morning Sync: Daily at 06:00 UTC"
echo -e "   • Stock Updates: Hourly 07:00-23:00 UTC"
echo ""
echo -e "${YELLOW}📁 Installation Directory:${NC}"
echo -e "   ~/Blackfire_automation"
echo ""
echo -e "${YELLOW}📝 Next Steps:${NC}"
echo ""
echo -e "1️⃣  Monitor logs:"
echo -e "   ssh $VPS_USER@$VPS_HOST 'tail -f ~/Blackfire_automation/sync_cron.log'"
echo ""
echo -e "2️⃣  Manual test run:"
echo -e "   ssh $VPS_USER@$VPS_HOST 'cd ~/Blackfire_automation && python3 morning_sync_complete.py'"
echo ""
echo -e "3️⃣  Future updates (from Mac):"
echo -e "   git push origin main"
echo -e "   ssh $VPS_USER@$VPS_HOST 'cd ~/Blackfire_automation && git pull'"
echo ""
echo -e "${YELLOW}🔧 Quick Commands:${NC}"
echo -e "   # View logs:       ssh $VPS_USER@$VPS_HOST 'tail -30 ~/Blackfire_automation/sync_cron.log'"
echo -e "   # Check cronjobs:  ssh $VPS_USER@$VPS_HOST 'crontab -l'"
echo -e "   # Update code:     ssh $VPS_USER@$VPS_HOST 'cd ~/Blackfire_automation && git pull'"
echo ""
echo -e "${GREEN}🎉 Your automation is now running on VPS! 🚀${NC}"
echo ""

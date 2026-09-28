#!/usr/bin/env bash
# Run on production VPS — initial setup + each redeploy
# Usage: bash deploy/deploy.sh

set -euo pipefail

APP_DIR="/opt/leadhunt"
APP_USER="leadhunt"

echo "═══ LeadHunt Production Deploy ═══"

cd "$APP_DIR"

# 1. Pull latest code
if [ -d ".git" ]; then
    echo "→ Pulling latest from git..."
    git pull --ff-only
fi

# 2. Activate venv
source venv/bin/activate

# 3. Install/update deps
echo "→ Installing requirements..."
pip install --upgrade pip
pip install -r requirements-prod.txt -q

# 4. Run migrations
echo "→ Running migrations..."
export DJANGO_SETTINGS_MODULE=core.settings_prod
python manage.py migrate --noinput

# 5. Collect static files
echo "→ Collecting static..."
python manage.py collectstatic --noinput --clear

# 6. Compile messages (if any translations)
# python manage.py compilemessages 2>/dev/null || true

# 7. Restart Gunicorn via systemd
echo "→ Restarting service..."
sudo systemctl restart leadhunt
sleep 2
sudo systemctl status leadhunt --no-pager | head -n 10

echo "✓ Deploy complete."
echo "  Visit https://$(grep ALLOWED_HOSTS .env | head -1 | cut -d= -f2 | cut -d, -f1)/"

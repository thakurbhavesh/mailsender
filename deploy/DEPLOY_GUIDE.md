# LeadHunt — Production Deployment Guide

Hybrid setup: **Scraper runs locally** (your home PC) + **Web app runs on a VPS**.
Callers log in from anywhere via the production URL.

---

## 🏗️ Architecture

```
LOCAL PC (Aap)                          VPS (production)
─────────────                           ─────────────────
• Selenium scraper                      • Gunicorn + Nginx
• SQLite (or local Postgres)            • PostgreSQL
• Push: python manage.py sync_to_prod   • Domain + HTTPS
                                         • Callers log in here
```

---

## Part 1 — One-Time VPS Setup

### 1.1 Get a VPS

Recommended for India:
| Provider | Plan | Cost |
|---|---|---|
| **Hetzner CX22** (Germany/US) | 2vCPU / 4GB / 40GB | €4.85/mo (~₹430) |
| **DigitalOcean** | Basic Droplet | $6/mo (~₹500) |
| **Hostinger VPS India** | KVM 1 | ₹399/mo |
| **AWS Lightsail Mumbai** | 2GB | $10/mo |

Pick Ubuntu 22.04 or 24.04 LTS.

### 1.2 Point Domain to VPS

In your domain DNS settings:
```
A     leadhunt.com       → <VPS IP>
A     www.leadhunt.com   → <VPS IP>
```
Wait 5-15 min for propagation. Test: `ping leadhunt.com` should show VPS IP.

### 1.3 Install Software (SSH into VPS as root)

```bash
# Update + basics
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git nginx postgresql postgresql-contrib \
               certbot python3-certbot-nginx ufw

# Firewall
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

# Create dedicated user
adduser --disabled-password --gecos '' leadhunt
mkdir -p /opt/leadhunt
chown leadhunt:leadhunt /opt/leadhunt

# Switch to that user
su - leadhunt
cd /opt/leadhunt
```

### 1.4 Setup PostgreSQL

```bash
# Back as root or sudo
sudo -u postgres psql <<EOF
CREATE USER leadhunt WITH PASSWORD 'CHANGE-THIS-STRONG-PASSWORD';
CREATE DATABASE leadhunt OWNER leadhunt;
GRANT ALL PRIVILEGES ON DATABASE leadhunt TO leadhunt;
ALTER USER leadhunt CREATEDB;  -- needed for migrations + future
EOF
```

### 1.5 Clone Code + Setup Python

```bash
# As leadhunt user
cd /opt/leadhunt
git clone <your-repo-url> .   # or rsync from local if no git
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements-prod.txt
```

### 1.6 Configure .env

```bash
cp deploy/.env.example .env
chmod 600 .env
nano .env
```

Set:
- `SECRET_KEY` — generate: `python -c "import secrets; print(secrets.token_urlsafe(50))"`
- `ALLOWED_HOSTS=leadhunt.com,www.leadhunt.com`
- `DB_PASSWORD` — from step 1.4
- `SYNC_API_TOKEN` — generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` (save this — needed on local PC)

### 1.7 Initial Migrate + Collect Static + Create Admin

```bash
export DJANGO_SETTINGS_MODULE=core.settings_prod
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser   # create your admin login
```

### 1.8 Setup Gunicorn + systemd

```bash
sudo cp deploy/leadhunt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable leadhunt
sudo systemctl start leadhunt
sudo systemctl status leadhunt   # should show 'active (running)'
```

### 1.9 Setup Nginx

```bash
sudo cp deploy/nginx-leadhunt.conf /etc/nginx/sites-available/leadhunt
# Edit to change leadhunt.com to your domain
sudo nano /etc/nginx/sites-available/leadhunt
sudo ln -s /etc/nginx/sites-available/leadhunt /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t              # test config
sudo systemctl reload nginx
```

### 1.10 Setup HTTPS (Let's Encrypt — free)

```bash
sudo certbot --nginx -d leadhunt.com -d www.leadhunt.com
# Pick option 2 (redirect HTTP→HTTPS). Auto-renews every 60 days.
```

### 1.11 Setup Daily Backup (cron)

```bash
sudo nano /etc/cron.daily/leadhunt-backup
```
Content:
```bash
#!/bin/bash
mkdir -p /var/backups/leadhunt
sudo -u postgres pg_dump leadhunt | gzip > /var/backups/leadhunt/db-$(date +%Y%m%d).sql.gz
find /var/backups/leadhunt -name "db-*.sql.gz" -mtime +30 -delete
```
```bash
sudo chmod +x /etc/cron.daily/leadhunt-backup
```

### ✅ Visit https://leadhunt.com — production is live.

---

## Part 2 — Move Local Data to Production (first time)

If you already have data locally that you want on production:

### Option A — Dump + Restore (simplest, one-time)

**On your local PC:**
```powershell
# Export everything to JSON
python manage.py dumpdata `
  --exclude auth.permission --exclude contenttypes.contenttype `
  --exclude sessions.session --indent 2 > backup.json
```

**Copy to VPS:**
```bash
scp backup.json leadhunt@your-vps:/opt/leadhunt/
```

**On VPS:**
```bash
cd /opt/leadhunt
source venv/bin/activate
export DJANGO_SETTINGS_MODULE=core.settings_prod
python manage.py loaddata backup.json
```

### Option B — Use Sync API (continuous, recommended)

Once production is up, you keep scraping locally and push leads continuously.

**On local PC (Windows PowerShell):**
```powershell
$env:PROD_URL = "https://leadhunt.com"
$env:PROD_SYNC_TOKEN = "<paste the SYNC_API_TOKEN from VPS .env>"

# First time: push everything
python manage.py sync_to_prod --all

# After that: incremental (only updated leads since last sync)
python manage.py sync_to_prod
```

The command remembers the last sync timestamp in `.sync_state.json`. Next runs only push what's new.

---

## Part 3 — Daily Workflow

```
Morning:
  Local PC → Run scraper for "Mumbai restaurants"
            → 500 new leads saved locally

Anytime:
  python manage.py sync_to_prod
  → Pushes 500 new leads to production in 30-60 seconds

Callers (on production):
  → Log in at https://leadhunt.com
  → See new leads in Data Explorer
  → Admin assigns leads via "Assign Leads" page
  → Callers work them on caller workspace
```

### Optional — Schedule auto-sync

**On local PC (Windows Task Scheduler):**
- Trigger: Every 30 minutes
- Action: Run PowerShell:
  ```powershell
  $env:PROD_URL = "https://leadhunt.com"
  $env:PROD_SYNC_TOKEN = "..."
  cd C:\Users\desig\OneDrive\Desktop\Scrap\maps_dashboard
  & .\venv\Scripts\python.exe manage.py sync_to_prod
  ```

**On Linux/Mac (cron):**
```cron
*/30 * * * * cd /path/to/leadhunt && PROD_URL=... PROD_SYNC_TOKEN=... python manage.py sync_to_prod
```

---

## Part 4 — Updating Production Code

When you change code locally and want to push to prod:

```bash
# Local: commit + push to git
git add . && git commit -m "feature: ..." && git push

# VPS: deploy
ssh leadhunt@your-vps
cd /opt/leadhunt
bash deploy/deploy.sh
```

That script pulls git, migrates DB, collects static, restarts service.

---

## Part 5 — Troubleshooting

| Symptom | Fix |
|---|---|
| 502 Bad Gateway | `sudo systemctl status leadhunt` — likely Python error. Check `sudo journalctl -u leadhunt -n 50` |
| 500 Server Error | Check `/opt/leadhunt/logs/django.log` |
| CSRF errors | Check `CSRF_TRUSTED_ORIGINS` in .env includes `https://leadhunt.com` |
| Static files 404 | Re-run `python manage.py collectstatic --noinput` |
| Database errors | Check Postgres: `sudo systemctl status postgresql` |
| Sync API 401 | Token mismatch — check `SYNC_API_TOKEN` (VPS) == `PROD_SYNC_TOKEN` (local) |
| Sync API 0 (connection failed) | Domain DNS not propagated, or firewall blocking 443 |

### Useful commands

```bash
# Restart app
sudo systemctl restart leadhunt

# View logs
sudo journalctl -u leadhunt -f
tail -f /opt/leadhunt/logs/django.log

# Open Django shell on prod
cd /opt/leadhunt && source venv/bin/activate
export DJANGO_SETTINGS_MODULE=core.settings_prod
python manage.py shell

# Backup DB manually
sudo -u postgres pg_dump leadhunt > backup-$(date +%F).sql

# Restore DB
sudo -u postgres psql leadhunt < backup-2026-05-23.sql
```

---

## Part 6 — Security Checklist

- [x] `DEBUG = False` in production
- [x] `SECRET_KEY` from env, not committed
- [x] HTTPS enforced (HSTS 1 year)
- [x] `SESSION_COOKIE_SECURE = True`
- [x] `CSRF_COOKIE_SECURE = True`
- [x] `SECURE_PROXY_SSL_HEADER` set for Nginx
- [x] PostgreSQL bound to localhost only
- [x] Dedicated `leadhunt` user (no sudo)
- [x] systemd hardening (`NoNewPrivileges`, `ProtectSystem`, etc.)
- [x] Firewall (UFW): only 22, 80, 443 open
- [x] Auto-renew Let's Encrypt
- [x] Daily DB backup
- [ ] Setup Sentry (optional but recommended)
- [ ] Configure UFW rate-limiting for SSH
- [ ] Setup fail2ban for SSH brute-force protection

---

**Done.** Aapka platform live hai. Callers laptop/phone se kaam kar sakte hain.
Scraper still runs on your home PC — push button (or scheduled task) sends new leads to production.

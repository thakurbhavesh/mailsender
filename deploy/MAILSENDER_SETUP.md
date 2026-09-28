# Deploy LeadHunt → `mailsender.vvmtechnologies.com`

Target server: **66.116.249.96** (Ubuntu 22, WebHostBox VPS, also hosts `thechatnest.com`)
Repo: `https://github.com/thakurbhavesh/mailsender.git`
DNS: `mailsender` A record → `66.116.249.96` ✅ already set (TTL 14400)

---

## Step 0 — Server ki maujooda haalat (check ho chuki hai)

`ssh chatnest` se verify kiya gaya:

| Cheez | Haalat |
|---|---|
| OS | Ubuntu 22.04.5 LTS |
| nginx | **already chal raha hai** — dobara install mat karna |
| cPanel / Apache | nahi hai |
| PostgreSQL | installed + active |
| certbot | installed |
| Python | 3.10.12 |
| Disk | 81 GB free |

**Pehle se chalne wale sites** (inhe chhedna nahi):
`thechatnest`, `visitorconnect`, `vvmtechnologies` (Next.js on :3000)

**Do zaroori baatein:**
1. **Port 8001 free nahi hai** — `daphne` us par chal raha hai. Isliye gunicorn
   **8011** par bind hoga (`GUNICORN_BIND` in `.env`).
2. `vvmtechnologies.com` vhost ka `server_name` wildcard nahi hai, isliye
   `mailsender.vvmtechnologies.com` ke liye alag block safe hai.

---

## Step 1 — System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.10 python3.10-venv python3-pip \
                    postgresql postgresql-contrib \
                    nginx certbot python3-certbot-nginx git curl
```

## Step 2 — App user + directory

```bash
sudo adduser --disabled-password --gecos '' leadhunt
sudo mkdir -p /opt/leadhunt
sudo chown leadhunt:leadhunt /opt/leadhunt
```

## Step 3 — PostgreSQL

```bash
sudo -u postgres psql
```
```sql
CREATE DATABASE leadhunt;
CREATE USER leadhunt WITH PASSWORD 'YAHAN-STRONG-PASSWORD-DAALO';
ALTER ROLE leadhunt SET client_encoding TO 'utf8';
ALTER ROLE leadhunt SET default_transaction_isolation TO 'read committed';
ALTER ROLE leadhunt SET timezone TO 'Asia/Kolkata';
GRANT ALL PRIVILEGES ON DATABASE leadhunt TO leadhunt;
\c leadhunt
GRANT ALL ON SCHEMA public TO leadhunt;
\q
```

## Step 4 — Code + venv

```bash
sudo -u leadhunt -i
cd /opt/leadhunt
git clone https://github.com/thakurbhavesh/mailsender.git .
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements-prod.txt
```

## Step 5 — `.env` banao

```bash
cp deploy/.env.example .env
chmod 600 .env
nano .env
```

Ye values bharo (domain already sahi hai):

```ini
SECRET_KEY=<python3 -c "import secrets;print(secrets.token_urlsafe(50))">
ALLOWED_HOSTS=mailsender.vvmtechnologies.com
CSRF_TRUSTED_ORIGINS=https://mailsender.vvmtechnologies.com
TRACKING_BASE_URL=https://mailsender.vvmtechnologies.com

DB_NAME=leadhunt
DB_USER=leadhunt
DB_PASSWORD=<Step 3 wala password>
DB_HOST=localhost
DB_PORT=5432

SYNC_API_TOKEN=<python3 -c "import secrets;print(secrets.token_urlsafe(48))">
```

Phir migrate + admin banao:

```bash
export DJANGO_SETTINGS_MODULE=core.settings_prod
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
exit          # leadhunt user se bahar
```

## Step 6 — Nginx

```bash
sudo cp /opt/leadhunt/deploy/nginx-leadhunt.conf /etc/nginx/sites-available/leadhunt
sudo ln -s /etc/nginx/sites-available/leadhunt /etc/nginx/sites-enabled/
sudo mkdir -p /var/www/certbot
sudo nginx -t
```

`nginx -t` fail hoga kyunki SSL cert abhi nahi hai — pehle cert lo:

```bash
sudo certbot --nginx -d mailsender.vvmtechnologies.com \
     --agree-tos -m it@aabhyasa.com --redirect
sudo nginx -t && sudo systemctl reload nginx
```

## Step 7 — Gunicorn service

```bash
sudo cp /opt/leadhunt/deploy/leadhunt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now leadhunt
sudo systemctl status leadhunt --no-pager
```

Problem aaye to: `sudo journalctl -u leadhunt -n 50 --no-pager`

## Step 8 — Verify

```bash
curl -I https://mailsender.vvmtechnologies.com/login/        # 200 aana chahiye
curl -I https://mailsender.vvmtechnologies.com/t/o/test.gif  # 200, image/gif

# Baaki teen sites abhi bhi zinda hain?
curl -I https://thechatnest.com
curl -I https://vvmtechnologies.com
```

Browser mein `https://mailsender.vvmtechnologies.com/leads/` — Lead Management page khulna chahiye.

---

## Har baar redeploy

```bash
sudo -u leadhunt -i
cd /opt/leadhunt && bash deploy/deploy.sh
```

---

## Email tracking ke baare mein

Is subdomain se mail **bhejta** nahi hai — bhejna Gmail SMTP se hota hai
(`EmailAccount` + app password). Ye subdomain sirf **tracking domain** hai:
open pixel `https://mailsender.vvmtechnologies.com/t/o/<token>.gif` aur
click links `/t/c/<token>/` yahin se serve honge, isi liye
`TRACKING_BASE_URL` set karna zaroori tha.

Deliverability ke liye alag se `vvmtechnologies.com` par SPF/DKIM chahiye —
wo Gmail ke records honge, is IP ke nahi.

## Pehla kaam deploy ke baad

Local `db.sqlite3` mein jo Gmail app password aur Gemini API key padi hai, wo
git mein nahi gayi — server par UI se dobara daalni hongi:

- `/email/accounts/` → Gmail account + app password
- `/ai/settings/` → Gemini API key

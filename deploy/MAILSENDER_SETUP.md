# Deploy LeadHunt → `mailsender.vvmtechnologies.com`

Target server: **66.116.249.96** (Ubuntu 22, WebHostBox VPS, also hosts `thechatnest.com`)
Repo: `https://github.com/thakurbhavesh/mailsender.git`
DNS: `mailsender` A record → `66.116.249.96` ✅ already set (TTL 14400)

---

## ⚠️ Step 0 — Check karo ki port 80/443 free hai

Is server par `thechatnest.com` pehle se chal raha hai. Agar wahan cPanel/Apache hai, to
nginx install karte hi conflict hoga **aur thechatnest.com down ho jayega**. Pehle ye chalao:

```bash
ssh root@66.116.249.96          # ya jo user aapne banaya hai

sudo ss -tlnp | grep -E ':80|:443'
which apache2 httpd nginx
ls /usr/local/cpanel 2>/dev/null && echo "⚠️ cPanel server hai"
```

**Result ke hisaab se:**

| Kya mila | Kya karo |
|---|---|
| Kuch nahi (ports free) | Neeche Step 1 se aage badho — nginx install karo |
| `nginx` chal raha hai | nginx install skip karo, sirf server block add karo (Step 6) |
| `apache2` / cPanel chal raha hai | **nginx mat install karo.** Apache vhost banao (neeche "Apache variant" dekho) |

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
curl -I http://thechatnest.com                               # abhi bhi zinda hai?
```

Browser mein `https://mailsender.vvmtechnologies.com/leads/` — Lead Management page khulna chahiye.

---

## Har baar redeploy

```bash
sudo -u leadhunt -i
cd /opt/leadhunt && bash deploy/deploy.sh
```

---

## Apache variant (agar server pe cPanel/Apache hai)

nginx skip karo. `/etc/apache2/sites-available/mailsender.conf`:

```apache
<VirtualHost *:80>
    ServerName mailsender.vvmtechnologies.com
    Redirect permanent / https://mailsender.vvmtechnologies.com/
</VirtualHost>

<VirtualHost *:443>
    ServerName mailsender.vvmtechnologies.com

    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/mailsender.vvmtechnologies.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/mailsender.vvmtechnologies.com/privkey.pem

    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass        /static/ !
    Alias /static/ /opt/leadhunt/staticfiles/
    <Directory /opt/leadhunt/staticfiles>Require all granted</Directory>
    ProxyPass        / http://127.0.0.1:8001/
    ProxyPassReverse / http://127.0.0.1:8001/
    ProxyTimeout 180
</VirtualHost>
```

```bash
sudo a2enmod proxy proxy_http ssl headers
sudo certbot --apache -d mailsender.vvmtechnologies.com
sudo a2ensite mailsender && sudo systemctl reload apache2
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

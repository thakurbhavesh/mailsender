# Deploy to Render (Easy Mode — Recommended)

Render = managed PaaS. Auto-deploys from git push. No SSH, no Nginx, no systemd. **~10 minutes** to live.

---

## Cost

| Service | Plan | Monthly |
|---|---|---|
| Web service | Starter | **$7** |
| PostgreSQL | Starter (1GB, daily backups) | **$7** |
| Custom domain | Free (you bring your own) | $0 |
| HTTPS | Free auto-SSL | $0 |
| **Total** | | **~$14/month (~₹1170)** |

Or **Free tier** for testing:
- Web: spins down after 15 min idle (slow first request)
- DB: free for 90 days, then must upgrade or migrate

---

## Steps

### 1. Push code to GitHub

```bash
cd C:\Users\desig\OneDrive\Desktop\Scrap\maps_dashboard
git init               # if not already
git add .
git commit -m "production-ready deploy"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/leadhunt.git
git push -u origin main
```

> ⚠️ **Important:** Make sure your `.gitignore` excludes `db.sqlite3`, `media/`, `.env`, `venv/`. Already in the repo if you copied my files.

### 2. Create Render account

Go to **render.com** → Sign up with GitHub (so it can read your repo).

### 3. Deploy via Blueprint (one-click)

In Render dashboard:
1. Click **New +** → **Blueprint**
2. Connect your `leadhunt` repository
3. Render auto-detects `render.yaml` and proposes:
   - Web Service: `leadhunt` ($7/mo)
   - PostgreSQL: `leadhunt-db` ($7/mo)
4. Click **Apply**

It will:
- Create the Postgres database
- Set `DATABASE_URL` env var automatically
- Generate `SECRET_KEY` and `SYNC_API_TOKEN` automatically
- Run `build.sh` (installs deps, migrates, collects static)
- Start Gunicorn

Wait 3-5 minutes. You'll get a URL like:
```
https://leadhunt.onrender.com
```

### 4. Create your admin user

In Render dashboard → leadhunt service → **Shell** (top right):

```bash
python manage.py createsuperuser
# username: bhavesh
# email: ...
# password: ...
```

### 5. Visit your site

`https://leadhunt.onrender.com/login/` — login with the user you just created.

### 6. Get your sync token

Render Dashboard → leadhunt service → **Environment** → copy the value of `SYNC_API_TOKEN`.

### 7. Configure local sync (on your PC)

```powershell
# In project folder
$env:PROD_URL = "https://leadhunt.onrender.com"
$env:PROD_SYNC_TOKEN = "<paste token from step 6>"

# First time: push everything
python manage.py sync_to_prod --all

# Daily: incremental
python manage.py sync_to_prod
```

To make these env vars persistent on Windows:
1. Win+R → `sysdm.cpl`
2. Advanced → Environment Variables
3. New User Variable: `PROD_URL` = `https://leadhunt.onrender.com`
4. New User Variable: `PROD_SYNC_TOKEN` = `<token>`

### 8. (Optional) Custom domain

In Render dashboard → leadhunt service → **Settings** → Custom Domains → Add `leadhunt.com`.

Render gives you DNS records to add. Update your domain registrar. SSL is automatic.

Then update env var:
- `ALLOWED_HOSTS` = `leadhunt.com,leadhunt.onrender.com`
- `CSRF_TRUSTED_ORIGINS` = `https://leadhunt.com,https://leadhunt.onrender.com`

---

## Migrating existing local data to Render

If you already have leads on local SQLite:

```powershell
# Local: export
python manage.py dumpdata `
  --exclude auth.permission --exclude contenttypes.contenttype `
  --exclude sessions.session --indent 2 > backup.json
```

Then in Render Shell:
```bash
# Upload backup.json via Render dashboard → Disks (if you added a disk) OR
# Use the sync API instead (easier):
```

**Easier: use sync_to_prod** (already incremental + safe):
```powershell
python manage.py sync_to_prod --all
```

This pushes all leads via the API. No file upload needed.

---

## Updating later

Every time you `git push` to your `main` branch, Render **auto-deploys**. That's it.

```powershell
# Local
git add .
git commit -m "added new feature"
git push
```

Render will:
1. Detect the push
2. Run `build.sh`
3. Restart Gunicorn
4. Switch traffic to new version (zero downtime)

Watch progress in Render dashboard → leadhunt → **Logs**.

---

## Database migrations

Migrations run **automatically** during `build.sh` on every deploy. You don't need to do anything special.

If a migration is risky (e.g., big data change), test locally first:
```bash
python manage.py migrate --plan      # shows what will run
python manage.py migrate              # apply
```

---

## Troubleshooting

| Issue | Fix |
|---|---|
| Build fails | Render Dashboard → Logs → check error. Usually missing dep in `requirements-prod.txt` |
| App returns 500 | Logs → look for Python traceback |
| Database connection refused | Render Postgres is in same region — should auto-work. Check `DATABASE_URL` env |
| Static files 404 | Make sure `build.sh` runs `collectstatic` (it does) |
| Login CSRF error | Add your domain to `CSRF_TRUSTED_ORIGINS` env var |
| Sync API 401 | Token mismatch — copy from Render Dashboard → Environment again |

---

## Render free vs starter

**Free tier** ($0):
- 750 hours/month (plenty for 1 service)
- Spins down after 15 min idle
- First request after sleep takes 30-60 sec
- Good for: testing, demo, internal-only use

**Starter** ($7):
- Always on
- 512 MB RAM / 0.5 CPU
- Fast responses
- Good for: live customer-facing use

Start with **Free**. Upgrade when you have callers actively working.

---

## Daily workflow on Render

```
1. Morning — Local PC
   python core/scrape some businesses (Selenium)
   → 200 new leads in local SQLite

2. Any time
   python manage.py sync_to_prod
   → Pushes incrementally to https://leadhunt.onrender.com
   → 200 leads created, 0 errors

3. Callers (anywhere)
   Open https://leadhunt.onrender.com on phone/laptop
   Login, see new leads, start calling

4. Evening — Caller activity stays on production
   Conversions, notes, callbacks all on prod DB
   Local DB never gets these (and shouldn't — see SYNC_STRATEGY.md)
```

That's the workflow. Local for scraping, prod for everything else.

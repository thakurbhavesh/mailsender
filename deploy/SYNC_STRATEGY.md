# Sync Strategy — Avoiding Conflicts

## TL;DR

```
LOCAL  ────► PROD     (new leads only — one way)
LOCAL  ✗◄── PROD      (callers' edits never come back to local)
```

**Local is a SCRAPING STAGING AREA. Production is the SOURCE OF TRUTH for live data.**

---

## How conflicts are prevented

When you push a lead that **already exists** on production (same `unique_key`):

### Production side (`scraper/sync_api.py`):

```python
if uk in existing:           # lead already on prod
    p = existing[uk]
    for field, new_value in incoming_fields.items():
        if new_value and not getattr(p, field, None):
            # Only fill empty fields — NEVER overwrite caller-edited data
            setattr(p, field, new_value)
    p.times_seen += 1
```

### What this means in practice:

| Scenario | Local pushes | Prod has | Result |
|---|---|---|---|
| Lead is brand new | name, phone, ... | nothing | **Created on prod** |
| Same business scraped again | phone, address | phone="987...", caller added notes | **Skipped** — empty fields stay empty, caller notes untouched |
| Scraper found a new email | email="new@..." | email="" (caller didn't have it) | **Email added** (was empty) |
| Lead converted by caller | phone="...", lead_status="new" | lead_status="converted" | **lead_status preserved** (never pushed by sync) |

---

## Fields that sync NEVER touches on production

The API deliberately excludes these from updates:

- `lead_status` — caller-controlled (new/contacted/interested/converted/rejected)
- `notes` — caller-edited
- `lead_score` — recalculated on prod from current data
- `times_seen` — incremented by prod
- `created_at` — preserved
- Any caller-added fields

---

## "But local DB will get stale!"

**Correct, and that's by design.** After you push a lead to prod, the local copy is just a historical record.

If you scrape the SAME business 30 days later:
- Locally: you might see the old lead with no caller updates
- On prod: callers have probably already worked it (marked converted, added notes)
- Push it: prod sees `unique_key` match → skips ALL caller-edited fields → only adds anything genuinely new (rare)

So you NEVER overwrite production data with stale local data.

---

## Recommended workflows

### Option 1 — Keep local "lite" (recommended)

After every successful push, **delete local Place rows** to keep things clean:

```powershell
# Push to prod
python manage.py sync_to_prod

# (Optional) purge synced leads from local
python manage.py shell -c "
from scraper.models import Place
deleted, _ = Place.objects.filter(created_at__lte='2026-05-23').delete()
print(f'Removed {deleted} synced leads')
"
```

Local DB becomes a temporary scraping pad. Production becomes the only place where leads live.

### Option 2 — Keep local as historical archive

Don't delete. Just understand that local data is "frozen at scrape time" and out of date for caller activity. Local view shows leads as they were when scraped.

### Option 3 — Two-way pull (advanced, not built yet)

If you genuinely need local to reflect production state, build a `pull_from_prod` command:

```python
# scraper/management/commands/pull_from_prod.py
# Calls a new API /api/sync/pull/?since=<timestamp>
# Updates local lead_status, notes, lead_score from prod
```

I can build this if you want. But Option 1 is cleaner.

---

## Code conflict (different topic)

If you update CODE locally and prod also has changes:
- Use **git** properly: `git pull --rebase` before pushing
- Migrations: never edit a migration that's already on prod. Always create a new one
- Settings: use `core/settings_prod.py` for prod-only changes, keep `core/settings.py` as base
- `.env` file on prod is NEVER committed to git — production-only secrets stay on the server

---

## TL;DR cheat sheet

| What | Where |
|---|---|
| Scraper code | Local |
| New leads | Local → Prod (via sync) |
| Caller workspace | Production only |
| Notes, statuses, calls | Production only |
| Models / migrations | Git → Both sides update from git |
| Source of truth for leads | **Production** (after first sync) |
| Source of truth for code | **Git repo** |

You will never lose data. The sync is **append-only and additive** — it can only create new leads or fill missing fields, never overwrite caller work.

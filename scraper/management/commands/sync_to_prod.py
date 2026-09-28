"""Push local leads to production server via /api/sync/leads/.

Usage:
    python manage.py sync_to_prod
    python manage.py sync_to_prod --batch-size 200
    python manage.py sync_to_prod --source google_maps
    python manage.py sync_to_prod --since 2026-05-20
    python manage.py sync_to_prod --all              # push everything (slow)
    python manage.py sync_to_prod --dry-run          # show what would be sent

Environment variables (set on LOCAL machine):
    PROD_URL          = https://your-domain.com
    PROD_SYNC_TOKEN   = same token set on production as SYNC_API_TOKEN
"""
import json
import os
import sys
import time
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from scraper.models import Place, ScrapeJob

# Force UTF-8 on Windows so emoji + Hindi don't crash the cmd codec
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

try:
    import urllib.request
    import urllib.error
except ImportError:
    urllib = None


SYNC_STATE_FILE = '.sync_state.json'


def _load_state():
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_state(state):
    try:
        with open(SYNC_STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def _post_json(url, payload, token, timeout=60):
    """POST JSON body with Bearer token. Returns (status, body_dict_or_text)."""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}',
            'User-Agent': 'LeadHunt-Sync/1.0',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace') if hasattr(e, 'read') else str(e)
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except urllib.error.URLError as e:
        return 0, str(e)


def _serialize_place(p):
    """Convert a Place to API-friendly dict."""
    return {
        'unique_key': p.unique_key,
        'name': p.name,
        'phone': p.phone,
        'email': p.email,
        'website': p.website,
        'address': p.address,
        'category': p.category,
        'rating': p.rating,
        'reviews_count': p.reviews_count,
        'description': p.description,
        'facebook': p.facebook,
        'instagram': p.instagram,
        'linkedin': p.linkedin,
        'opening_hours': p.opening_hours,
        'place_url': p.place_url,
        'plus_code': p.plus_code,
        'photos_count': p.photos_count,
        'latitude': p.latitude,
        'longitude': p.longitude,
        'extra_data': p.extra_data or {},
    }


class Command(BaseCommand):
    help = 'Push local leads to production via /api/sync/leads/'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=300,
                            help='Leads per API call (default 300, max 2000)')
        parser.add_argument('--source', type=str, default='',
                            help='Filter by source (google_maps/justdial/indiamart/upload/...)')
        parser.add_argument('--since', type=str, default='',
                            help='Only push leads created/updated on or after YYYY-MM-DD')
        parser.add_argument('--all', action='store_true',
                            help='Push EVERY lead (ignore last-sync timestamp)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without actually pushing')
        parser.add_argument('--url', type=str, default='', help='Override PROD_URL')
        parser.add_argument('--token', type=str, default='', help='Override PROD_SYNC_TOKEN')

    def handle(self, *args, **opts):
        url = (opts['url'] or os.environ.get('PROD_URL', '')).rstrip('/')
        token = opts['token'] or os.environ.get('PROD_SYNC_TOKEN', '')

        if not url or not token:
            raise CommandError(
                'Missing PROD_URL or PROD_SYNC_TOKEN.\n'
                'Set them as env vars or pass --url / --token flags.\n'
                'Example: $env:PROD_URL="https://leadhunt.com"; $env:PROD_SYNC_TOKEN="xyz"'
            )

        # Ping first to verify connectivity + token
        self.stdout.write(self.style.NOTICE(f'🌐 Pinging {url}/api/sync/ping/ ...'))
        status, body = _post_json(f'{url}/api/sync/ping/', {}, token, timeout=15)
        if status != 200:
            raise CommandError(f'Ping failed: HTTP {status}\n  Body: {body}')
        self.stdout.write(self.style.SUCCESS(
            f'✓ Connected. Production has {body.get("total_leads", "?")} leads. Server time: {body.get("server_time")}'
        ))

        # Build queryset
        state = _load_state()
        last_sync = state.get('last_sync_iso')

        qs = Place.objects.all()
        if opts['source']:
            qs = qs.filter(source=opts['source'])
        if opts['since']:
            try:
                d = datetime.strptime(opts['since'], '%Y-%m-%d').date()
                qs = qs.filter(updated_at__date__gte=d)
            except ValueError:
                raise CommandError('--since must be YYYY-MM-DD')
        elif not opts['all'] and last_sync:
            try:
                last_dt = datetime.fromisoformat(last_sync)
                if timezone.is_naive(last_dt):
                    last_dt = timezone.make_aware(last_dt)
                qs = qs.filter(updated_at__gte=last_dt)
                self.stdout.write(self.style.NOTICE(
                    f'⏱  Incremental sync — leads updated since {last_dt}'
                ))
            except ValueError:
                pass

        qs = qs.order_by('updated_at')
        total = qs.count()
        if not total:
            self.stdout.write(self.style.WARNING('Nothing to sync.'))
            return

        self.stdout.write(self.style.HTTP_INFO(f'📦 {total} leads to push'))

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('🧪 DRY RUN — showing first 5:'))
            for p in qs[:5]:
                self.stdout.write(f'  - {p.name[:40]} | {p.phone} | {p.source}')
            self.stdout.write(self.style.WARNING('Run without --dry-run to actually push.'))
            return

        batch_size = max(1, min(2000, opts['batch_size']))
        total_created = total_updated = total_skipped = total_errors = 0
        latest_updated_at = None
        offset = 0
        batch_num = 0

        start_time = time.time()
        while offset < total:
            batch_num += 1
            chunk = list(qs[offset:offset + batch_size])
            if not chunk:
                break
            offset += len(chunk)

            payload = {
                'source': opts['source'] or chunk[0].source or 'sync',
                'batch_label': f'Sync batch #{batch_num} ({timezone.now():%Y-%m-%d %H:%M})',
                'leads': [_serialize_place(p) for p in chunk],
            }
            self.stdout.write(
                f'  → Batch {batch_num}: pushing {len(chunk)} leads ({offset}/{total})...',
                ending='',
            )
            self.stdout.flush()
            status, body = _post_json(f'{url}/api/sync/leads/', payload, token, timeout=120)

            if status != 200 or not isinstance(body, dict) or not body.get('ok'):
                self.stdout.write(self.style.ERROR(f' ✗ FAILED: HTTP {status}, body: {body}'))
                self.stdout.write(self.style.ERROR(
                    f'⚠ Stopped at batch {batch_num}. Re-run to continue.'
                ))
                break

            c = body.get('created', 0)
            u = body.get('updated', 0)
            s = body.get('skipped', 0)
            e_list = body.get('errors', []) or []
            total_created += c
            total_updated += u
            total_skipped += s
            total_errors += len(e_list)

            # Track the latest updated_at in this batch
            for p in chunk:
                if not latest_updated_at or p.updated_at > latest_updated_at:
                    latest_updated_at = p.updated_at

            self.stdout.write(self.style.SUCCESS(
                f' ✓ +{c} new, ↻{u} updated, ⏭{s} skip, ⚠{len(e_list)} err'
            ))

        elapsed = time.time() - start_time

        # Save state
        if latest_updated_at:
            state['last_sync_iso'] = latest_updated_at.isoformat()
            state['last_sync_at'] = timezone.now().isoformat()
            _save_state(state)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'═══ SYNC COMPLETE ({elapsed:.1f}s) ═══\n'
            f'  Created: {total_created}\n'
            f'  Updated: {total_updated}\n'
            f'  Skipped: {total_skipped}\n'
            f'  Errors:  {total_errors}\n'
        ))
        if latest_updated_at:
            self.stdout.write(f'  Next incremental sync will start from: {latest_updated_at}')

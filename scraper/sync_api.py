"""Production sync API — accepts lead batches from local scraper.

Auth: Bearer token in `Authorization: Bearer <SYNC_TOKEN>` header.
Set the token via env var SYNC_API_TOKEN on production server.
"""
import json
import os
import hmac
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db import transaction

from .models import Place, ScrapeJob


def _get_token():
    """Get sync token from env (set SYNC_API_TOKEN on production server)."""
    return os.environ.get('SYNC_API_TOKEN', '').strip()


def _check_auth(request):
    """Validate Bearer token. Returns True/False."""
    expected = _get_token()
    if not expected:
        return False  # Not configured = reject all
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return False
    received = auth[7:].strip()
    return hmac.compare_digest(expected, received)


@csrf_exempt
@require_POST
def sync_leads(request):
    """Accept a batch of leads from local scraper.

    POST body (JSON):
    {
        "source": "google_maps",
        "batch_label": "Mumbai restaurants 2026-05-23",
        "leads": [
            {
                "unique_key": "...",          # required
                "name": "Sunshine Hotel",     # required
                "phone": "9876543210",
                "email": "info@hotel.com",
                "website": "https://...",
                "address": "...",
                "category": "...",
                "rating": "4.5",
                "reviews_count": 123,
                "description": "...",
                "facebook": "...",
                "instagram": "...",
                "linkedin": "...",
                "latitude": 12.97, "longitude": 77.59,
                "opening_hours": "...",
                "place_url": "...",
                "extra_data": {...}
            },
            ...
        ]
    }

    Response: { ok, created, updated, skipped, errors[] }
    """
    if not _check_auth(request):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=401)

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError as e:
        return JsonResponse({'ok': False, 'error': f'Invalid JSON: {e}'}, status=400)

    source = (data.get('source') or 'sync').strip()[:30]
    batch_label = (data.get('batch_label') or f'Sync {timezone.now():%Y-%m-%d %H:%M}').strip()[:255]
    leads = data.get('leads') or []

    if not isinstance(leads, list):
        return JsonResponse({'ok': False, 'error': 'leads must be a list'}, status=400)
    if len(leads) > 2000:
        return JsonResponse({'ok': False, 'error': 'Max 2000 leads per batch'}, status=400)

    # Create or reuse a ScrapeJob for this batch
    job, _ = ScrapeJob.objects.get_or_create(
        search_term=batch_label,
        defaults={'source': source if source in dict(ScrapeJob.SOURCE_CHOICES) else 'google_maps',
                  'status': 'completed', 'total_results': 0},
    )

    created = updated = skipped = 0
    errors = []

    # Pre-load existing unique_keys to minimize queries
    incoming_keys = [str(l.get('unique_key', '')).strip() for l in leads if l.get('unique_key')]
    existing = {
        p.unique_key: p
        for p in Place.objects.filter(unique_key__in=incoming_keys)
    }

    for idx, lead in enumerate(leads):
        try:
            uk = str(lead.get('unique_key', '')).strip()
            name = str(lead.get('name', '')).strip()
            if not uk:
                skipped += 1
                continue
            if not name:
                errors.append({'idx': idx, 'error': 'missing name'})
                continue

            fields = {
                'name': name[:500],
                'phone': str(lead.get('phone') or '')[:50],
                'email': str(lead.get('email') or '')[:255],
                'website': str(lead.get('website') or '')[:500],
                'address': str(lead.get('address') or ''),
                'category': str(lead.get('category') or '')[:200],
                'rating': str(lead.get('rating') or '')[:20],
                'description': str(lead.get('description') or ''),
                'facebook': str(lead.get('facebook') or '')[:500],
                'instagram': str(lead.get('instagram') or '')[:500],
                'linkedin': str(lead.get('linkedin') or '')[:500],
                'opening_hours': str(lead.get('opening_hours') or ''),
                'place_url': str(lead.get('place_url') or '')[:500],
                'plus_code': str(lead.get('plus_code') or '')[:50],
                'source': source,
            }
            try:
                fields['reviews_count'] = int(lead.get('reviews_count') or 0)
            except (ValueError, TypeError):
                fields['reviews_count'] = 0
            try:
                fields['photos_count'] = int(lead.get('photos_count') or 0)
            except (ValueError, TypeError):
                fields['photos_count'] = 0
            try:
                fields['latitude'] = float(lead.get('latitude')) if lead.get('latitude') else None
            except (ValueError, TypeError):
                fields['latitude'] = None
            try:
                fields['longitude'] = float(lead.get('longitude')) if lead.get('longitude') else None
            except (ValueError, TypeError):
                fields['longitude'] = None
            extra = lead.get('extra_data') or {}
            if isinstance(extra, dict):
                fields['extra_data'] = extra

            if uk in existing:
                # Update — only fill empty fields (don't overwrite caller-edited data)
                p = existing[uk]
                changed = False
                for k, v in fields.items():
                    if v and not getattr(p, k, None):
                        setattr(p, k, v)
                        changed = True
                p.times_seen = (p.times_seen or 0) + 1
                p.updated_at = timezone.now()
                p.lead_score = p.calculate_score()
                p.save()
                if changed:
                    updated += 1
                else:
                    skipped += 1
            else:
                # Create new
                p = Place(unique_key=uk, job=job, **fields)
                p.lead_score = p.calculate_score()
                p.save()
                created += 1
        except Exception as e:
            errors.append({'idx': idx, 'error': str(e)[:200]})

    # Update job total
    if created:
        job.total_results = (job.total_results or 0) + created
        job.save(update_fields=['total_results'])

    return JsonResponse({
        'ok': True,
        'created': created,
        'updated': updated,
        'skipped': skipped,
        'errors': errors[:50],
        'job_id': job.id,
    })


@csrf_exempt
def sync_ping(request):
    """Health check — confirms API is up and token works."""
    if not _check_auth(request):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=401)
    return JsonResponse({
        'ok': True,
        'server_time': timezone.now().isoformat(),
        'total_leads': Place.objects.count(),
    })

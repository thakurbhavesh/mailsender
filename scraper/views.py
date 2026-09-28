import csv
import io
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.db.models import Count, Q, Avg
from django.core.paginator import Paginator
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.conf import settings

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

from .models import ScrapeJob, Place, EmailLog
from .services import start_scrape_async
from .justdial import start_justdial_async
from .indiamart import start_indiamart_async
from .enrichment import enrich_job_async, enrich_all_async, enrich_place


def _filter_places(request):
    q = request.GET.get('q', '').strip()
    category = request.GET.get('category', '').strip()
    job_id = request.GET.get('job', '').strip()
    status = request.GET.get('lead_status', '').strip()
    has_email = request.GET.get('has_email', '').strip()
    min_score = request.GET.get('min_score', '').strip()

    places = Place.objects.select_related('job').all()
    if q:
        places = places.filter(
            Q(name__icontains=q) | Q(address__icontains=q) |
            Q(phone__icontains=q) | Q(category__icontains=q) |
            Q(email__icontains=q)
        )
    if category:
        places = places.filter(category__iexact=category)
    if job_id:
        places = places.filter(job_id=job_id)
    if status:
        places = places.filter(lead_status=status)
    if has_email == '1':
        places = places.exclude(email='')
    if min_score:
        try:
            places = places.filter(lead_score__gte=int(min_score))
        except ValueError:
            pass
    return places, {'q': q, 'category': category, 'job_id': job_id,
                    'lead_status': status, 'has_email': has_email, 'min_score': min_score}


def dashboard(request):
    places, filters = _filter_places(request)

    # One pass over Place instead of seven separate counts.
    totals = Place.objects.aggregate(
        total=Count('id'),
        with_email=Count('id', filter=~Q(email='')),
        with_phone=Count('id', filter=~Q(phone='')),
        with_website=Count('id', filter=~Q(website='')),
        converted=Count('id', filter=Q(lead_status='converted')),
        avg_reviews=Avg('reviews_count'),
        avg_score=Avg('lead_score'),
    )
    total_places = totals['total']
    with_email = totals['with_email']
    with_phone = totals['with_phone']
    with_website = totals['with_website']
    converted = totals['converted']
    avg_reviews = totals['avg_reviews'] or 0
    avg_score = totals['avg_score'] or 0

    job_totals = ScrapeJob.objects.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status='completed')),
        running=Count('id', filter=Q(status='running')),
    )
    total_jobs = job_totals['total']
    completed_jobs = job_totals['completed']
    running_jobs = job_totals['running']

    top_categories = list(
        Place.objects.exclude(category='')
        .values('category')
        .annotate(count=Count('id'))
        .order_by('-count')[:8]
    )

    status_breakdown = list(
        Place.objects.values('lead_status')
        .annotate(count=Count('id'))
        .order_by('lead_status')
    )

    source_breakdown = list(
        Place.objects.values('source')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    # Platform usage breakdown
    SOURCE_LABELS = {
        'google_maps': {'name': 'Google Maps', 'icon': '🗺️', 'color': '#4285F4', 'status': 'live'},
        'justdial': {'name': 'JustDial', 'icon': '📱', 'color': '#FFC107', 'status': 'soon'},
        'indiamart': {'name': 'IndiaMART', 'icon': '🏭', 'color': '#FF6B35', 'status': 'soon'},
    }
    # source_breakdown already has the lead counts; group the jobs once too.
    leads_by_source = {r['source']: r['count'] for r in source_breakdown}
    jobs_by_source = {
        r['source']: r['count']
        for r in ScrapeJob.objects.values('source').annotate(count=Count('id'))
    }
    platforms_used = []
    for src_key, meta in SOURCE_LABELS.items():
        leads = leads_by_source.get(src_key, 0)
        jobs_count = jobs_by_source.get(src_key, 0)
        platforms_used.append({
            'key': src_key, 'name': meta['name'], 'icon': meta['icon'], 'color': meta['color'],
            'status': meta['status'],
            'leads': leads, 'jobs': jobs_count,
            'used': (leads > 0 or jobs_count > 0) and meta['status'] == 'live',
        })

    recent_jobs = ScrapeJob.objects.all()[:8]
    # Last 5 unique searches with re-search ability
    last_searches = []
    seen_terms = set()
    # Bounded: five distinct terms live well inside the newest 60 jobs.
    for j in ScrapeJob.objects.exclude(search_term='__manual__').order_by('-started_at')[:60]:
        if j.search_term in seen_terms:
            continue
        seen_terms.add(j.search_term)
        last_searches.append(j)
        if len(last_searches) >= 5:
            break

    # Hot leads — top 6 by score
    hot_leads = Place.objects.filter(lead_score__gte=40).order_by('-lead_score', '-created_at')[:6]

    # Pipeline counts
    counts_by_status = {r['lead_status']: r['count'] for r in status_breakdown}
    pipeline = {
        v: {'label': l, 'count': counts_by_status.get(v, 0)}
        for v, l in Place.LEAD_STATUS_CHOICES
    }

    # Recent activity (jobs + emails + AI)
    from .models import EmailLog, AIGenerationLog
    activity = []
    for j in ScrapeJob.objects.order_by('-started_at')[:5]:
        activity.append({
            'icon': '🕷️', 'kind': 'scrape',
            'text': f'Scrape "{j.search_term[:40]}" · {j.total_results} results',
            'when': j.started_at, 'color': '#6366f1',
        })
    for e in EmailLog.objects.order_by('-created_at')[:5]:
        activity.append({
            'icon': '📧' if e.status == 'sent' else '⚠️',
            'kind': 'email',
            'text': f'Email to {e.to_email}: {e.subject[:35]}',
            'when': e.created_at,
            'color': '#10b981' if e.status == 'sent' else '#ef4444',
        })
    for a in AIGenerationLog.objects.order_by('-created_at')[:5]:
        activity.append({
            'icon': '🤖', 'kind': 'ai',
            'text': f'AI {a.get_kind_display()} ({a.total_tokens or "?"} tokens)',
            'when': a.created_at, 'color': '#ec4899',
        })
    activity.sort(key=lambda x: x['when'], reverse=True)
    activity = activity[:8]
    categories = (
        Place.objects.exclude(category='')
        .values_list('category', flat=True)
        .distinct().order_by('category')
    )

    paginator = Paginator(places.order_by('-lead_score', '-created_at'), 25)
    page = paginator.get_page(request.GET.get('page'))

    context = {
        'page': page,
        **filters,
        'total_places': total_places,
        'total_jobs': total_jobs,
        'completed_jobs': completed_jobs,
        'running_jobs': running_jobs,
        'with_email': with_email,
        'with_phone': with_phone,
        'with_website': with_website,
        'converted': converted,
        'avg_reviews': round(avg_reviews, 1),
        'avg_score': round(avg_score, 1),
        'top_categories': top_categories,
        'top_categories_json': json.dumps([{'label': c['category'], 'value': c['count']} for c in top_categories]),
        'status_breakdown_json': json.dumps([{'label': s['lead_status'], 'value': s['count']} for s in status_breakdown]),
        'source_breakdown': source_breakdown,
        'platforms_used': platforms_used,
        'recent_jobs': recent_jobs,
        'last_searches': last_searches,
        'hot_leads': hot_leads,
        'pipeline': pipeline,
        'activity': activity,
        'categories': categories,
        'status_choices': Place.LEAD_STATUS_CHOICES,
    }
    return render(request, 'scraper/dashboard.html', context)


SCRAPE_DISABLED_MSG = (
    '🖥️ Scraping is disabled on this server — it needs a local Chrome browser. '
    'Run the scrape on your own machine, then push the results with '
    '`python manage.py sync_to_prod`.'
)


def _scraping_allowed(request):
    """False (and flashes why) when this deployment cannot drive Chrome."""
    if getattr(settings, 'SCRAPING_ENABLED', True):
        return True
    messages.error(request, SCRAPE_DISABLED_MSG)
    return False


@require_POST
def start_job(request):
    if not _scraping_allowed(request):
        return redirect('dashboard')
    term = request.POST.get('search_term', '').strip()
    source = request.POST.get('source', 'google_maps')
    city = request.POST.get('city', 'Delhi').strip() or 'Delhi'
    headless = request.POST.get('headless') == 'on'
    if not term:
        messages.error(request, 'Please enter a search term.')
        return redirect('dashboard')

    if source in ('justdial', 'indiamart'):
        messages.error(request, f'⚠️ {source.title()} is temporarily disabled (anti-bot protection). Use Google Maps for now.')
        return redirect('dashboard')

    job = start_scrape_async(term, headless=headless)
    messages.success(request, f'Google Maps scrape started for "{term}" (Job #{job.id}).')
    return redirect(reverse('jobs'))


@require_POST
def re_search(request, job_id):
    """Re-run a previous search using the same term and source."""
    if not _scraping_allowed(request):
        return redirect(reverse('job_detail', args=[job_id]))
    old = get_object_or_404(ScrapeJob, id=job_id)
    term = old.search_term
    src = old.source or 'google_maps'
    if src == 'justdial':
        job = start_justdial_async(term, headless=True)
    elif src == 'indiamart':
        job = start_indiamart_async(term, headless=True)
    else:
        job = start_scrape_async(term, headless=True)
    messages.success(request, f'🔁 Re-running "{term}" on {src} → Job #{job.id}')
    return redirect(reverse('job_detail', args=[job.id]))


def jobs(request):
    job_list = ScrapeJob.objects.annotate(places_count=Count('places')).order_by('-started_at')
    paginator = Paginator(job_list, 20)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'scraper/jobs.html', {'page': page})


def job_detail(request, job_id):
    job = get_object_or_404(ScrapeJob, id=job_id)
    places = job.places.all().order_by('-lead_score', '-created_at')
    paginator = Paginator(places, 50)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'scraper/job_detail.html', {
        'job': job, 'page': page,
        'status_choices': Place.LEAD_STATUS_CHOICES,
    })


def job_status(request, job_id):
    job = get_object_or_404(ScrapeJob, id=job_id)
    return JsonResponse({
        'id': job.id, 'status': job.status,
        'total_results': job.total_results,
        'places_count': job.places.count(),
        'error': job.error_message,
    })


@require_POST
def delete_job(request, job_id):
    job = get_object_or_404(ScrapeJob, id=job_id)
    job.delete()
    messages.success(request, f'Job #{job_id} deleted.')
    return redirect('jobs')


@require_POST
def enrich_job(request, job_id):
    job = get_object_or_404(ScrapeJob, id=job_id)
    enrich_job_async(job.id)
    messages.success(request, f'Enrichment started for Job #{job_id}. Emails & social links will appear shortly.')
    return redirect(reverse('job_detail', args=[job_id]))


@require_POST
def enrich_all(request):
    enrich_all_async()
    messages.success(request, 'Enrichment started for all places with websites.')
    return redirect('dashboard')


@require_POST
def update_lead(request, place_id):
    place = get_object_or_404(Place, id=place_id)
    new_status = request.POST.get('lead_status', '').strip()
    notes = request.POST.get('notes', None)
    if new_status and new_status in dict(Place.LEAD_STATUS_CHOICES):
        place.lead_status = new_status
    if notes is not None:
        place.notes = notes
    place.save()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'status': place.lead_status})
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))


def whatsapp_bulk(request):
    places, filters = _filter_places(request)
    places = places.exclude(phone='').order_by('-lead_score')[:200]
    template = request.GET.get('msg', 'Hello! I came across your business and wanted to connect.')
    return render(request, 'scraper/whatsapp.html', {
        'places': places, 'template': template, **filters,
    })


AVAILABLE_COLUMNS = [
    ('name', 'Name', 35),
    ('lead_score', 'Score', 8),
    ('lead_status', 'Status', 12),
    ('rating', 'Rating', 8),
    ('reviews_count', 'Reviews', 10),
    ('category', 'Category', 22),
    ('secondary_categories', 'Other Categories', 28),
    ('address', 'Address', 45),
    ('phone', 'Phone', 18),
    ('email', 'Email', 28),
    ('website', 'Website', 35),
    ('opening_hours', 'Hours', 40),
    ('description', 'Description', 50),
    ('services', 'Services', 35),
    ('plus_code', 'Plus Code', 14),
    ('photos_count', 'Photos', 10),
    ('claimed', 'Claimed', 10),
    ('price_level', 'Price', 10),
    ('latitude', 'Latitude', 12),
    ('longitude', 'Longitude', 12),
    ('place_url', 'Maps URL', 50),
    ('facebook', 'Facebook', 30),
    ('instagram', 'Instagram', 30),
    ('linkedin', 'LinkedIn', 30),
    ('notes', 'Notes', 30),
    ('source', 'Source', 14),
    ('search_term', 'Search Term', 22),
    ('created_at', 'Scraped On', 18),
]
DEFAULT_COLUMNS = ['name', 'lead_score', 'rating', 'category', 'address', 'phone', 'email', 'website']


def _row_value(p, col):
    if col == 'lead_status':
        return p.get_lead_status_display()
    if col == 'search_term':
        return p.job.search_term
    if col == 'created_at':
        return p.created_at.strftime('%Y-%m-%d %H:%M')
    return getattr(p, col, '')


def export_config(request):
    """Configuration page: pick columns, filters, see count, then download."""
    places, filters = _filter_places(request)
    total = places.count()
    selected_cols = request.GET.getlist('cols')
    if not selected_cols:
        selected_cols = DEFAULT_COLUMNS

    limit_str = request.GET.get('limit', '').strip()
    try:
        limit = int(limit_str) if limit_str else 0
    except ValueError:
        limit = 0

    categories = (
        Place.objects.exclude(category='')
        .values_list('category', flat=True).distinct().order_by('category')
    )
    jobs_list = ScrapeJob.objects.order_by('-started_at')[:50]

    preview_qs = places.order_by('-lead_score', '-created_at')[:5]

    return render(request, 'scraper/export_config.html', {
        'total': total,
        'limit': limit,
        'effective_count': min(total, limit) if limit > 0 else total,
        'available_columns': AVAILABLE_COLUMNS,
        'selected_cols': selected_cols,
        'default_cols': DEFAULT_COLUMNS,
        'preview': preview_qs,
        'categories': categories,
        'jobs_list': jobs_list,
        'status_choices': Place.LEAD_STATUS_CHOICES,
        **filters,
    })


def export_excel(request):
    places, filters = _filter_places(request)
    selected_cols = request.GET.getlist('cols') or DEFAULT_COLUMNS
    selected_cols = [c for c in selected_cols if c in dict(
        (k, v) for k, v, _ in AVAILABLE_COLUMNS
    )]
    if not selected_cols:
        selected_cols = DEFAULT_COLUMNS

    try:
        limit = int(request.GET.get('limit', '0') or 0)
    except ValueError:
        limit = 0

    cols_meta = [(k, lbl, w) for k, lbl, w in AVAILABLE_COLUMNS if k in selected_cols]
    cols_meta.sort(key=lambda x: selected_cols.index(x[0]))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Leads'

    headers = ['#'] + [m[1] for m in cols_meta]
    ws.append(headers)

    header_font = Font(bold=True, color='FFFFFF', size=12)
    header_fill = PatternFill('solid', fgColor='6366F1')
    for col_idx, _ in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')

    qs = places.order_by('-lead_score', '-created_at')
    if limit > 0:
        qs = qs[:limit]

    for i, p in enumerate(qs, 1):
        ws.append([i] + [_row_value(p, k) for k, _, _ in cols_meta])

    widths = [5] + [m[2] for m in cols_meta]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 26

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    job_id = filters.get('job_id', '')
    filename = f"leads_job{job_id}.xlsx" if job_id else "leads_export.xlsx"
    response = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def export_csv(request):
    places, filters = _filter_places(request)
    selected_cols = request.GET.getlist('cols') or DEFAULT_COLUMNS
    selected_cols = [c for c in selected_cols if c in dict(
        (k, v) for k, v, _ in AVAILABLE_COLUMNS
    )]
    if not selected_cols:
        selected_cols = DEFAULT_COLUMNS

    try:
        limit = int(request.GET.get('limit', '0') or 0)
    except ValueError:
        limit = 0

    cols_meta = [(k, lbl, w) for k, lbl, w in AVAILABLE_COLUMNS if k in selected_cols]
    cols_meta.sort(key=lambda x: selected_cols.index(x[0]))

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    job_id = filters.get('job_id', '')
    filename = f"leads_job{job_id}.csv" if job_id else "leads_export.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('\ufeff')

    writer = csv.writer(response)
    writer.writerow([m[1] for m in cols_meta])
    qs = places.order_by('-lead_score', '-created_at')
    if limit > 0:
        qs = qs[:limit]
    for p in qs:
        writer.writerow([_row_value(p, k) for k, _, _ in cols_meta])
    return response


def data_view(request):
    """Dedicated data tab: rich table with filters, sort, pagination."""
    places, filters = _filter_places(request)

    sort = request.GET.get('sort', '-lead_score').strip()
    allowed_sort = {
        'name', '-name', 'lead_score', '-lead_score',
        'reviews_count', '-reviews_count', 'created_at', '-created_at',
        'lead_status', '-lead_status',
    }
    if sort not in allowed_sort:
        sort = '-lead_score'

    try:
        per_page = int(request.GET.get('per_page', 50))
        per_page = max(10, min(per_page, 200))
    except ValueError:
        per_page = 50

    places = places.order_by(sort, '-created_at')
    paginator = Paginator(places, per_page)
    page = paginator.get_page(request.GET.get('page'))

    categories = (
        Place.objects.exclude(category='')
        .values_list('category', flat=True).distinct().order_by('category')
    )
    jobs_list = ScrapeJob.objects.order_by('-started_at')[:50]

    enrich_pending = Place.objects.filter(enrichment_status='pending').exclude(website='').count()
    enrich_done = Place.objects.filter(enrichment_status='done').count()

    return render(request, 'scraper/data_view.html', {
        'page': page, 'sort': sort, 'per_page': per_page,
        'total_filtered': places.count(),
        'total_all': Place.objects.count(),
        'enrich_pending': enrich_pending,
        'enrich_done': enrich_done,
        'categories': categories,
        'jobs_list': jobs_list,
        'status_choices': Place.LEAD_STATUS_CHOICES,
        **filters,
    })


# ───── Bulk actions ─────
@require_POST
def bulk_action(request):
    """Apply an action to selected place IDs."""
    action = request.POST.get('action', '').strip()
    ids = request.POST.getlist('ids')
    if not ids:
        messages.error(request, 'Select at least one lead.')
        return redirect(request.META.get('HTTP_REFERER', 'data_view'))

    qs = Place.objects.filter(id__in=ids)
    n = qs.count()

    if action == 'delete':
        qs.delete()
        messages.success(request, f'🗑️ Deleted {n} leads.')
    elif action.startswith('status:'):
        new_status = action.split(':', 1)[1]
        if new_status in dict(Place.LEAD_STATUS_CHOICES):
            qs.update(lead_status=new_status)
            messages.success(request, f'✓ Marked {n} leads as {new_status}.')
    elif action == 'enrich':
        import threading
        def _bulk_enrich():
            for p in qs.exclude(website=''):
                try:
                    enrich_place(p)
                except Exception:
                    pass
        threading.Thread(target=_bulk_enrich, daemon=True).start()
        messages.success(request, f'✨ Enrichment started for {n} leads.')
    else:
        messages.error(request, 'Unknown action.')

    return redirect(request.META.get('HTTP_REFERER', reverse('data_view')))


# ───── Lead detail ─────
def lead_detail(request, place_id):
    place = get_object_or_404(Place, id=place_id)
    emails_sent = EmailLog.objects.filter(place=place).order_by('-created_at')[:20]
    return render(request, 'scraper/lead_detail.html', {
        'place': place, 'emails_sent': emails_sent,
        'status_choices': Place.LEAD_STATUS_CHOICES,
    })


@require_POST
def lead_update(request, place_id):
    """Update notes / fields for a lead from detail page."""
    place = get_object_or_404(Place, id=place_id)
    for field in ['name', 'phone', 'email', 'website', 'address', 'category', 'notes', 'lead_status']:
        if field in request.POST:
            val = request.POST.get(field, '').strip()
            if field == 'lead_status' and val not in dict(Place.LEAD_STATUS_CHOICES):
                continue
            setattr(place, field, val)
    place.lead_score = place.calculate_score()
    place.save()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'score': place.lead_score})
    messages.success(request, '✓ Lead updated.')
    return redirect(reverse('lead_detail', args=[place.id]))


@require_POST
def lead_enrich_now(request, place_id):
    place = get_object_or_404(Place, id=place_id)
    try:
        enrich_place(place)
        messages.success(request, '✨ Enriched! Email/social links updated.')
    except Exception as e:
        messages.error(request, f'❌ Enrichment failed: {e}')
    return redirect(reverse('lead_detail', args=[place.id]))


# ───── Quick add lead ─────
def quick_add(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if not name:
            messages.error(request, 'Name is required.')
            return redirect('quick_add')

        # Get or create a "Manual" job
        job, _ = ScrapeJob.objects.get_or_create(
            search_term='__manual__',
            defaults={'status': 'completed', 'source': 'google_maps'},
        )
        if job.status != 'completed':
            job.status = 'completed'
            job.save(update_fields=['status'])

        phone = request.POST.get('phone', '').strip()
        address = request.POST.get('address', '').strip()
        unique_key = f"{name}|{address}|{phone}".lower().strip()

        from .services import smart_upsert_place
        action, p = smart_upsert_place(job, unique_key, {
            'name': name,
            'email': request.POST.get('email', '').strip(),
            'phone': phone,
            'address': address,
            'website': request.POST.get('website', '').strip(),
            'category': request.POST.get('category', '').strip(),
            'notes': request.POST.get('notes', '').strip(),
            'source': 'manual',
        })
        p.lead_score = p.calculate_score()
        p.save()
        messages.success(request, f'✓ Lead {action}: {p.name}')
        return redirect(reverse('lead_detail', args=[p.id]))

    return render(request, 'scraper/quick_add.html', {})


# ───── Danger zone: clear scraped data ─────
def clear_data_page(request):
    if not request.user.is_staff:
        messages.error(request, 'Admin access required.')
        return redirect('dashboard')
    return render(request, 'scraper/clear_data.html', {
        'place_count': Place.objects.count(),
        'job_count': ScrapeJob.objects.count(),
        'email_count': EmailLog.objects.count(),
    })


@require_POST
def clear_data_confirm(request):
    if not request.user.is_staff:
        messages.error(request, 'Admin access required.')
        return redirect('dashboard')
    if request.POST.get('confirm') != 'DELETE':
        messages.error(request, 'Type DELETE to confirm.')
        return redirect('clear_data')

    keep_emails = request.POST.get('keep_emails') == 'on'

    p_n = Place.objects.count()
    j_n = ScrapeJob.objects.count()

    # TRUNCATE — wipe rows AND reset auto-increment so next IDs start at 1
    from django.db import connection
    place_table = Place._meta.db_table
    job_table = ScrapeJob._meta.db_table
    email_table = EmailLog._meta.db_table

    is_sqlite = connection.vendor == 'sqlite'
    with connection.cursor() as cur:
        # Disable FK enforcement during truncate (SQLite)
        if is_sqlite:
            cur.execute("PRAGMA foreign_keys = OFF;")
        try:
            if keep_emails:
                # Null out the FK in email logs so they survive without orphan errors
                cur.execute(f"UPDATE {email_table} SET place_id = NULL;")
            else:
                cur.execute(f"DELETE FROM {email_table};")

            # Now safe to delete the parent tables
            cur.execute(f"DELETE FROM {place_table};")
            cur.execute(f"DELETE FROM {job_table};")

            # Reset SQLite auto-increment counters
            if is_sqlite:
                cur.execute(f"DELETE FROM sqlite_sequence WHERE name='{place_table}';")
                cur.execute(f"DELETE FROM sqlite_sequence WHERE name='{job_table}';")
                if not keep_emails:
                    cur.execute(f"DELETE FROM sqlite_sequence WHERE name='{email_table}';")
        finally:
            if is_sqlite:
                cur.execute("PRAGMA foreign_keys = ON;")

    messages.success(
        request,
        f'🗑️ TRUNCATED: {p_n} leads + {j_n} jobs wiped. IDs reset to 1. '
        f'{"Email logs kept (place links removed)." if keep_emails else "Email logs also truncated."}'
    )
    return redirect('dashboard')

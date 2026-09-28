"""Lead import from CSV / Excel.

Flow:
1. Upload file -> show preview + auto-detected headers
2. Map columns (header -> Place field)
3. Process: dedupe by phone/email/name+address, create Place rows
"""
import csv
import io
from datetime import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.http import require_POST

import openpyxl

from .models import LeadImportJob, Place, ScrapeJob
from .calling_utils import admin_required


# Friendly column names -> Place field names
FIELD_CHOICES = [
    ('', '— Skip this column —'),
    ('name', 'Business Name'),
    ('phone', 'Phone'),
    ('email', 'Email'),
    ('website', 'Website'),
    ('address', 'Address'),
    ('category', 'Category'),
    ('rating', 'Rating'),
    ('reviews_count', 'Reviews Count'),
    ('description', 'Description'),
    ('notes', 'Notes'),
    ('facebook', 'Facebook URL'),
    ('instagram', 'Instagram URL'),
    ('linkedin', 'LinkedIn URL'),
    ('latitude', 'Latitude'),
    ('longitude', 'Longitude'),
]

# Auto-detect: header keyword -> field
AUTO_MAP_KEYWORDS = {
    'name': 'name', 'business': 'name', 'company': 'name', 'shop': 'name',
    'phone': 'phone', 'mobile': 'phone', 'contact': 'phone', 'tel': 'phone',
    'email': 'email', 'mail': 'email', 'e-mail': 'email',
    'website': 'website', 'url': 'website', 'web': 'website', 'site': 'website',
    'address': 'address', 'location': 'address', 'addr': 'address',
    'category': 'category', 'type': 'category', 'industry': 'category',
    'rating': 'rating', 'stars': 'rating',
    'review': 'reviews_count',
    'description': 'description', 'about': 'description',
    'note': 'notes', 'remark': 'notes',
    'facebook': 'facebook', 'fb': 'facebook',
    'instagram': 'instagram', 'insta': 'instagram', 'ig': 'instagram',
    'linkedin': 'linkedin', 'lnk': 'linkedin',
    'lat': 'latitude',
    'lng': 'longitude', 'lon': 'longitude',
}


def _auto_map(headers):
    """Auto-detect column -> field mapping from header names."""
    mapping = {}
    for h in headers:
        if not h:
            continue
        low = str(h).strip().lower()
        guessed = ''
        for kw, field in AUTO_MAP_KEYWORDS.items():
            if kw in low:
                guessed = field
                break
        mapping[h] = guessed
    return mapping


def _read_file(file_obj, filename, max_rows=None):
    """Return (headers, list-of-row-dicts) from CSV or Excel file."""
    headers, rows = [], []
    low = filename.lower()

    if low.endswith('.xlsx') or low.endswith('.xls'):
        wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        first = next(it, None)
        if not first:
            return [], []
        headers = [str(c).strip() if c is not None else f'col_{i}' for i, c in enumerate(first)]
        for i, row in enumerate(it):
            if max_rows and i >= max_rows:
                break
            d = {}
            for h, v in zip(headers, row):
                d[h] = '' if v is None else str(v).strip()
            if any(d.values()):
                rows.append(d)
    else:
        # CSV — try utf-8 first, fall back to latin-1
        try:
            content = file_obj.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            file_obj.seek(0)
            content = file_obj.read().decode('latin-1', errors='replace')
        reader = csv.reader(io.StringIO(content))
        first = next(reader, None)
        if not first:
            return [], []
        headers = [str(c).strip() if c else f'col_{i}' for i, c in enumerate(first)]
        for i, row in enumerate(reader):
            if max_rows and i >= max_rows:
                break
            d = {h: (str(v).strip() if v is not None else '') for h, v in zip(headers, row)}
            if any(d.values()):
                rows.append(d)

    return headers, rows


def _make_unique_key(name, phone, address):
    bits = '|'.join((s or '').strip().lower() for s in [name, phone, address])
    return f"upload:{bits[:550]}"


@admin_required
def import_home(request):
    """List all past imports + new upload button."""
    jobs = LeadImportJob.objects.filter(user=request.user).order_by('-created_at')[:30]
    return render(request, 'scraper/import/home.html', {'jobs': jobs})


@admin_required
def import_upload(request):
    """Upload step — accepts file, parses headers, redirects to mapping."""
    if request.method != 'POST' or 'file' not in request.FILES:
        return redirect('import_home')

    f = request.FILES['file']
    name = f.name.lower()
    if not (name.endswith('.csv') or name.endswith('.xlsx') or name.endswith('.xls')):
        messages.error(request, 'Only .csv, .xlsx, or .xls files supported.')
        return redirect('import_home')

    job = LeadImportJob.objects.create(
        user=request.user,
        file=f,
        original_filename=f.name,
        source_label=request.POST.get('source_label', 'upload') or 'upload',
        status='pending',
    )

    # Parse preview
    try:
        with job.file.open('rb') as fh:
            headers, sample = _read_file(fh, job.original_filename, max_rows=5)
    except Exception as e:
        job.status = 'failed'
        job.error_log = f'Read error: {e}'
        job.save()
        messages.error(request, f'Could not read file: {e}')
        return redirect('import_home')

    if not headers:
        job.status = 'failed'
        job.error_log = 'File appears empty.'
        job.save()
        messages.error(request, 'File appears empty.')
        return redirect('import_home')

    job.column_mapping = _auto_map(headers)
    job.save()
    return redirect('import_map', job_id=job.id)


@admin_required
def import_map(request, job_id):
    """Show preview + column-mapping form."""
    job = get_object_or_404(LeadImportJob, id=job_id, user=request.user)
    with job.file.open('rb') as fh:
        headers, sample = _read_file(fh, job.original_filename, max_rows=8)

    if request.method == 'POST':
        # Save column mapping
        mapping = {}
        for h in headers:
            mapping[h] = request.POST.get(f'map_{h}', '').strip()
        job.column_mapping = mapping
        job.source_label = request.POST.get('source_label', job.source_label) or 'upload'
        job.status = 'previewed'
        job.save()
        return redirect('import_run', job_id=job.id)

    # Detected fields used:
    used_fields = set(v for v in (job.column_mapping or {}).values() if v)
    has_name = 'name' in used_fields
    has_contact = 'phone' in used_fields or 'email' in used_fields

    return render(request, 'scraper/import/map.html', {
        'job': job,
        'headers': headers,
        'sample': sample,
        'field_choices': FIELD_CHOICES,
        'has_name': has_name,
        'has_contact': has_contact,
    })


@admin_required
@require_POST
def import_run(request, job_id):
    """Process the file: dedupe + create Place rows."""
    job = get_object_or_404(LeadImportJob, id=job_id, user=request.user)
    job.status = 'processing'
    job.save()

    mapping = job.column_mapping or {}
    # Determine which CSV col maps to which field
    field_to_col = {}
    for col, fld in mapping.items():
        if fld and fld not in field_to_col:
            field_to_col[fld] = col

    if 'name' not in field_to_col:
        job.status = 'failed'
        job.error_log = 'Name column is required.'
        job.save()
        messages.error(request, 'Name column is required.')
        return redirect('import_map', job_id=job.id)

    # Ensure / find an "Imported" ScrapeJob to attach
    scrape_job, _ = ScrapeJob.objects.get_or_create(
        search_term=f'Imported: {job.original_filename[:200]}',
        defaults={'source': 'google_maps', 'status': 'completed', 'total_results': 0},
    )

    imported = dupes = errors = 0
    errs = []
    total = 0

    with job.file.open('rb') as fh:
        headers, rows = _read_file(fh, job.original_filename)
    total = len(rows)

    existing_phones = set(Place.objects.exclude(phone='').values_list('phone', flat=True))
    existing_emails = set(Place.objects.exclude(email='').values_list('email', flat=True))

    new_places = []
    for i, row in enumerate(rows, start=1):
        try:
            data = {}
            for fld, col in field_to_col.items():
                v = (row.get(col) or '').strip()
                data[fld] = v

            name = data.get('name', '').strip()
            if not name:
                errors += 1
                if len(errs) < 20:
                    errs.append(f'Row {i}: missing name')
                continue

            phone = (data.get('phone') or '').strip()
            email = (data.get('email') or '').strip()

            # Dedup by phone or email
            if phone and phone in existing_phones:
                dupes += 1
                continue
            if email and email in existing_emails:
                dupes += 1
                continue

            # Numeric fields safety
            try:
                rev = int(float(data.get('reviews_count') or 0))
            except (ValueError, TypeError):
                rev = 0
            try:
                lat = float(data.get('latitude')) if data.get('latitude') else None
            except (ValueError, TypeError):
                lat = None
            try:
                lng = float(data.get('longitude')) if data.get('longitude') else None
            except (ValueError, TypeError):
                lng = None

            uk = _make_unique_key(name, phone, data.get('address', ''))

            place = Place(
                job=scrape_job,
                name=name[:500],
                phone=phone[:50],
                email=email[:255],
                website=(data.get('website') or '')[:500],
                address=data.get('address') or '',
                category=(data.get('category') or '')[:200],
                rating=(data.get('rating') or '')[:20],
                reviews_count=rev,
                description=data.get('description') or '',
                notes=data.get('notes') or '',
                facebook=(data.get('facebook') or '')[:500],
                instagram=(data.get('instagram') or '')[:500],
                linkedin=(data.get('linkedin') or '')[:500],
                latitude=lat, longitude=lng,
                source=job.source_label or 'upload',
                unique_key=uk,
            )
            place.lead_score = place.calculate_score()
            new_places.append(place)
            if phone:
                existing_phones.add(phone)
            if email:
                existing_emails.add(email)
            imported += 1
        except Exception as e:
            errors += 1
            if len(errs) < 20:
                errs.append(f'Row {i}: {e}')

    # Bulk create
    if new_places:
        Place.objects.bulk_create(new_places, ignore_conflicts=True, batch_size=500)
        scrape_job.total_results = (scrape_job.total_results or 0) + imported
        scrape_job.save(update_fields=['total_results'])

    job.total_rows = total
    job.imported = imported
    job.duplicates = dupes
    job.errors = errors
    job.error_log = '\n'.join(errs)
    job.status = 'completed'
    job.completed_at = timezone.now()
    job.save()

    messages.success(
        request,
        f'Imported {imported} leads · {dupes} duplicates skipped · {errors} errors out of {total} rows.'
    )
    return redirect('import_home')


@admin_required
def import_template(request):
    """Download a sample CSV template."""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="leads_import_template.csv"'
    writer = csv.writer(response)
    writer.writerow(['name', 'phone', 'email', 'website', 'address', 'category', 'rating', 'reviews_count', 'description', 'notes'])
    writer.writerow(['Example Pvt Ltd', '9876543210', 'info@example.com', 'https://example.com',
                     '123 MG Road, Bangalore', 'Software', '4.5', '120', 'IT services company', 'Met at conference'])
    writer.writerow(['Sample Restaurant', '9012345678', 'order@sample.com', '',
                     '45 Park Street, Kolkata', 'Restaurant', '4.2', '350', '', ''])
    return response


@admin_required
def import_delete(request, job_id):
    job = get_object_or_404(LeadImportJob, id=job_id, user=request.user)
    if request.method == 'POST':
        try:
            if job.file:
                job.file.delete(save=False)
        except Exception:
            pass
        job.delete()
        messages.success(request, 'Import record deleted.')
    return redirect('import_home')

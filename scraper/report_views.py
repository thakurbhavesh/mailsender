"""Custom Report Builder — parameterized reports with dimensions, metrics, filters, charts."""
import json
from datetime import datetime, timedelta, date

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.db.models import Count, Q, Sum, Avg, F
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib.auth.models import User

from .models import (
    SavedReport, CallLog, LeadAssignment, Place, Attendance, CallerProfile,
)
from .calling_utils import admin_required


# Entity field definitions
ENTITY_DIMS = {
    'calls': [
        ('outcome', 'Outcome'),
        ('caller__username', 'Caller'),
        ('place__category', 'Category'),
        ('place__source', 'Source'),
        ('date', 'Date'),
    ],
    'assignments': [
        ('status', 'Status'),
        ('caller__username', 'Caller'),
        ('place__category', 'Category'),
        ('outcome', 'Outcome'),
        ('date', 'Assigned Date'),
    ],
    'leads': [
        ('lead_status', 'Lead Status'),
        ('source', 'Source'),
        ('category', 'Category'),
        ('date', 'Created Date'),
    ],
    'attendance': [
        ('user__username', 'User'),
        ('date', 'Date'),
    ],
}

ENTITY_METRICS = {
    'calls': [
        ('count', 'Count of Calls'),
    ],
    'assignments': [
        ('count', 'Count'),
    ],
    'leads': [
        ('count', 'Count'),
        ('avg_score', 'Avg Lead Score'),
        ('total_reviews', 'Total Reviews'),
    ],
    'attendance': [
        ('count', 'Sessions'),
        ('total_minutes', 'Total Minutes'),
    ],
}


def _date_range_for(preset, start_s='', end_s=''):
    today = timezone.localdate()
    if preset == 'today': return today, today
    if preset == 'yesterday':
        d = today - timedelta(days=1); return d, d
    if preset == 'last7': return today - timedelta(days=6), today
    if preset == 'last30': return today - timedelta(days=29), today
    if preset == 'month': return today.replace(day=1), today
    if preset == 'custom':
        try:
            s = datetime.strptime(start_s, '%Y-%m-%d').date()
            e = datetime.strptime(end_s, '%Y-%m-%d').date()
            return s, e
        except ValueError:
            pass
    return today - timedelta(days=29), today


def _run_report(entity, config):
    """Execute the report based on entity + config dict. Returns dict with labels, rows, totals."""
    dims = config.get('dimensions', [])
    metrics = config.get('metrics', ['count'])
    filters = config.get('filters', {})
    preset = filters.get('preset', 'last30')
    start, end = _date_range_for(preset, filters.get('start', ''), filters.get('end', ''))

    if entity == 'calls':
        qs = CallLog.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
        if filters.get('caller_id'):
            try:
                qs = qs.filter(caller_id=int(filters['caller_id']))
            except ValueError:
                pass
        if filters.get('outcome'):
            qs = qs.filter(outcome=filters['outcome'])
    elif entity == 'assignments':
        qs = LeadAssignment.objects.filter(assigned_at__date__gte=start, assigned_at__date__lte=end)
        if filters.get('caller_id'):
            try:
                qs = qs.filter(caller_id=int(filters['caller_id']))
            except ValueError:
                pass
        if filters.get('status'):
            qs = qs.filter(status=filters['status'])
    elif entity == 'leads':
        qs = Place.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
        if filters.get('source'):
            qs = qs.filter(source=filters['source'])
        if filters.get('lead_status'):
            qs = qs.filter(lead_status=filters['lead_status'])
    elif entity == 'attendance':
        qs = Attendance.objects.filter(date__gte=start, date__lte=end)
    else:
        return {'labels': [], 'rows': [], 'totals': {}}

    # Resolve "date" pseudo-dim into appropriate truncation
    if 'date' in dims:
        if entity == 'calls':
            qs = qs.annotate(_date=TruncDate('created_at'))
        elif entity == 'assignments':
            qs = qs.annotate(_date=TruncDate('assigned_at'))
        elif entity == 'leads':
            qs = qs.annotate(_date=TruncDate('created_at'))
        elif entity == 'attendance':
            qs = qs.annotate(_date=F('date'))

    # Build group-by
    group_fields = []
    for d in dims:
        if d == 'date':
            group_fields.append('_date')
        else:
            group_fields.append(d)

    # Metric aggregations
    annotations = {}
    for m in metrics:
        if m == 'count':
            annotations['m_count'] = Count('id')
        elif m == 'avg_score' and entity == 'leads':
            annotations['m_avg_score'] = Avg('lead_score')
        elif m == 'total_reviews' and entity == 'leads':
            annotations['m_total_reviews'] = Sum('reviews_count')
        elif m == 'total_minutes' and entity == 'attendance':
            annotations['m_total_minutes'] = Sum('total_minutes')

    if not annotations:
        annotations['m_count'] = Count('id')

    if group_fields:
        results = list(qs.values(*group_fields).annotate(**annotations).order_by(*group_fields))
    else:
        agg = qs.aggregate(**annotations)
        results = [{**agg}]

    # Build rows
    rows = []
    totals = {k: 0 for k in annotations.keys()}
    for r in results:
        row = {'dims': {}, 'metrics': {}}
        for d in dims:
            key = '_date' if d == 'date' else d
            val = r.get(key)
            if hasattr(val, 'strftime'):
                val = val.strftime('%Y-%m-%d')
            row['dims'][d] = val
        for k in annotations.keys():
            v = r.get(k) or 0
            row['metrics'][k] = round(v, 2) if isinstance(v, float) else v
            try:
                totals[k] += v or 0
            except TypeError:
                pass
        rows.append(row)

    # Sort by first metric desc
    if annotations:
        first_metric = list(annotations.keys())[0]
        rows.sort(key=lambda r: -(r['metrics'].get(first_metric) or 0))

    # Build chart-ready data
    labels = []
    for r in rows[:30]:  # cap for chart
        if r['dims']:
            labels.append(' / '.join(str(v) for v in r['dims'].values()))
        else:
            labels.append('Total')

    return {
        'labels': labels,
        'rows': rows,
        'totals': {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()},
        'metric_keys': list(annotations.keys()),
        'start': start, 'end': end,
    }


@admin_required
def reports_list(request):
    own = SavedReport.objects.filter(owner=request.user)
    shared = SavedReport.objects.filter(is_shared=True).exclude(owner=request.user)
    return render(request, 'scraper/reports/list.html', {
        'own_reports': own,
        'shared_reports': shared,
    })


@admin_required
def report_builder(request, report_id=None):
    """Create or edit a report."""
    report = get_object_or_404(SavedReport, id=report_id, owner=request.user) if report_id else None

    # Build config from POST/GET
    if request.method == 'POST' and request.POST.get('action') == 'save':
        name = request.POST.get('name', '').strip() or 'Untitled Report'
        entity = request.POST.get('entity', 'calls')
        config = _config_from_post(request.POST)
        if report:
            report.name = name
            report.entity = entity
            report.config = config
            report.is_shared = request.POST.get('is_shared') == 'on'
            report.save()
        else:
            report = SavedReport.objects.create(
                owner=request.user, name=name, entity=entity, config=config,
                is_shared=request.POST.get('is_shared') == 'on',
            )
        messages.success(request, f'Report "{name}" saved.')
        return redirect('report_run', report_id=report.id)

    # Defaults
    entity = (report.entity if report else request.GET.get('entity', 'calls'))
    config = report.config if report else {
        'dimensions': ['outcome'],
        'metrics': ['count'],
        'filters': {'preset': 'last30'},
        'chart': 'bar',
    }

    return render(request, 'scraper/reports/builder.html', {
        'report': report,
        'entity': entity,
        'config': config,
        'entity_dims': ENTITY_DIMS,
        'entity_metrics': ENTITY_METRICS,
    })


def _config_from_post(data):
    dims = data.getlist('dimensions')
    metrics = data.getlist('metrics') or ['count']
    filters = {
        'preset': data.get('preset', 'last30'),
        'start': data.get('start', ''),
        'end': data.get('end', ''),
        'caller_id': data.get('caller_id', ''),
        'outcome': data.get('outcome', ''),
        'status': data.get('status', ''),
        'source': data.get('source', ''),
        'lead_status': data.get('lead_status', ''),
    }
    return {
        'dimensions': dims,
        'metrics': metrics,
        'filters': filters,
        'chart': data.get('chart', 'bar'),
    }


@admin_required
def report_run(request, report_id):
    """Execute a saved report and show results."""
    report = get_object_or_404(SavedReport, id=report_id)
    if report.owner != request.user and not report.is_shared:
        messages.error(request, 'No access to this report.')
        return redirect('reports_list')

    # Allow overrides from query string
    config = dict(report.config or {})
    if request.GET.get('preset'):
        config.setdefault('filters', {})['preset'] = request.GET.get('preset')
    if request.GET.get('start'):
        config.setdefault('filters', {})['start'] = request.GET.get('start')
    if request.GET.get('end'):
        config.setdefault('filters', {})['end'] = request.GET.get('end')

    result = _run_report(report.entity, config)
    report.times_run = (report.times_run or 0) + 1
    report.save(update_fields=['times_run'])

    return render(request, 'scraper/reports/run.html', {
        'report': report,
        'config': config,
        'result': result,
        'chart_type': config.get('chart', 'bar'),
    })


@admin_required
def report_preview(request):
    """AJAX preview while building."""
    entity = request.GET.get('entity', 'calls')
    config = _config_from_post(request.GET)
    result = _run_report(entity, config)
    # Strip dates for JSON
    return JsonResponse({
        'labels': result['labels'],
        'rows': result['rows'][:50],
        'totals': result['totals'],
        'metric_keys': result['metric_keys'],
        'count': len(result['rows']),
    })


@admin_required
@require_POST
def report_delete(request, report_id):
    report = get_object_or_404(SavedReport, id=report_id, owner=request.user)
    name = report.name
    report.delete()
    messages.success(request, f'Report "{name}" deleted.')
    return redirect('reports_list')


@admin_required
def report_export_csv(request, report_id):
    """Export report results as CSV."""
    import csv
    report = get_object_or_404(SavedReport, id=report_id)
    if report.owner != request.user and not report.is_shared:
        return HttpResponse('Forbidden', status=403)
    result = _run_report(report.entity, report.config or {})

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{report.name}.csv"'
    writer = csv.writer(response)

    dims = (report.config or {}).get('dimensions', [])
    metric_keys = result['metric_keys']
    writer.writerow(list(dims) + metric_keys)
    for row in result['rows']:
        out = [row['dims'].get(d, '') for d in dims]
        out += [row['metrics'].get(k, 0) for k in metric_keys]
        writer.writerow(out)
    return response

"""Lead Management console — one place to see every lead's real work history.

Answers, per lead: has anyone touched it, when was the mail sent, was it opened,
when and how many times, was a link clicked, who called and what happened.
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, Q, Max, Sum, F, Value, DateTimeField
from django.db.models.functions import Coalesce, Greatest
from django.contrib import messages
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (Place, EmailLog, CallLog, LeadAssignment, ScrapeJob,
                     Meeting, Suppression, SequenceEnrollment)


# ─────────────────────────────────────────────────────────────
# Engagement buckets — the "which leads have been worked?" question
# ─────────────────────────────────────────────────────────────
WORK_CHOICES = [
    ('due', 'Due now'),
    ('overdue', 'Overdue'),
    ('today', 'Due today'),
    ('no_followup', 'No follow-up set'),
    ('untouched', 'Never contacted'),
    ('worked', 'Contacted'),
    ('emailed', 'Emailed'),
    ('not_emailed', 'Not emailed'),
    ('opened', 'Opened an email'),
    ('not_opened', 'Sent, not opened'),
    ('clicked', 'Clicked a link'),
    ('called', 'Called'),
    ('never_called', 'Never called'),
    ('stale', 'No activity in 7 days'),
]

SORT_CHOICES = [
    ('next_follow_up', 'Follow-up: soonest'),
    ('-last_activity_at', 'Last activity: newest'),
    ('last_activity_at', 'Last activity: oldest'),
    ('-total_opens', 'Most opens'),
    ('-total_clicks', 'Most clicks'),
    ('-email_count', 'Most emails sent'),
    ('-lead_score', 'Score: high to low'),
    ('lead_score', 'Score: low to high'),
    ('name', 'Name: A to Z'),
    ('-created_at', 'Newest lead'),
]

def _local_day_bounds(day=None):
    """Start and end of a local calendar day, as aware datetimes.

    Using now.replace(hour=0) would give UTC midnight, which is 5.5 hours off
    the day people actually mean in Asia/Kolkata.
    """
    from datetime import datetime as _dt, time as _time
    day = day or timezone.localdate()
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(_dt.combine(day, _time.min), tz)
    end = timezone.make_aware(_dt.combine(day, _time.max), tz)
    return start, end


# Stand-in for NULL so Greatest() behaves the same on SQLite and Postgres.
# Must be timezone-aware — USE_TZ is on.
_EPOCH = Value(datetime(1970, 1, 1, tzinfo=dt_timezone.utc), output_field=DateTimeField())


def _annotate_engagement(qs):
    """Attach email / call / open / click rollups to a Place queryset."""
    sent_q = Q(emails__status='sent')
    return qs.annotate(
        email_count=Count('emails', filter=sent_q, distinct=True),
        last_email_at=Max('emails__sent_at', filter=sent_q),
        total_opens=Coalesce(Sum('emails__opened_count', filter=sent_q), 0),
        opened_mails=Count('emails', filter=sent_q & Q(emails__opened_at__isnull=False), distinct=True),
        first_open_at=Max('emails__opened_at', filter=sent_q),
        last_open_at=Max('emails__last_opened_at', filter=sent_q),
        total_clicks=Coalesce(Sum('emails__clicked_count', filter=sent_q), 0),
        call_count=Count('call_logs', distinct=True),
        last_call_at=Max('call_logs__created_at'),
    ).annotate(
        last_activity_at=Greatest(
            Coalesce(F('last_email_at'), _EPOCH),
            Coalesce(F('last_call_at'), _EPOCH),
            Coalesce(F('last_open_at'), _EPOCH),
        ),
    )


def _apply_work_filter(qs, work):
    """Filter by what has (or hasn't) actually been done on the lead."""
    now = timezone.now()
    if work == 'due':
        # Anything already past, plus the rest of today.
        _, end_of_today = _local_day_bounds()
        return qs.filter(next_follow_up__isnull=False, next_follow_up__lte=end_of_today)
    if work == 'overdue':
        return qs.filter(next_follow_up__isnull=False, next_follow_up__lt=now)
    if work == 'today':
        return qs.filter(next_follow_up__range=_local_day_bounds())
    if work == 'no_followup':
        return qs.filter(next_follow_up__isnull=True)
    if work == 'untouched':
        return qs.filter(email_count=0, call_count=0)
    if work == 'worked':
        return qs.filter(Q(email_count__gt=0) | Q(call_count__gt=0))
    if work == 'emailed':
        return qs.filter(email_count__gt=0)
    if work == 'not_emailed':
        return qs.filter(email_count=0)
    if work == 'opened':
        return qs.filter(opened_mails__gt=0)
    if work == 'not_opened':
        return qs.filter(email_count__gt=0, opened_mails=0)
    if work == 'clicked':
        return qs.filter(total_clicks__gt=0)
    if work == 'called':
        return qs.filter(call_count__gt=0)
    if work == 'never_called':
        return qs.filter(call_count=0)
    if work == 'stale':
        cutoff = timezone.now() - timedelta(days=7)
        return qs.filter(Q(email_count__gt=0) | Q(call_count__gt=0)).filter(
            last_activity_at__lt=cutoff)
    return qs


def lead_management(request):
    """Main console: every lead plus its complete outreach footprint."""
    q = request.GET.get('q', '').strip()
    work = request.GET.get('work', '').strip()
    status = request.GET.get('lead_status', '').strip()
    owner = request.GET.get('owner', '').strip()
    category = request.GET.get('category', '').strip()
    job_id = request.GET.get('job', '').strip()
    days = request.GET.get('days', '').strip()
    sort = request.GET.get('sort', '-last_activity_at').strip()

    qs = _annotate_engagement(Place.objects.select_related('job'))

    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(email__icontains=q) |
            Q(phone__icontains=q) | Q(address__icontains=q) |
            Q(category__icontains=q)
        )
    if status:
        qs = qs.filter(lead_status=status)
    if category:
        qs = qs.filter(category__iexact=category)
    if job_id:
        qs = qs.filter(job_id=job_id)
    if owner:
        qs = qs.filter(assignments__caller_id=owner)
    if days:
        try:
            qs = qs.filter(last_activity_at__gte=timezone.now() - timedelta(days=int(days)))
        except ValueError:
            pass

    qs = _apply_work_filter(qs, work)

    if sort not in dict(SORT_CHOICES):
        sort = '-last_activity_at'
    qs = qs.order_by(sort, '-id')

    # ── KPI strip, computed over the filtered set ──
    stats = qs.aggregate(
        total=Count('id', distinct=True),
        emailed=Count('id', filter=Q(email_count__gt=0), distinct=True),
        opened=Count('id', filter=Q(opened_mails__gt=0), distinct=True),
        clicked=Count('id', filter=Q(total_clicks__gt=0), distinct=True),
        called=Count('id', filter=Q(call_count__gt=0), distinct=True),
        untouched=Count('id', filter=Q(email_count=0, call_count=0), distinct=True),
        converted=Count('id', filter=Q(lead_status='converted'), distinct=True),
        overdue=Count('id', filter=Q(next_follow_up__lt=timezone.now()), distinct=True),
        due_today=Count('id', filter=Q(next_follow_up__range=_local_day_bounds()), distinct=True),
    )
    total = stats['total'] or 0
    stats['worked'] = total - (stats['untouched'] or 0)
    stats['open_rate'] = round(100 * stats['opened'] / stats['emailed'], 1) if stats['emailed'] else 0
    stats['click_rate'] = round(100 * stats['clicked'] / stats['emailed'], 1) if stats['emailed'] else 0
    stats['coverage'] = round(100 * stats['worked'] / total, 1) if total else 0

    try:
        per_page = max(10, min(int(request.GET.get('per_page', 50)), 200))
    except ValueError:
        per_page = 50
    page = Paginator(qs, per_page).get_page(request.GET.get('page'))

    # Owner name per lead, for the current page only — avoids a join blowup.
    owners = {}
    for a in LeadAssignment.objects.filter(
            place_id__in=[p.id for p in page]).select_related('caller').order_by('-assigned_at'):
        owners.setdefault(a.place_id, a.caller.username)
    for p in page:
        p.owner_name = owners.get(p.id, '')

    return render(request, 'scraper/lead_management.html', {
        'page': page, 'stats': stats, 'sort': sort, 'per_page': per_page,
        'q': q, 'work': work, 'lead_status': status, 'owner': owner,
        'category': category, 'job_id': job_id, 'days': days,
        'work_choices': WORK_CHOICES,
        'sort_choices': SORT_CHOICES,
        'status_choices': Place.LEAD_STATUS_CHOICES,
        'categories': Place.objects.exclude(category='')
            .values_list('category', flat=True).distinct().order_by('category'),
        'jobs_list': ScrapeJob.objects.order_by('-started_at')[:50],
        'members': User.objects.filter(lead_assignments__isnull=False).distinct().order_by('username'),
    })


def build_timeline(place):
    """Merge every touchpoint on a lead into one reverse-chronological feed."""
    events = []

    for log in EmailLog.objects.filter(place=place).select_related('template', 'account'):
        when = log.sent_at or log.created_at
        if log.status == 'sent':
            events.append({
                'at': when, 'kind': 'sent', 'icon': '📤',
                'title': 'Email sent: %s' % log.subject,
                'detail': 'To %s%s' % (log.to_email, ' · via %s' % log.account.email if log.account else ''),
                'meta': log.template.name if log.template else 'Custom mail',
            })
        elif log.status == 'failed':
            events.append({
                'at': when, 'kind': 'failed', 'icon': '❌',
                'title': 'Email failed: %s' % log.subject,
                'detail': log.error_message[:200], 'meta': log.to_email,
            })
        else:
            events.append({
                'at': when, 'kind': 'queued', 'icon': '⏳',
                'title': 'Email queued: %s' % log.subject,
                'detail': log.to_email, 'meta': '',
            })

        if log.opened_at:
            events.append({
                'at': log.opened_at, 'kind': 'open', 'icon': '👀',
                'title': 'Email opened',
                'detail': '"%s" — %s' % (log.subject, log.to_email),
                'meta': 'Opened %s times in total' % log.opened_count,
            })
        if log.last_opened_at and (log.opened_count or 0) > 1 and log.last_opened_at != log.opened_at:
            events.append({
                'at': log.last_opened_at, 'kind': 'open', 'icon': '🔁',
                'title': 'Opened again (%s times)' % log.opened_count,
                'detail': '"%s"' % log.subject, 'meta': log.user_agent[:60],
            })
        for click in (log.clicks or []):
            parsed = parse_datetime(click.get('at') or '')
            if not parsed:
                continue
            events.append({
                'at': parsed, 'kind': 'click', 'icon': '🔗',
                'title': 'Link clicked',
                'detail': click.get('url', '')[:160],
                'meta': 'From "%s"' % log.subject[:40],
            })

    for call in CallLog.objects.filter(place=place).select_related('caller'):
        events.append({
            'at': call.created_at, 'kind': 'call', 'icon': '📞',
            'title': 'Call: %s' % call.get_outcome_display(),
            'detail': call.notes[:200] or 'No notes',
            'meta': 'by %s' % call.caller.username,
        })

    for a in LeadAssignment.objects.filter(place=place).select_related('caller', 'assigned_by'):
        events.append({
            'at': a.assigned_at, 'kind': 'assign', 'icon': '👤',
            'title': 'Assigned to %s' % a.caller.username,
            'detail': 'Assignment status: %s' % a.get_status_display(),
            'meta': 'by %s' % a.assigned_by.username if a.assigned_by else '',
        })

    for m in Meeting.objects.filter(place=place):
        events.append({
            'at': m.created_at, 'kind': 'meeting', 'icon': '📅',
            'title': 'Meeting booked — %s' % m.get_status_display(),
            'detail': 'With %s (%s) at %s' % (
                m.booker_name, m.booker_email, m.scheduled_at.strftime('%d %b %Y, %H:%M')),
            'meta': '%s min' % m.duration_min,
        })

    events.append({
        'at': place.created_at, 'kind': 'created', 'icon': '🌱',
        'title': 'Lead added to the database',
        'detail': 'Source: %s' % place.source, 'meta': 'Job #%s' % place.job_id,
    })

    events = [e for e in events if e['at']]
    events.sort(key=lambda e: e['at'], reverse=True)
    return events


def lead_activity(request, place_id):
    """Full per-lead activity page: timeline plus every mail's tracking detail."""
    place = get_object_or_404(Place, id=place_id)
    timeline = build_timeline(place)

    emails = list(EmailLog.objects.filter(place=place)
                  .select_related('template', 'account').order_by('-created_at'))
    sent = [e for e in emails if e.status == 'sent']
    opened = [e for e in sent if e.opened_at]

    summary = {
        'emails_total': len(emails),
        'emails_sent': len(sent),
        'emails_failed': sum(1 for e in emails if e.status == 'failed'),
        'opened_mails': len(opened),
        'total_opens': sum(e.opened_count or 0 for e in sent),
        'total_clicks': sum(e.clicked_count or 0 for e in sent),
        'open_rate': round(100 * len(opened) / len(sent), 1) if sent else 0,
        'first_sent': min((e.sent_at for e in sent if e.sent_at), default=None),
        'last_sent': max((e.sent_at for e in sent if e.sent_at), default=None),
        'last_open': max((e.last_opened_at for e in sent if e.last_opened_at), default=None),
        'calls': CallLog.objects.filter(place=place).count(),
        'last_call': CallLog.objects.filter(place=place).order_by('-created_at').first(),
    }
    summary['is_untouched'] = summary['emails_sent'] == 0 and summary['calls'] == 0

    return render(request, 'scraper/lead_activity.html', {
        'place': place, 'timeline': timeline, 'emails': emails,
        'summary': summary,
        'assignments': LeadAssignment.objects.filter(place=place).select_related('caller'),
        'calls': CallLog.objects.filter(place=place).select_related('caller'),
        'status_choices': Place.LEAD_STATUS_CHOICES,
    })


# ─────────────────────────────────────────────────────────────
# Follow-ups and bulk actions
# ─────────────────────────────────────────────────────────────
def _parse_when(value, preset):
    """Turn either a datetime-local string or a preset like '3d' into a time."""
    now = timezone.now()
    local_now = timezone.localtime(now)
    presets = {
        'today': timezone.localtime(now).replace(hour=17, minute=0, second=0, microsecond=0),
        'tomorrow': (local_now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0),
        '3d': now + timedelta(days=3),
        '1w': now + timedelta(days=7),
        '2w': now + timedelta(days=14),
        '1m': now + timedelta(days=30),
    }
    if preset in presets:
        return presets[preset]
    if value:
        parsed = parse_datetime(value)
        if parsed and timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return parsed
    return None


@require_POST
def set_follow_up(request, place_id):
    """Schedule (or clear) the next touch on one lead."""
    place = get_object_or_404(Place, id=place_id)

    if request.POST.get('clear'):
        place.next_follow_up = None
        place.follow_up_note = ''
        place.save(update_fields=['next_follow_up', 'follow_up_note'])
        messages.success(request, 'Follow-up cleared.')
    else:
        when = _parse_when(request.POST.get('when', ''), request.POST.get('preset', ''))
        if not when:
            messages.error(request, 'Enter a valid date and time.')
            return redirect(request.META.get('HTTP_REFERER') or 'lead_management')
        place.next_follow_up = when
        place.follow_up_note = request.POST.get('note', '').strip()[:300]
        place.save(update_fields=['next_follow_up', 'follow_up_note'])
        messages.success(request, 'Follow-up set for %s' % timezone.localtime(when).strftime('%d %b %Y, %H:%M'))

    return redirect(request.META.get('HTTP_REFERER') or 'lead_management')


@require_POST
def lead_bulk_action(request):
    """Apply one action to the leads ticked on the Lead Management table."""
    ids = request.POST.getlist('ids')
    action = request.POST.get('action', '').strip()
    back = request.META.get('HTTP_REFERER') or reverse('lead_management')

    if not ids:
        messages.error(request, 'Select at least one lead first.')
        return redirect(back)

    qs = Place.objects.filter(id__in=ids)
    n = qs.count()

    if action.startswith('status:'):
        new_status = action.split(':', 1)[1]
        if new_status in dict(Place.LEAD_STATUS_CHOICES):
            qs.update(lead_status=new_status, updated_at=timezone.now())
            messages.success(request, '%d leads moved to %s.' % (n, new_status))
        else:
            messages.error(request, 'Unknown status.')

    elif action.startswith('followup:'):
        when = _parse_when('', action.split(':', 1)[1])
        if when:
            qs.update(next_follow_up=when, updated_at=timezone.now())
            messages.success(request, 'Follow-up set for %d leads on %s.'
                             % (n, timezone.localtime(when).strftime('%d %b, %H:%M')))

    elif action == 'clear_followup':
        qs.update(next_follow_up=None, follow_up_note='', updated_at=timezone.now())
        messages.success(request, 'Follow-up cleared for %d leads.' % n)

    elif action == 'suppress':
        # Stop emailing these addresses, and halt any drip already running.
        emails = [e for e in qs.values_list('email', flat=True) if e]
        for addr in emails:
            Suppression.add(addr, reason='manual', note='Bulk action from Lead Management')
        SequenceEnrollment.objects.filter(place__in=qs, status='active').update(
            status='stopped', stop_reason='Suppressed by admin')
        messages.success(request, '%d addresses added to the suppression list.' % len(emails))

    else:
        messages.error(request, 'Unknown action.')

    return redirect(back)


def suppression_list(request):
    """Who we must not email, and why."""
    q = request.GET.get('q', '').strip()
    reason = request.GET.get('reason', '').strip()

    rows = Suppression.objects.select_related('place')
    if q:
        rows = rows.filter(email__icontains=q)
    if reason:
        rows = rows.filter(reason=reason)

    counts = {r['reason']: r['count'] for r in
              Suppression.objects.values('reason').annotate(count=Count('id'))}

    page = Paginator(rows, 50).get_page(request.GET.get('page'))
    return render(request, 'scraper/email/suppressions.html', {
        'page': page, 'q': q, 'reason': reason,
        'total': Suppression.objects.count(),
        'counts': counts,
        'reason_choices': Suppression.REASON_CHOICES,
    })


@require_POST
def suppression_add(request):
    addr = request.POST.get('email', '').strip()
    if not addr or '@' not in addr:
        messages.error(request, 'Enter a valid email address.')
    else:
        Suppression.add(addr, reason='manual',
                        note=request.POST.get('note', '').strip())
        messages.success(request, '%s will no longer be emailed.' % addr)
    return redirect('suppression_list')


@require_POST
def suppression_remove(request, supp_id):
    """Undo — someone asked back in, or it was added by mistake."""
    s = get_object_or_404(Suppression, id=supp_id)
    addr = s.email
    s.delete()
    messages.success(request, '%s removed — emails can be sent again.' % addr)
    return redirect('suppression_list')

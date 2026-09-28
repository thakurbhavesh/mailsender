"""Calling Team views: caller dashboard, admin management, monitoring, reports."""
from datetime import datetime, timedelta, date

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.models import User
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.core.paginator import Paginator

from .models import (
    CallerProfile, Attendance, LeadAssignment, CallLog, Place,
)
from .calling_utils import (
    admin_required, caller_required, is_admin, is_caller, get_profile,
    open_attendance, close_attendance, pending_count, assign_leads_to_caller,
    ensure_daily_assignment,
)


# ============================================================
# ENTRY POINT — login lands here, redirect by role
# ============================================================

def role_router(request):
    """After login, route the user to the correct dashboard."""
    if not request.user.is_authenticated:
        return redirect('login')
    profile = get_profile(request.user)
    role = profile.role if profile else ('admin' if request.user.is_superuser else 'caller')

    if request.user.is_superuser or role == 'admin':
        return redirect('dashboard')          # admin → main LeadHunt dashboard
    if role == 'manager':
        return redirect('admin_monitor')      # manager → live monitor of their team
    if role == 'caller' and (not profile or profile.is_active):
        return redirect('caller_dashboard')   # caller → caller workspace
    messages.error(request, 'Your account is not active. Contact admin.')
    return redirect('logout')


# ============================================================
# CALLER VIEWS
# ============================================================

@caller_required
def caller_dashboard(request):
    """Caller's main dashboard — progress, assigned leads, attendance.
    Optimized: single aggregation query for stats + tab counts."""
    user = request.user
    profile = get_profile(user)
    today = timezone.localdate()
    now = timezone.now()

    # Ensure attendance is open
    if is_caller(user):
        open_attendance(user, request=request)
        ensure_daily_assignment(user)

    att = Attendance.objects.filter(user=user, date=today).first()

    # --- OPTIMIZED STATS ---
    # Single query for today's call outcomes
    today_outcome_data = list(
        CallLog.objects.filter(caller=user, created_at__date=today)
        .values('outcome').annotate(c=Count('id'))
    )
    outcome_counts = {row['outcome']: row['c'] for row in today_outcome_data}

    # Single aggregation: today's done count
    today_done = LeadAssignment.objects.filter(
        caller=user, assigned_at__date=today, status='done'
    ).count()

    # --- TAB FILTER ---
    tab = request.GET.get('tab', 'pending')
    q = request.GET.get('q', '').strip()

    base_qs = LeadAssignment.objects.filter(caller=user).select_related('place')

    # Compute all tab counts in ONE query
    from django.db.models import Case, When, IntegerField, Sum
    counts_agg = base_qs.aggregate(
        total=Count('id'),
        pending=Sum(Case(When(status__in=['pending', 'in_progress'], then=1), default=0, output_field=IntegerField())),
        done=Sum(Case(When(status='done', then=1), default=0, output_field=IntegerField())),
        interested=Sum(Case(When(outcome='interested', then=1), default=0, output_field=IntegerField())),
    )

    if tab == 'pending':
        leads_qs = base_qs.filter(status__in=['pending', 'in_progress'])
    elif tab == 'callbacks':
        callback_assignment_ids = CallLog.objects.filter(
            caller=user, callback_at__isnull=False, callback_at__gte=now - timedelta(hours=2)
        ).values_list('assignment_id', flat=True)
        leads_qs = base_qs.filter(id__in=callback_assignment_ids)
    elif tab == 'interested':
        leads_qs = base_qs.filter(outcome='interested')
    elif tab == 'done':
        leads_qs = base_qs.filter(status='done')
    elif tab == 'all':
        leads_qs = base_qs
    else:
        leads_qs = base_qs.filter(status__in=['pending', 'in_progress'])

    if q:
        leads_qs = leads_qs.filter(
            Q(place__name__icontains=q) |
            Q(place__phone__icontains=q) |
            Q(place__address__icontains=q) |
            Q(place__category__icontains=q)
        )

    leads_qs = leads_qs.order_by('-place__lead_score', '-assigned_at')

    # Callback reminders — limit to 5
    upcoming_callbacks = list(CallLog.objects.filter(
        caller=user, callback_at__isnull=False,
        callback_at__lte=now + timedelta(hours=24),
        callback_at__gte=now - timedelta(days=1),
        assignment__status__in=['pending', 'in_progress'],
    ).select_related('place', 'assignment').order_by('callback_at')[:5])

    # Pagination
    paginator = Paginator(leads_qs, 20)
    page = paginator.get_page(request.GET.get('page', 1))

    quota = profile.daily_quota if profile else 100
    progress_pct = int((today_done / quota) * 100) if quota else 0

    tab_counts = {
        'pending': counts_agg.get('pending') or 0,
        'callbacks': len(upcoming_callbacks),
        'interested': counts_agg.get('interested') or 0,
        'done': counts_agg.get('done') or 0,
        'all': counts_agg.get('total') or 0,
    }

    from .models import BreakLog
    open_break = BreakLog.objects.filter(user=user, end__isnull=True).first()

    ctx = {
        'profile': profile,
        'attendance': att,
        'today_total': tab_counts.get('all', 0),
        'today_done': today_done,
        'today_pending': tab_counts.get('pending', 0),
        'quota': quota,
        'progress_pct': min(progress_pct, 100),
        'outcome_counts': outcome_counts,
        'leads_qs': page,
        'leads_total': leads_qs.count(),
        'tab': tab,
        'q': q,
        'tab_counts': tab_counts,
        'upcoming_callbacks': upcoming_callbacks,
        'page_obj': page,
        'active_nav': 'dashboard',
        'open_break': open_break,
    }
    return render(request, 'scraper/calling/caller_dashboard.html', ctx)


@caller_required
@require_POST
def caller_quick_status(request, assignment_id):
    """AJAX: Quick status change without creating a call log (e.g., user sets In Progress)."""
    assignment = get_object_or_404(LeadAssignment, id=assignment_id, caller=request.user)
    new_status = request.POST.get('status', '').strip()
    if new_status not in dict(LeadAssignment.STATUS_CHOICES):
        return JsonResponse({'ok': False, 'error': 'Invalid status'}, status=400)
    assignment.status = new_status
    if new_status == 'done' and not assignment.completed_at:
        assignment.completed_at = timezone.now()
    assignment.save(update_fields=['status', 'completed_at'])
    return JsonResponse({'ok': True, 'status': new_status})


@caller_required
@require_POST
def caller_save_note(request, assignment_id):
    """AJAX: Auto-save note on a lead without creating a call log. Parses @mentions."""
    from .extra_views import process_mentions_in_text

    assignment = get_object_or_404(LeadAssignment, id=assignment_id, caller=request.user)
    note = request.POST.get('note', '').strip()
    if not note:
        return JsonResponse({'ok': True, 'saved': False})
    place = assignment.place
    sep = '\n' if place.notes else ''
    place.notes = (place.notes or '') + f"{sep}[{timezone.now():%Y-%m-%d %H:%M} {request.user.username}] {note}"
    place.save(update_fields=['notes', 'updated_at'])

    # Parse @mentions and notify
    lead_url = f'/lead/{place.id}/'
    process_mentions_in_text(note, actor=request.user, place=place, url=lead_url)

    return JsonResponse({'ok': True, 'saved': True})


@caller_required
@require_POST
def caller_quick_outcome(request, assignment_id):
    """AJAX: Log a quick call outcome (creates CallLog) and updates lead status."""
    assignment = get_object_or_404(LeadAssignment, id=assignment_id, caller=request.user)
    outcome = request.POST.get('outcome', '').strip()
    note = request.POST.get('note', '').strip()
    callback_at = request.POST.get('callback_at', '').strip()

    if outcome not in dict(CallLog.OUTCOME_CHOICES):
        return JsonResponse({'ok': False, 'error': 'Invalid outcome'}, status=400)

    cb = None
    if callback_at:
        try:
            cb = datetime.fromisoformat(callback_at)
            if timezone.is_naive(cb):
                cb = timezone.make_aware(cb)
        except ValueError:
            cb = None

    CallLog.objects.create(
        place=assignment.place, caller=request.user,
        assignment=assignment, outcome=outcome, notes=note, callback_at=cb,
    )

    terminal = {'converted', 'rejected', 'not_interested', 'wrong_number'}
    if outcome in terminal or outcome == 'interested':
        assignment.status = 'done'
        assignment.completed_at = timezone.now()
    else:
        assignment.status = 'in_progress'
    assignment.outcome = outcome
    assignment.save()

    place = assignment.place
    map_outcome_to_status = {
        'interested': 'interested', 'converted': 'converted',
        'not_interested': 'rejected', 'rejected': 'rejected',
        'answered': 'contacted', 'callback': 'contacted',
        'no_answer': 'contacted', 'busy': 'contacted',
        'wrong_number': 'rejected',
    }
    final_status = map_outcome_to_status.get(outcome)
    if final_status:
        place.lead_status = final_status
    if note:
        sep = '\n' if place.notes else ''
        place.notes = (place.notes or '') + f"{sep}[{timezone.now():%Y-%m-%d %H:%M} {request.user.username}] {note}"
    # A callback the caller booked is the lead's next follow-up, so the admin
    # queue shows it too instead of it living only in the caller's own list.
    fields = ['lead_status', 'notes', 'updated_at']
    if cb:
        place.next_follow_up = cb
        place.follow_up_note = 'Callback requested'
        fields += ['next_follow_up', 'follow_up_note']
    elif outcome in {'converted', 'rejected', 'not_interested', 'wrong_number'}:
        # Closed either way — nothing left to chase.
        place.next_follow_up = None
        place.follow_up_note = ''
        fields += ['next_follow_up', 'follow_up_note']

    place.save(update_fields=fields)

    # Process @mentions in note
    if note:
        from .extra_views import process_mentions_in_text
        process_mentions_in_text(note, actor=request.user, place=place, url=f'/lead/{place.id}/')

    # Notify admins on conversion
    if outcome == 'converted':
        from .extra_views import _create_notification
        from django.contrib.auth.models import User
        admins = User.objects.filter(is_superuser=True).exclude(id=request.user.id)[:5]
        for adm in admins:
            _create_notification(
                user=adm, kind='deal_closed',
                title=f'🎉 {request.user.username} closed a deal!',
                body=f'{place.name} converted by {request.user.username}.',
                url=f'/admin-panel/calling/members/{request.user.id}/detail/',
                actor=request.user, place=place,
            )

    new_pending = pending_count(request.user)
    if new_pending == 0:
        # Only auto-refill if enabled for this caller
        prof = get_profile(request.user)
        if prof and prof.auto_assign_enabled:
            assign_leads_to_caller(request.user)
            new_pending = pending_count(request.user)

    today = timezone.localdate()
    today_done = LeadAssignment.objects.filter(
        caller=request.user, assigned_at__date=today, status='done'
    ).count()
    return JsonResponse({
        'ok': True,
        'assignment_status': assignment.status,
        'pending_count': new_pending,
        'today_done': today_done,
    })


@caller_required
@require_POST
def caller_update_lead(request, assignment_id):
    """Caller logs a call outcome on a specific assignment."""
    assignment = get_object_or_404(LeadAssignment, id=assignment_id, caller=request.user)
    outcome = request.POST.get('outcome', '').strip()
    notes = request.POST.get('notes', '').strip()
    new_status = request.POST.get('lead_status', '').strip()
    callback_at = request.POST.get('callback_at', '').strip()

    valid_outcomes = dict(CallLog.OUTCOME_CHOICES).keys()
    if outcome not in valid_outcomes:
        return JsonResponse({'ok': False, 'error': 'Invalid outcome'}, status=400)

    cb = None
    if callback_at:
        try:
            cb = datetime.fromisoformat(callback_at)
            if timezone.is_naive(cb):
                cb = timezone.make_aware(cb)
        except ValueError:
            cb = None

    # Save call log
    CallLog.objects.create(
        place=assignment.place,
        caller=request.user,
        assignment=assignment,
        outcome=outcome,
        notes=notes,
        callback_at=cb,
    )

    # Update assignment + lead status
    terminal = {'converted', 'rejected', 'not_interested', 'wrong_number'}
    if outcome in terminal or outcome == 'interested':
        assignment.status = 'done'
        assignment.completed_at = timezone.now()
    else:
        assignment.status = 'in_progress'
    assignment.outcome = outcome
    assignment.save()

    # Update Place lead_status if user picked one
    place = assignment.place
    map_outcome_to_status = {
        'interested': 'interested',
        'converted': 'converted',
        'not_interested': 'rejected',
        'rejected': 'rejected',
        'answered': 'contacted',
        'callback': 'contacted',
        'no_answer': 'contacted',
        'busy': 'contacted',
        'wrong_number': 'rejected',
    }
    final_status = new_status if new_status in dict(Place.LEAD_STATUS_CHOICES) else map_outcome_to_status.get(outcome)
    if final_status:
        place.lead_status = final_status
    if notes:
        sep = '\n' if place.notes else ''
        place.notes = (place.notes or '') + f"{sep}[{timezone.now():%Y-%m-%d %H:%M} {request.user.username}] {notes}"
    # A callback the caller booked is the lead's next follow-up, so the admin
    # queue shows it too instead of it living only in the caller's own list.
    fields = ['lead_status', 'notes', 'updated_at']
    if cb:
        place.next_follow_up = cb
        place.follow_up_note = 'Callback requested'
        fields += ['next_follow_up', 'follow_up_note']
    elif outcome in {'converted', 'rejected', 'not_interested', 'wrong_number'}:
        # Closed either way — nothing left to chase.
        place.next_follow_up = None
        place.follow_up_note = ''
        fields += ['next_follow_up', 'follow_up_note']

    place.save(update_fields=fields)

    # Auto-assign next batch if pending hit zero (only if enabled)
    if pending_count(request.user) == 0:
        prof = get_profile(request.user)
        if prof and prof.auto_assign_enabled:
            assign_leads_to_caller(request.user)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'assignment_status': assignment.status})
    messages.success(request, f'Lead updated: {outcome}')
    return redirect('caller_dashboard')


@caller_required
def caller_history(request):
    """Date-wise performance view for caller."""
    user = request.user
    today = timezone.localdate()
    preset = request.GET.get('preset', 'today')
    start_str = request.GET.get('start', '')
    end_str = request.GET.get('end', '')

    if preset == 'yesterday':
        start = end = today - timedelta(days=1)
    elif preset == 'last7':
        start, end = today - timedelta(days=6), today
    elif preset == 'last30':
        start, end = today - timedelta(days=29), today
    elif preset == 'custom' and start_str and end_str:
        try:
            start = datetime.strptime(start_str, '%Y-%m-%d').date()
            end = datetime.strptime(end_str, '%Y-%m-%d').date()
        except ValueError:
            start = end = today
    else:
        preset = 'today'
        start = end = today

    calls = CallLog.objects.filter(caller=user, created_at__date__gte=start, created_at__date__lte=end)
    assignments = LeadAssignment.objects.filter(caller=user, assigned_at__date__gte=start, assigned_at__date__lte=end)
    attendances = Attendance.objects.filter(user=user, date__gte=start, date__lte=end).order_by('-date')

    # Per-day aggregation
    daily = []
    days = (end - start).days + 1
    for i in range(days):
        d = start + timedelta(days=i)
        day_assigns = assignments.filter(assigned_at__date=d)
        day_calls = calls.filter(created_at__date=d)
        att = attendances.filter(date=d).first()
        daily.append({
            'date': d,
            'assigned': day_assigns.count(),
            'done': day_assigns.filter(status='done').count(),
            'calls': day_calls.count(),
            'converted': day_calls.filter(outcome='converted').count(),
            'interested': day_calls.filter(outcome='interested').count(),
            'attendance': att,
        })
    daily.reverse()

    totals = {
        'assigned': assignments.count(),
        'done': assignments.filter(status='done').count(),
        'calls': calls.count(),
        'converted': calls.filter(outcome='converted').count(),
        'interested': calls.filter(outcome='interested').count(),
        'minutes': sum(a.total_minutes for a in attendances if a.total_minutes),
    }

    outcome_breakdown = list(calls.values('outcome').annotate(c=Count('id')).order_by('-c'))

    # Also gather break-related context for sidebar
    from .models import BreakLog
    open_break = BreakLog.objects.filter(user=user, end__isnull=True).first()
    today = timezone.localdate()
    att_today = Attendance.objects.filter(user=user, date=today).first()

    return render(request, 'scraper/calling/caller_history.html', {
        'preset': preset,
        'start': start, 'end': end,
        'daily': daily,
        'totals': totals,
        'outcome_breakdown': outcome_breakdown,
        'attendances': attendances,
        'active_nav': 'history',
        'open_break': open_break,
        'attendance': att_today,
    })


@caller_required
def caller_attendance(request):
    """Caller's own attendance calendar view + manual check-in/out."""
    from calendar import monthcalendar
    from .models import BreakLog

    user = request.user
    today = timezone.localdate()

    # Allow viewing other months
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
    except ValueError:
        year, month = today.year, today.month

    # Bounds
    if month < 1 or month > 12:
        year, month = today.year, today.month

    # Build calendar grid (list of weeks; each week list of day numbers, 0 = empty)
    weeks = monthcalendar(year, month)

    # Attendance for the month
    from datetime import date as _date
    first_day = _date(year, month, 1)
    if month == 12:
        last_day = _date(year + 1, 1, 1)
    else:
        last_day = _date(year, month + 1, 1)

    atts = {a.date.day: a for a in Attendance.objects.filter(
        user=user, date__gte=first_day, date__lt=last_day,
    )}

    # Build day cells
    grid = []
    total_days_worked = 0
    total_minutes = 0
    for week in weeks:
        row = []
        for d in week:
            if d == 0:
                row.append({'day': 0, 'empty': True})
                continue
            cell_date = _date(year, month, d)
            att = atts.get(d)
            is_today = (cell_date == today)
            is_future = cell_date > today
            is_weekend = cell_date.weekday() >= 5
            if att:
                total_days_worked += 1
                mins = att.total_minutes_live
                total_minutes += mins
                row.append({
                    'day': d, 'date': cell_date,
                    'attended': True,
                    'minutes': mins,
                    'hours_display': f"{mins // 60}h {mins % 60}m",
                    'check_in': att.check_in,
                    'check_out': att.check_out,
                    'is_active': att.check_out is None,
                    'is_today': is_today,
                    'is_future': is_future,
                    'is_weekend': is_weekend,
                })
            else:
                row.append({
                    'day': d, 'date': cell_date,
                    'attended': False,
                    'is_today': is_today,
                    'is_future': is_future,
                    'is_weekend': is_weekend,
                })
        grid.append(row)

    # Today's attendance + break info
    today_att = Attendance.objects.filter(user=user, date=today).first()
    open_break = BreakLog.objects.filter(user=user, end__isnull=True).first()

    # Today's breaks
    today_breaks = BreakLog.objects.filter(user=user, start__date=today).order_by('-start')[:10]

    # Prev / next month nav
    if month == 1:
        prev_m = (year - 1, 12)
    else:
        prev_m = (year, month - 1)
    if month == 12:
        next_m = (year + 1, 1)
    else:
        next_m = (year, month + 1)

    month_name = first_day.strftime('%B %Y')

    return render(request, 'scraper/calling/caller_attendance.html', {
        'grid': grid,
        'year': year, 'month': month,
        'month_name': month_name,
        'prev_year': prev_m[0], 'prev_month': prev_m[1],
        'next_year': next_m[0], 'next_month': next_m[1],
        'total_days_worked': total_days_worked,
        'total_minutes': total_minutes,
        'total_hours_display': f"{total_minutes // 60}h {total_minutes % 60}m",
        'today_att': today_att,
        'open_break': open_break,
        'today_breaks': today_breaks,
        'today': today,
        'active_nav': 'attendance',
        'attendance': today_att,  # for sidebar
    })


@caller_required
def caller_attendance_checkin(request):
    """Manual check-in for caller."""
    if request.method != 'POST':
        return redirect('caller_attendance')
    from .calling_utils import open_attendance
    open_attendance(request.user, request=request)
    messages.success(request, 'Checked in successfully ✓')
    return redirect('caller_attendance')


@caller_required
def caller_attendance_checkout(request):
    """Manual check-out for caller (without logging out)."""
    if request.method != 'POST':
        return redirect('caller_attendance')
    from .calling_utils import close_attendance
    close_attendance(request.user)
    messages.success(request, 'Checked out for now. Click "Check In" to resume.')
    return redirect('caller_attendance')


@caller_required
def caller_lead_detail(request, place_id):
    """Read-only detail page for a lead — caller can see full info."""
    assignment = get_object_or_404(LeadAssignment, place_id=place_id, caller=request.user)
    place = assignment.place
    call_logs = CallLog.objects.filter(place=place, caller=request.user).order_by('-created_at')

    from .models import BreakLog
    open_break = BreakLog.objects.filter(user=request.user, end__isnull=True).first()
    today = timezone.localdate()
    att_today = Attendance.objects.filter(user=request.user, date=today).first()

    return render(request, 'scraper/calling/caller_lead_detail.html', {
        'assignment': assignment,
        'place': place,
        'call_logs': call_logs,
        'open_break': open_break,
        'attendance': att_today,
    })


# ============================================================
# ADMIN — MEMBER MANAGEMENT
# ============================================================

@admin_required
def members_list(request):
    """List all callers + admins; admin can add new."""
    profiles = CallerProfile.objects.select_related('user').order_by('-is_active', 'user__username')
    return render(request, 'scraper/calling/members_list.html', {'profiles': profiles})


@admin_required
def member_add(request):
    """Create new caller (or admin) user + profile."""
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '').strip()
        full_name = request.POST.get('full_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        role = request.POST.get('role', 'caller')
        quota = int(request.POST.get('daily_quota', '100') or 100)

        if not username or not password:
            messages.error(request, 'Username aur password required.')
            return redirect('member_add')
        if User.objects.filter(username=username).exists():
            messages.error(request, 'Yeh username already exists.')
            return redirect('member_add')

        user = User.objects.create_user(username=username, password=password, email=email)
        if full_name:
            parts = full_name.split(' ', 1)
            user.first_name = parts[0]
            if len(parts) > 1:
                user.last_name = parts[1]
        if role == 'admin':
            user.is_staff = True
            user.is_superuser = True
        user.save()

        CallerProfile.objects.create(
            user=user, role=role, phone=phone, daily_quota=quota, is_active=True,
            auto_assign_enabled=(request.POST.get('auto_assign_enabled') == 'on'),
        )
        messages.success(request, f'Member "{username}" added successfully.')
        return redirect('members_list')

    return render(request, 'scraper/calling/member_form.html', {'member': None})


@admin_required
def member_edit(request, user_id):
    profile = get_object_or_404(CallerProfile, user_id=user_id)
    user = profile.user
    if request.method == 'POST':
        user.first_name = request.POST.get('first_name', '').strip()
        user.last_name = request.POST.get('last_name', '').strip()
        user.email = request.POST.get('email', '').strip()
        new_password = request.POST.get('password', '').strip()
        if new_password:
            user.set_password(new_password)
        # Don't allow demoting yourself
        new_role = request.POST.get('role', profile.role)
        if user.id != request.user.id:
            profile.role = new_role
            user.is_staff = (new_role == 'admin')
            user.is_superuser = (new_role == 'admin')
        profile.phone = request.POST.get('phone', '').strip()
        profile.daily_quota = int(request.POST.get('daily_quota', '100') or 100)
        profile.is_active = request.POST.get('is_active') == 'on'
        profile.auto_assign_enabled = request.POST.get('auto_assign_enabled') == 'on'
        profile.notes = request.POST.get('notes', '').strip()
        user.save()
        profile.save()
        messages.success(request, 'Member updated.')
        return redirect('members_list')

    return render(request, 'scraper/calling/member_form.html', {'member': profile})


@admin_required
@require_POST
def member_delete(request, user_id):
    profile = get_object_or_404(CallerProfile, user_id=user_id)
    if profile.user.id == request.user.id:
        messages.error(request, 'Cannot delete yourself.')
        return redirect('members_list')
    username = profile.user.username
    profile.user.delete()  # cascades
    messages.success(request, f'Deleted member "{username}".')
    return redirect('members_list')


@admin_required
def manual_assign(request):
    """Admin manually assigns specific leads to specific callers.
    Supports filtering by area/city/category/source/score before selection."""
    from django.db.models import Q
    from .models import ScrapeJob

    # Filters
    q = request.GET.get('q', '').strip()
    city = request.GET.get('city', '').strip()
    category = request.GET.get('category', '').strip()
    source = request.GET.get('source', '').strip()
    status_filter = request.GET.get('lead_status', '').strip()
    min_score = request.GET.get('min_score', '').strip()
    only_unassigned = request.GET.get('only_unassigned', '1') == '1'
    only_with_phone = request.GET.get('only_with_phone', '1') == '1'

    leads = Place.objects.all()
    if q:
        leads = leads.filter(
            Q(name__icontains=q) | Q(category__icontains=q)
            | Q(address__icontains=q) | Q(phone__icontains=q)
        )
    if city:
        leads = leads.filter(address__icontains=city)
    if category:
        leads = leads.filter(category__icontains=category)
    if source:
        leads = leads.filter(source=source)
    if status_filter:
        leads = leads.filter(lead_status=status_filter)
    if min_score:
        try:
            leads = leads.filter(lead_score__gte=int(min_score))
        except ValueError:
            pass
    if only_with_phone:
        leads = leads.exclude(phone='')
    if only_unassigned:
        assigned_place_ids = LeadAssignment.objects.values_list('place_id', flat=True)
        leads = leads.exclude(id__in=assigned_place_ids)

    leads = leads.order_by('-lead_score', '-id')

    # Distinct cities (extract last part of address) and categories — for filter dropdowns
    all_categories = list(
        Place.objects.exclude(category='').values_list('category', flat=True).distinct().order_by('category')[:80]
    )
    all_sources = list(
        Place.objects.values_list('source', flat=True).distinct().order_by('source')[:30]
    )

    paginator = Paginator(leads, 50)
    page = paginator.get_page(request.GET.get('page', 1))

    # Available callers
    callers = CallerProfile.objects.select_related('user').filter(
        is_active=True, role__in=['caller', 'manager']
    ).order_by('user__username')

    # Per-caller pending count so admin sees current load
    caller_loads = []
    for c in callers:
        pend = LeadAssignment.objects.filter(
            caller=c.user, status__in=['pending', 'in_progress']
        ).count()
        caller_loads.append({
            'profile': c, 'pending': pend, 'quota': c.daily_quota,
            'capacity_pct': min(100, int((pend / c.daily_quota) * 100)) if c.daily_quota else 0,
        })

    total_filtered = leads.count()

    return render(request, 'scraper/calling/manual_assign.html', {
        'page_obj': page,
        'leads_page': page,
        'caller_loads': caller_loads,
        'all_categories': all_categories,
        'all_sources': all_sources,
        'lead_status_choices': Place.LEAD_STATUS_CHOICES,
        'total_filtered': total_filtered,
        'filters': {
            'q': q, 'city': city, 'category': category, 'source': source,
            'lead_status': status_filter, 'min_score': min_score,
            'only_unassigned': only_unassigned, 'only_with_phone': only_with_phone,
        },
    })


@admin_required
@require_POST
def manual_assign_submit(request):
    """Submit a manual assignment: list of place_ids → one caller."""
    try:
        caller_id = int(request.POST.get('caller_id', 0))
    except ValueError:
        caller_id = 0
    place_ids = request.POST.getlist('place_ids')
    if not caller_id or not place_ids:
        messages.error(request, 'Pick a caller and at least one lead.')
        return redirect('manual_assign')

    caller_profile = get_object_or_404(CallerProfile, user_id=caller_id)
    caller_user = caller_profile.user

    # Build IDs cleanly
    try:
        ids = [int(x) for x in place_ids if x]
    except ValueError:
        ids = []

    # Skip places that are already assigned to ANYONE
    already_assigned = set(
        LeadAssignment.objects.filter(place_id__in=ids).values_list('place_id', flat=True)
    )
    eligible = [pid for pid in ids if pid not in already_assigned]

    created = 0
    bulk = [
        LeadAssignment(place_id=pid, caller=caller_user, assigned_by=request.user, status='pending')
        for pid in eligible
    ]
    if bulk:
        LeadAssignment.objects.bulk_create(bulk, ignore_conflicts=True)
        created = len(bulk)

    skipped = len(ids) - created
    if created:
        # Notify the caller
        from .extra_views import _create_notification
        _create_notification(
            user=caller_user, kind='lead_assigned',
            title=f'📋 {created} new lead{"" if created == 1 else "s"} assigned',
            body=f'{request.user.username} assigned {created} lead(s) to you.',
            url='/calling/',
            actor=request.user,
        )
    messages.success(request, f'Assigned {created} lead(s) to {caller_user.username}. {skipped} skipped (already assigned).')
    return redirect('manual_assign')


@admin_required
@require_POST
def member_assign_now(request, user_id):
    """Force-assign daily quota to a caller right now."""
    profile = get_object_or_404(CallerProfile, user_id=user_id)
    count = int(request.POST.get('count', profile.daily_quota) or profile.daily_quota)
    n = assign_leads_to_caller(profile.user, count=count, assigned_by=request.user)
    messages.success(request, f'Assigned {n} leads to {profile.user.username}.')
    return redirect('members_list')


@admin_required
@require_POST
def member_reset_pending(request, user_id):
    """Release a caller's pending leads back into the pool."""
    profile = get_object_or_404(CallerProfile, user_id=user_id)
    deleted, _ = LeadAssignment.objects.filter(
        caller=profile.user, status__in=['pending', 'in_progress']
    ).delete()
    messages.success(request, f'Released {deleted} pending leads from {profile.user.username}.')
    return redirect('members_list')


# ============================================================
# ADMIN — MONITORING
# ============================================================

@admin_required
def admin_monitor(request):
    """Live dashboard — see who's online, what they're doing."""
    today = timezone.localdate()

    profiles = CallerProfile.objects.select_related('user').filter(is_active=True).order_by('user__username')

    rows = []
    online_count = 0
    total_calls_today = 0
    total_conversions_today = 0

    for p in profiles:
        att = Attendance.objects.filter(user=p.user, date=today).first()
        online = bool(att and not att.check_out)
        if online:
            online_count += 1

        day_assigns = LeadAssignment.objects.filter(caller=p.user, assigned_at__date=today)
        day_done = day_assigns.filter(status='done').count()
        day_pending = day_assigns.exclude(status='done').count()

        day_calls = CallLog.objects.filter(caller=p.user, created_at__date=today)
        calls_n = day_calls.count()
        converted_n = day_calls.filter(outcome='converted').count()
        interested_n = day_calls.filter(outcome='interested').count()

        total_calls_today += calls_n
        total_conversions_today += converted_n

        rows.append({
            'profile': p,
            'online': online,
            'attendance': att,
            'assigned': day_assigns.count(),
            'done': day_done,
            'pending': day_pending,
            'calls': calls_n,
            'converted': converted_n,
            'interested': interested_n,
        })

    return render(request, 'scraper/calling/admin_monitor.html', {
        'rows': rows,
        'online_count': online_count,
        'total_callers': len(profiles),
        'total_calls_today': total_calls_today,
        'total_conversions_today': total_conversions_today,
        'today': today,
    })


@admin_required
def admin_caller_attendance(request, user_id):
    """Admin views a specific caller's attendance calendar."""
    from calendar import monthcalendar
    from .models import BreakLog
    from datetime import date as _date

    profile = get_object_or_404(CallerProfile, user_id=user_id)
    user = profile.user
    today = timezone.localdate()

    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
    except ValueError:
        year, month = today.year, today.month
    if month < 1 or month > 12:
        year, month = today.year, today.month

    weeks = monthcalendar(year, month)
    first_day = _date(year, month, 1)
    last_day = _date(year + 1, 1, 1) if month == 12 else _date(year, month + 1, 1)

    atts = {a.date.day: a for a in Attendance.objects.filter(
        user=user, date__gte=first_day, date__lt=last_day
    )}

    grid = []
    total_days_worked = total_minutes = 0
    for week in weeks:
        row = []
        for d in week:
            if d == 0:
                row.append({'day': 0, 'empty': True})
                continue
            cell_date = _date(year, month, d)
            att = atts.get(d)
            if att:
                total_days_worked += 1
                mins = att.total_minutes_live
                total_minutes += mins
                row.append({
                    'day': d, 'date': cell_date, 'attended': True,
                    'minutes': mins,
                    'hours_display': f"{mins // 60}h {mins % 60}m",
                    'check_in': att.check_in, 'check_out': att.check_out,
                    'is_active': att.check_out is None,
                    'is_today': cell_date == today,
                    'is_future': cell_date > today,
                    'is_weekend': cell_date.weekday() >= 5,
                })
            else:
                row.append({
                    'day': d, 'date': cell_date, 'attended': False,
                    'is_today': cell_date == today,
                    'is_future': cell_date > today,
                    'is_weekend': cell_date.weekday() >= 5,
                })
        grid.append(row)

    if month == 1:
        prev_m = (year - 1, 12)
    else:
        prev_m = (year, month - 1)
    next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    # All breaks for this month
    breaks = BreakLog.objects.filter(
        user=user, start__date__gte=first_day, start__date__lt=last_day
    ).order_by('-start')[:50]

    return render(request, 'scraper/calling/admin_caller_attendance.html', {
        'profile': profile,
        'grid': grid,
        'year': year, 'month': month,
        'month_name': first_day.strftime('%B %Y'),
        'prev_year': prev_m[0], 'prev_month': prev_m[1],
        'next_year': next_m[0], 'next_month': next_m[1],
        'total_days_worked': total_days_worked,
        'total_minutes': total_minutes,
        'total_hours_display': f"{total_minutes // 60}h {total_minutes % 60}m",
        'breaks': breaks,
        'today': today,
    })


@admin_required
def admin_caller_detail(request, user_id):
    """Drill into a specific caller — see all their activity with date filter."""
    profile = get_object_or_404(CallerProfile, user_id=user_id)
    user = profile.user
    today = timezone.localdate()

    preset = request.GET.get('preset', 'today')
    if preset == 'yesterday':
        start = end = today - timedelta(days=1)
    elif preset == 'last7':
        start, end = today - timedelta(days=6), today
    elif preset == 'last30':
        start, end = today - timedelta(days=29), today
    elif preset == 'custom':
        try:
            start = datetime.strptime(request.GET.get('start', ''), '%Y-%m-%d').date()
            end = datetime.strptime(request.GET.get('end', ''), '%Y-%m-%d').date()
        except ValueError:
            start = end = today
    else:
        start = end = today

    calls = CallLog.objects.filter(caller=user, created_at__date__gte=start, created_at__date__lte=end).select_related('place')
    assignments = LeadAssignment.objects.filter(caller=user, assigned_at__date__gte=start, assigned_at__date__lte=end)
    attendances = Attendance.objects.filter(user=user, date__gte=start, date__lte=end).order_by('-date')

    totals = {
        'assigned': assignments.count(),
        'done': assignments.filter(status='done').count(),
        'calls': calls.count(),
        'converted': calls.filter(outcome='converted').count(),
        'interested': calls.filter(outcome='interested').count(),
        'minutes': sum(a.total_minutes for a in attendances if a.total_minutes),
    }

    outcome_breakdown = list(calls.values('outcome').annotate(c=Count('id')).order_by('-c'))

    paginator = Paginator(calls.order_by('-created_at'), 30)
    page = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'scraper/calling/admin_caller_detail.html', {
        'profile': profile,
        'preset': preset, 'start': start, 'end': end,
        'totals': totals,
        'outcome_breakdown': outcome_breakdown,
        'attendances': attendances,
        'calls': page,
        'page_obj': page,
    })

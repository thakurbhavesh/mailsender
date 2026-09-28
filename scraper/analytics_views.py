"""Analytics views: leaderboard, conversion funnel, activity heatmap."""
from datetime import datetime, timedelta, date
from collections import defaultdict
from decimal import Decimal

from django.shortcuts import render, get_object_or_404
from django.db.models import Count, Q, Sum, F
from django.utils import timezone

from .models import (
    Place, ScrapeJob, CallerProfile, Attendance, LeadAssignment, CallLog,
    CallerGoal, IncentiveRule, BreakLog, Notification,
)
from .calling_utils import admin_required


def _resolve_range(request):
    """Returns (start_date, end_date, preset_label)."""
    today = timezone.localdate()
    preset = request.GET.get('preset', 'today')
    if preset == 'yesterday':
        d = today - timedelta(days=1)
        return d, d, preset
    if preset == 'last7':
        return today - timedelta(days=6), today, preset
    if preset == 'last30':
        return today - timedelta(days=29), today, preset
    if preset == 'month':
        return today.replace(day=1), today, preset
    if preset == 'custom':
        try:
            s = datetime.strptime(request.GET.get('start', ''), '%Y-%m-%d').date()
            e = datetime.strptime(request.GET.get('end', ''), '%Y-%m-%d').date()
            return s, e, preset
        except ValueError:
            pass
    return today, today, 'today'


@admin_required
def leaderboard(request):
    start, end, preset = _resolve_range(request)
    profiles = CallerProfile.objects.select_related('user').filter(is_active=True, role='caller')

    rows = []
    for p in profiles:
        u = p.user
        assigns = LeadAssignment.objects.filter(caller=u, assigned_at__date__gte=start, assigned_at__date__lte=end)
        calls = CallLog.objects.filter(caller=u, created_at__date__gte=start, created_at__date__lte=end)
        attendances = Attendance.objects.filter(user=u, date__gte=start, date__lte=end)
        breaks_min = BreakLog.objects.filter(user=u, start__date__gte=start, start__date__lte=end).aggregate(s=Sum('duration_minutes'))['s'] or 0
        worked_min = sum(a.total_minutes for a in attendances if a.total_minutes)
        net_min = max(0, worked_min - breaks_min)

        n_calls = calls.count()
        n_answered = calls.filter(outcome='answered').count()
        n_interested = calls.filter(outcome='interested').count()
        n_converted = calls.filter(outcome='converted').count()
        n_assigned = assigns.count()
        n_done = assigns.filter(status='done').count()

        conv_rate = round((n_converted / n_calls) * 100, 1) if n_calls else 0
        ans_rate = round((n_answered / n_calls) * 100, 1) if n_calls else 0
        completion = round((n_done / n_assigned) * 100, 1) if n_assigned else 0

        # Composite score for ranking
        score = (n_converted * 30) + (n_interested * 10) + (n_answered * 2) + (n_calls * 0.5)

        rows.append({
            'user': u,
            'profile': p,
            'calls': n_calls,
            'answered': n_answered,
            'interested': n_interested,
            'converted': n_converted,
            'assigned': n_assigned,
            'done': n_done,
            'conv_rate': conv_rate,
            'ans_rate': ans_rate,
            'completion': completion,
            'minutes': net_min,
            'score': round(score, 1),
        })

    rows.sort(key=lambda r: (-r['converted'], -r['interested'], -r['calls']))
    for i, r in enumerate(rows, start=1):
        r['rank'] = i

    return render(request, 'scraper/analytics/leaderboard.html', {
        'rows': rows, 'start': start, 'end': end, 'preset': preset,
    })


@admin_required
def funnel(request):
    start, end, preset = _resolve_range(request)
    caller_id = request.GET.get('caller', '').strip()

    # Top of funnel: leads created in range
    places_qs = Place.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
    scraped = places_qs.count()
    if caller_id:
        try:
            caller_id = int(caller_id)
        except ValueError:
            caller_id = None
    else:
        caller_id = None

    assigns_qs = LeadAssignment.objects.filter(assigned_at__date__gte=start, assigned_at__date__lte=end)
    calls_qs = CallLog.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
    if caller_id:
        assigns_qs = assigns_qs.filter(caller_id=caller_id)
        calls_qs = calls_qs.filter(caller_id=caller_id)

    assigned = assigns_qs.count()
    called = calls_qs.values('place_id').distinct().count()
    answered = calls_qs.filter(outcome='answered').values('place_id').distinct().count()
    interested = calls_qs.filter(outcome='interested').values('place_id').distinct().count()
    converted = calls_qs.filter(outcome='converted').values('place_id').distinct().count()

    # Sequential drop-off
    stages = [
        ('Scraped / Available', scraped, '#6366f1'),
        ('Assigned', assigned, '#8b5cf6'),
        ('Called', called, '#ec4899'),
        ('Answered', answered, '#f59e0b'),
        ('Interested', interested, '#10b981'),
        ('Converted', converted, '#16a34a'),
    ]
    max_val = max((v for _, v, _ in stages), default=1) or 1
    funnel_data = []
    prev = None
    for label, val, color in stages:
        pct_of_top = round((val / max_val) * 100, 1) if max_val else 0
        drop = None if prev is None else (round(((prev - val) / prev) * 100, 1) if prev else 0)
        funnel_data.append({
            'label': label, 'val': val, 'color': color,
            'pct_of_top': pct_of_top, 'drop': drop,
        })
        prev = val

    # Source breakdown
    by_source = list(places_qs.values('source').annotate(c=Count('id')).order_by('-c'))

    callers = CallerProfile.objects.select_related('user').filter(role='caller', is_active=True)

    return render(request, 'scraper/analytics/funnel.html', {
        'start': start, 'end': end, 'preset': preset,
        'funnel_data': funnel_data,
        'by_source': by_source,
        'callers': callers,
        'caller_id': caller_id,
    })


@admin_required
def heatmap(request):
    start, end, preset = _resolve_range(request)
    metric = request.GET.get('metric', 'calls')

    calls = CallLog.objects.filter(created_at__date__gte=start, created_at__date__lte=end)

    # 7 days × 24 hours grid
    grid = [[0] * 24 for _ in range(7)]  # 0=Mon ... 6=Sun
    converted_grid = [[0] * 24 for _ in range(7)]

    # Use Django's database functions when possible — but simpler to iterate
    for c in calls.only('created_at', 'outcome'):
        # Convert UTC to local
        local = timezone.localtime(c.created_at)
        wd = local.weekday()
        hr = local.hour
        grid[wd][hr] += 1
        if c.outcome == 'converted':
            converted_grid[wd][hr] += 1

    if metric == 'conversion':
        # Use conversion grid
        used = converted_grid
    else:
        used = grid

    max_val = max((max(row) for row in used), default=1) or 1
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    hours = list(range(24))

    rendered = []
    for di, dayname in enumerate(days):
        row_cells = []
        for h in hours:
            v = used[di][h]
            intensity = (v / max_val) if max_val else 0
            row_cells.append({'v': v, 'pct': round(intensity * 100), 'dh': f'{dayname} {h:02d}:00'})
        rendered.append({'day': dayname, 'cells': row_cells})

    # Top hours
    flat = []
    for di in range(7):
        for h in range(24):
            if used[di][h]:
                flat.append({'day': days[di], 'hour': h, 'v': used[di][h]})
    flat.sort(key=lambda x: -x['v'])
    top_slots = flat[:6]

    return render(request, 'scraper/analytics/heatmap.html', {
        'start': start, 'end': end, 'preset': preset,
        'rendered': rendered, 'hours': hours, 'metric': metric,
        'max_val': max_val, 'top_slots': top_slots,
        'total_calls': sum(sum(r) for r in grid),
    })


# ============================================================
# GOALS & INCENTIVES
# ============================================================

@admin_required
def goals_list(request):
    """List all callers with their current-month goals + progress."""
    today = timezone.localdate()
    month_start = today.replace(day=1)
    profiles = CallerProfile.objects.select_related('user').filter(role='caller')

    rows = []
    for p in profiles:
        goal = CallerGoal.objects.filter(user=p.user, month=month_start).first()
        calls_n = CallLog.objects.filter(caller=p.user, created_at__date__gte=month_start, created_at__date__lte=today).count()
        conv_n = CallLog.objects.filter(caller=p.user, outcome='converted', created_at__date__gte=month_start, created_at__date__lte=today).count()
        rows.append({
            'profile': p, 'goal': goal,
            'calls': calls_n, 'conversions': conv_n,
            'calls_pct': round((calls_n / goal.target_calls) * 100, 1) if (goal and goal.target_calls) else 0,
            'conv_pct': round((conv_n / goal.target_conversions) * 100, 1) if (goal and goal.target_conversions) else 0,
        })

    return render(request, 'scraper/analytics/goals.html', {
        'rows': rows, 'month_start': month_start,
    })


@admin_required
def goal_edit(request, user_id):
    profile = get_object_or_404(
        CallerProfile.objects.select_related('user'), user_id=user_id)
    today = timezone.localdate()
    month_str = request.GET.get('month') or today.strftime('%Y-%m')
    try:
        month_start = datetime.strptime(month_str + '-01', '%Y-%m-%d').date()
    except ValueError:
        month_start = today.replace(day=1)

    goal, _ = CallerGoal.objects.get_or_create(user=profile.user, month=month_start)

    if request.method == 'POST':
        goal.target_calls = int(request.POST.get('target_calls', 0) or 0)
        goal.target_conversions = int(request.POST.get('target_conversions', 0) or 0)
        try:
            goal.target_revenue = Decimal(request.POST.get('target_revenue', '0') or 0)
        except Exception:
            goal.target_revenue = Decimal('0')
        goal.notes = request.POST.get('notes', '')
        goal.save()
        from django.contrib import messages
        messages.success(request, f'Goal saved for {profile.user.username} ({month_start:%b %Y})')
        from django.shortcuts import redirect
        return redirect('goals_list')

    return render(request, 'scraper/analytics/goal_form.html', {
        'profile': profile, 'goal': goal, 'month_start': month_start,
    })


@admin_required
def incentives(request):
    """Calculate incentives per caller for selected month."""
    today = timezone.localdate()
    month_str = request.GET.get('month') or today.strftime('%Y-%m')
    try:
        month_start = datetime.strptime(month_str + '-01', '%Y-%m-%d').date()
    except ValueError:
        month_start = today.replace(day=1)

    # Month end
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1) - timedelta(days=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1) - timedelta(days=1)

    rule = IncentiveRule.current()

    if request.method == 'POST' and request.POST.get('action') == 'save_rule':
        rule = rule or IncentiveRule(name='Default')
        rule.name = request.POST.get('name', 'Default')
        try:
            rule.per_answered_call = Decimal(request.POST.get('per_answered_call', '0') or 0)
            rule.per_interested = Decimal(request.POST.get('per_interested', '0') or 0)
            rule.per_conversion = Decimal(request.POST.get('per_conversion', '0') or 0)
            rule.bonus_threshold = int(request.POST.get('bonus_threshold', 0) or 0)
            rule.bonus_amount = Decimal(request.POST.get('bonus_amount', '0') or 0)
        except Exception:
            pass
        rule.is_active = True
        rule.save()
        from django.contrib import messages
        messages.success(request, 'Incentive rule updated.')
        from django.shortcuts import redirect
        return redirect(f"{request.path}?month={month_str}")

    profiles = CallerProfile.objects.select_related('user').filter(role='caller', is_active=True)
    rows = []
    grand_total = Decimal('0')

    for p in profiles:
        u = p.user
        calls_q = CallLog.objects.filter(caller=u, created_at__date__gte=month_start, created_at__date__lte=month_end)
        n_ans = calls_q.filter(outcome='answered').count()
        n_int = calls_q.filter(outcome='interested').count()
        n_conv = calls_q.filter(outcome='converted').count()

        amt = Decimal('0')
        bonus = Decimal('0')
        if rule:
            amt += rule.per_answered_call * n_ans
            amt += rule.per_interested * n_int
            amt += rule.per_conversion * n_conv
            if rule.bonus_threshold and n_conv >= rule.bonus_threshold:
                bonus = rule.bonus_amount
                amt += bonus

        rows.append({
            'profile': p,
            'answered': n_ans, 'interested': n_int, 'conversions': n_conv,
            'amount': amt, 'bonus': bonus,
        })
        grand_total += amt

    return render(request, 'scraper/analytics/incentives.html', {
        'rows': rows, 'rule': rule, 'month_start': month_start, 'month_end': month_end,
        'month_str': month_str, 'grand_total': grand_total,
    })

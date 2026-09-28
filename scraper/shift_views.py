"""Shift Management + Team Hierarchy views."""
from datetime import datetime, time, timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Shift, ShiftAssignment, CallerProfile
from .calling_utils import admin_required, get_profile


# ============================================================
# Permission helpers for team hierarchy
# ============================================================

def get_visible_callers_for(user):
    """Returns CallerProfile queryset visible to this user.
    - admin / superuser: all callers
    - manager: only their direct reports
    - caller: only themselves
    """
    base = CallerProfile.objects.select_related('user')
    if not user.is_authenticated:
        return base.none()
    if user.is_superuser:
        return base
    profile = getattr(user, 'caller_profile', None)
    if not profile:
        return base.none()
    if profile.role == 'admin':
        return base
    if profile.role == 'manager':
        return base.filter(Q(manager=user) | Q(user=user))
    # caller — only self
    return base.filter(user=user)


def can_view_caller(viewer, caller_user):
    """Can `viewer` see `caller_user`'s data?"""
    if not viewer.is_authenticated:
        return False
    if viewer.is_superuser or viewer.id == caller_user.id:
        return True
    vp = getattr(viewer, 'caller_profile', None)
    if not vp:
        return False
    if vp.role == 'admin':
        return True
    if vp.role == 'manager':
        cp = getattr(caller_user, 'caller_profile', None)
        return bool(cp and cp.manager_id == viewer.id)
    return False


def manager_or_admin_required(view_func):
    """Allow admin OR manager (not regular callers)."""
    from functools import wraps
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        p = getattr(request.user, 'caller_profile', None)
        if p and p.role in ('admin', 'manager'):
            return view_func(request, *args, **kwargs)
        messages.error(request, 'Manager or admin access required.')
        return redirect('caller_dashboard')
    return wrapper


# ============================================================
# Shift Management
# ============================================================

@admin_required
def shifts_list(request):
    shifts = Shift.objects.all().order_by('-is_active', 'start_time')
    today = timezone.localdate()

    # Today's caller -> shift map
    assignments = ShiftAssignment.objects.filter(
        effective_from__lte=today
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today)).select_related('user', 'shift')

    by_shift = {}
    for sa in assignments:
        by_shift.setdefault(sa.shift_id, []).append(sa)

    rows = []
    for s in shifts:
        rows.append({
            'shift': s,
            'is_active_now': s.is_active_now(),
            'assignees': by_shift.get(s.id, []),
        })

    return render(request, 'scraper/shifts/list.html', {
        'rows': rows,
        'today': today,
    })


@admin_required
def shift_form(request, shift_id=None):
    shift = get_object_or_404(Shift, id=shift_id) if shift_id else None

    if request.method == 'POST':
        data = request.POST
        try:
            start_t = datetime.strptime(data.get('start_time', ''), '%H:%M').time()
            end_t = datetime.strptime(data.get('end_time', ''), '%H:%M').time()
        except ValueError:
            messages.error(request, 'Invalid time format. Use HH:MM.')
            return redirect(request.path)

        days = ','.join(d for d in ['mon','tue','wed','thu','fri','sat','sun'] if data.get('day_' + d))
        if not days:
            messages.error(request, 'Select at least one day.')
            return redirect(request.path)

        if shift:
            shift.name = data.get('name', '').strip()
            shift.start_time = start_t
            shift.end_time = end_t
            shift.days = days
            shift.timezone_name = data.get('timezone_name', 'Asia/Kolkata')
            shift.color = data.get('color', '#6366f1')
            shift.is_active = data.get('is_active') == 'on'
            shift.save()
            messages.success(request, f'Shift "{shift.name}" updated.')
        else:
            Shift.objects.create(
                name=data.get('name', '').strip(),
                start_time=start_t, end_time=end_t, days=days,
                timezone_name=data.get('timezone_name', 'Asia/Kolkata'),
                color=data.get('color', '#6366f1'),
                is_active=True,
            )
            messages.success(request, 'Shift created.')
        return redirect('shifts_list')

    return render(request, 'scraper/shifts/form.html', {'shift': shift})


@admin_required
@require_POST
def shift_delete(request, shift_id):
    s = get_object_or_404(Shift, id=shift_id)
    name = s.name
    s.delete()
    messages.success(request, f'Shift "{name}" deleted.')
    return redirect('shifts_list')


@admin_required
def shift_assign(request, shift_id):
    shift = get_object_or_404(Shift, id=shift_id)
    today = timezone.localdate()

    if request.method == 'POST':
        user_ids = request.POST.getlist('user_ids')
        try:
            eff_from = datetime.strptime(request.POST.get('effective_from', ''), '%Y-%m-%d').date()
        except ValueError:
            eff_from = today
        eff_to_str = request.POST.get('effective_to', '').strip()
        eff_to = None
        if eff_to_str:
            try:
                eff_to = datetime.strptime(eff_to_str, '%Y-%m-%d').date()
            except ValueError:
                eff_to = None

        added = 0
        for uid in user_ids:
            try:
                uid = int(uid)
            except ValueError:
                continue
            # End any existing current assignment for this user
            ShiftAssignment.objects.filter(
                user_id=uid, effective_to__isnull=True
            ).update(effective_to=eff_from - timedelta(days=1))
            ShiftAssignment.objects.create(
                user_id=uid, shift=shift, effective_from=eff_from, effective_to=eff_to,
            )
            added += 1
        messages.success(request, f'Assigned shift "{shift.name}" to {added} caller(s).')
        return redirect('shifts_list')

    # GET: show form
    profiles = CallerProfile.objects.select_related('user').filter(is_active=True, role__in=['caller', 'manager']).order_by('user__username')
    # Mark current shift per caller
    current = {}
    for sa in ShiftAssignment.objects.filter(
        effective_from__lte=today
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today)).select_related('shift'):
        current[sa.user_id] = sa.shift

    callers_info = []
    for p in profiles:
        callers_info.append({
            'profile': p,
            'current_shift': current.get(p.user.id),
        })
    return render(request, 'scraper/shifts/assign.html', {
        'shift': shift,
        'callers_info': callers_info,
        'today': today,
    })


@admin_required
@require_POST
def shift_unassign(request, assignment_id):
    sa = get_object_or_404(ShiftAssignment, id=assignment_id)
    name = sa.user.username
    sa.delete()
    messages.success(request, f'Unassigned {name} from shift.')
    return redirect('shifts_list')


# ============================================================
# Team Hierarchy view
# ============================================================

@manager_or_admin_required
def team_hierarchy(request):
    """Tree view of the org: admins -> managers -> callers."""
    is_admin = request.user.is_superuser or (
        getattr(request.user, 'caller_profile', None)
        and request.user.caller_profile.role == 'admin'
    )

    if is_admin:
        # Build full tree
        managers = CallerProfile.objects.select_related('user').filter(role='manager').order_by('user__username')
        unassigned_callers = CallerProfile.objects.select_related('user').filter(
            role='caller', manager__isnull=True
        ).order_by('user__username')
        manager_blocks = []
        for m in managers:
            team = CallerProfile.objects.select_related('user').filter(manager=m.user).order_by('user__username')
            manager_blocks.append({'manager': m, 'team': team})
        ctx = {
            'is_admin': True,
            'manager_blocks': manager_blocks,
            'unassigned': unassigned_callers,
        }
    else:
        # Manager — show only their team
        team = CallerProfile.objects.select_related('user').filter(manager=request.user).order_by('user__username')
        ctx = {
            'is_admin': False,
            'manager_blocks': [{'manager': request.user.caller_profile, 'team': team}],
            'unassigned': [],
        }
    return render(request, 'scraper/shifts/hierarchy.html', ctx)


@admin_required
@require_POST
def set_manager(request, user_id):
    """Admin sets the manager for a caller."""
    profile = get_object_or_404(CallerProfile, user_id=user_id)
    manager_id = request.POST.get('manager_id', '').strip()
    if manager_id:
        try:
            mid = int(manager_id)
            manager_user = User.objects.get(id=mid)
            mp = getattr(manager_user, 'caller_profile', None)
            if not mp or mp.role not in ('admin', 'manager'):
                messages.error(request, 'Selected user is not a manager/admin.')
                return redirect('team_hierarchy')
            profile.manager = manager_user
        except (ValueError, User.DoesNotExist):
            messages.error(request, 'Invalid manager.')
            return redirect('team_hierarchy')
    else:
        profile.manager = None
    profile.save(update_fields=['manager', 'updated_at'])
    messages.success(request, f'Reporting line updated for {profile.user.username}.')
    return redirect('team_hierarchy')

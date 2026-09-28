"""Helpers for calling-team role checks, decorators, and assignment logic."""
from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import CallerProfile, LeadAssignment, Place, Attendance


# ---------- Role helpers ----------

def get_profile(user):
    """Return CallerProfile for user, creating one if missing (defaults to caller)."""
    if not user or not user.is_authenticated:
        return None
    profile, _ = CallerProfile.objects.get_or_create(
        user=user,
        defaults={'role': 'admin' if user.is_superuser else 'caller'}
    )
    return profile


def is_admin(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    p = getattr(user, 'caller_profile', None)
    return bool(p and p.role == 'admin')


def is_caller(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return False
    p = getattr(user, 'caller_profile', None)
    return bool(p and p.role == 'caller' and p.is_active)


# ---------- Decorators ----------

def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not is_admin(request.user):
            messages.error(request, 'Admin access required.')
            return redirect('caller_dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper


def caller_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if is_admin(request.user):
            # Admins can also view caller pages (read-only would be ideal)
            return view_func(request, *args, **kwargs)
        if not is_caller(request.user):
            messages.error(request, 'Your account is inactive. Contact admin.')
            return redirect('login')
        return view_func(request, *args, **kwargs)
    return wrapper


# ---------- Attendance ----------

def open_attendance(user, request=None):
    """Create or return today's Attendance for the user. Re-opens if closed."""
    today = timezone.localdate()
    ip = None
    if request is not None:
        ip = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
              or request.META.get('REMOTE_ADDR'))
    att, created = Attendance.objects.get_or_create(
        user=user, date=today,
        defaults={'check_in': timezone.now(), 'ip_address': ip}
    )
    # Re-open if previously closed (multiple sessions per day supported)
    if not created and att.check_out is not None:
        att.check_in = timezone.now()
        att.check_out = None
        if ip and not att.ip_address:
            att.ip_address = ip
        att.save(update_fields=['check_in', 'check_out', 'ip_address'])
    return att, created


def close_attendance(user):
    """Close today's open attendance (called on logout)."""
    today = timezone.localdate()
    att = Attendance.objects.filter(user=user, date=today, check_out__isnull=True).first()
    if att:
        att.close()
    return att


# ---------- Auto-assignment ----------

def pending_count(user):
    """How many leads is this caller still working on."""
    return LeadAssignment.objects.filter(caller=user, status__in=['pending', 'in_progress']).count()


def assign_leads_to_caller(caller_user, count=None, assigned_by=None):
    """
    Pick the top `count` un-assigned leads (sorted by lead_score desc)
    and assign them to caller_user. Returns number actually assigned.
    """
    profile = get_profile(caller_user)
    if count is None:
        count = profile.daily_quota if profile else 100

    # Exclude leads that are already assigned to ANYONE (active or done)
    # OR that are already converted/rejected (no point calling again)
    already_assigned_ids = LeadAssignment.objects.values_list('place_id', flat=True)

    eligible = (
        Place.objects.filter(lead_status__in=['new', 'contacted', 'interested'])
        .exclude(id__in=already_assigned_ids)
        .exclude(phone='')  # need a phone to call
        .order_by('-lead_score', '-reviews_count', '-id')
    )[:count]

    eligible_ids = list(eligible.values_list('id', flat=True))
    if not eligible_ids:
        return 0

    with transaction.atomic():
        # Re-lock check inside transaction
        existing = set(LeadAssignment.objects.filter(place_id__in=eligible_ids)
                       .values_list('place_id', flat=True))
        to_create = [
            LeadAssignment(place_id=pid, caller=caller_user, assigned_by=assigned_by, status='pending')
            for pid in eligible_ids if pid not in existing
        ]
        LeadAssignment.objects.bulk_create(to_create, ignore_conflicts=True)

    return len(to_create)


def ensure_daily_assignment(caller_user):
    """
    Called when caller opens dashboard. If they have zero pending leads
    AND auto_assign_enabled is ON, auto-assign up to their daily quota.
    """
    profile = get_profile(caller_user)
    if not profile or not profile.is_active:
        return 0
    if not getattr(profile, 'auto_assign_enabled', False):
        return 0  # Manual-only mode — admin assigns explicitly
    pend = pending_count(caller_user)
    if pend > 0:
        return 0
    return assign_leads_to_caller(caller_user, count=profile.daily_quota)

"""Break tracking, notifications, mentions, lead deduplication."""
import re
from collections import defaultdict
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.core.paginator import Paginator

from .models import (
    BreakLog, Notification, Place, CallerProfile,
)
from .calling_utils import caller_required, admin_required, is_caller


# ============================================================
# BREAK TRACKING
# ============================================================

@caller_required
@require_POST
def break_start(request):
    """Caller starts a break."""
    user = request.user
    # Close any existing open break
    open_break = BreakLog.objects.filter(user=user, end__isnull=True).first()
    if open_break:
        open_break.close()

    reason = request.POST.get('reason', 'break')
    if reason not in dict(BreakLog.REASON_CHOICES):
        reason = 'break'
    note = request.POST.get('note', '').strip()[:200]

    br = BreakLog.objects.create(user=user, reason=reason, note=note)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'id': br.id, 'start': br.start.isoformat()})
    messages.success(request, f'Break started ({br.get_reason_display()}).')
    return redirect('caller_dashboard')


@caller_required
@require_POST
def break_end(request):
    """Caller ends current break."""
    user = request.user
    br = BreakLog.objects.filter(user=user, end__isnull=True).order_by('-start').first()
    if br:
        br.close()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': True, 'duration': br.duration_minutes})
        messages.success(request, f'Break ended ({br.duration_minutes} min).')
    return redirect('caller_dashboard')


# ============================================================
# NOTIFICATIONS
# ============================================================

def _create_notification(user, title, body='', url='', kind='system', actor=None, place=None):
    if not user:
        return None
    return Notification.objects.create(
        user=user, title=title, body=body, url=url, kind=kind, actor=actor, place=place
    )


def notifications_list(request):
    """List all notifications for current user."""
    if not request.user.is_authenticated:
        return redirect('login')
    notifs = Notification.objects.filter(user=request.user).order_by('-created_at')
    paginator = Paginator(notifs, 30)
    page = paginator.get_page(request.GET.get('page', 1))
    return render(request, 'scraper/notifications/list.html', {
        'notifs': page, 'page_obj': page,
    })


def notifications_api(request):
    """JSON endpoint for the bell badge.
    Rewrites /lead/<id>/ URLs to /calling/lead/<id>/view/ for callers
    so they don't hit middleware redirect.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'count': 0, 'items': []})

    from .calling_utils import is_caller as _is_caller
    is_caller_user = _is_caller(request.user)

    qs = Notification.objects.filter(user=request.user).select_related('place').order_by('-created_at')
    unread = qs.filter(is_read=False).count()
    items = []
    for n in qs[:8]:
        url = n.url or ''
        # Rewrite admin-only URLs to caller-friendly ones
        if is_caller_user:
            if n.place_id and url.startswith('/lead/'):
                url = f'/calling/lead/{n.place_id}/view/'
            elif url.startswith('/admin-panel/'):
                url = '/calling/'  # admins-only — send to dashboard
        items.append({
            'id': n.id,
            'kind': n.kind,
            'title': n.title,
            'body': n.body[:140],
            'url': url,
            'is_read': n.is_read,
            'ago': _humanize(timezone.now() - n.created_at),
        })
    return JsonResponse({'count': unread, 'items': items})


def _humanize(delta):
    s = int(delta.total_seconds())
    if s < 60: return f'{s}s ago'
    if s < 3600: return f'{s // 60}m ago'
    if s < 86400: return f'{s // 3600}h ago'
    return f'{s // 86400}d ago'


@require_POST
def notifications_mark_read(request):
    """Mark one or all notifications as read."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False}, status=401)
    nid = request.POST.get('id', '')
    if nid == 'all':
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    else:
        try:
            Notification.objects.filter(user=request.user, id=int(nid)).update(is_read=True)
        except (ValueError, TypeError):
            pass
    return JsonResponse({'ok': True})


# ============================================================
# @MENTIONS — parse from notes and notify
# ============================================================

MENTION_RE = re.compile(r'@([a-zA-Z0-9_.-]{2,})')


def process_mentions_in_text(text, actor, place=None, url=''):
    """Find @username mentions, create notifications. Returns list of mentioned users."""
    if not text:
        return []
    seen = set()
    notified = []
    for m in MENTION_RE.finditer(text):
        uname = m.group(1)
        if uname.lower() in seen:
            continue
        seen.add(uname.lower())
        user = User.objects.filter(username__iexact=uname).first()
        if user and user.id != getattr(actor, 'id', None):
            snippet = text[:200]
            _create_notification(
                user=user, kind='mention',
                title=f'{actor.username} mentioned you',
                body=snippet, url=url, actor=actor, place=place,
            )
            notified.append(user)
    return notified


@admin_required
def mention_users_api(request):
    """Return list of users matching prefix — for @-autocomplete."""
    q = request.GET.get('q', '').strip().lstrip('@').lower()
    qs = CallerProfile.objects.select_related('user')
    if q:
        qs = qs.filter(user__username__istartswith=q)
    items = [{
        'username': p.user.username,
        'name': p.user.get_full_name() or p.user.username,
        'role': p.role,
    } for p in qs[:8]]
    return JsonResponse({'items': items})


# ============================================================
# LEAD DEDUPLICATION
# ============================================================

def _norm(s):
    return re.sub(r'\s+', ' ', (s or '').strip().lower())


def _phone_norm(p):
    digits = ''.join(c for c in (p or '') if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


@admin_required
def dedup_home(request):
    """Find duplicate Place rows by phone, email, or normalized name+address."""
    threshold = int(request.GET.get('limit', 50))

    # Phone duplicates
    phone_groups = []
    phone_dupes = (
        Place.objects.exclude(phone='')
        .values('phone').annotate(c=Count('id'))
        .filter(c__gt=1).order_by('-c')[:threshold]
    )
    for row in phone_dupes:
        places = list(Place.objects.filter(phone=row['phone']).order_by('-lead_score', 'id'))
        phone_groups.append({'key': row['phone'], 'count': row['c'], 'places': places, 'kind': 'phone'})

    # Email duplicates
    email_groups = []
    email_dupes = (
        Place.objects.exclude(email='')
        .values('email').annotate(c=Count('id'))
        .filter(c__gt=1).order_by('-c')[:threshold]
    )
    for row in email_dupes:
        places = list(Place.objects.filter(email=row['email']).order_by('-lead_score', 'id'))
        email_groups.append({'key': row['email'], 'count': row['c'], 'places': places, 'kind': 'email'})

    # Name + address fuzzy (simple: same lowercased name)
    name_groups = []
    name_dupes = (
        Place.objects.exclude(name='')
        .values('name').annotate(c=Count('id'))
        .filter(c__gt=1).order_by('-c')[:threshold]
    )
    for row in name_dupes:
        if row['c'] < 2:
            continue
        places = list(Place.objects.filter(name=row['name']).order_by('-lead_score', 'id'))
        # Only consider if same address too OR no address comparison
        if len(places) > 1:
            name_groups.append({'key': row['name'], 'count': row['c'], 'places': places, 'kind': 'name'})

    total_dupes = sum(g['count'] - 1 for g in phone_groups + email_groups + name_groups)

    return render(request, 'scraper/dedup/home.html', {
        'phone_groups': phone_groups,
        'email_groups': email_groups,
        'name_groups': name_groups,
        'total_dupes': total_dupes,
    })


@admin_required
@require_POST
def dedup_merge(request):
    """Keep one Place, delete the rest. Optionally merge non-empty fields into keeper."""
    keep_id = request.POST.get('keep_id', '')
    delete_ids = request.POST.getlist('delete_ids')

    try:
        keep_id = int(keep_id)
        delete_ids = [int(x) for x in delete_ids if x]
    except ValueError:
        messages.error(request, 'Invalid request.')
        return redirect('dedup_home')

    keeper = get_object_or_404(Place, id=keep_id)
    losers = Place.objects.filter(id__in=delete_ids).exclude(id=keep_id)

    # Merge non-empty fields from losers into keeper
    fields_to_fill = ['email', 'website', 'address', 'category', 'phone',
                      'facebook', 'instagram', 'linkedin', 'description']
    for f in fields_to_fill:
        if not getattr(keeper, f, ''):
            for l in losers:
                v = getattr(l, f, '')
                if v:
                    setattr(keeper, f, v)
                    break

    # Concatenate notes
    extra_notes = '\n'.join(l.notes for l in losers if l.notes)
    if extra_notes:
        keeper.notes = (keeper.notes + '\n' + extra_notes) if keeper.notes else extra_notes

    keeper.lead_score = keeper.calculate_score()
    keeper.save()

    n = losers.count()
    losers.delete()
    messages.success(request, f'Merged {n} duplicate(s) into "{keeper.name}".')
    return redirect('dedup_home')


@admin_required
@require_POST
def dedup_delete(request):
    """Delete specific duplicate IDs without merging."""
    ids = request.POST.getlist('delete_ids')
    try:
        ids = [int(x) for x in ids if x]
    except ValueError:
        ids = []
    n, _ = Place.objects.filter(id__in=ids).delete()
    messages.success(request, f'Deleted {n} lead(s).')
    return redirect('dedup_home')

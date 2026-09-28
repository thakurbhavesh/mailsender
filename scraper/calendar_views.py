"""Calendar booking — Calendly-style public scheduling."""
import secrets
from datetime import datetime, timedelta, date, time

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponseRedirect, Http404
from django.contrib import messages
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from django.urls import reverse

from .models import AvailabilitySchedule, Meeting, Place
from .calling_utils import admin_required, caller_required


DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
DAY_NAMES = {'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday',
             'thu': 'Thursday', 'fri': 'Friday', 'sat': 'Saturday', 'sun': 'Sunday'}


# ============================================================
# Internal: my schedule + meetings
# ============================================================

@caller_required
def my_schedule(request):
    """The logged-in user's own bookable schedule editor."""
    schedule = getattr(request.user, 'schedule', None)

    if request.method == 'POST':
        if not schedule:
            base_slug = slugify(request.user.username) or f'user-{request.user.id}'
            slug = base_slug
            i = 1
            while AvailabilitySchedule.objects.filter(slug=slug).exclude(user=request.user).exists():
                slug = f'{base_slug}-{i}'; i += 1
            schedule = AvailabilitySchedule.objects.create(
                user=request.user, slug=slug,
                title=f'Book a meeting with {request.user.first_name or request.user.username}',
            )
            schedule.working_hours = schedule.default_hours()

        # Save settings
        schedule.title = request.POST.get('title', schedule.title).strip()
        schedule.description = request.POST.get('description', '').strip()
        try:
            schedule.slot_duration_min = int(request.POST.get('slot_duration_min', 30))
        except ValueError:
            pass
        try:
            schedule.buffer_min = int(request.POST.get('buffer_min', 10))
        except ValueError:
            pass
        try:
            schedule.advance_days = int(request.POST.get('advance_days', 14))
        except ValueError:
            pass
        try:
            schedule.min_notice_hours = int(request.POST.get('min_notice_hours', 4))
        except ValueError:
            pass

        # Working hours per day
        wh = {}
        for d in DAYS:
            if request.POST.get(f'enable_{d}') == 'on':
                start = request.POST.get(f'{d}_start', '09:00')
                end = request.POST.get(f'{d}_end', '17:00')
                wh[d] = [[start, end]]
            else:
                wh[d] = []
        schedule.working_hours = wh
        schedule.color = request.POST.get('color', schedule.color)
        schedule.timezone_name = request.POST.get('timezone_name', schedule.timezone_name)
        schedule.is_active = request.POST.get('is_active') == 'on'
        schedule.require_phone = request.POST.get('require_phone') == 'on'
        schedule.save()
        messages.success(request, 'Schedule saved.')
        return redirect('my_schedule')

    if schedule and not schedule.working_hours:
        schedule.working_hours = schedule.default_hours()
        schedule.save()

    days_data = []
    wh = schedule.working_hours if schedule else {}
    for d in DAYS:
        slots = wh.get(d, [])
        if slots:
            s, e = slots[0]
        else:
            s, e = '09:00', '17:00'
        days_data.append({
            'code': d, 'name': DAY_NAMES[d],
            'enabled': bool(slots),
            'start': s, 'end': e,
        })

    public_url = ''
    if schedule:
        public_url = request.build_absolute_uri(reverse('public_booking', args=[schedule.slug]))

    return render(request, 'scraper/calendar/my_schedule.html', {
        'schedule': schedule,
        'days_data': days_data,
        'public_url': public_url,
    })


@caller_required
def meetings_list(request):
    """List meetings for the logged-in user."""
    schedule = getattr(request.user, 'schedule', None)
    if not schedule:
        return render(request, 'scraper/calendar/meetings.html', {
            'schedule': None, 'upcoming': [], 'past': [],
        })

    now = timezone.now()
    upcoming = Meeting.objects.filter(
        schedule=schedule, scheduled_at__gte=now, status='confirmed'
    ).order_by('scheduled_at')
    past = Meeting.objects.filter(
        schedule=schedule,
    ).exclude(scheduled_at__gte=now, status='confirmed').order_by('-scheduled_at')[:30]

    return render(request, 'scraper/calendar/meetings.html', {
        'schedule': schedule, 'upcoming': upcoming, 'past': past,
    })


@require_POST
@caller_required
def meeting_status(request, meeting_id):
    m = get_object_or_404(Meeting, id=meeting_id, schedule__user=request.user)
    new_status = request.POST.get('status', '')
    if new_status in dict(Meeting.STATUS_CHOICES):
        m.status = new_status
        m.save(update_fields=['status', 'updated_at'])
        messages.success(request, f'Meeting marked as {m.get_status_display()}.')
    return redirect('meetings_list')


# ============================================================
# Public booking page
# ============================================================

def public_booking(request, slug):
    schedule = get_object_or_404(AvailabilitySchedule, slug=slug, is_active=True)

    # Build available slots for next N days
    tz_now = timezone.localtime()
    today = tz_now.date()
    min_dt = tz_now + timedelta(hours=schedule.min_notice_hours)
    max_date = today + timedelta(days=schedule.advance_days)

    # Already-booked slots
    booked = set(
        Meeting.objects.filter(
            schedule=schedule, status='confirmed',
            scheduled_at__date__gte=today, scheduled_at__date__lte=max_date,
        ).values_list('scheduled_at', flat=True)
    )

    days_with_slots = []
    duration = schedule.slot_duration_min + schedule.buffer_min
    cur = today
    while cur <= max_date:
        wd = DAYS[cur.weekday()]
        windows = (schedule.working_hours or {}).get(wd, [])
        slots = []
        for win in windows:
            try:
                s_h, s_m = map(int, win[0].split(':'))
                e_h, e_m = map(int, win[1].split(':'))
            except (ValueError, IndexError):
                continue
            slot_start = timezone.make_aware(datetime.combine(cur, time(s_h, s_m)))
            window_end = timezone.make_aware(datetime.combine(cur, time(e_h, e_m)))
            while slot_start + timedelta(minutes=schedule.slot_duration_min) <= window_end:
                if slot_start >= min_dt and slot_start not in booked:
                    slots.append(slot_start)
                slot_start += timedelta(minutes=duration)
        if slots:
            days_with_slots.append({'date': cur, 'slots': slots})
        cur += timedelta(days=1)

    return render(request, 'scraper/calendar/public_booking.html', {
        'schedule': schedule,
        'days_with_slots': days_with_slots[:7],  # show 7 days at a time
        'all_days': days_with_slots,
    })


def public_book_submit(request, slug):
    schedule = get_object_or_404(AvailabilitySchedule, slug=slug, is_active=True)
    if request.method != 'POST':
        return redirect('public_booking', slug=slug)

    when_str = request.POST.get('when', '').strip()
    name = request.POST.get('name', '').strip()
    email = request.POST.get('email', '').strip()
    phone = request.POST.get('phone', '').strip()
    notes = request.POST.get('notes', '').strip()

    if not (when_str and name and email):
        messages.error(request, 'Please provide your name, email, and pick a slot.')
        return redirect('public_booking', slug=slug)
    if schedule.require_phone and not phone:
        messages.error(request, 'Phone number required.')
        return redirect('public_booking', slug=slug)

    try:
        when = datetime.fromisoformat(when_str)
        if timezone.is_naive(when):
            when = timezone.make_aware(when)
    except ValueError:
        messages.error(request, 'Invalid date/time.')
        return redirect('public_booking', slug=slug)

    # Check still free
    if Meeting.objects.filter(schedule=schedule, scheduled_at=when, status='confirmed').exists():
        messages.error(request, 'Sorry, this slot was just taken. Pick another.')
        return redirect('public_booking', slug=slug)

    # Auto-link to existing Place if email matches
    place = Place.objects.filter(email__iexact=email).first()

    meeting = Meeting.objects.create(
        schedule=schedule, place=place,
        booker_name=name, booker_email=email, booker_phone=phone,
        scheduled_at=when, duration_min=schedule.slot_duration_min,
        notes=notes, status='confirmed',
        cancel_token=secrets.token_urlsafe(24),
    )

    # Notify schedule owner
    from .extra_views import _create_notification
    _create_notification(
        user=schedule.user, kind='system',
        title=f'📅 New meeting booked',
        body=f'{name} ({email}) booked for {when:%d %b at %h:%M %p}',
        url=reverse('meetings_list'),
    )

    return render(request, 'scraper/calendar/booked.html', {
        'meeting': meeting, 'schedule': schedule,
    })


def public_cancel(request, slug, token):
    schedule = get_object_or_404(AvailabilitySchedule, slug=slug)
    meeting = get_object_or_404(Meeting, schedule=schedule, cancel_token=token)
    if request.method == 'POST':
        meeting.status = 'cancelled'
        meeting.save(update_fields=['status', 'updated_at'])
        return render(request, 'scraper/calendar/cancelled.html', {'meeting': meeting})
    return render(request, 'scraper/calendar/cancel_confirm.html', {
        'meeting': meeting, 'schedule': schedule,
    })

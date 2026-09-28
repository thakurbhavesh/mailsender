"""Social outreach — work leads through Facebook, Instagram and LinkedIn.

Email has open and click tracking; social has none, so the only way to know
what happened is to record it. Opening a profile goes through a redirect that
logs the visit, and messaging is logged explicitly. Both land on the lead's
timeline next to the emails and calls.
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import (Count, Q, Max, Exists, OuterRef, Case, When,
                              Value, IntegerField)
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Place, SocialTouch, SocialTemplate

PLATFORMS = dict(SocialTouch.PLATFORM_CHOICES)

# Only these three live on the Place record; WhatsApp comes from the phone.
PROFILE_FIELDS = {'facebook': 'facebook', 'instagram': 'instagram', 'linkedin': 'linkedin'}

STATE_CHOICES = [
    ('has_social', 'Has a profile'),
    ('no_social', 'No profile found'),
    ('untouched', 'Not approached yet'),
    ('visited', 'Profile opened, not messaged'),
    ('messaged', 'Messaged'),
    ('replied', 'Replied'),
]

SORT_CHOICES = [
    ('-last_touch', 'Last touched: newest'),
    ('last_touch', 'Last touched: oldest'),
    ('-social_links', 'Most profiles'),
    ('-lead_score', 'Score: high to low'),
    ('name', 'Name: A to Z'),
]


def _annotate_social(qs):
    """Attach per-lead social counts and the strongest action taken."""
    touched = SocialTouch.objects.filter(place=OuterRef('pk'))
    one = lambda field: Case(When(~Q(**{field: ''}), then=Value(1)),
                             default=Value(0), output_field=IntegerField())
    return qs.annotate(
        social_links=one('facebook') + one('instagram') + one('linkedin'),
    ).annotate(
        touch_count=Count('social_touches', distinct=True),
        last_touch=Max('social_touches__created_at'),
        has_messaged=Exists(touched.filter(action__in=('messaged', 'connected'))),
        has_replied=Exists(touched.filter(action='replied')),
        has_visited=Exists(touched.filter(action='visited')),
    )


def _apply_state(qs, state):
    any_social = Q(facebook='') & Q(instagram='') & Q(linkedin='')
    if state == 'has_social':
        return qs.exclude(any_social)
    if state == 'no_social':
        return qs.filter(any_social)
    if state == 'untouched':
        return qs.exclude(any_social).filter(touch_count=0)
    if state == 'visited':
        return qs.filter(has_visited=True, has_messaged=False, has_replied=False)
    if state == 'messaged':
        return qs.filter(has_messaged=True, has_replied=False)
    if state == 'replied':
        return qs.filter(has_replied=True)
    return qs


def social_outreach(request):
    """The working list: which leads have profiles, and who has been approached."""
    q = request.GET.get('q', '').strip()
    platform = request.GET.get('platform', '').strip()
    state = request.GET.get('state', 'has_social').strip()
    status = request.GET.get('lead_status', '').strip()
    category = request.GET.get('category', '').strip()
    sort = request.GET.get('sort', '-last_touch').strip()

    qs = _annotate_social(Place.objects.all())

    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(category__icontains=q) |
                       Q(address__icontains=q))
    if status:
        qs = qs.filter(lead_status=status)
    if category:
        qs = qs.filter(category__iexact=category)
    if platform in PROFILE_FIELDS:
        qs = qs.exclude(**{PROFILE_FIELDS[platform]: ''})

    qs = _apply_state(qs, state)

    if sort not in dict(SORT_CHOICES):
        sort = '-last_touch'
    qs = qs.order_by(sort, '-lead_score', '-id')

    # Counts over every lead, not the filtered set — these are the headline
    # numbers for the channel as a whole.
    base = Place.objects.all()
    no_social = Q(facebook='') & Q(instagram='') & Q(linkedin='')
    stats = {
        'with_social': base.exclude(no_social).count(),
        'facebook': base.exclude(facebook='').count(),
        'instagram': base.exclude(instagram='').count(),
        'linkedin': base.exclude(linkedin='').count(),
    }
    acted = SocialTouch.objects.filter(action__in=('messaged', 'connected'))
    stats['messaged'] = acted.values('place').distinct().count()
    stats['replied'] = (SocialTouch.objects.filter(action='replied')
                        .values('place').distinct().count())
    stats['untouched'] = stats['with_social'] - (
        SocialTouch.objects.values('place').distinct().count())
    stats['reply_rate'] = (round(100 * stats['replied'] / stats['messaged'], 1)
                           if stats['messaged'] else 0)

    try:
        per_page = max(10, min(int(request.GET.get('per_page', 50)), 200))
    except ValueError:
        per_page = 50
    page = Paginator(qs, per_page).get_page(request.GET.get('page'))

    # Latest touch per lead on this page, for the status column.
    latest = {}
    for t in SocialTouch.objects.filter(
            place_id__in=[p.id for p in page]).select_related('user').order_by('-created_at'):
        latest.setdefault(t.place_id, t)
    for p in page:
        p.latest_touch = latest.get(p.id)

    return render(request, 'scraper/social/outreach.html', {
        'page': page, 'stats': stats, 'sort': sort, 'per_page': per_page,
        'q': q, 'platform': platform, 'state': state,
        'lead_status': status, 'category': category,
        'state_choices': STATE_CHOICES,
        'sort_choices': SORT_CHOICES,
        'platform_choices': [(k, PLATFORMS[k]) for k in PROFILE_FIELDS],
        'action_choices': SocialTouch.ACTION_CHOICES,
        'status_choices': Place.LEAD_STATUS_CHOICES,
        'total_filtered': qs.count(),
        'categories': Place.objects.exclude(category='')
            .values_list('category', flat=True).distinct().order_by('category'),
    })


def social_open(request, place_id, platform):
    """Record that the profile was opened, then send the user to it.

    A plain link would lose the visit, and a JS beacon would lose it whenever
    the new tab wins the race. A redirect cannot miss.
    """
    place = get_object_or_404(Place, id=place_id)
    field = PROFILE_FIELDS.get(platform)
    url = getattr(place, field, '') if field else ''
    if not url:
        messages.error(request, 'No %s profile on this lead.' % PLATFORMS.get(platform, platform))
        return redirect('social_outreach')

    # One visit row per person per profile per day is enough signal.
    today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    already = SocialTouch.objects.filter(
        place=place, platform=platform, action='visited',
        user=request.user if request.user.is_authenticated else None,
        created_at__gte=today).exists()
    if not already:
        SocialTouch.objects.create(
            place=place, platform=platform, action='visited',
            user=request.user if request.user.is_authenticated else None)

    return redirect(url)


@require_POST
def social_log(request, place_id):
    """Record a real action — messaged, connected, replied, no response."""
    place = get_object_or_404(Place, id=place_id)
    platform = request.POST.get('platform', '').strip()
    action = request.POST.get('action', '').strip()

    if platform not in PLATFORMS or action not in dict(SocialTouch.ACTION_CHOICES):
        messages.error(request, 'Pick a platform and an action.')
        return redirect(request.META.get('HTTP_REFERER') or 'social_outreach')

    SocialTouch.objects.create(
        place=place, platform=platform, action=action,
        note=request.POST.get('note', '').strip()[:300],
        user=request.user if request.user.is_authenticated else None)

    # A reply is real interest; move the lead along unless it is already further.
    if action == 'replied' and place.lead_status in ('new', 'contacted'):
        place.lead_status = 'interested'
        place.save(update_fields=['lead_status', 'updated_at'])
    elif action in ('messaged', 'connected') and place.lead_status == 'new':
        place.lead_status = 'contacted'
        place.save(update_fields=['lead_status', 'updated_at'])

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'action': action, 'platform': platform})

    messages.success(request, '%s logged for %s.' % (
        dict(SocialTouch.ACTION_CHOICES)[action], place.name))
    return redirect(request.META.get('HTTP_REFERER') or 'social_outreach')


@require_POST
def social_bulk(request):
    """Log the same action against several leads at once."""
    ids = request.POST.getlist('ids')
    platform = request.POST.get('platform', '').strip()
    action = request.POST.get('action', '').strip()
    back = request.META.get('HTTP_REFERER') or reverse('social_outreach')

    if not ids:
        messages.error(request, 'Select at least one lead first.')
        return redirect(back)
    if platform not in PLATFORMS or action not in dict(SocialTouch.ACTION_CHOICES):
        messages.error(request, 'Pick a platform and an action.')
        return redirect(back)

    places = list(Place.objects.filter(id__in=ids))
    user = request.user if request.user.is_authenticated else None
    SocialTouch.objects.bulk_create([
        SocialTouch(place=p, platform=platform, action=action, user=user)
        for p in places
    ])

    if action in ('messaged', 'connected'):
        Place.objects.filter(id__in=[p.id for p in places], lead_status='new').update(
            lead_status='contacted', updated_at=timezone.now())

    messages.success(request, '%s logged on %s for %d leads.' % (
        dict(SocialTouch.ACTION_CHOICES)[action], PLATFORMS[platform], len(places)))
    return redirect(back)


# ─────────────────────────────────────────────────────────────
# Per-lead panel: pick a network, write the message, log what happened
# ─────────────────────────────────────────────────────────────
DEFAULT_TEMPLATES = [
    ('LinkedIn — connection note', 'linkedin',
     "Hi — came across {{name}} while looking at {{category}} teams in {{city}}. "
     "Would like to connect."),
    ('LinkedIn — first message', 'linkedin',
     "Hi, thanks for connecting.\n\n"
     "Quick one: where does {{name}} handle internal team chat right now — "
     "Slack, Teams or WhatsApp?\n\n"
     "We build a self-hosted alternative, so the data stays on your own "
     "servers. Worth a short look?"),
    ('Instagram / Facebook — opener', '',
     "Hi {{name}} — saw your page while looking at {{category}} businesses "
     "in {{city}}. Are you the right person to speak to about your "
     "internal team communication?"),
    ('WhatsApp — short intro', 'whatsapp',
     "Hi, this is from VVM Technologies. We work with {{category}} teams in "
     "{{city}} on secure internal chat that runs on your own servers. "
     "Is it worth a 10-minute call?"),
]


def ensure_default_templates():
    """Seed a few starters the first time the panel is opened."""
    if SocialTemplate.objects.exists():
        return
    SocialTemplate.objects.bulk_create([
        SocialTemplate(name=n, platform=p, body=b) for n, p, b in DEFAULT_TEMPLATES
    ])


def social_panel(request, place_id):
    """Everything needed to work one lead, as JSON for the slide-over."""
    place = get_object_or_404(Place, id=place_id)
    ensure_default_templates()

    profiles = [
        {'key': k, 'label': PLATFORMS[k], 'url': getattr(place, f),
         'open_url': reverse('social_open', args=[place.id, k])}
        for k, f in PROFILE_FIELDS.items() if getattr(place, f)
    ]
    if place.phone:
        profiles.append({'key': 'whatsapp', 'label': 'WhatsApp',
                         'url': place.whatsapp_link(), 'open_url': place.whatsapp_link()})

    touches = [{
        'id': t.id,
        'platform': t.platform,
        'platform_label': t.get_platform_display(),
        'action': t.action,
        'action_label': t.get_action_display(),
        'note': t.note,
        'user': t.user.username if t.user else '',
        'when': timezone.localtime(t.created_at).strftime('%d %b %Y, %H:%M'),
    } for t in SocialTouch.objects.filter(place=place).select_related('user')]

    templates = [{
        'id': t.id, 'name': t.name, 'platform': t.platform,
        'body': t.render(place),
    } for t in SocialTemplate.objects.filter(is_active=True)]

    return JsonResponse({
        'ok': True,
        'lead': {
            'id': place.id, 'name': place.name,
            'category': place.category, 'address': place.address,
            'website': place.website, 'phone': place.phone,
            'status': place.lead_status,
            'status_label': place.get_lead_status_display(),
        },
        'profiles': profiles,
        'touches': touches,
        'templates': templates,
        'actions': [{'value': v, 'label': l} for v, l in SocialTouch.ACTION_CHOICES],
        'statuses': [{'value': v, 'label': l} for v, l in Place.LEAD_STATUS_CHOICES],
    })


@require_POST
def social_touch_update(request, touch_id):
    """Correct a logged touch — wrong network, wrong action, or a better note."""
    touch = get_object_or_404(SocialTouch, id=touch_id)
    platform = request.POST.get('platform', touch.platform)
    action = request.POST.get('action', touch.action)

    if platform not in PLATFORMS or action not in dict(SocialTouch.ACTION_CHOICES):
        return JsonResponse({'ok': False, 'error': 'Unknown platform or action'}, status=400)

    touch.platform = platform
    touch.action = action
    touch.note = request.POST.get('note', '').strip()[:300]
    touch.save(update_fields=['platform', 'action', 'note'])
    return JsonResponse({'ok': True})


@require_POST
def social_touch_delete(request, touch_id):
    """Remove a touch logged by mistake."""
    get_object_or_404(SocialTouch, id=touch_id).delete()
    return JsonResponse({'ok': True})


@require_POST
def social_set_status(request, place_id):
    """Change the lead's stage from inside the panel."""
    place = get_object_or_404(Place, id=place_id)
    status = request.POST.get('lead_status', '')
    if status not in dict(Place.LEAD_STATUS_CHOICES):
        return JsonResponse({'ok': False, 'error': 'Unknown status'}, status=400)
    place.lead_status = status
    place.save(update_fields=['lead_status', 'updated_at'])
    return JsonResponse({'ok': True, 'status': status,
                         'label': place.get_lead_status_display()})

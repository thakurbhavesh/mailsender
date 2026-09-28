"""Email tool views: accounts, templates, compose, send, history."""
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.core.paginator import Paginator
from django.db.models import Count, Q

from .models import EmailAccount, EmailTemplate, EmailLog, Place
from .email_service import (
    send_one, send_test_email, send_bulk_async,
    get_default_account, seed_default_templates,
)


# ─── Email Dashboard ───────────────────────────────────────
def email_home(request):
    accounts = EmailAccount.objects.all()
    templates = EmailTemplate.objects.filter(is_active=True)
    if not templates.exists():
        seed_default_templates()
        templates = EmailTemplate.objects.filter(is_active=True)

    total_sent = EmailLog.objects.filter(status='sent').count()
    total_failed = EmailLog.objects.filter(status='failed').count()
    total_queued = EmailLog.objects.filter(status='queued').count()
    leads_with_email = Place.objects.exclude(email='').count()

    recent_logs = EmailLog.objects.select_related('account', 'template', 'place')[:10]
    default_acc = get_default_account()
    if default_acc:
        default_acc.reset_quota_if_needed()

    return render(request, 'scraper/email_home.html', {
        'accounts': accounts,
        'templates': templates,
        'total_sent': total_sent,
        'total_failed': total_failed,
        'total_queued': total_queued,
        'leads_with_email': leads_with_email,
        'recent_logs': recent_logs,
        'default_account': default_acc,
    })


# ─── Accounts ──────────────────────────────────────────────
def account_list(request):
    accounts = EmailAccount.objects.all()
    return render(request, 'scraper/email_accounts.html', {'accounts': accounts})


def account_form(request, account_id=None):
    account = get_object_or_404(EmailAccount, id=account_id) if account_id else None
    if request.method == 'POST':
        data = request.POST
        if account is None:
            account = EmailAccount()
        account.name = data.get('name', '').strip()
        account.email = data.get('email', '').strip()
        account.sender_name = data.get('sender_name', '').strip()
        new_pass = data.get('app_password', '').strip()
        if new_pass:
            account.app_password = new_pass.replace(' ', '')
        account.smtp_host = data.get('smtp_host', 'smtp.gmail.com').strip()
        account.smtp_port = int(data.get('smtp_port', 587))
        account.use_tls = data.get('use_tls') == 'on'
        account.is_default = data.get('is_default') == 'on'
        account.is_active = data.get('is_active') == 'on'
        account.daily_limit = int(data.get('daily_limit', 400))
        account.save()
        messages.success(request, f'Account "{account.email}" saved.')
        return redirect('email_accounts')
    return render(request, 'scraper/email_account_form.html', {'account': account})


@require_POST
def account_delete(request, account_id):
    a = get_object_or_404(EmailAccount, id=account_id)
    a.delete()
    messages.success(request, 'Account deleted.')
    return redirect('email_accounts')


@require_POST
def account_test(request, account_id):
    account = get_object_or_404(EmailAccount, id=account_id)
    test_to = request.POST.get('test_email', account.email).strip()
    ok, err = send_test_email(account, test_to)
    if ok:
        messages.success(request, f'✅ Test email sent to {test_to}!')
    else:
        messages.error(request, f'❌ Failed: {err}')
    return redirect('email_accounts')


# ─── Templates ─────────────────────────────────────────────
def template_list(request):
    templates = EmailTemplate.objects.all()
    if not templates.exists():
        seed_default_templates()
        templates = EmailTemplate.objects.all()
    return render(request, 'scraper/email_templates.html', {'templates': templates})


@require_POST
def restore_default_templates(request):
    seed_default_templates(force_update=True)
    messages.success(request, '✨ Default templates refreshed with latest professional designs.')
    return redirect('email_templates')


def template_form(request, template_id=None):
    tpl = get_object_or_404(EmailTemplate, id=template_id) if template_id else None
    if request.method == 'POST':
        data = request.POST
        if tpl is None:
            tpl = EmailTemplate()
        tpl.name = data.get('name', '').strip()
        tpl.subject = data.get('subject', '').strip()
        tpl.body_html = data.get('body_html', '').strip()
        tpl.description = data.get('description', '').strip()
        tpl.is_active = data.get('is_active') == 'on'
        tpl.save()
        messages.success(request, f'Template "{tpl.name}" saved.')
        return redirect('email_templates')
    return render(request, 'scraper/email_template_form.html', {'tpl': tpl})


@require_POST
def template_delete(request, template_id):
    t = get_object_or_404(EmailTemplate, id=template_id)
    t.delete()
    messages.success(request, 'Template deleted.')
    return redirect('email_templates')


# ─── Compose & Send ────────────────────────────────────────
def compose(request):
    place_id = request.GET.get('place')
    place = get_object_or_404(Place, id=place_id) if place_id else None

    accounts = EmailAccount.objects.filter(is_active=True)
    templates = EmailTemplate.objects.filter(is_active=True)
    default_acc = get_default_account()

    return render(request, 'scraper/email_compose.html', {
        'place': place,
        'accounts': accounts,
        'templates': templates,
        'default_account': default_acc,
    })


@require_POST
def send_single(request):
    place_id = request.POST.get('place_id')
    template_id = request.POST.get('template_id')
    account_id = request.POST.get('account_id')
    custom_subject = request.POST.get('subject', '').strip()
    custom_body = request.POST.get('body_html', '').strip()

    place = get_object_or_404(Place, id=place_id)
    if not place.email:
        messages.error(request, 'This lead has no email address.')
        return redirect(reverse('compose') + f'?place={place_id}')

    account = get_object_or_404(EmailAccount, id=account_id) if account_id else get_default_account()
    if not account:
        messages.error(request, 'No active email account. Configure one first.')
        return redirect('email_accounts')

    if template_id:
        template = get_object_or_404(EmailTemplate, id=template_id)
        if custom_subject or custom_body:
            ad_hoc = EmailTemplate(
                name=f'(custom) {template.name}',
                subject=custom_subject or template.subject,
                body_html=custom_body or template.body_html,
            )
            from .email_service import _send_via_smtp
            subject = custom_subject or template.subject
            body = custom_body or template.body_html
            for k, v in {'name': place.name, 'category': place.category,
                          'address': place.address, 'phone': place.phone,
                          'website': place.website, 'rating': place.rating,
                          'city': (place.address or '').split(',')[-1].strip()}.items():
                subject = subject.replace('{{' + k + '}}', str(v or ''))
                body = body.replace('{{' + k + '}}', str(v or ''))
            ok, err = _send_via_smtp(account, place.email, place.name, subject, body)
            from django.utils import timezone
            EmailLog.objects.create(
                place=place, template=template, account=account,
                to_email=place.email, to_name=place.name,
                subject=subject, body_html=body,
                status='sent' if ok else 'failed',
                error_message=err if not ok else '',
                sent_at=timezone.now() if ok else None,
            )
            if ok:
                account.sent_today += 1
                account.save(update_fields=['sent_today'])
                messages.success(request, f'✅ Email sent to {place.email}')
            else:
                messages.error(request, f'❌ Send failed: {err}')
        else:
            log = send_one(account, place, template)
            if log and log.status == 'sent':
                messages.success(request, f'✅ Email sent to {place.email}')
            else:
                messages.error(request, f'❌ Send failed: {log.error_message if log else "unknown"}')
    else:
        messages.error(request, 'Pick a template.')

    return redirect('email_history')


@require_POST
def send_bulk(request):
    template_id = request.POST.get('template_id')
    account_id = request.POST.get('account_id')
    place_ids = request.POST.getlist('place_ids')
    throttle = int(request.POST.get('throttle', 2))

    if not template_id or not place_ids:
        messages.error(request, 'Select template and at least one lead.')
        return redirect('compose_bulk')

    account = get_object_or_404(EmailAccount, id=account_id) if account_id else get_default_account()
    if not account:
        messages.error(request, 'No active email account configured.')
        return redirect('email_accounts')

    send_bulk_async(account.id, place_ids, template_id, throttle_seconds=throttle)
    messages.success(request, f'🚀 Bulk send started: {len(place_ids)} emails queued.')
    return redirect('email_history')


def compose_bulk(request):
    """Pick recipients with filters, then send."""
    q = request.GET.get('q', '').strip()
    category = request.GET.get('category', '').strip()
    job_id = request.GET.get('job', '').strip()
    min_score = request.GET.get('min_score', '').strip()
    status = request.GET.get('lead_status', '').strip()

    places = Place.objects.exclude(email='').select_related('job')
    if q:
        places = places.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(address__icontains=q))
    if category:
        places = places.filter(category__iexact=category)
    if job_id:
        places = places.filter(job_id=job_id)
    if status:
        places = places.filter(lead_status=status)
    if min_score:
        try:
            places = places.filter(lead_score__gte=int(min_score))
        except ValueError:
            pass

    places = places.order_by('-lead_score', '-created_at')
    paginator = Paginator(places, 50)
    page = paginator.get_page(request.GET.get('page'))

    accounts = EmailAccount.objects.filter(is_active=True)
    templates = EmailTemplate.objects.filter(is_active=True)
    default_acc = get_default_account()
    categories = sorted(set(Place.objects.exclude(category='').values_list('category', flat=True)))

    return render(request, 'scraper/email_compose_bulk.html', {
        'page': page, 'all_count': places.count(),
        'accounts': accounts, 'templates': templates,
        'default_account': default_acc,
        'categories': categories,
        'q': q, 'category': category, 'job_id': job_id,
        'min_score': min_score, 'lead_status': status,
        'status_choices': Place.LEAD_STATUS_CHOICES,
    })


# ─── History ───────────────────────────────────────────────
def email_history(request):
    status = request.GET.get('status', '').strip()
    q = request.GET.get('q', '').strip()
    logs = EmailLog.objects.select_related('account', 'template', 'place').all()
    if status:
        logs = logs.filter(status=status)
    if q:
        logs = logs.filter(Q(to_email__icontains=q) | Q(subject__icontains=q))
    paginator = Paginator(logs, 30)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'scraper/email_history.html', {
        'page': page, 'status': status, 'q': q,
    })


def log_detail(request, log_id):
    log = get_object_or_404(EmailLog, id=log_id)
    return render(request, 'scraper/email_log_detail.html', {'log': log})


def template_preview(request, template_id):
    """AJAX endpoint: render template against a sample place."""
    tpl = get_object_or_404(EmailTemplate, id=template_id)
    place_id = request.GET.get('place')
    if place_id:
        place = get_object_or_404(Place, id=place_id)
    else:
        place = Place.objects.exclude(email='').first() or Place.objects.first()
    if place:
        subject, body = tpl.render(place)
        meta = {'name': place.name, 'category': place.category, 'address': place.address}
    else:
        # Use sample data if no places
        class _Dummy:
            name = 'Acme Industries'
            category = 'Manufacturing'
            address = '123 Industrial Park, Mumbai'
            phone = '+91 98765 43210'
            website = 'https://acme.example.com'
            rating = '4.5'
            reviews_count = 120
        subject, body = tpl.render(_Dummy())
        meta = {'name': 'Acme Industries (sample)', 'category': 'Manufacturing', 'address': '123 Industrial Park, Mumbai'}
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'json' in request.GET:
        return JsonResponse({'subject': subject, 'body': body, 'sample': meta})
    return render(request, 'scraper/email_template_preview.html', {
        'tpl': tpl, 'subject': subject, 'body': body, 'sample': meta,
    })

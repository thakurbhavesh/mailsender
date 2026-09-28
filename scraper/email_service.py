"""Gmail SMTP email sending service."""
import smtplib
import threading
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from django.utils import timezone

from .models import EmailAccount, EmailTemplate, EmailLog, Place


def _send_via_smtp(account, to_email, to_name, subject, body_html):
    """Low-level SMTP send. Returns (success, error_message)."""
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        from_display = account.sender_name or account.email.split('@')[0].title()
        msg['From'] = formataddr((from_display, account.email))
        msg['To'] = formataddr((to_name, to_email)) if to_name else to_email
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        if account.use_tls:
            server = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30)

        server.login(account.email, account.app_password)
        server.sendmail(account.email, [to_email], msg.as_string())
        server.quit()
        return True, ''
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Auth failed: Use Gmail App Password (16 chars). {e}"
    except Exception as e:
        return False, str(e)[:500]


def _get_base_url():
    """Best-effort base URL for tracking links. Override via TRACKING_BASE_URL env."""
    import os
    return os.environ.get('TRACKING_BASE_URL', '').rstrip('/') or 'http://127.0.0.1:8000'


def send_one(account, place, template, enrollment=None):
    """Send one email to a place using a template; create EmailLog with tracking."""
    if not place.email:
        return None
    account.reset_quota_if_needed()
    if account.quota_remaining <= 0:
        log = EmailLog.objects.create(
            place=place, template=template, account=account,
            to_email=place.email, to_name=place.name,
            subject='', body_html='', status='failed',
            error_message='Daily quota exhausted',
        )
        return log

    subject, body = template.render(place)
    log = EmailLog.objects.create(
        place=place, template=template, account=account,
        to_email=place.email, to_name=place.name,
        subject=subject, body_html=body, status='queued',
        sequence_enrollment=enrollment,
    )

    # Inject tracking pixel + click rewriting
    from .email_tracking import inject_tracking
    try:
        tracked_body = inject_tracking(body, log, base_url=_get_base_url())
    except Exception:
        tracked_body = body

    ok, err = _send_via_smtp(account, place.email, place.name, subject, tracked_body)
    if ok:
        log.status = 'sent'
        log.sent_at = timezone.now()
        account.sent_today += 1
        account.save(update_fields=['sent_today'])
        template.times_used += 1
        template.save(update_fields=['times_used'])
    else:
        log.status = 'failed'
        log.error_message = err
    log.save()
    return log


def send_test_email(account, recipient_email):
    """Send a quick test email to verify SMTP credentials."""
    subject = "✅ LeadHunt SMTP Test"
    body = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:20px;">
        <h2 style="color:#6366f1;">SMTP Test Successful 🎉</h2>
        <p>This is a test email from your LeadHunt dashboard.</p>
        <p><strong>Account:</strong> {account.email}<br>
        <strong>Sender:</strong> {account.sender_name or 'Not set'}<br>
        <strong>Sent at:</strong> {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <hr><p style="color:#64748b;font-size:12px;">If you received this, your Gmail App Password is configured correctly.</p>
    </div>"""
    ok, err = _send_via_smtp(account, recipient_email, '', subject, body)
    EmailLog.objects.create(
        account=account, to_email=recipient_email,
        subject=subject, body_html=body,
        status='sent' if ok else 'failed',
        error_message=err if not ok else '',
        sent_at=timezone.now() if ok else None,
    )
    return ok, err


def send_bulk_async(account_id, place_ids, template_id, throttle_seconds=2):
    """Send to many places in a background thread with rate limiting."""
    def _run():
        try:
            account = EmailAccount.objects.get(id=account_id)
            template = EmailTemplate.objects.get(id=template_id)
            places = Place.objects.filter(id__in=place_ids).exclude(email='')
            for p in places:
                try:
                    send_one(account, p, template)
                except Exception:
                    pass
                time.sleep(throttle_seconds)
        except Exception:
            pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def get_default_account():
    return EmailAccount.objects.filter(is_default=True, is_active=True).first() \
        or EmailAccount.objects.filter(is_active=True).first()


def _wrap(content_html, accent='#6366f1', preheader=''):
    """Professional email shell with header band, content card, footer."""
    return f'''<div style="background:#f4f6fb;padding:32px 16px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937;">
{f'<div style="display:none;font-size:1px;color:#f4f6fb;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">{preheader}</div>' if preheader else ''}
<table role="presentation" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 8px 32px rgba(15,23,42,0.08);">
  <tr>
    <td style="background:linear-gradient(135deg,{accent} 0%,#ec4899 100%);padding:28px 32px;color:#ffffff;">
      <div style="font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:2px;opacity:0.9;">A personal note for you</div>
      <div style="font-size:22px;font-weight:800;margin-top:6px;letter-spacing:-0.3px;">Hi {{{{name}}}}, 👋</div>
    </td>
  </tr>
  <tr>
    <td style="padding:32px;font-size:15px;line-height:1.7;color:#334155;">
      {content_html}
    </td>
  </tr>
  <tr>
    <td style="padding:20px 32px 28px;border-top:1px solid #e2e8f0;background:#fafbfc;font-size:12px;color:#64748b;line-height:1.6;">
      <div style="margin-bottom:6px;">Sent with care from our team — replies go straight to my inbox.</div>
      <div style="font-size:11px;color:#94a3b8;">If this is not relevant, just reply "unsubscribe" and I will remove you immediately.</div>
    </td>
  </tr>
</table>
</div>'''


def _btn(label, color='#6366f1'):
    return f'''<table role="presentation" cellpadding="0" cellspacing="0" style="margin:18px 0;">
  <tr><td style="background:{color};border-radius:10px;">
    <a href="#reply" style="display:inline-block;padding:13px 28px;color:#ffffff;font-weight:700;font-size:14px;text-decoration:none;letter-spacing:0.3px;">{label} →</a>
  </td></tr>
</table>'''


DEFAULT_TEMPLATES = [
    {
        'name': 'Cold Outreach',
        'description': 'First-touch introduction email',
        'subject': 'Quick idea for {{name}}',
        'body_html': _wrap(
            preheader="A 10-minute idea I'd love to share with you",
            accent='#6366f1',
            content_html='''<p style="margin:0 0 14px;">I came across <strong>{{name}}</strong> while researching the best <strong>{{category}}</strong> businesses in <strong>{{city}}</strong> — and what you've built really stood out.</p>
<p style="margin:0 0 14px;">I help teams like yours <strong>generate qualified leads on autopilot</strong> using a combination of smart scraping, AI personalization, and proven outreach playbooks. A few clients have seen <strong>3-5x more replies</strong> within the first 30 days.</p>
<div style="background:#f8fafc;border-left:4px solid #6366f1;padding:14px 18px;border-radius:6px;margin:18px 0;font-size:14px;color:#475569;">
<strong style="color:#1e293b;">What I am proposing:</strong> A 10-minute call this week. No deck, no pitch — just an honest look at whether we can help. If yes, we go further. If not, we part friends.
</div>
<p style="margin:0 0 14px;">Worth a quick chat?</p>
''' + _btn('Reply yes — book a slot') + '''<p style="margin:18px 0 0;color:#475569;">Best regards,<br><strong>The Growth Team</strong></p>'''
        ),
    },
    {
        'name': 'Follow-Up',
        'description': 'Polite follow-up after no reply',
        'subject': 'One last thought, {{name}}',
        'body_html': _wrap(
            preheader="Bumping this in case it got buried",
            accent='#0ea5e9',
            content_html='''<p style="margin:0 0 14px;">I know inboxes get noisy, so I wanted to bring my last note back to the top in case it got missed.</p>
<p style="margin:0 0 14px;">Here is the short version: We help <strong>{{category}}</strong> businesses like yours win more clients with personalized outreach — and we would love to explore if we are a fit for <strong>{{name}}</strong>.</p>
<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:16px 18px;margin:18px 0;font-size:14px;color:#1e40af;">
<strong>Three ways to respond:</strong><br>
✅ <strong>Yes</strong> — I will share a 2-minute Loom and a calendar link.<br>
🤔 <strong>Not now</strong> — happy to circle back next quarter.<br>
🚫 <strong>Not interested</strong> — say the word, I will close the loop.
</div>
<p style="margin:18px 0 0;color:#475569;">Thank you for reading,<br><strong>The Growth Team</strong></p>'''
        ),
    },
    {
        'name': 'Special Offer',
        'description': 'Promotional email with offer',
        'subject': 'A limited offer crafted for {{name}}',
        'body_html': _wrap(
            preheader="30% off — this week only, hand-picked for you",
            accent='#f59e0b',
            content_html='''<p style="margin:0 0 14px;">We are running an exclusive offer for <strong>{{category}}</strong> businesses in {{city}} — and you are on our shortlist.</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;margin:18px 0;">
  <tr><td style="background:linear-gradient(135deg,#f59e0b 0%,#ef4444 100%);border-radius:14px;padding:28px 24px;text-align:center;color:#ffffff;">
    <div style="font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:2px;opacity:0.9;">Exclusive offer</div>
    <div style="font-size:42px;font-weight:900;margin:8px 0;letter-spacing:-1px;">30% OFF</div>
    <div style="font-size:14px;opacity:0.95;">Growth Package · This week only</div>
  </td></tr>
</table>
<p style="margin:0 0 8px;">Whats included:</p>
<ul style="margin:0 0 14px;padding-left:22px;color:#475569;">
  <li style="margin:4px 0;">100 AI-personalized cold emails</li>
  <li style="margin:4px 0;">Priority lead scraping (Maps + B2B platforms)</li>
  <li style="margin:4px 0;">Dedicated onboarding call</li>
</ul>
''' + _btn('Claim my 30% off', '#f59e0b') + '''<p style="margin:18px 0 0;color:#94a3b8;font-size:12px;">Offer expires in 7 days. Reply to lock it in.</p>'''
        ),
    },
    {
        'name': 'Partnership',
        'description': 'Collaboration / partnership pitch',
        'subject': 'A natural fit between us, {{name}}',
        'body_html': _wrap(
            preheader="Could we create something together?",
            accent='#10b981',
            content_html='''<p style="margin:0 0 14px;">I lead partnerships at our team and I have been studying <strong>{{name}}</strong> for a while. The way you serve the <strong>{{category}}</strong> market is genuinely impressive.</p>
<p style="margin:0 0 14px;">Here is the thing — we work with non-competing businesses in adjacent niches and have built a referral engine that consistently sends <strong>warm, paying clients</strong> in both directions.</p>
<div style="display:table;width:100%;margin:18px 0;border-collapse:separate;border-spacing:8px;">
  <div style="display:table-row;">
    <div style="display:table-cell;background:#ecfdf5;border-radius:10px;padding:14px;text-align:center;width:33%;">
      <div style="font-size:24px;">🤝</div>
      <div style="font-size:13px;font-weight:700;color:#065f46;margin-top:4px;">Mutual referrals</div>
    </div>
    <div style="display:table-cell;background:#eff6ff;border-radius:10px;padding:14px;text-align:center;width:33%;">
      <div style="font-size:24px;">📈</div>
      <div style="font-size:13px;font-weight:700;color:#1e40af;margin-top:4px;">Co-marketing</div>
    </div>
    <div style="display:table-cell;background:#fef3c7;border-radius:10px;padding:14px;text-align:center;width:33%;">
      <div style="font-size:24px;">💰</div>
      <div style="font-size:13px;font-weight:700;color:#92400e;margin-top:4px;">Revenue share</div>
    </div>
  </div>
</div>
<p style="margin:0 0 14px;">A 15-minute exploratory chat would tell us both whether this is worth pursuing. No commitments.</p>
''' + _btn('Lets talk partnership', '#10b981') + '''<p style="margin:18px 0 0;color:#475569;">Looking forward,<br><strong>The Partnerships Team</strong></p>'''
        ),
    },
    {
        'name': 'Demo Invite',
        'description': 'Invite a lead to a product demo',
        'subject': 'See {{name}} grow — 10-min demo?',
        'body_html': _wrap(
            preheader="A short walkthrough tailored to your business",
            accent='#8b5cf6',
            content_html='''<p style="margin:0 0 14px;">A quick demo would show you exactly how <strong>{{name}}</strong> can land 5-10 qualified <strong>{{category}}</strong> leads every single day — without lifting a finger.</p>
<p style="margin:0 0 14px;">Here is what you will see in 10 minutes:</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;margin:14px 0;">
  <tr><td style="padding:10px 0;border-bottom:1px solid #e2e8f0;font-size:14px;">
    <strong style="color:#8b5cf6;">①</strong> &nbsp; How we surface high-intent prospects in your niche
  </td></tr>
  <tr><td style="padding:10px 0;border-bottom:1px solid #e2e8f0;font-size:14px;">
    <strong style="color:#8b5cf6;">②</strong> &nbsp; Live AI-personalized email written for one of your prospects
  </td></tr>
  <tr><td style="padding:10px 0;border-bottom:1px solid #e2e8f0;font-size:14px;">
    <strong style="color:#8b5cf6;">③</strong> &nbsp; The dashboard — track every lead from first touch to closed deal
  </td></tr>
  <tr><td style="padding:10px 0;font-size:14px;">
    <strong style="color:#8b5cf6;">④</strong> &nbsp; Realistic numbers — what 30 days could look like for you
  </td></tr>
</table>
''' + _btn('Pick a 10-min slot', '#8b5cf6') + '''<p style="margin:14px 0 0;color:#475569;">Cheers,<br><strong>The Team</strong></p>'''
        ),
    },
    {
        'name': 'Re-engagement',
        'description': 'Bring back a cold or rejected lead',
        'subject': 'Did things change at {{name}}?',
        'body_html': _wrap(
            preheader="Thought of you and figured I would check in",
            accent='#ec4899',
            content_html='''<p style="margin:0 0 14px;">It has been a while since we last connected, and I have been thinking about <strong>{{name}}</strong>.</p>
<p style="margin:0 0 14px;">A lot has changed on our side too — and I figured if priorities have shifted at <strong>{{name}}</strong>, this might be the right moment to reopen the conversation.</p>
<div style="background:#fdf2f8;border:1px dashed #f9a8d4;border-radius:10px;padding:16px 18px;margin:18px 0;font-size:14px;color:#831843;">
<strong>Quick yes/no:</strong> Would a fresh look at how we could help <strong>{{name}}</strong> grow be useful right now?
</div>
<p style="margin:0 0 14px;">No long emails, no pressure — just a quick reply gets us started.</p>
<p style="margin:18px 0 0;color:#475569;">All the best,<br><strong>The Team</strong></p>'''
        ),
    },
]


def seed_default_templates(force_update=False):
    """Seed default templates. If force_update=True, also overwrite existing ones."""
    for t in DEFAULT_TEMPLATES:
        if force_update:
            EmailTemplate.objects.update_or_create(
                name=t['name'],
                defaults={
                    'subject': t['subject'],
                    'body_html': t['body_html'],
                    'description': t['description'],
                    'is_active': True,
                },
            )
        else:
            EmailTemplate.objects.get_or_create(
                name=t['name'],
                defaults={
                    'subject': t['subject'],
                    'body_html': t['body_html'],
                    'description': t['description'],
                },
            )

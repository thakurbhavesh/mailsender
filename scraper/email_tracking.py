"""Email open + click tracking via pixel + redirect."""
import base64
import re
from datetime import datetime
from urllib.parse import unquote

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.urls import reverse

from .models import EmailLog
from .calling_utils import admin_required


# 1x1 transparent GIF
TRANSPARENT_GIF = base64.b64decode(
    'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'
)


def open_pixel(request, token):
    """Tracking pixel — fires when email is opened. Returns 1x1 GIF."""
    log = EmailLog.objects.filter(open_token=token).first()
    if log:
        now = timezone.now()
        if not log.opened_at:
            log.opened_at = now
        log.last_opened_at = now
        log.opened_count = (log.opened_count or 0) + 1
        ua = request.META.get('HTTP_USER_AGENT', '')[:300]
        if ua and not log.user_agent:
            log.user_agent = ua
        log.save(update_fields=['opened_at', 'last_opened_at', 'opened_count', 'user_agent'])

    response = HttpResponse(TRANSPARENT_GIF, content_type='image/gif')
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    return response


def click_redirect(request, token):
    """Click tracking — log + redirect to target URL."""
    target = request.GET.get('u', '/')
    target = unquote(target)
    # Whitelist: must be http(s)
    if not (target.startswith('http://') or target.startswith('https://')):
        target = '/'

    log = EmailLog.objects.filter(open_token=token).first()
    if log:
        clicks = log.clicks or []
        clicks.append({
            'url': target[:500],
            'at': timezone.now().isoformat(),
        })
        log.clicks = clicks[-50:]  # keep last 50
        log.clicked_count = (log.clicked_count or 0) + 1
        log.save(update_fields=['clicks', 'clicked_count'])
    return HttpResponseRedirect(target)


def inject_tracking(html_body, log, base_url=''):
    """Inject tracking pixel + rewrite links for click tracking.

    Args:
        html_body: original HTML
        log: EmailLog instance (must have open_token)
        base_url: absolute URL prefix e.g. 'http://example.com'
    Returns:
        modified HTML
    """
    token = log.ensure_token()
    pixel_url = f'{base_url}/t/o/{token}.gif'
    pixel_tag = f'<img src="{pixel_url}" width="1" height="1" style="display:block;border:0;width:1px;height:1px;" alt="" />'

    # Rewrite href links to go through click tracker
    def rewrite(match):
        full = match.group(0)
        url = match.group(1)
        # Skip mailto/tel/anchor
        low = url.lower()
        if low.startswith('mailto:') or low.startswith('tel:') or low.startswith('#'):
            return full
        # Skip already-tracked
        if '/t/c/' in url:
            return full
        from urllib.parse import quote
        return full.replace(url, f'{base_url}/t/c/{token}/?u={quote(url, safe="")}')

    body = re.sub(r'href=["\']([^"\']+)["\']', rewrite, html_body, flags=re.IGNORECASE)

    # Append pixel before </body> or at end
    if '</body>' in body.lower():
        body = re.sub(r'</body>', pixel_tag + '</body>', body, count=1, flags=re.IGNORECASE)
    else:
        body = body + pixel_tag
    return body


@admin_required
def tracking_stats(request, log_id):
    """View detailed tracking stats for one email."""
    log = get_object_or_404(EmailLog, id=log_id)
    return render(request, 'scraper/email/tracking_stats.html', {'log': log})

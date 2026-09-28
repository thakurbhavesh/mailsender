"""Visit a Place's website (and contact pages) to extract emails + social links.
Robust logging — every attempt records URLs visited, what was found, and any errors,
so the user can SEE exactly what happened in the UI.
"""
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from django.utils import timezone

from .models import Place

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
SOCIAL_PATTERNS = {
    'facebook': re.compile(r'https?://(?:www\.|m\.|web\.)?facebook\.com/[^\s"\'<>?#]+', re.I),
    'instagram': re.compile(r'https?://(?:www\.)?instagram\.com/[^\s"\'<>?#]+', re.I),
    'linkedin': re.compile(r'https?://(?:www\.)?linkedin\.com/(?:company|in)/[^\s"\'<>?#]+', re.I),
}
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}
JUNK_FRAGMENTS = (
    '@sentry.', '@example.', '@wixpress.', '@2x.png', '.png@', '.jpg@',
    '@gmail.png', '@photos.', '@email.png', 'noreply@',
)


def _clean_email(e):
    e = e.lower().strip().rstrip('.,;:)>"\'')
    if any(b in e for b in JUNK_FRAGMENTS):
        return ''
    if e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico')):
        return ''
    if len(e) < 6 or len(e) > 100:
        return ''
    return e


def _fetch(url, timeout=12):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True, verify=True)
        ct = r.headers.get('Content-Type', '')
        if r.status_code == 200 and 'html' in ct.lower():
            return r.text, f"{r.status_code} OK ({len(r.text)} bytes)"
        return '', f"{r.status_code} {ct or 'no-html'}"
    except requests.exceptions.SSLError as e:
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True, verify=False)
            if r.status_code == 200:
                return r.text, f"{r.status_code} OK (SSL bypassed)"
            return '', f"SSL+{r.status_code}"
        except Exception as e2:
            return '', f"SSL error: {str(e2)[:60]}"
    except requests.exceptions.Timeout:
        return '', "timeout"
    except requests.exceptions.ConnectionError as e:
        return '', f"conn error: {str(e)[:60]}"
    except Exception as e:
        return '', f"err: {str(e)[:60]}"


COMMON_PREFIXES = ['info', 'contact', 'hello', 'sales', 'support', 'office', 'admin', 'team']


def _check_mx(domain, timeout=4):
    """Quick MX presence check — domain accepts mail at all."""
    try:
        socket.setdefaulttimeout(timeout)
        # Cheap: just resolve A record. MX would need dnspython.
        socket.gethostbyname(domain)
        return True
    except Exception:
        return False


def _guess_emails(domain, log_lines):
    """Generate likely emails from domain using common prefixes."""
    if not domain:
        return []
    if not _check_mx(domain, timeout=4):
        log_lines.append(f"  ⚠️ Domain {domain} not resolvable — skipping guess")
        return []
    guesses = [f"{p}@{domain}" for p in COMMON_PREFIXES]
    log_lines.append(f"  🎯 Guessed {len(guesses)} pattern emails: {', '.join(guesses[:3])}...")
    return guesses


def _ai_extract(html, log_lines):
    """Send HTML to Gemini for structured extraction. Returns dict or None."""
    try:
        from .ai_service import extract_emails_with_ai
        data, err = extract_emails_with_ai(html)
        if err:
            log_lines.append(f"  🤖 Gemini extraction failed: {err[:80]}")
            return None
        return data
    except Exception as e:
        log_lines.append(f"  🤖 Gemini error: {str(e)[:80]}")
        return None


def enrich_place(place):
    """Visit website + common contact pages. Use regex first, then Gemini AI fallback,
    finally pattern guess. Save findings + log."""
    log_lines = []
    log_lines.append(f"[{timezone.now().strftime('%H:%M:%S')}] Starting enrichment for: {place.name}")

    if not place.website:
        place.enrichment_status = 'skipped'
        place.enrichment_log = "No website URL — skipped."
        place.enriched = True
        place.enriched_at = timezone.now()
        place.lead_score = place.calculate_score()
        place.save()
        return

    log_lines.append(f"Website: {place.website}")
    pages = [place.website]
    domain = ''
    try:
        parsed = urlparse(place.website)
        base = f"{parsed.scheme}://{parsed.netloc}"
        domain = parsed.netloc.replace('www.', '')
        for path in ['/contact', '/contact-us', '/about', '/about-us', '/connect']:
            pages.append(urljoin(base, path))
    except Exception as e:
        log_lines.append(f"URL parse error: {e}")

    emails = set()
    socials = {'facebook': '', 'instagram': '', 'linkedin': ''}
    pages_visited = 0
    pages_ok = 0
    best_html = ''  # save best contact-page HTML for Gemini fallback

    for url in pages[:5]:
        pages_visited += 1
        html, status = _fetch(url)
        log_lines.append(f"  → {url[:80]}  [{status}]")
        if not html:
            continue
        pages_ok += 1
        # Save the contact page (or first ok page) for AI fallback
        if 'contact' in url.lower() or not best_html:
            best_html = html
        before_emails = len(emails)
        for m in EMAIL_RE.findall(html):
            cleaned = _clean_email(m)
            if cleaned:
                emails.add(cleaned)
        new_emails = len(emails) - before_emails
        if new_emails:
            log_lines.append(f"     +{new_emails} email(s) found")
        for key, pat in SOCIAL_PATTERNS.items():
            if not socials[key]:
                m = pat.search(html)
                if m:
                    socials[key] = m.group(0).rstrip('"\'<>)#?').strip()
                    log_lines.append(f"     {key}: {socials[key][:60]}")
        time.sleep(0.5)

    # ── Tier 2: Gemini AI extractor (when regex finds nothing useful) ──
    if not emails and best_html:
        log_lines.append("  🤖 Regex found nothing — trying Gemini AI extractor...")
        ai_data = _ai_extract(best_html, log_lines)
        if ai_data:
            for e in ai_data.get('emails') or []:
                cleaned = _clean_email(e)
                if cleaned:
                    emails.add(cleaned)
            if emails:
                log_lines.append(f"  ✨ Gemini found {len(emails)} email(s)")
            ai_socials = ai_data.get('socials') or {}
            for k in ['facebook', 'instagram', 'linkedin']:
                if not socials[k] and ai_socials.get(k):
                    socials[k] = ai_socials[k][:500]
                    log_lines.append(f"  ✨ Gemini found {k}: {socials[k][:60]}")

    # ── Tier 3: Pattern guesser (last resort) ──
    guessed_email = ''
    if not emails and domain:
        log_lines.append("  🎯 Still no email — pattern guessing...")
        guesses = _guess_emails(domain, log_lines)
        if guesses:
            guessed_email = guesses[0]  # info@domain by default

    if emails:
        priority = sorted(emails, key=lambda e: (
            0 if any(p + '@' in e for p in COMMON_PREFIXES) else 1,
            len(e),
        ))
        place.email = priority[0][:255]
        log_lines.append(f"✓ Picked email: {place.email}")
        if len(emails) > 1:
            log_lines.append(f"  (other: {', '.join(list(emails)[:3])})")
    elif guessed_email:
        place.email = guessed_email[:255]
        log_lines.append(f"🎯 Using guessed email: {place.email} (verify before sending)")
    else:
        log_lines.append("✗ No emails found on any page")

    place.facebook = socials['facebook'][:500]
    place.instagram = socials['instagram'][:500]
    place.linkedin = socials['linkedin'][:500]
    has_data = emails or guessed_email or any(socials.values())
    place.enrichment_status = 'done' if has_data else 'failed'
    place.enrichment_log = "\n".join(log_lines[-50:])
    place.enriched = True
    place.enriched_at = timezone.now()
    place.lead_score = place.calculate_score()

    method_summary = 'regex' if emails and not guessed_email else ('guessed' if guessed_email else 'none')
    log_lines.append(f"Pages: {pages_visited} tried, {pages_ok} ok | Emails: {len(emails) if emails else (1 if guessed_email else 0)} ({method_summary}) | Status: {place.enrichment_status}")
    place.enrichment_log = "\n".join(log_lines[-50:])
    place.save()


def enrich_job_async(job_id):
    def _run():
        places = Place.objects.filter(job_id=job_id).exclude(website='')
        for p in places:
            try:
                enrich_place(p)
            except Exception as e:
                p.enrichment_status = 'failed'
                p.enrichment_log = f"Exception: {e}"
                p.enriched_at = timezone.now()
                p.save()
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def enrich_all_async():
    def _run():
        places = Place.objects.filter(enrichment_status__in=['pending', 'failed']).exclude(website='')
        for p in places:
            try:
                enrich_place(p)
            except Exception as e:
                p.enrichment_status = 'failed'
                p.enrichment_log = f"Exception: {e}"
                p.enriched_at = timezone.now()
                p.save()
    t = threading.Thread(target=_run, daemon=True)
    t.start()

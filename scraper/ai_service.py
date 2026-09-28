"""Google Gemini AI service — email writer, reply assist, subject suggest, text improve."""
from .models import GeminiSetting, AIGenerationLog


# Curated list of Gemini models with FREE tier limits (as of 2026)
# Source: https://ai.google.dev/gemini-api/docs/rate-limits
GEMINI_MODELS = [
    {
        'id': 'gemini-2.5-flash',
        'name': 'Gemini 2.5 Flash',
        'desc': 'Best-in-class reasoning + speed. Recommended.',
        'free_rpm': 10, 'free_tpm': 250000, 'free_rpd': 250,
        'context': '1M tokens', 'badge': 'Recommended',
    },
    {
        'id': 'gemini-2.5-pro',
        'name': 'Gemini 2.5 Pro',
        'desc': 'Highest quality, slower. For complex tasks.',
        'free_rpm': 5, 'free_tpm': 250000, 'free_rpd': 100,
        'context': '1M tokens', 'badge': 'Pro',
    },
    {
        'id': 'gemini-2.5-flash-lite',
        'name': 'Gemini 2.5 Flash Lite',
        'desc': 'Faster, cheaper, slightly less capable.',
        'free_rpm': 15, 'free_tpm': 250000, 'free_rpd': 1000,
        'context': '1M tokens', 'badge': 'Fast',
    },
    {
        'id': 'gemini-2.0-flash',
        'name': 'Gemini 2.0 Flash',
        'desc': 'Stable, fast, multimodal.',
        'free_rpm': 15, 'free_tpm': 1000000, 'free_rpd': 200,
        'context': '1M tokens', 'badge': 'Stable',
    },
    {
        'id': 'gemini-2.0-flash-lite',
        'name': 'Gemini 2.0 Flash Lite',
        'desc': 'Lightweight, cheap, fast.',
        'free_rpm': 30, 'free_tpm': 1000000, 'free_rpd': 200,
        'context': '1M tokens', 'badge': '',
    },
    {
        'id': 'gemini-1.5-flash',
        'name': 'Gemini 1.5 Flash',
        'desc': 'Legacy fast model. Free generous quota.',
        'free_rpm': 15, 'free_tpm': 1000000, 'free_rpd': 1500,
        'context': '1M tokens', 'badge': 'Legacy',
    },
    {
        'id': 'gemini-1.5-pro',
        'name': 'Gemini 1.5 Pro',
        'desc': 'Legacy pro model.',
        'free_rpm': 2, 'free_tpm': 32000, 'free_rpd': 50,
        'context': '2M tokens', 'badge': 'Legacy',
    },
]


def _client():
    from google import genai
    s = GeminiSetting.get()
    if not s.api_key or not s.is_active:
        return None, s, "Gemini not configured. Add API key in AI Settings."
    try:
        client = genai.Client(api_key=s.api_key)
        return client, s, None
    except Exception as e:
        return None, s, f"Gemini init failed: {e}"


def _generate(prompt, kind='email_write', place=None):
    client, s, err = _client()
    if err:
        AIGenerationLog.objects.create(kind=kind, place=place, prompt=prompt[:2000], error=err)
        return None, err, None
    try:
        resp = client.models.generate_content(model=s.model, contents=prompt)
        text = (resp.text or '').strip()
        usage = getattr(resp, 'usage_metadata', None)
        prompt_tok = getattr(usage, 'prompt_token_count', 0) or 0
        out_tok = getattr(usage, 'candidates_token_count', 0) or 0
        total_tok = getattr(usage, 'total_token_count', prompt_tok + out_tok) or 0
        AIGenerationLog.objects.create(
            kind=kind, model=s.model, place=place,
            prompt=prompt[:2000], output=text[:5000],
            prompt_tokens=prompt_tok, output_tokens=out_tok, total_tokens=total_tok,
        )
        return text, None, {
            'prompt_tokens': prompt_tok, 'output_tokens': out_tok, 'total_tokens': total_tok,
            'model': s.model,
        }
    except Exception as e:
        AIGenerationLog.objects.create(kind=kind, model=s.model, place=place,
            prompt=prompt[:2000], error=str(e)[:1000])
        return None, str(e), None


def _place_context(place):
    if not place:
        return "Generic business"
    parts = []
    if place.name: parts.append(f"Name: {place.name}")
    if place.category: parts.append(f"Category: {place.category}")
    if place.address: parts.append(f"Address: {place.address}")
    if place.rating: parts.append(f"Rating: {place.rating} ({place.reviews_count} reviews)")
    if place.website: parts.append(f"Website: {place.website}")
    return "\n".join(parts)


def write_email(place, goal='cold outreach', tone='professional and warm', extra=''):
    ctx = _place_context(place)
    extra_block = f"\nExtra instructions: {extra}" if extra else ""
    prompt = f"""You write personalized cold outreach emails for B2B sales.

LEAD CONTEXT:
{ctx}

GOAL: {goal}
TONE: {tone}{extra_block}

Write ONE short email (max 120 words) that:
1. Opens with a specific, personalized line referencing their business
2. States the value proposition in 1 sentence
3. Asks for a 10-minute call as the CTA
4. Avoids spam words (free, urgent, click here)
5. Sounds human, not generic AI

OUTPUT FORMAT (strict):
SUBJECT: <one short subject under 60 chars>
BODY:
<HTML body, use <p> tags, no greetings like "Dear Sir/Madam">

Do not include any markdown, explanations, or text outside SUBJECT/BODY."""
    text, err, usage = _generate(prompt, kind='email_write', place=place)
    if err:
        return None, None, err, usage
    subject, body = '', ''
    if 'SUBJECT:' in text and 'BODY:' in text:
        try:
            subject = text.split('SUBJECT:')[1].split('BODY:')[0].strip()
            body = text.split('BODY:')[1].strip()
        except Exception:
            body = text
    else:
        body = text
    return subject[:200], body, None, usage


def suggest_subject(name, category, current_subject=''):
    prompt = f"""Suggest 5 short, high-open-rate email subject lines (each under 50 chars) for outreach to:
Name: {name}
Category: {category}
Current draft: {current_subject or '(none)'}

Output ONE subject per line, numbered 1-5. No explanations."""
    text, err, usage = _generate(prompt, kind='subject')
    if err:
        return None, err
    return text, None


def suggest_reply(incoming, place=None, my_intent='warm response leading to a call'):
    ctx = _place_context(place) if place else ''
    prompt = f"""A prospect replied to my outreach email. Draft a brief reply.

LEAD:
{ctx}

THEIR REPLY:
\"\"\"{incoming}\"\"\"

MY INTENT: {my_intent}

Write a 60-100 word reply that:
- Acknowledges their message
- Moves toward a call or next step
- Sounds human and friendly

Output the reply text only — no subject line, no explanations."""
    text, err, usage = _generate(prompt, kind='reply', place=place)
    return text, err


def improve_text(text, instruction='make it sound more natural and concise'):
    prompt = f"""Improve the following email text. Instruction: {instruction}

ORIGINAL:
\"\"\"{text}\"\"\"

Output the improved version only. Keep HTML tags if present."""
    out, err, usage = _generate(prompt, kind='improve')
    return out, err


def enhance_search_query(query, source='google_maps'):
    """Suggest 6 better, more specific search query variations for the chosen source.
    Returns (list_of_suggestions, error)."""
    src_label = {
        'google_maps': 'Google Maps',
        'justdial': 'JustDial India (B2B local)',
        'indiamart': 'IndiaMART (B2B suppliers/manufacturers)',
    }.get(source, 'business directory')
    prompt = f"""You are an expert at writing high-yield search queries for {src_label}.

USER ENTERED: "{query}"

Generate 6 better, more specific search variations that will return more relevant business results.
Mix in: location specifics, niche modifiers, intent qualifiers, alternative names, and target sub-types.

Output exactly 6 lines, each one a complete search query (no numbering, no bullets, no quotes, no explanation).
Each line should be on its own line."""
    text, err, _ = _generate(prompt, kind='subject')
    if err:
        return [], err
    suggestions = [line.strip() for line in text.split('\n') if line.strip()]
    # Clean numbering/bullets
    cleaned = []
    for s in suggestions:
        s = s.lstrip('0123456789.-) "\'').strip().rstrip('"\'').strip()
        if s and len(s) < 200:
            cleaned.append(s)
    return cleaned[:6], None


def extract_emails_with_ai(html, max_chars=15000):
    """Send contact-page HTML to Gemini and extract emails + contact info as JSON.
    Returns (dict|None, error). dict shape: {emails: [...], phones: [...], socials: {fb, ig, li}}"""
    if not html:
        return None, "Empty HTML"
    # Trim HTML to focus on body (Gemini has 1M context but cheaper to be brief)
    snippet = html[:max_chars]
    prompt = f"""Extract real contact information from this contact-page HTML.

HTML:
\"\"\"
{snippet}
\"\"\"

Return ONLY valid JSON (no markdown, no commentary) with this exact shape:
{{"emails": ["..."], "phones": ["..."], "socials": {{"facebook": "", "instagram": "", "linkedin": ""}}}}

Rules:
- emails: deduped, real business emails only (skip noreply@, sentry@, .png@ junk, image filenames)
- phones: deduped, formatted with country code if visible
- socials: only profile URLs, not share links
- If a field has no data, use empty list/empty string. Never invent data."""
    text, err, _ = _generate(prompt, kind='improve')
    if err:
        return None, err
    # Parse JSON (strip markdown fences if present)
    import json, re as _re
    cleaned = text.strip()
    cleaned = _re.sub(r'^```(?:json)?\s*', '', cleaned)
    cleaned = _re.sub(r'\s*```$', '', cleaned)
    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return None, "Bad shape"
        return data, None
    except json.JSONDecodeError as e:
        return None, f"JSON parse: {e}"


def test_prompt(custom_prompt):
    """Send a custom prompt to the configured model. Returns (output, error, usage)."""
    if not custom_prompt or not custom_prompt.strip():
        custom_prompt = "In one sentence, suggest a creative business name for an AI lead-generation SaaS targeting Indian SMBs."
    return _generate(custom_prompt, kind='test')


def fetch_available_models():
    """Live-list models from Gemini API. Returns ([model_ids], error)."""
    client, s, err = _client()
    if err:
        return [], err
    try:
        models = client.models.list()
        ids = []
        for m in models:
            name = getattr(m, 'name', '') or ''
            short = name.replace('models/', '')
            if 'gemini' in short.lower():
                ids.append(short)
        return sorted(set(ids)), None
    except Exception as e:
        return [], str(e)


def usage_stats(model_id=None, days=1):
    """Aggregated token usage from local logs."""
    from django.db.models import Sum, Count
    from datetime import timedelta
    from django.utils import timezone
    qs = AIGenerationLog.objects.all()
    if model_id:
        qs = qs.filter(model=model_id)
    since = timezone.now() - timedelta(days=days)
    qs = qs.filter(created_at__gte=since)
    agg = qs.aggregate(
        n=Count('id'),
        prompt=Sum('prompt_tokens'),
        out=Sum('output_tokens'),
        total=Sum('total_tokens'),
    )
    return {
        'requests': agg['n'] or 0,
        'prompt_tokens': agg['prompt'] or 0,
        'output_tokens': agg['out'] or 0,
        'total_tokens': agg['total'] or 0,
    }

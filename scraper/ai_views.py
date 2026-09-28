"""AI-powered views (Gemini)."""
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import user_passes_test

from .models import GeminiSetting, AIGenerationLog, Place
from .ai_service import (
    write_email, suggest_subject, suggest_reply, improve_text,
    test_prompt, fetch_available_models, usage_stats, GEMINI_MODELS,
    enhance_search_query,
)


staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


def ai_home(request):
    s = GeminiSetting.get()
    recent = AIGenerationLog.objects.all()[:15]
    total = AIGenerationLog.objects.count()
    by_kind = {}
    for k, _ in AIGenerationLog.KIND_CHOICES:
        by_kind[k] = AIGenerationLog.objects.filter(kind=k).count()
    return render(request, 'scraper/ai_home.html', {
        'setting': s, 'recent': recent, 'total': total, 'by_kind': by_kind,
    })


@staff_required
def ai_settings(request):
    s = GeminiSetting.get()
    if request.method == 'POST':
        s.api_key = request.POST.get('api_key', '').strip() or s.api_key
        s.model = request.POST.get('model', 'gemini-2.5-flash').strip()
        s.is_active = request.POST.get('is_active') == 'on'
        s.save()
        messages.success(request, '✅ Gemini settings saved.')
        return redirect('ai_settings')

    # Per-model usage stats from logs (last 24h)
    models_with_stats = []
    for m in GEMINI_MODELS:
        u = usage_stats(model_id=m['id'], days=1)
        # Estimate remaining today (RPD - requests today)
        remaining_rpd = max(0, m['free_rpd'] - u['requests'])
        models_with_stats.append({
            **m,
            'is_current': m['id'] == s.model,
            'requests_today': u['requests'],
            'tokens_today': u['total_tokens'],
            'rpd_remaining': remaining_rpd,
            'rpd_pct': int((u['requests'] / m['free_rpd']) * 100) if m['free_rpd'] else 0,
        })

    overall = usage_stats(days=1)
    overall_all_time = usage_stats(days=3650)

    return render(request, 'scraper/ai_settings.html', {
        'setting': s,
        'models': models_with_stats,
        'overall': overall,
        'overall_all': overall_all_time,
    })


@require_POST
@staff_required
def ai_test(request):
    """Test the current configured model with a custom prompt."""
    prompt = request.POST.get('prompt', '').strip()
    output, err, usage = test_prompt(prompt)
    if err:
        return JsonResponse({'ok': False, 'error': err})
    return JsonResponse({
        'ok': True,
        'output': output,
        'usage': usage,
    })


@require_POST
def ai_enhance_search(request):
    """AI-suggest better search queries."""
    query = request.POST.get('query', '').strip()
    source = request.POST.get('source', 'google_maps').strip()
    if not query:
        return JsonResponse({'ok': False, 'error': 'Enter a search term first.'})
    suggestions, err = enhance_search_query(query, source=source)
    if err:
        return JsonResponse({'ok': False, 'error': err})
    return JsonResponse({'ok': True, 'suggestions': suggestions})


@staff_required
def ai_models_live(request):
    """List models live from Gemini API."""
    ids, err = fetch_available_models()
    return JsonResponse({'ok': err is None, 'models': ids, 'error': err})


def ai_writer(request):
    """Form to pick a place + goal + tone, generate email."""
    place_id = request.GET.get('place')
    place = get_object_or_404(Place, id=place_id) if place_id else None
    return render(request, 'scraper/ai_writer.html', {
        'place': place,
        'recent_places': Place.objects.exclude(email='').order_by('-lead_score')[:50],
    })


@require_POST
def ai_generate_email(request):
    place_id = request.POST.get('place_id')
    place = get_object_or_404(Place, id=place_id) if place_id else None
    goal = request.POST.get('goal', 'cold outreach to schedule a call').strip()
    tone = request.POST.get('tone', 'professional and warm').strip()
    extra = request.POST.get('extra', '').strip()
    subject, body, err, usage = write_email(place, goal=goal, tone=tone, extra=extra)
    if err:
        return JsonResponse({'ok': False, 'error': err})
    return JsonResponse({'ok': True, 'subject': subject or '', 'body': body or '', 'usage': usage})


@require_POST
def ai_suggest_subject(request):
    name = request.POST.get('name', 'business')
    category = request.POST.get('category', '')
    current = request.POST.get('current', '')
    text, err = suggest_subject(name, category, current)
    if err:
        return JsonResponse({'ok': False, 'error': err})
    return JsonResponse({'ok': True, 'output': text})


def ai_reply(request):
    return render(request, 'scraper/ai_reply.html', {
        'recent_places': Place.objects.order_by('-updated_at')[:30],
    })


@require_POST
def ai_generate_reply(request):
    place_id = request.POST.get('place_id')
    place = get_object_or_404(Place, id=place_id) if place_id else None
    incoming = request.POST.get('incoming', '').strip()
    intent = request.POST.get('intent', 'warm response leading to a call').strip()
    if not incoming:
        return JsonResponse({'ok': False, 'error': 'Paste the prospect reply.'})
    text, err = suggest_reply(incoming, place=place, my_intent=intent)
    if err:
        return JsonResponse({'ok': False, 'error': err})
    return JsonResponse({'ok': True, 'output': text})


@require_POST
def ai_improve(request):
    text = request.POST.get('text', '').strip()
    instr = request.POST.get('instruction', 'make it sound natural and concise').strip()
    if not text:
        return JsonResponse({'ok': False, 'error': 'No text provided.'})
    out, err = improve_text(text, instr)
    if err:
        return JsonResponse({'ok': False, 'error': err})
    return JsonResponse({'ok': True, 'output': out})


def ai_logs(request):
    logs = AIGenerationLog.objects.all()[:200]
    return render(request, 'scraper/ai_logs.html', {'logs': logs})


def features_page(request):
    return render(request, 'scraper/features.html', {})

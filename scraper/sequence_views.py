"""Email Sequences (drip campaigns) — admin views + send-due-step processing."""
from datetime import datetime, timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib import messages
from django.db.models import Count, Q, Max
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    EmailSequence, SequenceStep, SequenceEnrollment, EmailTemplate, EmailAccount,
    Place, EmailLog,
)
from .calling_utils import admin_required
from .email_service import send_one, get_default_account


# ============================================================
# Sequence CRUD
# ============================================================

@admin_required
def sequences_list(request):
    seqs = EmailSequence.objects.annotate(
        step_count=Count('steps', distinct=True),
        active_enrollments=Count('enrollments', filter=Q(enrollments__status='active'), distinct=True),
        total_enrollments=Count('enrollments', distinct=True),
    ).order_by('-updated_at')
    return render(request, 'scraper/sequences/list.html', {'seqs': seqs})


@admin_required
def sequence_form(request, seq_id=None):
    seq = get_object_or_404(EmailSequence, id=seq_id) if seq_id else None

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if not name:
            messages.error(request, 'Name required.')
            return redirect(request.path)

        if seq:
            seq.name = name
            seq.description = request.POST.get('description', '')
            seq.stop_on_reply = request.POST.get('stop_on_reply') == 'on'
            seq.stop_on_status = request.POST.get('stop_on_status', 'converted,rejected')
            seq.is_active = request.POST.get('is_active') == 'on'
            seq.save()
        else:
            seq = EmailSequence.objects.create(
                name=name,
                description=request.POST.get('description', ''),
                stop_on_reply=request.POST.get('stop_on_reply') == 'on',
                stop_on_status=request.POST.get('stop_on_status', 'converted,rejected'),
                created_by=request.user,
            )
        messages.success(request, f'Sequence "{name}" saved.')
        return redirect('sequence_edit', seq_id=seq.id)

    templates = EmailTemplate.objects.filter(is_active=True).order_by('name')
    steps = seq.steps.all().order_by('order') if seq else []
    enrollments = []
    if seq:
        enrollments = seq.enrollments.select_related('place').order_by('-enrolled_at')[:20]
    return render(request, 'scraper/sequences/form.html', {
        'seq': seq,
        'templates': templates,
        'steps': steps,
        'enrollments': enrollments,
    })


@admin_required
@require_POST
def sequence_delete(request, seq_id):
    seq = get_object_or_404(EmailSequence, id=seq_id)
    name = seq.name
    seq.delete()
    messages.success(request, f'Sequence "{name}" deleted.')
    return redirect('sequences_list')


@admin_required
@require_POST
def step_add(request, seq_id):
    seq = get_object_or_404(EmailSequence, id=seq_id)
    try:
        template_id = int(request.POST.get('template_id'))
    except (ValueError, TypeError):
        messages.error(request, 'Select a template.')
        return redirect('sequence_edit', seq_id=seq.id)
    template = get_object_or_404(EmailTemplate, id=template_id)

    next_order = (seq.steps.aggregate(m=Max('order'))['m'] or 0) + 1
    try:
        days = int(request.POST.get('days_after_previous', 3))
    except ValueError:
        days = 3
    SequenceStep.objects.create(
        sequence=seq, order=next_order, days_after_previous=days,
        template=template, subject_override=request.POST.get('subject_override', '').strip(),
    )
    messages.success(request, f'Step {next_order} added.')
    return redirect('sequence_edit', seq_id=seq.id)


@admin_required
@require_POST
def step_delete(request, step_id):
    step = get_object_or_404(SequenceStep, id=step_id)
    seq_id = step.sequence_id
    step.delete()
    # Re-number remaining steps
    for i, s in enumerate(SequenceStep.objects.filter(sequence_id=seq_id).order_by('order'), start=1):
        if s.order != i:
            s.order = i
            s.save(update_fields=['order'])
    messages.success(request, 'Step removed.')
    return redirect('sequence_edit', seq_id=seq_id)


# ============================================================
# Enrollment
# ============================================================

@admin_required
def sequence_enroll(request, seq_id):
    """Bulk enrollment page — pick leads to enroll."""
    seq = get_object_or_404(EmailSequence, id=seq_id)

    if request.method == 'POST':
        place_ids = request.POST.getlist('place_ids')
        added = skipped = 0
        for pid in place_ids:
            try:
                pid = int(pid)
            except ValueError:
                continue
            try:
                place = Place.objects.get(id=pid)
                if not place.email:
                    skipped += 1
                    continue
                _, created = SequenceEnrollment.objects.get_or_create(
                    sequence=seq, place=place,
                    defaults={
                        'current_step_order': 0,
                        'next_send_at': timezone.now(),
                        'status': 'active',
                        'enrolled_by': request.user,
                    },
                )
                if created:
                    added += 1
                else:
                    skipped += 1
            except Place.DoesNotExist:
                skipped += 1
        messages.success(request, f'Enrolled {added} leads ({skipped} skipped).')
        return redirect('sequence_edit', seq_id=seq.id)

    # GET: show form with eligible leads
    q = request.GET.get('q', '').strip()
    qs = Place.objects.exclude(email='').exclude(
        id__in=SequenceEnrollment.objects.filter(sequence=seq).values_list('place_id', flat=True)
    )
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(email__icontains=q) |
            Q(category__icontains=q) | Q(address__icontains=q)
        )
    leads = qs.order_by('-lead_score')[:100]
    return render(request, 'scraper/sequences/enroll.html', {
        'seq': seq, 'leads': leads, 'q': q,
    })


@admin_required
@require_POST
def enrollment_action(request, enrollment_id):
    enr = get_object_or_404(SequenceEnrollment, id=enrollment_id)
    action = request.POST.get('action', '')
    if action == 'pause':
        enr.status = 'paused'
    elif action == 'resume':
        enr.status = 'active'
    elif action == 'stop':
        enr.status = 'stopped'
        enr.stop_reason = 'Manual stop'
    elif action == 'delete':
        enr.delete()
        messages.success(request, 'Enrollment removed.')
        return redirect('sequence_edit', seq_id=request.POST.get('seq_id'))
    enr.save()
    messages.success(request, f'Enrollment {action}d.')
    return redirect('sequence_edit', seq_id=enr.sequence_id)


# ============================================================
# Auto-send pending steps (called via URL or background)
# ============================================================

def _process_due_enrollments(limit=50):
    """Process enrollments whose next step is due. Returns (sent, skipped, errors)."""
    now = timezone.now()
    sent = skipped = errors = 0

    due = SequenceEnrollment.objects.filter(
        status='active', next_send_at__lte=now,
    ).select_related('sequence', 'place')[:limit]

    account = get_default_account()
    if not account:
        return 0, 0, 0  # no email account configured

    for enr in due:
        try:
            # Determine next step
            next_order = enr.current_step_order + 1
            step = enr.sequence.steps.filter(order=next_order, is_active=True).first()
            if not step:
                # Out of steps
                enr.status = 'completed'
                enr.completed_at = now
                enr.save()
                skipped += 1
                continue

            # Check stop conditions
            if enr.place.lead_status in enr.sequence.stop_statuses:
                enr.status = 'stopped'
                enr.stop_reason = f'Lead status: {enr.place.lead_status}'
                enr.save()
                skipped += 1
                continue

            # Send
            log = send_one(account, enr.place, step.template, enrollment=enr)
            if log and log.status == 'sent':
                enr.current_step_order = next_order
                enr.last_sent_at = now
                # Compute next_send_at based on NEXT step's days_after_previous
                upcoming = enr.sequence.steps.filter(order=next_order + 1, is_active=True).first()
                if upcoming:
                    enr.next_send_at = now + timedelta(days=upcoming.days_after_previous)
                else:
                    # Last step done
                    enr.status = 'completed'
                    enr.completed_at = now
                enr.save()
                sent += 1
            else:
                errors += 1
        except Exception:
            errors += 1
    return sent, skipped, errors


@admin_required
def run_due_steps(request):
    """Manual trigger to send due steps (replaces cron for dev)."""
    sent, skipped, errors = _process_due_enrollments(limit=200)
    return JsonResponse({'sent': sent, 'skipped': skipped, 'errors': errors})

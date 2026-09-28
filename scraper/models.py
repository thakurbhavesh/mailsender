from django.db import models
from django.conf import settings
from django.utils import timezone


class ScrapeJob(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    SOURCE_CHOICES = [
        ('google_maps', 'Google Maps'),
        ('justdial', 'JustDial'),
        ('indiamart', 'IndiaMART'),
    ]

    search_term = models.CharField(max_length=255)
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, default='google_maps')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    total_results = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.search_term} ({self.status})"


class Place(models.Model):
    LEAD_STATUS_CHOICES = [
        ('new', 'New'),
        ('contacted', 'Contacted'),
        ('interested', 'Interested'),
        ('converted', 'Converted'),
        ('rejected', 'Rejected'),
    ]

    job = models.ForeignKey(ScrapeJob, on_delete=models.CASCADE, related_name='places')
    name = models.CharField(max_length=500)
    rating = models.CharField(max_length=20, blank=True, default='')
    reviews_count = models.IntegerField(default=0)
    rating_reviews = models.CharField(max_length=50, blank=True, default='')
    category = models.CharField(max_length=200, blank=True, default='')
    address = models.TextField(blank=True, default='')
    phone = models.CharField(max_length=50, blank=True, default='')
    website = models.URLField(max_length=500, blank=True, default='')

    # Lead fields
    email = models.EmailField(max_length=255, blank=True, default='')
    facebook = models.URLField(max_length=500, blank=True, default='')
    instagram = models.URLField(max_length=500, blank=True, default='')
    linkedin = models.URLField(max_length=500, blank=True, default='')
    lead_status = models.CharField(max_length=20, choices=LEAD_STATUS_CHOICES, default='new')
    notes = models.TextField(blank=True, default='')
    lead_score = models.IntegerField(default=0)
    enriched = models.BooleanField(default=False)
    source = models.CharField(max_length=30, default='google_maps')

    unique_key = models.CharField(max_length=600, db_index=True, unique=True)
    times_seen = models.IntegerField(default=1)
    last_change_log = models.TextField(blank=True, default='')
    enrichment_status = models.CharField(max_length=20, default='pending',
        choices=[('pending', 'Pending'), ('done', 'Done'), ('failed', 'Failed'), ('skipped', 'Skipped')])
    enrichment_log = models.TextField(blank=True, default='')
    enriched_at = models.DateTimeField(null=True, blank=True)

    # Extended fields from Google Maps
    opening_hours = models.TextField(blank=True, default='')
    description = models.TextField(blank=True, default='')
    services = models.TextField(blank=True, default='', help_text='Comma-separated: Dine-in, Takeout, Delivery, etc.')
    secondary_categories = models.TextField(blank=True, default='')
    plus_code = models.CharField(max_length=50, blank=True, default='')
    place_url = models.URLField(max_length=500, blank=True, default='', help_text='Direct Google Maps link')
    photos_count = models.IntegerField(default=0)
    claimed = models.BooleanField(default=False, help_text='Owner-verified business')
    price_level = models.CharField(max_length=10, blank=True, default='', help_text='₹/₹₹/₹₹₹/₹₹₹₹')
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    extra_data = models.JSONField(default=dict, blank=True, help_text='Any other extracted fields')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Lead Management filters and sorts on these constantly.
            models.Index(fields=['lead_status']),
            models.Index(fields=['-lead_score']),
            models.Index(fields=['category']),
            models.Index(fields=['email']),
            models.Index(fields=['source', 'lead_status']),
            models.Index(fields=['-created_at']),
            models.Index(fields=['enrichment_status']),
        ]

    def __str__(self):
        return self.name

    def calculate_score(self):
        """Score 0-100 based on data completeness and rating."""
        score = 0
        if self.phone: score += 20
        if self.website: score += 15
        if self.email: score += 25
        if self.address: score += 10
        if self.facebook or self.instagram or self.linkedin: score += 10
        try:
            rating = float(self.rating) if self.rating else 0
            score += int(rating * 2)
        except (TypeError, ValueError):
            pass
        if self.reviews_count > 50:
            score += 10
        elif self.reviews_count > 10:
            score += 5
        return min(score, 100)

    def whatsapp_link(self, message=''):
        if not self.phone:
            return ''
        digits = ''.join(c for c in self.phone if c.isdigit())
        if not digits:
            return ''
        if len(digits) == 10:
            digits = '91' + digits
        from urllib.parse import quote
        return f"https://wa.me/{digits}?text={quote(message)}"


class EmailAccount(models.Model):
    """Gmail SMTP credentials. Use Gmail App Password (16 chars), not main password."""
    name = models.CharField(max_length=100, help_text="Friendly name e.g. 'Sales Inbox'")
    email = models.EmailField(unique=True)
    sender_name = models.CharField(max_length=100, blank=True, default='',
                                   help_text="Display name shown to recipients")
    app_password = models.CharField(max_length=64, help_text="16-char Gmail App Password")
    smtp_host = models.CharField(max_length=100, default='smtp.gmail.com')
    smtp_port = models.IntegerField(default=587)
    use_tls = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    daily_limit = models.IntegerField(default=400, help_text="Gmail allows ~500/day")
    sent_today = models.IntegerField(default=0)
    last_reset = models.DateField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_default', 'email']

    def __str__(self):
        return f"{self.name} <{self.email}>"

    def save(self, *args, **kwargs):
        if self.is_default:
            EmailAccount.objects.exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    def reset_quota_if_needed(self):
        from django.utils import timezone
        today = timezone.now().date()
        if self.last_reset != today:
            self.sent_today = 0
            self.last_reset = today
            self.save(update_fields=['sent_today', 'last_reset'])

    @property
    def quota_remaining(self):
        return max(0, self.daily_limit - self.sent_today)


class EmailTemplate(models.Model):
    """Reusable email templates. Supports {{name}}, {{category}}, {{address}}, {{phone}}, {{website}}, {{city}}."""
    name = models.CharField(max_length=100)
    subject = models.CharField(max_length=200)
    body_html = models.TextField(help_text="HTML body. Use {{name}}, {{category}} etc.")
    description = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    times_used = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def render(self, place):
        """Render template with placeholders from a Place instance."""
        ctx = {
            'name': place.name or 'there',
            'category': place.category or 'business',
            'address': place.address or '',
            'phone': place.phone or '',
            'website': place.website or '',
            'city': (place.address or '').split(',')[-1].strip() if place.address else '',
            'rating': place.rating or '',
        }
        subject = self.subject
        body = self.body_html
        for k, v in ctx.items():
            subject = subject.replace('{{' + k + '}}', str(v))
            body = body.replace('{{' + k + '}}', str(v))
        return subject, body


class GeminiSetting(models.Model):
    """Google Gemini API settings (singleton)."""
    api_key = models.CharField(max_length=200, blank=True, default='')
    model = models.CharField(max_length=50, default='gemini-2.0-flash-exp')
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Gemini ({self.model})"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj


class AIGenerationLog(models.Model):
    """Track all AI generations for cost/usage monitoring."""
    KIND_CHOICES = [
        ('email_write', 'Email Writer'),
        ('reply', 'Reply Assist'),
        ('improve', 'Improve Text'),
        ('subject', 'Subject Suggest'),
        ('test', 'Test'),
    ]
    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    model = models.CharField(max_length=80, blank=True, default='')
    place = models.ForeignKey(Place, on_delete=models.SET_NULL, null=True, blank=True)
    prompt = models.TextField(blank=True, default='')
    output = models.TextField(blank=True, default='')
    error = models.TextField(blank=True, default='')
    prompt_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class EmailLog(models.Model):
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]
    place = models.ForeignKey(Place, on_delete=models.SET_NULL, null=True, blank=True, related_name='emails')
    template = models.ForeignKey(EmailTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    account = models.ForeignKey(EmailAccount, on_delete=models.SET_NULL, null=True, blank=True)
    to_email = models.EmailField()
    to_name = models.CharField(max_length=200, blank=True, default='')
    subject = models.CharField(max_length=300)
    body_html = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    error_message = models.TextField(blank=True, default='')
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Tracking fields
    open_token = models.CharField(max_length=64, blank=True, default='', db_index=True,
                                  help_text='Unique token for tracking pixel + clicks')
    opened_at = models.DateTimeField(null=True, blank=True)
    opened_count = models.IntegerField(default=0)
    last_opened_at = models.DateTimeField(null=True, blank=True)
    clicked_count = models.IntegerField(default=0)
    clicks = models.JSONField(default=list, blank=True,
                              help_text='List of {url, at} per click')
    user_agent = models.CharField(max_length=300, blank=True, default='')

    # Delivery failures the SMTP server reported outright, as opposed to a
    # send that left but was rejected later.
    bounced = models.BooleanField(default=False)
    bounce_reason = models.CharField(max_length=300, blank=True, default='')
    bounced_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    # Sequence linkage
    sequence_enrollment = models.ForeignKey(
        'SequenceEnrollment', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='emails',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['place', 'status']),
            models.Index(fields=['status', 'sent_at']),
            models.Index(fields=['to_email']),
        ]

    def __str__(self):
        return f"{self.to_email} - {self.subject[:30]}"

    def ensure_token(self):
        if not self.open_token:
            import secrets
            self.open_token = secrets.token_urlsafe(24)
            self.save(update_fields=['open_token'])
        return self.open_token


class Suppression(models.Model):
    """Addresses that must never be emailed again.

    Keyed on the address rather than the lead, so the same person is still
    covered when they appear under a second scraped business.
    """
    REASON_CHOICES = [
        ('unsubscribed', 'Unsubscribed'),
        ('bounced', 'Hard bounce'),
        ('complained', 'Marked as spam'),
        ('manual', 'Added by hand'),
    ]

    email = models.EmailField(unique=True, db_index=True)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default='unsubscribed')
    note = models.CharField(max_length=300, blank=True, default='')
    place = models.ForeignKey(Place, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='suppressions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.email} ({self.reason})"

    @classmethod
    def blocks(cls, email):
        if not email:
            return False
        return cls.objects.filter(email__iexact=email.strip()).exists()

    @classmethod
    def add(cls, email, reason='unsubscribed', place=None, note=''):
        if not email:
            return None
        obj, _ = cls.objects.get_or_create(
            email=email.strip().lower(),
            defaults={'reason': reason, 'place': place, 'note': note[:300]},
        )
        return obj


# ============================================================
# EMAIL SEQUENCES (Drip Campaigns)
# ============================================================

class EmailSequence(models.Model):
    """A multi-step drip campaign."""
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    stop_on_reply = models.BooleanField(default=True,
                                        help_text='Stop if lead replies (manual mark for now)')
    stop_on_status = models.CharField(max_length=100, blank=True, default='converted,rejected',
                                      help_text='Comma-sep lead_status values that stop the sequence')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    times_used = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name

    @property
    def stop_statuses(self):
        return [s.strip() for s in (self.stop_on_status or '').split(',') if s.strip()]


class SequenceStep(models.Model):
    """A single step in a drip campaign."""
    sequence = models.ForeignKey(EmailSequence, on_delete=models.CASCADE, related_name='steps')
    order = models.IntegerField(default=1)
    days_after_previous = models.IntegerField(default=3,
                                              help_text='Days to wait after previous step (0=immediate)')
    template = models.ForeignKey(EmailTemplate, on_delete=models.CASCADE, related_name='+')
    subject_override = models.CharField(max_length=300, blank=True, default='',
                                        help_text='Optional override for the template subject')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['sequence', 'order']
        unique_together = [('sequence', 'order')]

    def __str__(self):
        return f"{self.sequence.name} step {self.order}"


class SequenceEnrollment(models.Model):
    """A lead enrolled in a sequence."""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('completed', 'Completed'),
        ('stopped', 'Stopped'),
    ]

    sequence = models.ForeignKey(EmailSequence, on_delete=models.CASCADE, related_name='enrollments')
    place = models.ForeignKey(Place, on_delete=models.CASCADE, related_name='enrollments')
    current_step_order = models.IntegerField(default=0,
                                             help_text='0 = not yet sent, 1 = step 1 sent, etc.')
    next_send_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    enrolled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    enrolled_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    stop_reason = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-enrolled_at']
        unique_together = [('sequence', 'place')]
        indexes = [models.Index(fields=['status', 'next_send_at'])]

    def __str__(self):
        return f"{self.place.name} -> {self.sequence.name} (step {self.current_step_order})"


# ============================================================
# CALENDAR BOOKING
# ============================================================

class AvailabilitySchedule(models.Model):
    """A user's bookable calendar — Calendly-style."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='schedule')
    slug = models.SlugField(unique=True, max_length=80,
                            help_text='Public URL slug e.g. /meet/bhavesh/')
    title = models.CharField(max_length=200, default='Book a meeting')
    description = models.TextField(blank=True, default='')
    slot_duration_min = models.IntegerField(default=30)
    buffer_min = models.IntegerField(default=10,
                                     help_text='Buffer time between meetings')
    advance_days = models.IntegerField(default=14, help_text='How many days ahead to allow booking')
    min_notice_hours = models.IntegerField(default=4,
                                           help_text='Min hours notice required')
    # Working hours as JSON: {"mon": [["09:00","17:00"]], ...}
    working_hours = models.JSONField(default=dict, blank=True)
    color = models.CharField(max_length=20, default='#6366f1')
    is_active = models.BooleanField(default=True)
    require_phone = models.BooleanField(default=True)
    timezone_name = models.CharField(max_length=64, default='Asia/Kolkata')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}'s schedule (/meet/{self.slug}/)"

    def default_hours(self):
        return {
            'mon': [['09:00', '17:00']],
            'tue': [['09:00', '17:00']],
            'wed': [['09:00', '17:00']],
            'thu': [['09:00', '17:00']],
            'fri': [['09:00', '17:00']],
            'sat': [],
            'sun': [],
        }


class Meeting(models.Model):
    """A booked meeting via the public calendar."""
    STATUS_CHOICES = [
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
        ('no_show', 'No Show'),
    ]

    schedule = models.ForeignKey(AvailabilitySchedule, on_delete=models.CASCADE, related_name='meetings')
    place = models.ForeignKey(Place, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='meetings')
    booker_name = models.CharField(max_length=200)
    booker_email = models.EmailField()
    booker_phone = models.CharField(max_length=30, blank=True, default='')
    scheduled_at = models.DateTimeField()
    duration_min = models.IntegerField(default=30)
    notes = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='confirmed')
    cancel_token = models.CharField(max_length=64, blank=True, default='', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-scheduled_at']

    def __str__(self):
        return f"{self.booker_name} with {self.schedule.user.username} @ {self.scheduled_at:%d %b %H:%M}"


# ============================================================
# CALLING TEAM MODELS
# ============================================================

class CallerProfile(models.Model):
    """Extra info for each calling-team member. One per User."""
    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('manager', 'Manager'),
        ('caller', 'Caller'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='caller_profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='caller')
    phone = models.CharField(max_length=20, blank=True, default='')
    daily_quota = models.IntegerField(default=100, help_text='Suggested daily target (for goals/progress only)')
    auto_assign_enabled = models.BooleanField(default=False,
        help_text='If ON: system auto-assigns leads up to daily_quota. If OFF: admin assigns manually only.')
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default='')
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='team_members',
        help_text='Reporting manager — leave empty for top-level',
    )
    timezone = models.CharField(max_length=64, blank=True, default='Asia/Kolkata',
                                help_text='IANA timezone, e.g. Asia/Kolkata')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['user__username']

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    @property
    def is_admin(self):
        return self.role == 'admin' or self.user.is_superuser

    @property
    def is_manager(self):
        return self.role == 'manager'

    @property
    def is_caller(self):
        return self.role == 'caller' and not self.user.is_superuser


class Attendance(models.Model):
    """One row per caller per day. Created on check-in."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='attendances')
    date = models.DateField(default=timezone.now)
    check_in = models.DateTimeField(default=timezone.now)
    check_out = models.DateTimeField(null=True, blank=True)
    total_minutes = models.IntegerField(default=0)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-date', '-check_in']
        unique_together = [('user', 'date')]

    def __str__(self):
        return f"{self.user.username} - {self.date}"

    def close(self):
        """Mark check-out and ADD current session minutes to total."""
        if not self.check_out:
            self.check_out = timezone.now()
            delta = self.check_out - self.check_in
            session_min = max(0, int(delta.total_seconds() // 60))
            self.total_minutes = (self.total_minutes or 0) + session_min
            self.save(update_fields=['check_out', 'total_minutes'])

    @property
    def hours_worked_display(self):
        """Total worked across all sessions today."""
        total = self.total_minutes or 0
        if not self.check_out:
            # Currently active session — add live elapsed
            delta = timezone.now() - self.check_in
            total += max(0, int(delta.total_seconds() // 60))
        h, m = divmod(total, 60)
        return f"{h}h {m}m"

    @property
    def total_minutes_live(self):
        """Live total (accumulated + current session if open)."""
        total = self.total_minutes or 0
        if not self.check_out:
            delta = timezone.now() - self.check_in
            total += max(0, int(delta.total_seconds() // 60))
        return total


class LeadAssignment(models.Model):
    """Maps which Place (lead) is assigned to which caller. Prevents duplicates."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('skipped', 'Skipped'),
    ]

    place = models.ForeignKey(Place, on_delete=models.CASCADE, related_name='assignments')
    caller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='lead_assignments')
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='assignments_made')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    completed_at = models.DateTimeField(null=True, blank=True)
    # Snapshot of lead status when caller marked it (for daily reporting)
    outcome = models.CharField(max_length=30, blank=True, default='')

    class Meta:
        ordering = ['-assigned_at']
        unique_together = [('place', 'caller')]
        indexes = [
            models.Index(fields=['caller', 'status']),
            models.Index(fields=['assigned_at']),
        ]

    def __str__(self):
        return f"{self.place.name} -> {self.caller.username} ({self.status})"


class CallLog(models.Model):
    """Record of every call attempt by a caller on a lead."""
    OUTCOME_CHOICES = [
        ('answered', 'Answered'),
        ('no_answer', 'No Answer'),
        ('busy', 'Busy'),
        ('wrong_number', 'Wrong Number'),
        ('callback', 'Callback Requested'),
        ('interested', 'Interested'),
        ('not_interested', 'Not Interested'),
        ('converted', 'Converted'),
        ('rejected', 'Rejected'),
    ]

    place = models.ForeignKey(Place, on_delete=models.CASCADE, related_name='call_logs')
    caller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='call_logs')
    assignment = models.ForeignKey(LeadAssignment, on_delete=models.SET_NULL, null=True, blank=True, related_name='calls')
    outcome = models.CharField(max_length=30, choices=OUTCOME_CHOICES, default='no_answer')
    notes = models.TextField(blank=True, default='')
    callback_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['caller', 'created_at']),
        ]

    def __str__(self):
        return f"{self.caller.username} -> {self.place.name} [{self.outcome}]"


# ============================================================
# LEAD IMPORT, PERFORMANCE, NOTIFICATIONS
# ============================================================

class LeadImportJob(models.Model):
    """Tracks a CSV/Excel upload that creates leads in bulk."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('previewed', 'Previewed'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='lead_imports')
    file = models.FileField(upload_to='lead_imports/%Y/%m/')
    original_filename = models.CharField(max_length=255)
    source_label = models.CharField(max_length=50, default='upload',
                                    help_text='Source tag for imported leads')
    column_mapping = models.JSONField(default=dict, blank=True,
                                      help_text='Maps CSV header -> Place field')
    total_rows = models.IntegerField(default=0)
    imported = models.IntegerField(default=0)
    duplicates = models.IntegerField(default=0)
    errors = models.IntegerField(default=0)
    error_log = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.original_filename} by {self.user.username} ({self.status})"


class CallerGoal(models.Model):
    """Monthly performance target per caller."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='goals')
    month = models.DateField(help_text='First day of the month, e.g. 2026-05-01')
    target_calls = models.IntegerField(default=0)
    target_conversions = models.IntegerField(default=0)
    target_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-month']
        unique_together = [('user', 'month')]

    def __str__(self):
        return f"{self.user.username} - {self.month:%b %Y}"


class IncentiveRule(models.Model):
    """Org-wide incentive payout rules (₹ per outcome)."""
    name = models.CharField(max_length=100, default='Default')
    per_answered_call = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    per_interested = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    per_conversion = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bonus_threshold = models.IntegerField(default=0,
                                          help_text='Extra bonus after this many conversions in a month')
    bonus_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-effective_from']

    def __str__(self):
        return f"{self.name} (active={self.is_active})"

    @classmethod
    def current(cls):
        return cls.objects.filter(is_active=True).order_by('-effective_from').first()


class BreakLog(models.Model):
    """Caller break records (lunch, tea, etc.)."""
    REASON_CHOICES = [
        ('break', 'Short Break'),
        ('lunch', 'Lunch'),
        ('meeting', 'Meeting'),
        ('other', 'Other'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='breaks')
    start = models.DateTimeField(default=timezone.now)
    end = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default='break')
    duration_minutes = models.IntegerField(default=0)
    note = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-start']

    def __str__(self):
        return f"{self.user.username} {self.reason} {self.start:%Y-%m-%d %H:%M}"

    def close(self):
        if not self.end:
            self.end = timezone.now()
            delta = self.end - self.start
            self.duration_minutes = max(0, int(delta.total_seconds() // 60))
            self.save(update_fields=['end', 'duration_minutes'])


class Shift(models.Model):
    """A working shift definition — e.g. Morning 9-5, Evening 5-11."""
    DAY_CHOICES = [
        ('mon', 'Monday'), ('tue', 'Tuesday'), ('wed', 'Wednesday'),
        ('thu', 'Thursday'), ('fri', 'Friday'),
        ('sat', 'Saturday'), ('sun', 'Sunday'),
    ]

    name = models.CharField(max_length=100, help_text='e.g. Morning Shift')
    start_time = models.TimeField(help_text='Local time, e.g. 09:00')
    end_time = models.TimeField(help_text='Local time, e.g. 17:00')
    # Comma-separated day codes: "mon,tue,wed,thu,fri"
    days = models.CharField(max_length=64, default='mon,tue,wed,thu,fri',
                            help_text='Comma-separated codes from: mon,tue,wed,thu,fri,sat,sun')
    timezone_name = models.CharField(max_length=64, default='Asia/Kolkata')
    color = models.CharField(max_length=20, default='#6366f1',
                             help_text='Hex color for calendar display')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start_time']

    def __str__(self):
        return f"{self.name} ({self.start_time:%H:%M}-{self.end_time:%H:%M})"

    @property
    def day_list(self):
        return [d.strip() for d in self.days.split(',') if d.strip()]

    def includes_today(self):
        from django.utils import timezone as tz
        wd = ['mon','tue','wed','thu','fri','sat','sun'][tz.localtime().weekday()]
        return wd in self.day_list

    def is_active_now(self):
        from django.utils import timezone as tz
        if not self.includes_today():
            return False
        now = tz.localtime().time()
        return self.start_time <= now <= self.end_time


class ShiftAssignment(models.Model):
    """Assign callers to shifts with effective dates."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='shift_assignments')
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name='assignments')
    effective_from = models.DateField(default=timezone.now)
    effective_to = models.DateField(null=True, blank=True,
                                    help_text='Leave empty for ongoing assignment')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-effective_from']

    def __str__(self):
        return f"{self.user.username} -> {self.shift.name}"

    def is_current(self):
        today = timezone.localdate()
        if self.effective_from > today:
            return False
        if self.effective_to and self.effective_to < today:
            return False
        return True


class SavedReport(models.Model):
    """User-defined parameterized report."""
    ENTITY_CHOICES = [
        ('calls', 'Call Logs'),
        ('assignments', 'Lead Assignments'),
        ('leads', 'Leads'),
        ('attendance', 'Attendance'),
    ]
    CHART_CHOICES = [
        ('table', 'Table'),
        ('bar', 'Bar Chart'),
        ('line', 'Line Chart'),
        ('pie', 'Pie Chart'),
    ]

    name = models.CharField(max_length=200)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='saved_reports')
    entity = models.CharField(max_length=30, choices=ENTITY_CHOICES, default='calls')
    config = models.JSONField(default=dict, blank=True,
                              help_text='{dimensions:[], metrics:[], filters:{}, chart:"bar"}')
    is_shared = models.BooleanField(default=False, help_text='Visible to other admins')
    times_run = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.name} ({self.entity})"


class Notification(models.Model):
    """Generic notification for users (mentions, callbacks, deal-closed, etc.)."""
    KIND_CHOICES = [
        ('mention', 'Mention'),
        ('callback_due', 'Callback Due'),
        ('lead_assigned', 'Lead Assigned'),
        ('deal_closed', 'Deal Closed'),
        ('goal_hit', 'Goal Hit'),
        ('system', 'System'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name='+',
                              help_text='Who triggered the notification')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='system')
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default='')
    url = models.CharField(max_length=300, blank=True, default='')
    place = models.ForeignKey(Place, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read']),
        ]

    def __str__(self):
        return f"{self.user.username}: {self.title}"

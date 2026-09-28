from django.contrib import admin
from .models import (
    ScrapeJob, Place, EmailAccount, EmailTemplate, EmailLog, GeminiSetting, AIGenerationLog,
    CallerProfile, Attendance, LeadAssignment, CallLog,
)


@admin.register(GeminiSetting)
class GeminiSettingAdmin(admin.ModelAdmin):
    list_display = ('id', 'model', 'is_active', 'updated_at')


@admin.register(AIGenerationLog)
class AIGenerationLogAdmin(admin.ModelAdmin):
    list_display = ('kind', 'place', 'created_at')
    list_filter = ('kind',)


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    list_display = ('email', 'name', 'is_default', 'is_active', 'sent_today', 'daily_limit')
    list_filter = ('is_default', 'is_active')


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'subject', 'is_active', 'times_used', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'subject')


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ('to_email', 'subject', 'status', 'account', 'sent_at')
    list_filter = ('status',)
    search_fields = ('to_email', 'subject')


@admin.register(ScrapeJob)
class ScrapeJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'search_term', 'status', 'total_results', 'started_at', 'finished_at')
    list_filter = ('status',)
    search_fields = ('search_term',)


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'rating', 'reviews_count', 'phone', 'job')
    list_filter = ('category', 'job')
    search_fields = ('name', 'address', 'phone', 'category')


@admin.register(CallerProfile)
class CallerProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'phone', 'daily_quota', 'is_active', 'created_at')
    list_filter = ('role', 'is_active')
    search_fields = ('user__username', 'user__email', 'phone')


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'check_in', 'check_out', 'total_minutes')
    list_filter = ('date',)
    search_fields = ('user__username',)
    date_hierarchy = 'date'


@admin.register(LeadAssignment)
class LeadAssignmentAdmin(admin.ModelAdmin):
    list_display = ('place', 'caller', 'status', 'assigned_at', 'completed_at')
    list_filter = ('status', 'caller')
    search_fields = ('place__name', 'caller__username')
    raw_id_fields = ('place', 'caller', 'assigned_by')


@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display = ('caller', 'place', 'outcome', 'created_at')
    list_filter = ('outcome', 'caller')
    search_fields = ('place__name', 'caller__username', 'notes')
    raw_id_fields = ('place', 'caller', 'assignment')

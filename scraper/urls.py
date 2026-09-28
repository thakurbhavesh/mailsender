from django.urls import path
from . import (
    views, email_views, ai_views, calling_views, import_views, analytics_views,
    extra_views, shift_views, sse_views, report_views,
    email_tracking, sequence_views, calendar_views, sync_api, lead_views,
    social_views,
)

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('start/', views.start_job, name='start_job'),
    path('jobs/<int:job_id>/re-search/', views.re_search, name='re_search'),
    path('jobs/', views.jobs, name='jobs'),
    path('jobs/<int:job_id>/', views.job_detail, name='job_detail'),
    path('jobs/<int:job_id>/status/', views.job_status, name='job_status'),
    path('jobs/<int:job_id>/delete/', views.delete_job, name='delete_job'),
    path('jobs/<int:job_id>/enrich/', views.enrich_job, name='enrich_job'),
    path('enrich-all/', views.enrich_all, name='enrich_all'),
    path('lead/<int:place_id>/update/', views.update_lead, name='update_lead'),
    path('whatsapp/', views.whatsapp_bulk, name='whatsapp_bulk'),
    path('data/', views.data_view, name='data_view'),
    path('leads/', lead_views.lead_management, name='lead_management'),
    path('leads/<int:place_id>/activity/', lead_views.lead_activity, name='lead_activity'),
    path('leads/<int:place_id>/follow-up/', lead_views.set_follow_up, name='set_follow_up'),
    path('leads/bulk/', lead_views.lead_bulk_action, name='lead_bulk_action'),
    # ── Social outreach ──
    path('social/', social_views.social_outreach, name='social_outreach'),
    path('social/<int:place_id>/open/<str:platform>/', social_views.social_open, name='social_open'),
    path('social/<int:place_id>/log/', social_views.social_log, name='social_log'),
    path('social/bulk/', social_views.social_bulk, name='social_bulk'),
    path('social/<int:place_id>/panel/', social_views.social_panel, name='social_panel'),
    path('social/<int:place_id>/status/', social_views.social_set_status, name='social_set_status'),
    path('social/touch/<int:touch_id>/update/', social_views.social_touch_update, name='social_touch_update'),
    path('social/touch/<int:touch_id>/delete/', social_views.social_touch_delete, name='social_touch_delete'),

    path('email/suppressions/', lead_views.suppression_list, name='suppression_list'),
    path('email/suppressions/add/', lead_views.suppression_add, name='suppression_add'),
    path('email/suppressions/<int:supp_id>/remove/', lead_views.suppression_remove, name='suppression_remove'),
    path('data/bulk/', views.bulk_action, name='bulk_action'),
    path('lead/<int:place_id>/', views.lead_detail, name='lead_detail'),
    path('lead/<int:place_id>/edit/', views.lead_update, name='lead_update'),
    path('lead/<int:place_id>/enrich/', views.lead_enrich_now, name='lead_enrich_now'),
    path('quick-add/', views.quick_add, name='quick_add'),
    path('clear-data/', views.clear_data_page, name='clear_data'),
    path('clear-data/confirm/', views.clear_data_confirm, name='clear_data_confirm'),
    path('export/', views.export_config, name='export_config'),
    path('export/excel/', views.export_excel, name='export_excel'),
    path('export/csv/', views.export_csv, name='export_csv'),

    # Email tool
    path('email/', email_views.email_home, name='email_home'),
    path('email/accounts/', email_views.account_list, name='email_accounts'),
    path('email/accounts/new/', email_views.account_form, name='email_account_new'),
    path('email/accounts/<int:account_id>/', email_views.account_form, name='email_account_edit'),
    path('email/accounts/<int:account_id>/delete/', email_views.account_delete, name='email_account_delete'),
    path('email/accounts/<int:account_id>/test/', email_views.account_test, name='email_account_test'),
    path('email/templates/', email_views.template_list, name='email_templates'),
    path('email/templates/new/', email_views.template_form, name='email_template_new'),
    path('email/templates/<int:template_id>/', email_views.template_form, name='email_template_edit'),
    path('email/templates/<int:template_id>/delete/', email_views.template_delete, name='email_template_delete'),
    path('email/templates/<int:template_id>/preview/', email_views.template_preview, name='email_template_preview'),
    path('email/templates/restore-defaults/', email_views.restore_default_templates, name='restore_default_templates'),
    path('email/compose/', email_views.compose, name='compose'),
    path('email/compose/bulk/', email_views.compose_bulk, name='compose_bulk'),
    path('email/send/', email_views.send_single, name='email_send_single'),
    path('email/send/bulk/', email_views.send_bulk, name='email_send_bulk'),
    path('email/history/', email_views.email_history, name='email_history'),
    path('email/history/<int:log_id>/', email_views.log_detail, name='email_log_detail'),

    # AI (Gemini)
    path('ai/', ai_views.ai_home, name='ai_home'),
    path('ai/settings/', ai_views.ai_settings, name='ai_settings'),
    path('ai/writer/', ai_views.ai_writer, name='ai_writer'),
    path('ai/writer/generate/', ai_views.ai_generate_email, name='ai_generate_email'),
    path('ai/subject/', ai_views.ai_suggest_subject, name='ai_suggest_subject'),
    path('ai/reply/', ai_views.ai_reply, name='ai_reply'),
    path('ai/reply/generate/', ai_views.ai_generate_reply, name='ai_generate_reply'),
    path('ai/improve/', ai_views.ai_improve, name='ai_improve'),
    path('ai/logs/', ai_views.ai_logs, name='ai_logs'),
    path('ai/test/', ai_views.ai_test, name='ai_test'),
    path('ai/models-live/', ai_views.ai_models_live, name='ai_models_live'),
    path('ai/enhance-search/', ai_views.ai_enhance_search, name='ai_enhance_search'),

    # Features overview
    path('features/', ai_views.features_page, name='features'),

    # Calling Team — role router (post-login)
    path('route/', calling_views.role_router, name='role_router'),

    # Caller side
    path('calling/', calling_views.caller_dashboard, name='caller_dashboard'),
    path('calling/attendance/', calling_views.caller_attendance, name='caller_attendance'),
    path('calling/attendance/checkin/', calling_views.caller_attendance_checkin, name='caller_attendance_checkin'),
    path('calling/attendance/checkout/', calling_views.caller_attendance_checkout, name='caller_attendance_checkout'),
    path('calling/lead/<int:assignment_id>/update/', calling_views.caller_update_lead, name='caller_update_lead'),
    path('calling/lead/<int:assignment_id>/quick-status/', calling_views.caller_quick_status, name='caller_quick_status'),
    path('calling/lead/<int:assignment_id>/quick-outcome/', calling_views.caller_quick_outcome, name='caller_quick_outcome'),
    path('calling/lead/<int:assignment_id>/save-note/', calling_views.caller_save_note, name='caller_save_note'),
    path('calling/lead/<int:place_id>/view/', calling_views.caller_lead_detail, name='caller_lead_detail'),
    path('calling/history/', calling_views.caller_history, name='caller_history'),

    # Admin: calling team management
    path('admin-panel/calling/', calling_views.admin_monitor, name='admin_monitor'),
    path('admin-panel/calling/members/', calling_views.members_list, name='members_list'),
    path('admin-panel/calling/members/add/', calling_views.member_add, name='member_add'),
    path('admin-panel/calling/members/<int:user_id>/edit/', calling_views.member_edit, name='member_edit'),
    path('admin-panel/calling/members/<int:user_id>/delete/', calling_views.member_delete, name='member_delete'),
    path('admin-panel/calling/members/<int:user_id>/assign/', calling_views.member_assign_now, name='member_assign_now'),
    path('admin-panel/calling/assign/', calling_views.manual_assign, name='manual_assign'),
    path('admin-panel/calling/assign/submit/', calling_views.manual_assign_submit, name='manual_assign_submit'),
    path('admin-panel/calling/members/<int:user_id>/reset/', calling_views.member_reset_pending, name='member_reset_pending'),
    path('admin-panel/calling/members/<int:user_id>/detail/', calling_views.admin_caller_detail, name='admin_caller_detail'),
    path('admin-panel/calling/members/<int:user_id>/attendance/', calling_views.admin_caller_attendance, name='admin_caller_attendance'),

    # ============ Lead Import (CSV / Excel) ============
    path('admin-panel/import/', import_views.import_home, name='import_home'),
    path('admin-panel/import/upload/', import_views.import_upload, name='import_upload'),
    path('admin-panel/import/<int:job_id>/map/', import_views.import_map, name='import_map'),
    path('admin-panel/import/<int:job_id>/run/', import_views.import_run, name='import_run'),
    path('admin-panel/import/<int:job_id>/delete/', import_views.import_delete, name='import_delete'),
    path('admin-panel/import/template/', import_views.import_template, name='import_template'),

    # ============ Analytics ============
    path('admin-panel/analytics/leaderboard/', analytics_views.leaderboard, name='analytics_leaderboard'),
    path('admin-panel/analytics/funnel/', analytics_views.funnel, name='analytics_funnel'),
    path('admin-panel/analytics/heatmap/', analytics_views.heatmap, name='analytics_heatmap'),
    path('admin-panel/analytics/goals/', analytics_views.goals_list, name='goals_list'),
    path('admin-panel/analytics/goals/<int:user_id>/', analytics_views.goal_edit, name='goal_edit'),
    path('admin-panel/analytics/incentives/', analytics_views.incentives, name='incentives'),

    # ============ Break Tracking ============
    path('calling/break/start/', extra_views.break_start, name='break_start'),
    path('calling/break/end/', extra_views.break_end, name='break_end'),

    # ============ Notifications ============
    path('notifications/', extra_views.notifications_list, name='notifications_list'),
    path('notifications/api/', extra_views.notifications_api, name='notifications_api'),
    path('notifications/mark-read/', extra_views.notifications_mark_read, name='notifications_mark_read'),
    path('mentions/users/', extra_views.mention_users_api, name='mention_users_api'),

    # ============ Lead Deduplication ============
    path('admin-panel/dedup/', extra_views.dedup_home, name='dedup_home'),
    path('admin-panel/dedup/merge/', extra_views.dedup_merge, name='dedup_merge'),
    path('admin-panel/dedup/delete/', extra_views.dedup_delete, name='dedup_delete'),

    # ============ Shift Management ============
    path('admin-panel/shifts/', shift_views.shifts_list, name='shifts_list'),
    path('admin-panel/shifts/new/', shift_views.shift_form, name='shift_new'),
    path('admin-panel/shifts/<int:shift_id>/edit/', shift_views.shift_form, name='shift_edit'),
    path('admin-panel/shifts/<int:shift_id>/delete/', shift_views.shift_delete, name='shift_delete'),
    path('admin-panel/shifts/<int:shift_id>/assign/', shift_views.shift_assign, name='shift_assign'),
    path('admin-panel/shifts/unassign/<int:assignment_id>/', shift_views.shift_unassign, name='shift_unassign'),

    # ============ Team Hierarchy ============
    path('admin-panel/team/', shift_views.team_hierarchy, name='team_hierarchy'),
    path('admin-panel/team/set-manager/<int:user_id>/', shift_views.set_manager, name='set_manager'),

    # ============ SSE Notifications ============
    path('notifications/stream/', sse_views.notif_stream, name='notif_stream'),

    # ============ Custom Reports ============
    path('admin-panel/reports/', report_views.reports_list, name='reports_list'),
    path('admin-panel/reports/new/', report_views.report_builder, name='report_new'),
    path('admin-panel/reports/<int:report_id>/edit/', report_views.report_builder, name='report_edit'),
    path('admin-panel/reports/<int:report_id>/run/', report_views.report_run, name='report_run'),
    path('admin-panel/reports/<int:report_id>/delete/', report_views.report_delete, name='report_delete'),
    path('admin-panel/reports/<int:report_id>/csv/', report_views.report_export_csv, name='report_export_csv'),
    path('admin-panel/reports/preview/', report_views.report_preview, name='report_preview'),

    # ============ Email Tracking ============
    path('t/o/<str:token>.gif', email_tracking.open_pixel, name='track_open'),
    path('t/c/<str:token>/', email_tracking.click_redirect, name='track_click'),
    path('u/<str:token>/', email_tracking.unsubscribe, name='unsubscribe'),
    path('email/<int:log_id>/tracking/', email_tracking.tracking_stats, name='tracking_stats'),

    # ============ Email Sequences ============
    path('admin-panel/sequences/', sequence_views.sequences_list, name='sequences_list'),
    path('admin-panel/sequences/new/', sequence_views.sequence_form, name='sequence_new'),
    path('admin-panel/sequences/<int:seq_id>/edit/', sequence_views.sequence_form, name='sequence_edit'),
    path('admin-panel/sequences/<int:seq_id>/delete/', sequence_views.sequence_delete, name='sequence_delete'),
    path('admin-panel/sequences/<int:seq_id>/step/add/', sequence_views.step_add, name='step_add'),
    path('admin-panel/sequences/step/<int:step_id>/delete/', sequence_views.step_delete, name='step_delete'),
    path('admin-panel/sequences/<int:seq_id>/enroll/', sequence_views.sequence_enroll, name='sequence_enroll'),
    path('admin-panel/sequences/enrollment/<int:enrollment_id>/action/', sequence_views.enrollment_action, name='enrollment_action'),
    path('admin-panel/sequences/run-due/', sequence_views.run_due_steps, name='run_due_steps'),

    # ============ Calendar Booking ============
    path('calendar/schedule/', calendar_views.my_schedule, name='my_schedule'),
    path('calendar/meetings/', calendar_views.meetings_list, name='meetings_list'),
    path('calendar/meetings/<int:meeting_id>/status/', calendar_views.meeting_status, name='meeting_status'),
    path('meet/<slug:slug>/', calendar_views.public_booking, name='public_booking'),
    path('meet/<slug:slug>/book/', calendar_views.public_book_submit, name='public_book_submit'),
    path('meet/<slug:slug>/<str:token>/cancel/', calendar_views.public_cancel, name='public_cancel'),

    # ============ Sync API (Local → Production push) ============
    path('api/sync/ping/', sync_api.sync_ping, name='sync_ping'),
    path('api/sync/leads/', sync_api.sync_leads, name='sync_leads'),
]

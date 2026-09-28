"""Tests for the parts that quietly cost money or reputation when they break:
lead engagement rollups, suppression, and the unsubscribe flow.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .email_service import append_unsub_footer, deliver
from .lead_views import _annotate_engagement, _apply_work_filter, build_timeline
from .models import (CallLog, EmailAccount, EmailLog, EmailSequence, EmailTemplate,
                     Place, ScrapeJob, SequenceEnrollment, SocialTouch, Suppression)


def make_place(name='Acme Ltd', email='a@example.com', **kw):
    job = ScrapeJob.objects.first() or ScrapeJob.objects.create(search_term='t')
    return Place.objects.create(
        job=job, name=name, email=email,
        unique_key=kw.pop('unique_key', name.lower().replace(' ', '-')), **kw)


class SuppressionTests(TestCase):
    def test_blocks_is_case_insensitive(self):
        Suppression.add('Person@Example.COM')
        self.assertTrue(Suppression.blocks('person@example.com'))
        self.assertTrue(Suppression.blocks('PERSON@EXAMPLE.COM'))

    def test_blocks_is_false_for_unknown_and_blank(self):
        self.assertFalse(Suppression.blocks('nobody@example.com'))
        self.assertFalse(Suppression.blocks(''))
        self.assertFalse(Suppression.blocks(None))

    def test_add_is_idempotent(self):
        Suppression.add('dup@example.com')
        Suppression.add('dup@example.com', reason='bounced')
        self.assertEqual(Suppression.objects.filter(email='dup@example.com').count(), 1)


class DeliverTests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(
            name='Test', email='sender@example.com', app_password='x' * 16,
            daily_limit=10, is_active=True)
        self.template = EmailTemplate.objects.create(
            name='T', subject='Hello {{name}}', body_html='<p>Hi</p>')
        self.place = make_place()

    @patch('scraper.email_service._send_via_smtp', return_value=(True, '', False))
    def test_send_records_and_consumes_quota(self, smtp):
        log = deliver(self.account, self.place, 'Subject', '<p>Body</p>', self.template)
        self.assertEqual(log.status, 'sent')
        self.account.refresh_from_db()
        self.assertEqual(self.account.sent_today, 1)

    @patch('scraper.email_service._send_via_smtp')
    def test_suppressed_address_is_never_sent_to(self, smtp):
        Suppression.add(self.place.email)
        log = deliver(self.account, self.place, 'Subject', '<p>Body</p>', self.template)
        self.assertEqual(log.status, 'failed')
        self.assertIn('Suppressed', log.error_message)
        smtp.assert_not_called()

    @patch('scraper.email_service._send_via_smtp',
           return_value=(False, '550 no such user', True))
    def test_hard_bounce_suppresses_the_address(self, smtp):
        log = deliver(self.account, self.place, 'Subject', '<p>Body</p>', self.template)
        self.assertTrue(log.bounced)
        self.assertIsNotNone(log.bounced_at)
        self.assertTrue(Suppression.blocks(self.place.email))

    @patch('scraper.email_service._send_via_smtp',
           return_value=(False, 'temporary failure', False))
    def test_soft_failure_does_not_suppress(self, smtp):
        log = deliver(self.account, self.place, 'Subject', '<p>Body</p>', self.template)
        self.assertEqual(log.status, 'failed')
        self.assertFalse(log.bounced)
        self.assertFalse(Suppression.blocks(self.place.email))

    @patch('scraper.email_service._send_via_smtp', return_value=(True, '', False))
    def test_unsubscribe_header_is_sent(self, smtp):
        deliver(self.account, self.place, 'Subject', '<p>Body</p>', self.template)
        self.assertIn('unsub_url', smtp.call_args.kwargs)
        self.assertIn('/u/', smtp.call_args.kwargs['unsub_url'])

    def test_footer_is_added_once(self):
        once = append_unsub_footer('<p>hi</p>', 'https://x.test/u/abc/')
        self.assertIn('/u/abc/', once)
        self.assertEqual(append_unsub_footer(once, 'https://x.test/u/abc/').count('Unsubscribe'), 1)


class UnsubscribeViewTests(TestCase):
    def setUp(self):
        self.place = make_place(email='reader@example.com')
        self.log = EmailLog.objects.create(
            place=self.place, to_email=self.place.email, subject='Hi',
            body_html='<p>x</p>', status='sent')
        self.token = self.log.ensure_token()

    def test_get_asks_before_acting(self):
        r = self.client.get('/u/%s/' % self.token)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Suppression.blocks(self.place.email))

    def test_post_unsubscribes(self):
        r = self.client.post('/u/%s/' % self.token)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Suppression.blocks(self.place.email))
        self.log.refresh_from_db()
        self.assertIsNotNone(self.log.unsubscribed_at)

    def test_post_stops_an_active_sequence(self):
        seq = EmailSequence.objects.create(name='Drip')
        enr = SequenceEnrollment.objects.create(
            sequence=seq, place=self.place, status='active',
            next_send_at=timezone.now())
        self.client.post('/u/%s/' % self.token)
        enr.refresh_from_db()
        self.assertEqual(enr.status, 'stopped')

    def test_unknown_token_is_404(self):
        self.assertEqual(self.client.get('/u/nope/').status_code, 404)

    def test_page_is_public(self):
        """The recipient is never logged in, so the middleware must let it through."""
        self.assertEqual(self.client.get('/u/%s/' % self.token).status_code, 200)


class EngagementAnnotationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('caller', password='x')
        self.untouched = make_place('Untouched', 'u@example.com', unique_key='u')
        self.mailed = make_place('Mailed', 'm@example.com', unique_key='m')
        self.opened = make_place('Opened', 'o@example.com', unique_key='o')
        now = timezone.now()
        EmailLog.objects.create(place=self.mailed, to_email='m@example.com',
                                subject='s', body_html='b', status='sent', sent_at=now)
        EmailLog.objects.create(place=self.opened, to_email='o@example.com',
                                subject='s', body_html='b', status='sent', sent_at=now,
                                opened_at=now, last_opened_at=now, opened_count=3,
                                clicked_count=1)

    def qs(self):
        return _annotate_engagement(Place.objects.all())

    def test_counts_only_sent_mail(self):
        EmailLog.objects.create(place=self.untouched, to_email='u@example.com',
                                subject='s', body_html='b', status='failed')
        row = self.qs().get(pk=self.untouched.pk)
        self.assertEqual(row.email_count, 0)

    def test_open_and_click_rollups(self):
        row = self.qs().get(pk=self.opened.pk)
        self.assertEqual(row.email_count, 1)
        self.assertEqual(row.total_opens, 3)
        self.assertEqual(row.opened_mails, 1)
        self.assertEqual(row.total_clicks, 1)

    def test_work_filters_partition_the_leads(self):
        names = lambda w: set(_apply_work_filter(self.qs(), w).values_list('name', flat=True))
        self.assertEqual(names('untouched'), {'Untouched'})
        self.assertEqual(names('emailed'), {'Mailed', 'Opened'})
        self.assertEqual(names('opened'), {'Opened'})
        self.assertEqual(names('not_opened'), {'Mailed'})
        self.assertEqual(names('clicked'), {'Opened'})

    def test_call_activity_counts_as_worked(self):
        CallLog.objects.create(place=self.untouched, caller=self.user, outcome='answered')
        self.assertNotIn('Untouched',
                         _apply_work_filter(self.qs(), 'untouched').values_list('name', flat=True))
        self.assertIn('Untouched',
                      _apply_work_filter(self.qs(), 'worked').values_list('name', flat=True))


class TimelineTests(TestCase):
    def test_events_are_newest_first_and_include_every_touch(self):
        place = make_place('Timeline Co', 't@example.com', unique_key='t')
        now = timezone.now()
        EmailLog.objects.create(
            place=place, to_email='t@example.com', subject='Hi', body_html='b',
            status='sent', sent_at=now - timedelta(days=2),
            opened_at=now - timedelta(days=1), last_opened_at=now - timedelta(hours=1),
            opened_count=2,
            clicks=[{'url': 'https://example.com', 'at': (now - timedelta(hours=2)).isoformat()}])
        CallLog.objects.create(place=place, caller=User.objects.create_user('c2', password='x'),
                               outcome='answered')

        events = build_timeline(place)
        kinds = [e['kind'] for e in events]
        for expected in ('sent', 'open', 'click', 'call', 'created'):
            self.assertIn(expected, kinds)
        times = [e['at'] for e in events]
        self.assertEqual(times, sorted(times, reverse=True))


class ScrapingFlagTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('boss', 'b@example.com', 'x')
        self.client.force_login(self.admin)

    def test_start_is_refused_when_scraping_is_off(self):
        with self.settings(SCRAPING_ENABLED=False):
            r = self.client.post('/start/', {'search_term': 'cafes'}, follow=True)
            self.assertContains(r, 'Scraping is disabled on this server')
            self.assertEqual(ScrapeJob.objects.filter(search_term='cafes').count(), 0)

    def test_dashboard_hides_the_scrape_panel_when_off(self):
        with self.settings(SCRAPING_ENABLED=False):
            r = self.client.get('/')
            self.assertNotContains(r, 'Start a new scrape')


class DashboardAggregateTests(TestCase):
    """The dashboard counts everything in a few grouped queries now, so guard
    against those totals drifting from the plain per-field counts."""

    def setUp(self):
        self.admin = User.objects.create_superuser('chief', 'c@example.com', 'x')
        self.client.force_login(self.admin)
        make_place('Full', 'f@example.com', unique_key='f',
                   phone='123', website='https://f.test', lead_status='converted')
        make_place('NoEmail', '', unique_key='n', phone='456')
        make_place('Bare', '', unique_key='b')

    def test_totals_match_individual_counts(self):
        ctx = self.client.get('/').context
        self.assertEqual(ctx['total_places'], Place.objects.count())
        self.assertEqual(ctx['with_email'], Place.objects.exclude(email='').count())
        self.assertEqual(ctx['with_phone'], Place.objects.exclude(phone='').count())
        self.assertEqual(ctx['with_website'], Place.objects.exclude(website='').count())
        self.assertEqual(ctx['converted'],
                         Place.objects.filter(lead_status='converted').count())

    def test_pipeline_matches_per_status_counts(self):
        ctx = self.client.get('/').context
        for value, _label in Place.LEAD_STATUS_CHOICES:
            self.assertEqual(
                ctx['pipeline'][value]['count'],
                Place.objects.filter(lead_status=value).count(),
                msg='pipeline count drifted for %s' % value)

    def test_platform_lead_counts_match(self):
        ctx = self.client.get('/').context
        for row in ctx['platforms_used']:
            self.assertEqual(row['leads'],
                             Place.objects.filter(source=row['key']).count())

    def test_dashboard_stays_within_a_query_budget(self):
        """It was 36 queries; keep it from creeping back."""
        with CaptureQueriesContext(connection) as captured:
            self.client.get('/')
        self.assertLess(len(captured), 25,
                        'dashboard query count regressed to %d' % len(captured))


class FollowUpTests(TestCase):
    """Follow-up dates decide what shows in the daily queue, and the day
    boundary must be the local one — Asia/Kolkata is 5.5 hours off UTC."""

    def setUp(self):
        self.admin = User.objects.create_superuser('mgr', 'm@example.com', 'x')
        self.client.force_login(self.admin)
        self.lead = make_place('Follow Co', 'f@example.com', unique_key='fc')

    def test_preset_sets_a_future_date(self):
        self.client.post('/leads/%d/follow-up/' % self.lead.pk,
                         {'preset': '3d', 'note': 'Demo call'})
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.next_follow_up)
        self.assertEqual(self.lead.follow_up_note, 'Demo call')
        self.assertEqual(self.lead.follow_up_state, 'upcoming')

    def test_clear_removes_it(self):
        self.lead.next_follow_up = timezone.now() + timedelta(days=1)
        self.lead.save()
        self.client.post('/leads/%d/follow-up/' % self.lead.pk, {'clear': '1'})
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.next_follow_up)

    def test_state_reflects_the_clock(self):
        """Pinned to a fixed moment; otherwise the result depends on what
        time the suite happens to run."""
        tz = timezone.get_current_timezone()
        noon = datetime(2026, 6, 15, 12, 0, tzinfo=tz)

        with patch('django.utils.timezone.now', return_value=noon):
            self.lead.next_follow_up = noon - timedelta(hours=3)
            self.assertEqual(self.lead.follow_up_state, 'overdue')

            self.lead.next_follow_up = noon + timedelta(hours=6)   # same local day
            self.assertEqual(self.lead.follow_up_state, 'today')

            self.lead.next_follow_up = noon + timedelta(days=5)
            self.assertEqual(self.lead.follow_up_state, 'upcoming')

    def test_today_filter_uses_the_local_day(self):
        """23:30 local is still today, even though in UTC it is tomorrow."""
        tz = timezone.get_current_timezone()
        late_today = datetime(2026, 6, 15, 23, 30, tzinfo=tz)
        self.lead.next_follow_up = late_today
        self.lead.save()

        with patch('django.utils.timezone.now',
                   return_value=datetime(2026, 6, 15, 9, 0, tzinfo=tz)):
            due = _apply_work_filter(_annotate_engagement(Place.objects.all()), 'today')
            self.assertIn(self.lead.pk, [p.pk for p in due])

    def test_due_covers_overdue_and_today(self):
        overdue = make_place('Overdue Co', 'o2@example.com', unique_key='oc')
        overdue.next_follow_up = timezone.now() - timedelta(days=1)
        overdue.save()
        self.lead.next_follow_up = timezone.now() + timedelta(days=10)
        self.lead.save()
        names = set(_apply_work_filter(
            _annotate_engagement(Place.objects.all()), 'due'
        ).values_list('name', flat=True))
        self.assertIn('Overdue Co', names)
        self.assertNotIn('Follow Co', names)


class BulkActionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('boss2', 'b2@example.com', 'x')
        self.client.force_login(self.admin)
        self.a = make_place('Alpha', 'alpha@example.com', unique_key='al')
        self.b = make_place('Beta', 'beta@example.com', unique_key='be')

    def test_bulk_status(self):
        self.client.post('/leads/bulk/',
                         {'ids': [self.a.pk, self.b.pk], 'action': 'status:interested'})
        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual(self.a.lead_status, 'interested')
        self.assertEqual(self.b.lead_status, 'interested')

    def test_bulk_follow_up_and_clear(self):
        self.client.post('/leads/bulk/', {'ids': [self.a.pk], 'action': 'followup:1w'})
        self.a.refresh_from_db()
        self.assertEqual(self.a.follow_up_state, 'upcoming')
        self.client.post('/leads/bulk/', {'ids': [self.a.pk], 'action': 'clear_followup'})
        self.a.refresh_from_db()
        self.assertIsNone(self.a.next_follow_up)

    def test_bulk_suppress_also_stops_sequences(self):
        seq = EmailSequence.objects.create(name='Drip')
        enr = SequenceEnrollment.objects.create(
            sequence=seq, place=self.a, status='active', next_send_at=timezone.now())
        self.client.post('/leads/bulk/', {'ids': [self.a.pk], 'action': 'suppress'})
        enr.refresh_from_db()
        self.assertTrue(Suppression.blocks(self.a.email))
        self.assertEqual(enr.status, 'stopped')

    def test_no_selection_is_rejected(self):
        r = self.client.post('/leads/bulk/', {'action': 'status:interested'}, follow=True)
        self.assertContains(r, 'select')


class SocialOutreachTests(TestCase):
    """Social has no open or click tracking, so every action has to be
    recorded deliberately — and recorded once."""

    def setUp(self):
        self.user = User.objects.create_superuser('social', 's@example.com', 'x')
        self.client.force_login(self.user)
        self.lead = make_place('Social Co', 's@example.com', unique_key='sc')
        self.lead.facebook = 'https://facebook.com/socialco'
        self.lead.linkedin = 'https://linkedin.com/company/socialco'
        self.lead.save()
        self.bare = make_place('No Social', 'n@example.com', unique_key='ns')

    def test_profiles_helper_lists_only_what_exists(self):
        keys = [k for k, _label, _url in self.lead.social_profiles]
        self.assertEqual(keys, ['facebook', 'linkedin'])
        self.assertEqual(self.lead.social_count, 2)
        self.assertEqual(self.bare.social_profiles, [])

    def test_opening_a_profile_logs_it_and_redirects(self):
        r = self.client.get('/social/%d/open/facebook/' % self.lead.pk)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, 'https://facebook.com/socialco')
        self.assertEqual(
            SocialTouch.objects.filter(place=self.lead, action='visited').count(), 1)

    def test_repeat_visits_the_same_day_are_not_double_logged(self):
        self.client.get('/social/%d/open/facebook/' % self.lead.pk)
        self.client.get('/social/%d/open/facebook/' % self.lead.pk)
        self.assertEqual(
            SocialTouch.objects.filter(place=self.lead, action='visited').count(), 1)

    def test_opening_a_missing_profile_does_not_log(self):
        r = self.client.get('/social/%d/open/instagram/' % self.lead.pk, follow=True)
        self.assertEqual(SocialTouch.objects.filter(place=self.lead).count(), 0)
        self.assertContains(r, 'No Instagram profile')

    def test_messaging_moves_a_new_lead_to_contacted(self):
        self.client.post('/social/%d/log/' % self.lead.pk,
                         {'platform': 'linkedin', 'action': 'messaged'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.lead_status, 'contacted')

    def test_a_reply_moves_the_lead_to_interested(self):
        self.client.post('/social/%d/log/' % self.lead.pk,
                         {'platform': 'facebook', 'action': 'replied'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.lead_status, 'interested')

    def test_a_reply_does_not_demote_a_converted_lead(self):
        self.lead.lead_status = 'converted'
        self.lead.save()
        self.client.post('/social/%d/log/' % self.lead.pk,
                         {'platform': 'facebook', 'action': 'replied'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.lead_status, 'converted')

    def test_bulk_logs_one_row_per_lead(self):
        other = make_place('Second Co', 'x@example.com', unique_key='s2')
        self.client.post('/social/bulk/', {
            'ids': [self.lead.pk, other.pk],
            'platform': 'instagram', 'action': 'messaged'})
        self.assertEqual(SocialTouch.objects.filter(action='messaged').count(), 2)

    def test_state_filters_partition_the_list(self):
        self.client.get('/social/%d/open/facebook/' % self.lead.pk)
        names = lambda st: {p.name for p in
                            self.client.get('/social/?state=%s' % st).context['page']}
        self.assertIn('Social Co', names('has_social'))
        self.assertIn('No Social', names('no_social'))
        self.assertIn('Social Co', names('visited'))
        self.assertNotIn('Social Co', names('messaged'))

    def test_touches_reach_the_lead_timeline(self):
        self.client.post('/social/%d/log/' % self.lead.pk,
                         {'platform': 'linkedin', 'action': 'messaged'})
        kinds = [e['kind'] for e in build_timeline(self.lead)]
        self.assertIn('social', kinds)

    def test_a_bad_platform_is_rejected(self):
        self.client.post('/social/%d/log/' % self.lead.pk,
                         {'platform': 'myspace', 'action': 'messaged'})
        self.assertEqual(SocialTouch.objects.count(), 0)

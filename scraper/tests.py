"""Tests for the parts that quietly cost money or reputation when they break:
lead engagement rollups, suppression, and the unsubscribe flow.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .email_service import append_unsub_footer, deliver
from .lead_views import _annotate_engagement, _apply_work_filter, build_timeline
from .models import (CallLog, EmailAccount, EmailLog, EmailSequence, EmailTemplate,
                     Place, ScrapeJob, SequenceEnrollment, Suppression)


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

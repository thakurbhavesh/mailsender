"""Send drip-sequence steps that have come due.

The dashboard has a manual "run due steps" button, which is fine while
developing but means nothing ever sends on a server. Run this from a timer
instead:

    python manage.py send_due_steps

    python manage.py send_due_steps --limit 100
    python manage.py send_due_steps --dry-run     # list what would go out

A run takes a lock file, so a slow batch cannot overlap the next tick and
send the same step twice.
"""
import os
import sys
import tempfile
from contextlib import contextmanager

from django.core.management.base import BaseCommand
from django.utils import timezone

from scraper.models import SequenceEnrollment

LOCK_PATH = os.path.join(tempfile.gettempdir(), 'leadhunt-send-due-steps.lock')
STALE_LOCK_SECONDS = 30 * 60


@contextmanager
def single_run():
    """Refuse to start if another run holds the lock and is not stale."""
    if os.path.exists(LOCK_PATH):
        age = timezone.now().timestamp() - os.path.getmtime(LOCK_PATH)
        if age < STALE_LOCK_SECONDS:
            yield False
            return
        os.unlink(LOCK_PATH)  # previous run died; take over

    with open(LOCK_PATH, 'w') as f:
        f.write(str(os.getpid()))
    try:
        yield True
    finally:
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass


class Command(BaseCommand):
    help = 'Send drip-sequence steps whose send time has passed.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100,
                            help='Maximum enrollments to process in one run (default 100).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what is due without sending anything.')

    def handle(self, *args, **opts):
        if sys.platform.startswith('win'):
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except Exception:
                pass

        now = timezone.now()
        limit = opts['limit']

        if opts['dry_run']:
            due = (SequenceEnrollment.objects
                   .filter(status='active', next_send_at__lte=now)
                   .select_related('sequence', 'place')[:limit])
            self.stdout.write('%d enrollment(s) due at %s' % (len(due), now.strftime('%Y-%m-%d %H:%M')))
            for e in due:
                self.stdout.write('  - %s -> step %d of "%s"' % (
                    e.place.name[:44], e.current_step_order + 1, e.sequence.name))
            return

        with single_run() as acquired:
            if not acquired:
                self.stdout.write('Another run is still going; skipping this tick.')
                return

            from scraper.sequence_views import _process_due_enrollments
            sent, skipped, errors = _process_due_enrollments(limit=limit)

            line = 'sent=%d skipped=%d errors=%d' % (sent, skipped, errors)
            if errors:
                self.stderr.write(line)
            else:
                self.stdout.write(line)

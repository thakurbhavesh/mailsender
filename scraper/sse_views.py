"""Server-Sent Events for real-time notifications."""
import json
import time
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.db import close_old_connections

from .models import Notification


def _humanize(delta):
    s = int(delta.total_seconds())
    if s < 60: return f'{s}s ago'
    if s < 3600: return f'{s // 60}m ago'
    if s < 86400: return f'{s // 3600}h ago'
    return f'{s // 86400}d ago'


def notif_stream(request):
    """
    Long-lived SSE endpoint streaming new notifications to the user.
    Falls back gracefully when client disconnects.
    """
    if not request.user.is_authenticated:
        return StreamingHttpResponse('event: error\ndata: not authenticated\n\n',
                                     content_type='text/event-stream', status=401)

    user_id = request.user.id

    def event_stream():
        # Get last seen id from query param or default to current max
        try:
            last_id = int(request.GET.get('last_id', 0))
        except ValueError:
            last_id = 0

        # Send initial unread count
        unread = Notification.objects.filter(user_id=user_id, is_read=False).count()
        yield f'event: init\ndata: {json.dumps({"unread": unread})}\n\n'

        max_iterations = 600  # ~10 minutes max per connection (then client reconnects)
        for _ in range(max_iterations):
            close_old_connections()
            new = list(
                Notification.objects
                .filter(user_id=user_id, id__gt=last_id)
                .order_by('id')[:20]
            )
            for n in new:
                last_id = n.id
                payload = {
                    'id': n.id,
                    'kind': n.kind,
                    'title': n.title,
                    'body': (n.body or '')[:140],
                    'url': n.url,
                    'is_read': n.is_read,
                    'ago': _humanize(timezone.now() - n.created_at),
                }
                yield f'event: notification\ndata: {json.dumps(payload)}\n\n'

            # Heartbeat to keep connection alive
            yield f': heartbeat {time.time():.0f}\n\n'
            time.sleep(3)

        # Tell client to reconnect after 10 minutes
        yield 'event: reconnect\ndata: bye\n\n'

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'  # disable nginx buffering
    return response

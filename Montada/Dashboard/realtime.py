import json
import logging
from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .serializers import UserNotificationSerializer


logger = logging.getLogger(__name__)

# Each UserNotification row belongs to exactly one user and is addressed by
# its own id (mark-as-read is scoped to `id` + `user=request.user`), so a
# broadcast-to-many event can't collapse into a single shared-group send —
# every recipient's row has a different id, and handing every client the
# same id would let them mark-read a notification that isn't theirs. What we
# *can* do is stop sending them one at a time on the caller's thread: this
# pool fans the per-user group_send calls out concurrently instead, the same
# way firebase.py backgrounds FCM sends.
_BROADCAST_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dashboard-notify-broadcast")


def get_notification_group_name(user_id):
    return f"dashboard_notifications_{user_id}"


def serialize_notification(notification):
    return json.loads(json.dumps(UserNotificationSerializer(notification).data, default=str))


def broadcast_notification(notification, *, event_name="created"):
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.warning("Dashboard notification broadcast skipped: no channel layer configured.")
            return

        async_to_sync(channel_layer.group_send)(
            get_notification_group_name(notification.user_id),
            {
                "type": "dashboard.notification",
                "event": event_name,
                "notification": serialize_notification(notification),
            },
        )
    except Exception:
        logger.exception(
            "Dashboard notification broadcast failed for notification_id=%s",
            getattr(notification, "id", None),
        )


def broadcast_notifications(notifications, *, event_name="created"):
    """
    Broadcast a batch of notifications to each recipient's own WS group.

    Used both for small batches (a handful of followers notified about one
    signal) and for broadcast-to-all-active-users events (see
    poll_signal_change_notifications._notify_all_users), where this can be
    thousands of rows. Each send is an independent blocking Redis round-trip
    (async_to_sync group_send); running them concurrently instead of in a
    plain for-loop turns O(n) sequential round-trips into O(n / worker_count)
    wall-clock time without changing what gets sent to whom.
    """
    notifications = list(notifications)
    if not notifications:
        return
    if len(notifications) == 1:
        broadcast_notification(notifications[0], event_name=event_name)
        return
    futures = [
        _BROADCAST_EXECUTOR.submit(broadcast_notification, notification, event_name=event_name)
        for notification in notifications
    ]
    for future in futures:
        # broadcast_notification already catches and logs its own exceptions,
        # so this just makes sure we don't move on before every send lands.
        future.result()

import json
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .serializers import UserNotificationSerializer


logger = logging.getLogger(__name__)


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
    for notification in notifications:
        broadcast_notification(notification, event_name=event_name)

from django.db import transaction

# Rows per INSERT transaction. Broadcasts create one row per user; inserting them all in a
# single transaction keeps every recipient's newest rows locked until it commits, so the
# notification list API (which reads exactly those rows) blocks for the whole insert.
NOTIFICATION_INSERT_CHUNK_SIZE = 500


def bulk_create_user_notifications(notifications, chunk_size=NOTIFICATION_INSERT_CHUNK_SIZE):
    """Insert UserNotification rows in short, separately committed chunks."""
    from Mainapp.models import UserNotification

    notifications = list(notifications)
    for start in range(0, len(notifications), chunk_size):
        with transaction.atomic():
            UserNotification.objects.bulk_create(notifications[start:start + chunk_size])
    return notifications

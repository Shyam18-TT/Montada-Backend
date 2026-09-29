import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from News.models import LiveNews
from Mainapp.models import UserNotification

logger = logging.getLogger(__name__)

# Delete in chunks rather than one giant DELETE, so a table that has grown
# unbounded doesn't hold a long-running lock. Each iteration re-queries the
# oldest N ids still past the cutoff and deletes just those.
_BATCH_SIZE = 5000


def _batched_delete(queryset, batch_size=_BATCH_SIZE):
    """Delete queryset rows in fixed-size batches by pk. Returns total deleted."""
    total = 0
    while True:
        ids = list(queryset.order_by("pk").values_list("pk", flat=True)[:batch_size])
        if not ids:
            break
        deleted, _ = queryset.model.objects.filter(pk__in=ids).delete()
        total += deleted
        if len(ids) < batch_size:
            break
    return total


class Command(BaseCommand):
    help = "Delete old notifications and news, in batches to avoid long table locks."

    def handle(self, *args, **options):
        now = timezone.now()

        # Keep read notifications for 30 days, unread ones for 90 — unread rows
        # skip the short cutoff so a user who hasn't opened the app doesn't lose
        # notifications they've never seen, but nothing is retained forever.
        read_cutoff = now - timedelta(days=30)
        unread_cutoff = now - timedelta(days=90)

        # Keep news for 90 days
        news_cutoff = now - timedelta(days=90)

        deleted_read = _batched_delete(
            UserNotification.objects.filter(created_at__lt=read_cutoff, is_read=True)
        )
        deleted_unread = _batched_delete(
            UserNotification.objects.filter(created_at__lt=unread_cutoff, is_read=False)
        )
        deleted_news = _batched_delete(
            LiveNews.objects.filter(created_at__lt=news_cutoff)
        )

        deleted_notifications = deleted_read + deleted_unread
        message = (
            f"Deleted {deleted_notifications} notifications "
            f"({deleted_read} read, {deleted_unread} unread) "
            f"and {deleted_news} news records."
        )
        logger.info(message)
        self.stdout.write(self.style.SUCCESS(message))
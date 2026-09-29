from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from News.models import LiveNews
from Mainapp.models import UserNotification


class Command(BaseCommand):
    help = "Delete old notifications and news"

    def handle(self, *args, **options):
        now = timezone.now()

        # Keep notifications for 30 days
        notification_cutoff = now - timedelta(days=30)

        # Keep news for 90 days
        news_cutoff = now - timedelta(days=90)

        deleted_notifications, _ = UserNotification.objects.filter(
            created_at__lt=notification_cutoff,
            is_read = True
        ).delete()

        deleted_news, _ = LiveNews.objects.filter(
            created_at__lt=news_cutoff
        ).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_notifications} notifications "
                f"and {deleted_news} news records."
            )
        )
"""
Housekeeping for push devices. Run daily from the server's scheduler (cron / systemd timer),
like the other management commands:

    python manage.py cleanup_push_devices

- deletes devices not seen (save-fcm-token call) for 60 days: the app re-registers on every
  launch, and Firebase rejects tokens after ~270 days of inactivity
- deletes devices of inactive or soft-deleted users
- deletes PushDelivery rows older than 30 days
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from Mainapp.models import DeviceToken, PushDelivery

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete stale push devices, devices of disabled users and old delivery-log rows."

    def add_arguments(self, parser):
        parser.add_argument("--stale-days", type=int, default=60, help="Delete devices not seen for this many days.")
        parser.add_argument("--log-days", type=int, default=30, help="Keep PushDelivery rows this many days.")
        parser.add_argument("--dry-run", action="store_true", help="Only print the counts.")

    def handle(self, *args, **options):
        now = timezone.now()
        stale = DeviceToken.objects.filter(last_seen_at__lt=now - timedelta(days=options["stale_days"]))
        disabled = DeviceToken.objects.filter(Q(user__is_active=False) | Q(user__is_soft_deleted=True))
        old_log = PushDelivery.objects.filter(created_at__lt=now - timedelta(days=options["log_days"]))

        counts = {
            "stale_devices": stale.count(),
            "disabled_user_devices": disabled.count(),
            "old_delivery_rows": old_log.count(),
        }
        if not options["dry_run"]:
            stale.delete()
            disabled.delete()
            old_log.delete()

        message = "cleanup_push_devices %s: %s" % ("(dry run)" if options["dry_run"] else "done", counts)
        logger.info(message)
        self.stdout.write(message)

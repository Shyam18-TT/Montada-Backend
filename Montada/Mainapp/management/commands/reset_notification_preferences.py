"""
Reset every user's push notification preferences to the default (all categories on, with sound).

The default lives in Mainapp.notification_preferences.DEFAULT_MODE and a missing
NotificationPreference row means "default", so resetting is just deleting rows. Run it once
after changing DEFAULT_MODE so users who had picked "silent" under the old default also
get sound.

    python manage.py reset_notification_preferences            # dry run: only counts
    python manage.py reset_notification_preferences --apply    # delete the rows
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from Mainapp.audit import audit
from Mainapp.models import NotificationPreference
from Mainapp.notification_preferences import DEFAULT_MODE


class Command(BaseCommand):
    help = "Delete all stored notification preferences so every user is back on the default mode."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete the rows. Without it the command only reports counts.",
        )

    def handle(self, *args, **options):
        rows = NotificationPreference.objects.all()
        total = rows.count()
        users = rows.values("user_id").distinct().count()
        by_mode = {
            mode: rows.filter(mode=mode).count() for mode, _ in NotificationPreference.Mode.choices
        }
        self.stdout.write(
            f"{total} stored preference row(s) across {users} user(s): "
            + ", ".join(f"{mode}={count}" for mode, count in by_mode.items())
        )
        if not options["apply"]:
            self.stdout.write(
                f"Dry run. Re-run with --apply to reset them all to the default ({DEFAULT_MODE})."
            )
            return

        with transaction.atomic():
            deleted, _ = rows.delete()
        audit("notification_preferences.reset_all", actor="system", deleted=deleted, users=users,
              default_mode=DEFAULT_MODE)
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted} row(s). All users are now on the default ({DEFAULT_MODE})."
        ))

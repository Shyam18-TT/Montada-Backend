"""
Send one correctly shaped push of a given data.type to EVERY device of one user and print the
per-device FCM result. Use only an internal test account, never a customer.

    python manage.py send_test_push <user_id_or_email> <data.type> [--silent] [--yes]

Without --yes it only lists the devices it would send to. Preferences are ignored on purpose:
this tests the pipe, not the user's settings.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

import push_contract


class Command(BaseCommand):
    help = "Send a test push of one data.type to every device of a user and print the FCM result."

    def add_arguments(self, parser):
        parser.add_argument("user", help="User id (UUID) or email.")
        parser.add_argument("type", help="data.type: %s" % ", ".join(sorted(push_contract.SAMPLE_PAYLOADS)))
        parser.add_argument("--silent", action="store_true", help="Send in silent mode (no sound).")
        parser.add_argument("--yes", action="store_true", help="Actually send. Without it, devices are only listed.")

    def handle(self, *args, **options):
        import firebase
        from django.contrib.auth import get_user_model
        from Mainapp.models import DeviceToken

        push_type = options["type"].strip().lower()
        sample = push_contract.SAMPLE_PAYLOADS.get(push_type)
        if sample is None:
            raise CommandError(
                "Unknown type %r. Choose one of: %s" % (push_type, ", ".join(sorted(push_contract.SAMPLE_PAYLOADS)))
            )

        User = get_user_model()
        ident = options["user"].strip()
        query = Q(email__iexact=ident)
        try:
            import uuid

            query |= Q(pk=uuid.UUID(ident))
        except ValueError:
            pass
        user = User.objects.filter(query).first()
        if user is None:
            raise CommandError("No user matching %r" % ident)

        devices = list(DeviceToken.objects.filter(user=user, is_active=True).order_by("-updated_at"))
        sound = not options["silent"]
        self.stdout.write(
            "user=%s (%s)  type=%s  mode=%s  devices=%d"
            % (user.email, user.pk, push_type, "sound" if sound else "silent", len(devices))
        )
        if not devices:
            raise CommandError("This user has no active registered devices.")
        for device in devices:
            self.stdout.write("  - %s  platform=%s  device_id=%s  token=%s…" % (
                device.id, device.platform, device.device_id[:12], device.fcm_token[:10],
            ))
        if not options["yes"]:
            self.stdout.write("Dry run. Re-run with --yes to send.")
            return

        notification_id = push_contract.new_notification_id()
        data = dict(sample["data"], type=push_type, notification_id=notification_id)
        for device in devices:
            result = firebase.send_push_to_tokens(
                tokens=[device.fcm_token],
                title=sample["title"],
                body=sample["body"],
                data=data,
                sound=sound,
                platform=device.platform,
            )
            outcome = "OK" if result["success_count"] else "FAILED %s" % (result["errors"][:1],)
            self.stdout.write("  %s  platform=%s  %s" % (device.device_id[:12] or device.id, device.platform, outcome))
        self.stdout.write("notification_id=%s (see PushDelivery for the message ids)" % notification_id)

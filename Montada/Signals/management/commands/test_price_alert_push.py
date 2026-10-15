"""
Send a real test price-alert push through the exact path run_price_alerts uses
(send_push_to_users -> preferences -> device tokens -> FCM) and print what happened.

Run it on the same server/environment as run_price_alerts:
  python manage.py test_price_alert_push --email user@example.com
"""
from django.core.management.base import BaseCommand, CommandError

from Signals.management.commands.run_price_alerts import _describe_push_result


class Command(BaseCommand):
    help = "Send a test 'user_price_alert' push to one user and report the FCM result."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Email of the user to send the test push to.")

    def handle(self, *args, **options):
        import firebase
        from Mainapp.models import DeviceToken, User
        from Mainapp.notification_preferences import category_for_push, get_user_modes

        email = options["email"].strip()
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            raise CommandError("No user with email %s" % email)

        data = {
            "type": "user_price_alert",
            "alert_id": "test",
            "symbol": "TEST",
            "condition": "above",
            "current_price": "1.0000",
            "effective_target": "1.0000",
            "target_price": "1.0000",
        }
        category = category_for_push(data)
        self.stdout.write("push_enabled=%s firebase_app=%s" % (firebase.push_enabled(), firebase._ensure_firebase_app()))
        self.stdout.write("preference %s = %s" % (category, get_user_modes(user).get(category)))
        self.stdout.write("device token rows: %d, resolved send targets: %d" % (
            DeviceToken.objects.filter(user=user).count(),
            len(firebase.get_push_tokens_for_users([user])),
        ))

        result = firebase.send_push_to_users(
            users=[user],
            title="Price alert – TEST",
            body="This is a test price alert push.",
            data=data,
        )
        self.stdout.write("FCM result: %s" % _describe_push_result(result))

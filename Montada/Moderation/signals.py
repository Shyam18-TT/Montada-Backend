import json
import logging
from urllib.request import Request, urlopen

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ModerationReport


logger = logging.getLogger(__name__)


@receiver(post_save, sender=ModerationReport)
def notify_team_on_new_report(sender, instance, created, **kwargs):
    if not created:
        return

    # The report email is sent by ModerationReportCreateView (_send_moderation_report_email);
    # sending it here too would deliver every report twice.

    webhook_url = getattr(settings, "MODERATION_SLACK_WEBHOOK_URL", "")
    if webhook_url:
        try:
            payload = {
                "text": (
                    f"New moderation report `{instance.id}`: {instance.reason} "
                    f"against user `{instance.reported_user_id or 'deleted'}`"
                )
            }
            request = Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urlopen(request, timeout=5).read()
        except Exception:
            logger.exception("Failed to send moderation Slack webhook.")

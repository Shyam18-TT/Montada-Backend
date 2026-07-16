import json
import logging
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import send_mail
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ModerationReport


logger = logging.getLogger(__name__)


@receiver(post_save, sender=ModerationReport)
def notify_team_on_new_report(sender, instance, created, **kwargs):
    if not created:
        return

    subject = f"New moderation report: {instance.reason}"
    body = (
        f"A new moderation report was filed.\n\n"
        f"Report ID: {instance.id}\n"
        f"Reporter: {instance.reporter_id}\n"
        f"Reported user: {instance.reported_user_id_raw}\n"
        f"Content type: {instance.content_type}\n"
        f"Content ID: {instance.content_id}\n"
        f"Reason: {instance.reason}\n"
        f"Platform: {instance.platform}\n"
        f"Reported at: {instance.reported_at}\n"
        f"Created at: {instance.created_at}\n\n"
        f"Excerpt:\n{instance.content_excerpt}\n\n"
        f"Details:\n{instance.details}\n"
    )

    recipients = getattr(settings, "MODERATION_SUPPORT_EMAILS", None) or [
        email for _, email in getattr(settings, "ADMINS", [])
    ]
    if recipients:
        try:
            send_mail(
                subject,
                body,
                getattr(settings, "DEFAULT_FROM_EMAIL", None)
                or getattr(settings, "EMAIL_HOST_USER", None),
                recipients,
                fail_silently=True,
            )
        except Exception:
            logger.exception("Failed to send moderation report email.")

    webhook_url = getattr(settings, "MODERATION_SLACK_WEBHOOK_URL", "")
    if webhook_url:
        try:
            payload = {
                "text": (
                    f"New moderation report `{instance.id}`: {instance.reason} "
                    f"against user `{instance.reported_user_id_raw}`"
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


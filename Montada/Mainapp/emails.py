from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

# Must match the expiry used by the OTP models' is_expired() (Mainapp/models.py).
OTP_EXPIRY_MINUTES = 10


def send_templated_email(subject, recipients, template_name, context, text_body, fail_silently=False):
    """
    Send an HTML email rendered from templates/<template_name>, with text_body as the
    plain-text alternative for clients that don't render HTML.

    `support_email` and `expiry_minutes` are always available to the template.
    """
    full_context = {
        "support_email": getattr(settings, "SUPPORT_EMAIL", "support@trusttc.com"),
        "expiry_minutes": OTP_EXPIRY_MINUTES,
        **context,
    }
    html_body = render_to_string(template_name, full_context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@montada.com",
        to=list(recipients),
    )
    message.attach_alternative(html_body, "text/html")
    return message.send(fail_silently=fail_silently)


def user_display_name(user):
    return getattr(user, "name", None) or getattr(user, "username", None) or "there"

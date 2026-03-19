"""
Shared helpers for gating premium content.

Subscriptions unlock **market news** (third-party feeds) and **live market data** (e.g. MT5 quotes).
Analyst trading signals and social follow features do not require a subscription.
"""
from rest_framework import status
from rest_framework.response import Response

SUBSCRIPTION_REQUIRED_MESSAGE = (
    "An active subscription is required to access market news and live market data."
)
SUBSCRIPTION_REQUIRED_CODE = "subscription_required"


def check_active_subscription(user):
    """
    If *user* has an active subscription (including an in-date free trial), return None.
    Otherwise return a DRF Response (403 or 503) — assign with `return check_active_subscription(...)`.
    """
    try:
        from .models import Subscription
    except ImportError:
        return Response(
            {"error": "Subscription service is not available."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        subscription = Subscription.objects.get(user=user)
    except Subscription.DoesNotExist:
        return Response(
            {
                "error": SUBSCRIPTION_REQUIRED_MESSAGE,
                "code": SUBSCRIPTION_REQUIRED_CODE,
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    if not subscription.is_active():
        return Response(
            {
                "error": SUBSCRIPTION_REQUIRED_MESSAGE,
                "code": SUBSCRIPTION_REQUIRED_CODE,
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    return None

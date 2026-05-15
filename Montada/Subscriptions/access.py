"""
Shared helpers for gating premium content.

Subscriptions unlock **market news** (third-party feeds) and **live market data** (e.g. MT5 quotes).
Analyst trading signals and social follow features do not require a subscription.
"""
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

SUBSCRIPTION_REQUIRED_MESSAGE = (
    "An active subscription is required to access market news and live market data."
)
SUBSCRIPTION_REQUIRED_CODE = "subscription_required"


def market_news_and_data_free_access_enabled() -> bool:
    """
    When True, any authenticated user can access market news, live news, and live
    market data (MT5 quotes) without an active Subscription.
    Set ``MARKET_NEWS_AND_DATA_FREE_ACCESS`` in settings (or env) to toggle the paywall.
    """
    return bool(getattr(settings, "MARKET_NEWS_AND_DATA_FREE_ACCESS", False))


def user_has_active_admin_in_app_grant(user) -> bool:
    """
    True if staff granted full in-app access and optional expiry has not passed.
    """
    if not getattr(user, "admin_granted_in_app_access", False):
        return False
    exp = getattr(user, "admin_in_app_access_expires_at", None)
    if exp is not None and timezone.now() >= exp:
        return False
    return True


def check_active_subscription(user):
    """
    If *user* has an active subscription (including an in-date free trial), return None.
    Otherwise return a DRF Response (403 or 503) — assign with `return check_active_subscription(...)`.
    """
    if market_news_and_data_free_access_enabled():
        return None

    try:
        from .models import Subscription
    except ImportError:
        return Response(
            {"error": "Subscription service is not available."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if user_has_active_admin_in_app_grant(user):
        return None

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

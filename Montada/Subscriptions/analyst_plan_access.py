"""
Per-analyst content access (AnalystContentPlan / UserAnalystPlanSubscription).

Trader listings require an **active UserAnalystPlanSubscription** with the right scope
(signals / articles / all), not only a social follow.

- **Signals**: visible only for analysts the user follows *and* has an active subscription
  covering signals (scope `signals` or `all`).
- **Articles**: published articles from **analyst** authors require an active subscription
  covering articles (`articles` or `all`). Non-analyst authors are shown without a plan.
"""
from __future__ import annotations

from typing import Iterable, List

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import AnalystContentPlan, UserAnalystPlanSubscription


def _now():
    return timezone.now()


def _subscribed_analyst_ids_for_signals(subscriber) -> set:
    now = _now()
    return set(
        UserAnalystPlanSubscription.objects.filter(
            subscriber=subscriber,
            status=UserAnalystPlanSubscription.Status.ACTIVE,
            end_date__gte=now,
            plan__is_active=True,
            plan__scope__in=[
                AnalystContentPlan.Scope.SIGNALS,
                AnalystContentPlan.Scope.ALL,
            ],
        )
        .values_list("plan__analyst_id", flat=True)
        .distinct()
    )


def filter_visible_analyst_ids_for_signals(
    subscriber, following_analyst_ids: Iterable,
) -> List:
    """
    Analysts whose signals a trader may see: must **follow** them and hold an active
    subscription (plan scope signals or all) to that analyst.
    """
    following = set(following_analyst_ids)
    if not following:
        return []
    subscribed = _subscribed_analyst_ids_for_signals(subscriber)
    return list(following & subscribed)


def user_has_analyst_signal_access(subscriber, analyst_id) -> bool:
    """True if *subscriber* has an active analyst plan covering signals for *analyst_id*."""
    now = _now()
    return UserAnalystPlanSubscription.objects.filter(
        subscriber=subscriber,
        status=UserAnalystPlanSubscription.Status.ACTIVE,
        end_date__gte=now,
        plan__is_active=True,
        plan__analyst_id=analyst_id,
        plan__scope__in=[
            AnalystContentPlan.Scope.SIGNALS,
            AnalystContentPlan.Scope.ALL,
        ],
    ).exists()


def user_has_analyst_article_access(subscriber, author_id) -> bool:
    """
    Published articles: non-analyst authors are public to authenticated users.
    Analyst authors require an active subscription (articles or all) to that analyst.
    """
    User = get_user_model()
    try:
        author = User.objects.only("user_type").get(pk=author_id)
    except User.DoesNotExist:
        return False
    if getattr(author, "user_type", "") != "analyst":
        return True
    now = _now()
    return UserAnalystPlanSubscription.objects.filter(
        subscriber=subscriber,
        status=UserAnalystPlanSubscription.Status.ACTIVE,
        end_date__gte=now,
        plan__is_active=True,
        plan__analyst_id=author_id,
        plan__scope__in=[
            AnalystContentPlan.Scope.ARTICLES,
            AnalystContentPlan.Scope.ALL,
        ],
    ).exists()

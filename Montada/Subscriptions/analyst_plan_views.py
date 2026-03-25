"""
New API endpoints for per-analyst content plans.

These routes are separate from the existing app Subscription URLs and serializers.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AnalystContentPlan,
    UserAnalystPlanSubscription,
    create_analyst_plan_purchase,
)
from .analyst_plan_serializers import (
    AnalystContentPlanSerializer,
    AnalystContentPlanCreateUpdateSerializer,
    UserAnalystPlanSubscriptionSerializer,
    SubscribeAnalystPlanSerializer,
)


class IsAnalystUser(permissions.BasePermission):
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "user_type", "") == "analyst"
        )


def _end_date_for_plan(plan: AnalystContentPlan, duration_days: int | None = None):
    now = timezone.now()
    if duration_days is not None:
        return now + timedelta(days=duration_days)
    if plan.billing_period == AnalystContentPlan.BillingPeriod.YEARLY:
        return now + timedelta(days=365)
    if plan.billing_period == AnalystContentPlan.BillingPeriod.ONE_TIME:
        return now + timedelta(days=365 * 10)
    return now + timedelta(days=30)


def _ensure_follow_analyst_after_subscribe(subscriber, analyst_user):
    """
    Ensure *subscriber* has an active ACCEPTED Follow row to *analyst_user*.
    Mirrors Followers follow behaviour: pending → accepted; unfollowed/rejected → reactivated.

    Returns:
        dict with keys:
        - follows_analyst (bool): True if the user is following after this call (or was already).
        - action (str): "created" | "already_following" | "accepted_pending" | "reactivated" | "skipped_blocked"
    """
    from Followers.models import Follow

    if Follow.objects.filter(
        follower=subscriber,
        followed=analyst_user,
        status=Follow.Status.BLOCKED,
    ).exists():
        return {"follows_analyst": False, "action": "skipped_blocked"}

    existing = Follow.objects.filter(
        follower=subscriber, followed=analyst_user
    ).first()

    if existing:
        if existing.status == Follow.Status.ACCEPTED and existing.is_active:
            return {"follows_analyst": True, "action": "already_following"}
        if existing.status == Follow.Status.PENDING:
            existing.accept()
            return {"follows_analyst": True, "action": "accepted_pending"}
        if existing.status in (Follow.Status.REJECTED,) or (
            existing.status == Follow.Status.ACCEPTED and not existing.is_active
        ):
            existing.status = Follow.Status.ACCEPTED
            existing.is_active = True
            existing.accepted_at = timezone.now()
            existing.requested_at = timezone.now()
            existing.rejected_at = None
            existing.unfollowed_at = None
            existing.save(
                update_fields=[
                    "status",
                    "is_active",
                    "accepted_at",
                    "requested_at",
                    "rejected_at",
                    "unfollowed_at",
                ]
            )
            return {"follows_analyst": True, "action": "reactivated"}

    follow = Follow.objects.create(
        follower=subscriber,
        followed=analyst_user,
        status=Follow.Status.ACCEPTED,
        is_active=True,
    )
    follow.accepted_at = timezone.now()
    follow.save(update_fields=["accepted_at"])
    return {"follows_analyst": True, "action": "created"}


class AnalystContentPlanListCreateView(generics.ListCreateAPIView):
    """
    GET: List plans created by the authenticated analyst (empty for non-analysts).
    POST: Create a plan (analyst only).
    """

    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AnalystContentPlanCreateUpdateSerializer
        return AnalystContentPlanSerializer

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated or getattr(user, "user_type", "") != "analyst":
            return AnalystContentPlan.objects.none()
        return AnalystContentPlan.objects.filter(analyst=user).order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(analyst=self.request.user)

    def create(self, request, *args, **kwargs):
        if getattr(request.user, "user_type", "") != "analyst":
            return Response(
                {"error": "Only analysts can create content plans."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().create(request, *args, **kwargs)


class AnalystContentPlanDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET/PATCH/DELETE a plan. Only the owning analyst.
    """

    permission_classes = [permissions.IsAuthenticated, IsAnalystUser]
    lookup_field = "pk"

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return AnalystContentPlanCreateUpdateSerializer
        return AnalystContentPlanSerializer

    def get_queryset(self):
        return AnalystContentPlan.objects.filter(analyst=self.request.user)


class PublicAnalystContentPlanCatalogView(generics.ListAPIView):
    """
    GET: Active plans offered by a given analyst (for subscribers / storefront).
    URL includes analyst user id (UUID).

    Returns 404 if the user does not exist or is not an analyst.

    Each plan includes ``has_active_subscription``: whether the requesting user
    currently has an active UserAnalystPlanSubscription for that plan.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AnalystContentPlanSerializer
    pagination_class = None  # typically few plans per analyst; return full list

    def get_serializer_context(self):
        context = super().get_serializer_context()
        request = self.request
        analyst_id = self.kwargs.get("analyst_id")
        if request.user.is_authenticated and analyst_id:
            now = timezone.now()
            subscribed_ids = set(
                UserAnalystPlanSubscription.objects.filter(
                    subscriber=request.user,
                    status=UserAnalystPlanSubscription.Status.ACTIVE,
                    end_date__gte=now,
                    plan__analyst_id=analyst_id,
                ).values_list("plan_id", flat=True)
            )
            context["subscribed_plan_ids"] = subscribed_ids
        else:
            context["subscribed_plan_ids"] = set()
        return context

    def get_queryset(self):
        analyst_id = self.kwargs.get("analyst_id")
        User = get_user_model()
        analyst = get_object_or_404(User, pk=analyst_id)
        if getattr(analyst, "user_type", "") != "analyst":
            raise NotFound("No analyst found with this id.")
        return AnalystContentPlan.objects.filter(
            analyst_id=analyst_id,
            is_active=True,
        ).order_by("scope", "title")


class SubscribeToAnalystPlanView(APIView):
    """
    POST: Start a subscription to an analyst plan (payment flow can attach payment_intent_id later).
    Body: { "plan_id": "<uuid>", "payment_intent_id": optional, "duration_days": optional }

    On success, the subscriber is also set to follow the analyst (active ACCEPTED Follow),
    unless the pair is in a BLOCKED follow state.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ser = SubscribeAnalystPlanSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        plan_id = ser.validated_data["plan_id"]
        payment_intent_id = (ser.validated_data.get("payment_intent_id") or "").strip() or None

        plan = get_object_or_404(
            AnalystContentPlan.objects.filter(is_active=True).select_related("analyst"),
            pk=plan_id,
        )
        if plan.analyst_id == request.user.id:
            return Response(
                {"error": "You cannot subscribe to your own plan."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        duration_days = request.data.get("duration_days")
        if duration_days is not None:
            try:
                duration_days = int(duration_days)
            except (TypeError, ValueError):
                duration_days = None
            if duration_days is not None and not (1 <= duration_days <= 3650):
                duration_days = None

        end_date = _end_date_for_plan(plan, duration_days=duration_days)

        sub = UserAnalystPlanSubscription.objects.create(
            subscriber=request.user,
            plan=plan,
            status=UserAnalystPlanSubscription.Status.ACTIVE,
            end_date=end_date,
            payment_intent_id=payment_intent_id,
        )
        create_analyst_plan_purchase(sub, plan)
        sub = (
            UserAnalystPlanSubscription.objects.select_related("plan", "plan__analyst", "purchase")
            .get(pk=sub.pk)
        )

        follow_info = _ensure_follow_analyst_after_subscribe(request.user, plan.analyst)

        return Response(
            {
                "message": "Subscribed to analyst plan.",
                "subscription": UserAnalystPlanSubscriptionSerializer(sub, context={"request": request}).data,
                "follow": follow_info,
            },
            status=status.HTTP_201_CREATED,
        )


class MyAnalystPlanSubscriptionsListView(generics.ListAPIView):
    """GET: Current user's subscriptions to analyst plans."""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserAnalystPlanSubscriptionSerializer

    def get_queryset(self):
        return (
            UserAnalystPlanSubscription.objects.filter(subscriber=self.request.user)
            .select_related("plan", "plan__analyst", "purchase")
            .order_by("-created_at")
        )


class CancelAnalystPlanSubscriptionView(APIView):
    """POST: Cancel one of the current user's analyst-plan subscriptions."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        sub = get_object_or_404(
            UserAnalystPlanSubscription.objects.filter(subscriber=request.user),
            pk=pk,
        )
        sub.cancel()
        return Response(
            {
                "message": "Subscription cancelled.",
                "subscription": UserAnalystPlanSubscriptionSerializer(
                    sub, context={"request": request}
                ).data,
            },
            status=status.HTTP_200_OK,
        )

from rest_framework import serializers

from .models import AnalystContentPlan, UserAnalystPlanSubscription


class AnalystContentPlanSerializer(serializers.ModelSerializer):
    """Full plan representation (analyst-owned CRUD + catalog)."""

    analyst_id = serializers.UUIDField(read_only=True)
    active_subscribers_count = serializers.SerializerMethodField()
    has_active_subscription = serializers.SerializerMethodField()

    class Meta:
        model = AnalystContentPlan
        fields = (
            "id",
            "analyst_id",
            "title",
            "description",
            "scope",
            "price",
            "currency",
            "billing_period",
            "is_active",
            "active_subscribers_count",
            "has_active_subscription",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "analyst_id",
            "created_at",
            "updated_at",
            "active_subscribers_count",
            "has_active_subscription",
        )

    def get_active_subscribers_count(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        if getattr(request.user, "user_type", "") != "analyst" or obj.analyst_id != request.user.id:
            return None
        from django.utils import timezone

        now = timezone.now()
        return obj.user_subscriptions.filter(
            status=UserAnalystPlanSubscription.Status.ACTIVE,
            end_date__gte=now,
        ).count()

    def get_has_active_subscription(self, obj):
        """
        True if the request user has an active subscription to this plan.
        Set via context['subscribed_plan_ids'] (catalog view); otherwise False.
        """
        ids = self.context.get("subscribed_plan_ids")
        if not ids:
            return False
        return obj.pk in ids


class AnalystContentPlanCreateUpdateSerializer(serializers.ModelSerializer):
    """Writable fields for analysts creating/updating their plans."""

    class Meta:
        model = AnalystContentPlan
        fields = (
            "title",
            "description",
            "scope",
            "price",
            "currency",
            "billing_period",
            "is_active",
        )


class UserAnalystPlanSubscriptionSerializer(serializers.ModelSerializer):
    plan = AnalystContentPlanSerializer(read_only=True)
    is_effective = serializers.SerializerMethodField()

    class Meta:
        model = UserAnalystPlanSubscription
        fields = (
            "id",
            "plan",
            "status",
            "start_date",
            "end_date",
            "payment_intent_id",
            "is_effective",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "plan",
            "status",
            "start_date",
            "end_date",
            "payment_intent_id",
            "created_at",
            "updated_at",
            "is_effective",
        )

    def get_is_effective(self, obj):
        return obj.is_effective()


class SubscribeAnalystPlanSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField()
    payment_intent_id = serializers.CharField(required=False, allow_blank=True, default="")

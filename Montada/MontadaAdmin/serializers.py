"""
Serializers for admin list views (analysts, traders), admin login, and admin create user.
"""
from django.conf import settings
from rest_framework import serializers
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password

User = get_user_model()

try:
    from Subscriptions.models import AnalystContentPlan, UserAnalystPlanSubscription, InAppSubscriptionSettings
except ImportError:
    AnalystContentPlan = None
    UserAnalystPlanSubscription = None
    InAppSubscriptionSettings = None

try:
    from MontadaAdmin.models import EconomicCalendarGlobalReminderSettings
except ImportError:
    EconomicCalendarGlobalReminderSettings = None

from Moderation.models import UserBlock, ModerationReport


class AdminLoginSerializer(serializers.Serializer):
    """Email and password for admin login; validates credentials and sets user."""
    email = serializers.EmailField(required=True, write_only=True)
    password = serializers.CharField(required=True, write_only=True, style={"input_type": "password"})

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")
        if not email or not password:
            raise serializers.ValidationError("Email and password are required.")
        request = self.context.get("request")
        user = authenticate(request=request, username=email, password=password)
        if not user:
            raise serializers.ValidationError("Invalid email or password.")
        if not user.is_active:
            raise serializers.ValidationError("User account is disabled.")
        attrs["user"] = user
        return attrs


class AdminChangeUserPasswordSerializer(serializers.Serializer):
    """Admin sets a new password for a user. Body: user_id (UUID), new_password."""
    user_id = serializers.UUIDField(required=True)
    new_password = serializers.CharField(
        required=True,
        write_only=True,
        validators=[validate_password],
        style={"input_type": "password"},
    )


class AdminSuspendUserSerializer(serializers.Serializer):
    """Admin suspend or unsuspend a user. Body: user_id (UUID), suspend (boolean)."""
    user_id = serializers.UUIDField(required=True)
    suspend = serializers.BooleanField(required=True)


class AdminAssignAnalystPlanSubscriptionSerializer(serializers.Serializer):
    """
    Admin assigns a trader to an analyst plan without creating a purchase row (excluded from revenue).
    Body: subscriber_id, plan_id, optional duration_days, optional ensure_follow (default true).
    """

    subscriber_id = serializers.UUIDField()
    plan_id = serializers.UUIDField()
    duration_days = serializers.IntegerField(required=False, min_value=1, max_value=3650)
    ensure_follow = serializers.BooleanField(required=False, default=True)


class AdminRemoveAnalystPlanSubscriptionSerializer(serializers.Serializer):
    """
    Admin removes/cancels a trader subscription to an analyst plan.
    Body: subscriber_id, plan_id
    """

    subscriber_id = serializers.UUIDField()
    plan_id = serializers.UUIDField()


if InAppSubscriptionSettings is not None:
    class AdminInAppSubscriptionSettingsSerializer(serializers.ModelSerializer):
        """Singleton settings: trial_period_days for new in-app (market data) trials."""

        class Meta:
            model = InAppSubscriptionSettings
            fields = ("id", "trial_period_days", "updated_at")
            read_only_fields = ("id", "updated_at")

        def validate_trial_period_days(self, value):
            if value < 1 or value > 365:
                raise serializers.ValidationError("trial_period_days must be between 1 and 365.")
            return value
else:
    AdminInAppSubscriptionSettingsSerializer = None


if EconomicCalendarGlobalReminderSettings is not None:
    class AdminEconomicCalendarReminderSettingsSerializer(serializers.ModelSerializer):
        """Singleton: global advance reminder minutes for all users before economic events."""

        class Meta:
            model = EconomicCalendarGlobalReminderSettings
            fields = ("id", "is_enabled", "minutes_before", "updated_at")
            read_only_fields = ("id", "updated_at")

        def validate_minutes_before(self, value):
            if value < 1 or value > 1440:
                raise serializers.ValidationError(
                    "minutes_before must be between 1 and 1440 (24 hours)."
                )
            return value
else:
    AdminEconomicCalendarReminderSettingsSerializer = None


class AdminInAppFullAccessSerializer(serializers.Serializer):
    """
    Grant or revoke full in-app access (market news & live data) without a Subscription row.
    Body: user_id (single user), or all_users=true (every row in User); grant; optional expires_at.
    """

    user_id = serializers.UUIDField(required=False, allow_null=True)
    all_users = serializers.BooleanField(required=False, default=False)
    grant = serializers.BooleanField(default=True)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, attrs):
        all_users = attrs.get("all_users") is True
        uid = attrs.get("user_id")
        if all_users and uid is not None:
            raise serializers.ValidationError(
                {"user_id": "Omit user_id when all_users is true."}
            )
        if not all_users and uid is None:
            raise serializers.ValidationError(
                "Provide user_id for one user, or set all_users to true for every user."
            )
        return attrs


class AdminUserProfileSerializer(serializers.ModelSerializer):
    """Read-only user profile for admin view. Includes is_active, is_staff."""

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "name",
            "phone_number",
            "profile_picture",
            "date_of_birth",
            "user_type",
            "is_subscribed",
            "admin_granted_in_app_access",
            "admin_in_app_access_expires_at",
            "is_verified",
            "is_active",
            "is_staff",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class AdminCreateAnalystSerializer(serializers.Serializer):
    """Create analyst from admin; requires email and password."""
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True, validators=[validate_password], style={"input_type": "password"})
    name = serializers.CharField(required=False, allow_blank=True, default="")
    phone_number = serializers.CharField(required=False, allow_blank=True, default="")
    is_verified = serializers.BooleanField(required=False, default=False)

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        validated_data.setdefault("username", validated_data["email"])
        validated_data["user_type"] = "analyst"
        validated_data["is_subscribed"] = False
        user = User.objects.create_user(**validated_data)
        return user


class AdminCreateTraderSerializer(serializers.Serializer):
    """Create trader from admin; requires email, password, and subscription_plan (basic | free_trial | premium)."""
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True, validators=[validate_password], style={"input_type": "password"})
    name = serializers.CharField(required=False, allow_blank=True, default="")
    phone_number = serializers.CharField(required=False, allow_blank=True, default="")
    is_verified = serializers.BooleanField(required=False, default=False)
    subscription_plan = serializers.ChoiceField(
        choices=[("basic", "Basic"), ("free_trial", "Free Trial"), ("premium", "Premium")],
        required=True,
    )
    trial_days = serializers.IntegerField(required=False, default=7, min_value=1, max_value=365)
    premium_plan = serializers.ChoiceField(
        choices=[("monthly", "Monthly"), ("yearly", "Yearly")],
        required=False,
        default="monthly",
    )

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        from django.utils import timezone
        from datetime import timedelta

        subscription_plan = validated_data.pop("subscription_plan")
        trial_days = validated_data.pop("trial_days", 7)
        premium_plan = validated_data.pop("premium_plan", "monthly")

        validated_data.setdefault("username", validated_data["email"])
        validated_data["user_type"] = "trader"
        validated_data["is_subscribed"] = subscription_plan != "basic"
        validated_data["free_trial_eligible"] = False
        user = User.objects.create_user(**validated_data)

        if subscription_plan == "basic":
            pass
        elif subscription_plan == "free_trial":
            try:
                from Subscriptions.models import Subscription
                end_date = timezone.now() + timedelta(days=trial_days)
                Subscription.objects.create(
                    user=user,
                    plan_type="free_trial",
                    status="active",
                    end_date=end_date,
                    is_trial=True,
                )
                user.is_subscribed = True
                user.save()
            except Exception:
                pass
        else:  # premium
            try:
                from Subscriptions.models import Subscription
                if premium_plan == "yearly":
                    end_date = timezone.now() + timedelta(days=365)
                    plan_type = "yearly"
                else:
                    end_date = timezone.now() + timedelta(days=30)
                    plan_type = "monthly"
                Subscription.objects.create(
                    user=user,
                    plan_type=plan_type,
                    status="active",
                    end_date=end_date,
                    is_trial=False,
                )
                user.is_subscribed = True
                user.save()
            except Exception:
                pass

        return user


if AnalystContentPlan is not None:

    class AdminAnalystContentPlanDetailSerializer(serializers.ModelSerializer):
        """
        One analyst content plan with subscriber metrics (admin can read any analyst's plans).
        """

        analyst_id = serializers.UUIDField(read_only=True)
        active_subscribers_count = serializers.SerializerMethodField()
        total_subscriptions_count = serializers.SerializerMethodField()

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
                "total_subscriptions_count",
                "created_at",
                "updated_at",
            )
            read_only_fields = fields

        def get_active_subscribers_count(self, obj):
            from django.utils import timezone

            if UserAnalystPlanSubscription is None:
                return 0
            now = timezone.now()
            return obj.user_subscriptions.filter(
                status=UserAnalystPlanSubscription.Status.ACTIVE,
                end_date__gte=now,
            ).count()

        def get_total_subscriptions_count(self, obj):
            return obj.user_subscriptions.count()

else:
    AdminAnalystContentPlanDetailSerializer = None


if UserAnalystPlanSubscription is not None:

    class AdminAnalystSubscriberSubscriptionSerializer(serializers.ModelSerializer):
        """
        One subscription row for admin: subscriber identity + plan snapshot + subscription fields.
        No full user profile beyond what is needed for subscription context.
        """

        subscriber = serializers.SerializerMethodField()
        plan = serializers.SerializerMethodField()
        purchase = serializers.SerializerMethodField()
        is_effective = serializers.SerializerMethodField()

        class Meta:
            model = UserAnalystPlanSubscription
            fields = (
                "id",
                "subscriber",
                "plan",
                "status",
                "start_date",
                "end_date",
                "purchase",
                "payment_intent_id",
                "is_effective",
                "created_at",
                "updated_at",
            )
            read_only_fields = fields

        def get_subscriber(self, obj):
            u = obj.subscriber
            return {
                "id": str(u.id),
                "email": u.email,
                "name": u.name or "",
            }

        def get_plan(self, obj):
            p = obj.plan
            return {
                "id": str(p.id),
                "title": p.title,
                "scope": p.scope,
                "billing_period": p.billing_period,
            }

        def get_purchase(self, obj):
            pu = getattr(obj, "purchase", None)
            if pu is None:
                return None
            return {
                "amount": str(pu.amount),
                "currency": pu.currency,
                "plan_title_at_purchase": pu.plan_title,
            }

        def get_is_effective(self, obj):
            return obj.is_effective()

else:
    AdminAnalystSubscriberSubscriptionSerializer = None


class AdminAnalystWithPlansTableSerializer(serializers.ModelSerializer):
    """
    Analyst row for admin table: users with at least one content plan.
    Expects annotated total_plans, active_plans, distinct_subscribers, total_plan_subscriptions.
    """

    status = serializers.SerializerMethodField()
    total_plans = serializers.IntegerField(read_only=True)
    active_plans = serializers.IntegerField(read_only=True)
    distinct_subscribers = serializers.IntegerField(read_only=True)
    total_plan_subscriptions = serializers.IntegerField(read_only=True)
    registered_at = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "name",
            "email",
            "status",
            "is_active",
            "is_verified",
            "total_plans",
            "active_plans",
            "distinct_subscribers",
            "total_plan_subscriptions",
            "registered_at",
        )

    def get_status(self, obj):
        return "active" if obj.is_active else "inactive"


class AdminAnalystListSerializer(serializers.ModelSerializer):
    """Read-only serializer for analyst list; expects annotated wins, losses, signals_count, followers_count."""
    id = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    signals_count = serializers.SerializerMethodField()
    followers = serializers.SerializerMethodField()
    win_rate = serializers.SerializerMethodField()
    registered_at = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "name",
            "email",
            "status",
            "is_verified",
            "signals_count",
            "followers",
            "win_rate",
            "registered_at",
        )

    def get_id(self, obj):
        return str(obj.id)

    def get_status(self, obj):
        return "active" if obj.is_active else "inactive"

    def get_signals_count(self, obj):
        return getattr(obj, "signals_count", 0) or 0

    def get_followers(self, obj):
        return getattr(obj, "followers_count", 0) or 0

    def get_win_rate(self, obj):
        wins = getattr(obj, "wins", 0) or 0
        losses = getattr(obj, "losses", 0) or 0
        total = wins + losses
        if total == 0:
            return 0
        return round((wins / total) * 100, 2)

    def get_registered_at(self, obj):
        return obj.created_at.isoformat() if obj.created_at else None


class AdminTraderListSerializer(serializers.ModelSerializer):
    """Read-only serializer for trader list; subscription from related Subscription, signals_applied from annotation."""
    id = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    subscription = serializers.SerializerMethodField()
    signals_applied = serializers.SerializerMethodField()
    registered_at = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "name",
            "email",
            "status",
            "is_verified",
            "subscription",
            "signals_applied",
            "registered_at",
        )

    def get_id(self, obj):
        return str(obj.id)

    def get_status(self, obj):
        return "active" if obj.is_active else "inactive"

    def get_signals_applied(self, obj):
        return getattr(obj, "signals_applied_count", 0) or 0

    def get_subscription(self, obj):
        sub = getattr(obj, "subscription", None)
        if sub is None:
            return "basic"
        if sub.status != "active":
            return "basic"
        if getattr(sub, "is_trial", True) or sub.plan_type == "free_trial":
            return "trial"
        return "subscribed"

    def get_registered_at(self, obj):
        return obj.created_at.isoformat() if obj.created_at else None


class AdminTraderForAnalystSubscriptionSerializer(serializers.ModelSerializer):
    """
    Trader row for admin subscription assignment UI.
    ``is_subscribed`` reflects ``has_active_analyst_plan_subscription`` (annotated; active analyst-plan row).
    When subscribed, ``subscription_id`` and ``subscription_plan`` describe the active row (newest first).
    """

    id = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    is_subscribed = serializers.BooleanField(
        read_only=True,
        source="has_active_analyst_plan_subscription",
    )
    subscription_id = serializers.SerializerMethodField()
    subscription_plan = serializers.SerializerMethodField()
    registered_at = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "name",
            "email",
            "status",
            "is_active",
            "is_verified",
            "is_subscribed",
            "subscription_id",
            "subscription_plan",
            "registered_at",
        )

    def _active_analyst_plan_subscription(self, obj):
        if UserAnalystPlanSubscription is None:
            return None
        if not getattr(obj, "has_active_analyst_plan_subscription", False):
            return None
        oid = id(obj)
        cache = getattr(self, "_picker_active_sub_cache", None)
        if cache is None:
            cache = {}
            self._picker_active_sub_cache = cache
        if oid in cache:
            return cache[oid]

        from django.utils import timezone

        now = timezone.now()
        St = UserAnalystPlanSubscription.Status.ACTIVE
        qs = obj.analyst_content_subscriptions.filter(
            status=St.ACTIVE,
            end_date__gte=now,
        ).order_by("-created_at")
        analyst_id = self.context.get("analyst_id")
        if analyst_id:
            qs = qs.filter(plan__analyst_id=analyst_id)
        sub = qs.first()
        cache[oid] = sub
        return sub

    def get_subscription_id(self, obj):
        sub = self._active_analyst_plan_subscription(obj)
        return str(sub.id) if sub else None

    def get_subscription_plan(self, obj):
        sub = self._active_analyst_plan_subscription(obj)
        if sub is None or sub.plan_id is None:
            return None
        p = sub.plan
        return {
            "id": str(p.id),
            "analyst_id": str(p.analyst_id),
            "title": p.title,
            "description": p.description or "",
            "scope": p.scope,
            "price": str(p.price),
            "currency": p.currency or "",
            "billing_period": p.billing_period,
            "is_active": p.is_active,
        }

    def get_id(self, obj):
        return str(obj.id)

    def get_status(self, obj):
        return "active" if obj.is_active else "inactive"

    def get_registered_at(self, obj):
        return obj.created_at.isoformat() if obj.created_at else None


try:
    from News.models import NewsCategory, NewsArticle
except ImportError:
    NewsCategory = None
    NewsArticle = None

def _build_media_url(value):
    """Return full URL for a media file using PUBLIC_MEDIA_BASE_URL."""
    if not value:
        return None
    url = value.url if hasattr(value, "url") else str(value)
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = getattr(settings, "PUBLIC_MEDIA_BASE_URL", "").rstrip("/")
    return f"{base}{url}" if base else url


if NewsArticle is not None:
    class AdminNewsArticleListSerializer(serializers.ModelSerializer):
        """Read-only news article for admin list. Excludes tags and is_featured."""

        category_name = serializers.SerializerMethodField()

        class Meta:
            model = NewsArticle
            fields = (
                "id",
                "title",
                "slug",
                "summary",
                "content",
                "featured_image",
                "category",
                "category_name",
                "status",
                "content_access",
                "published_at",
                "created_at",
                "updated_at",
            )
            read_only_fields = fields

        def get_category_name(self, obj):
            return obj.category.name if obj.category else None

        def to_representation(self, instance):
            data = super().to_representation(instance)
            if data.get("featured_image") and instance.featured_image:
                data["featured_image"] = _build_media_url(instance.featured_image)
            return data
else:
    AdminNewsArticleListSerializer = None

if NewsCategory is not None:
    class AdminNewsCategoryCreateSerializer(serializers.ModelSerializer):
        """Create a news category from admin. Name required; slug optional (auto from name)."""

        class Meta:
            model = NewsCategory
            fields = ("id", "name", "slug")
            extra_kwargs = {
                "slug": {"required": False, "allow_blank": True},
            }
else:
    AdminNewsCategoryCreateSerializer = None

try:
    from Signals.serializers import TradingSignalSerializer

    class AdminCreateSignalSerializer(TradingSignalSerializer):
        """Create signal from admin. Analyst (created_by) is required and selectable from frontend."""

        analyst = serializers.PrimaryKeyRelatedField(
            queryset=User.objects.filter(user_type="analyst"),
            required=True,
        )

        class Meta(TradingSignalSerializer.Meta):
            read_only_fields = ("id", "created_at", "updated_at")
except ImportError:
    AdminCreateSignalSerializer = None




class UserBlockSerializer(serializers.ModelSerializer):
    blocked_user_id = serializers.CharField(write_only=True, max_length=64)

    class Meta:
        model = UserBlock
        fields = ("id", "blocked_user_id", "blocked", "created_at")
        read_only_fields = ("id", "blocked", "created_at")

    def validate_blocked_user_id(self, value):
        value = str(value or "").strip()
        if not value:
            raise serializers.ValidationError("blocked_user_id is required.")
        return value




class ModerationReportSerializer(serializers.ModelSerializer):
    # Read-only user details
    reporter_name = serializers.CharField(
        source="reporter.get_full_name",
        read_only=True
    )
    reporter_username = serializers.CharField(
        source="reporter.username",
        read_only=True
    )

    reported_user_name = serializers.CharField(
        source="reported_user.get_full_name",
        read_only=True
    )
    reported_user_username = serializers.CharField(
        source="reported_user.username",
        read_only=True
    )

    reviewed_by_name = serializers.CharField(
        source="reviewed_by.get_full_name",
        read_only=True
    )
    reviewed_by_username = serializers.CharField(
        source="reviewed_by.username",
        read_only=True
    )

    class Meta:
        model = ModerationReport
        fields = [
            "id",

            # Reporter
            "reporter",
            "reporter_name",
            "reporter_username",

            # Reported User
            "reported_user",
            "reported_user_name",
            "reported_user_username",

            # Content Information
            "content_type",
            "content_id",
            "content_excerpt",

            # Report Details
            "reason",
            "details",
            "platform",
            "reported_at",

            # Moderation
            "status",
            "reviewed_at",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_by_username",

            # Audit
            "created_at",
        ]

        read_only_fields = [
            "id",
            "created_at",
            "reviewed_at",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_by_username",
            "reporter_name",
            "reporter_username",
            "reported_user_name",
            "reported_user_username",
        ]

    def validate_reason(self, value):
        """
        Ensure the reason is one of the supported report reasons.
        """
        allowed_reasons = {
            "spam",
            "harassment",
            "hate_speech",
            "violence",
            "nudity",
            "misinformation",
            "copyright",
            "scam",
            "impersonation",
            "other",
        }

        if value.lower() not in allowed_reasons:
            raise serializers.ValidationError(
                f"Reason must be one of: {', '.join(sorted(allowed_reasons))}"
            )

        return value.lower()

    def validate_content_type(self, value):
        """
        Normalize the content type.
        """
        return value.lower().strip()

    def validate_platform(self, value):
        """
        Normalize the platform.
        """
        return value.lower().strip() if value else value

    def validate(self, attrs):
        """
        Cross-field validation.
        """
        content_type = attrs.get("content_type")
        content_id = attrs.get("content_id")

        # Most content types should include a content_id.
        if content_type != "user" and not content_id:
            raise serializers.ValidationError({
                "content_id": "This field is required for the selected content type."
            })

        return attrs

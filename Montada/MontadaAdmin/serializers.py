"""
Serializers for admin list views (analysts, traders), admin login, and admin create user.
"""
from rest_framework import serializers
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password

User = get_user_model()


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
        user = User.objects.create_user(**validated_data)

        if subscription_plan == "basic":
            pass  # no Subscription record; is_subscribed already False
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


try:
    from News.models import NewsCategory, NewsArticle
except ImportError:
    NewsCategory = None
    NewsArticle = None

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
                "published_at",
                "created_at",
                "updated_at",
            )
            read_only_fields = fields

        def get_category_name(self, obj):
            return obj.category.name if obj.category else None
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

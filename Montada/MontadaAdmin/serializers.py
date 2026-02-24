"""
Serializers for admin list views (analysts, traders) and admin login.
"""
from rest_framework import serializers
from django.contrib.auth import authenticate, get_user_model

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

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers
from .models import NewsArticle, NewsCategory, Tag, NewsArticleLike, NewsArticleComment, LiveNews, EconomicCalendarEvent, EconomicCalendarReminder, EconomicCalendarEventNotification


def _build_media_url(value):
    """Return full URL for a media file (e.g. featured_image) using PUBLIC_MEDIA_BASE_URL."""
    if not value:
        return None
    url = value.url if hasattr(value, 'url') else str(value)
    if not url:
        return None
    if url.startswith('http://') or url.startswith('https://'):
        return url
    base = getattr(settings, 'PUBLIC_MEDIA_BASE_URL', '').rstrip('/')
    return f"{base}{url}" if base else url


class NewsCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsCategory
        fields = ("id", "name", "slug")


class TagSerializer(serializers.ModelSerializer):
    """Minimal tag for list/detail responses."""

    class Meta:
        model = Tag
        fields = ("id", "name", "slug")
        read_only_fields = fields


class NewsArticleAuthorSerializer(serializers.ModelSerializer):
    """Minimal author details for news article responses."""

    class Meta:
        model = get_user_model()
        fields = ("id", "name", "username", "email", "user_type", "profile_picture")
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("profile_picture"):
            data["profile_picture"] = _build_media_url(instance.profile_picture)
        return data


class NewsArticleListSerializer(serializers.ModelSerializer):
    """Read-only news article for list API. Includes category_name, tags, like_count, comment_count, current_user_liked, author details."""

    category_name = serializers.SerializerMethodField()
    tags = TagSerializer(many=True, read_only=True)
    like_count = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    current_user_liked = serializers.SerializerMethodField()
    author_details = NewsArticleAuthorSerializer(source="author", read_only=True)

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
            "tags",
            "status",
            "content_access",
            "published_at",
            "like_count",
            "comment_count",
            "current_user_liked",
            "author_details",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_category_name(self, obj):
        return obj.category.name if obj.category else None

    def get_like_count(self, obj):
        return getattr(obj, "like_count", getattr(obj, "likes__count", obj.likes.count() if hasattr(obj, "likes") else 0))

    def get_comment_count(self, obj):
        return getattr(obj, "comment_count", getattr(obj, "comments__count", obj.comments.filter(is_deleted=False).count() if hasattr(obj, "comments") else 0))

    def get_current_user_liked(self, obj):
        if getattr(obj, "current_user_liked", None) is not None:
            return bool(obj.current_user_liked)
        request = self.context.get("request")
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return obj.likes.filter(user=request.user).exists() if hasattr(obj, "likes") else False

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("featured_image") and instance.featured_image:
            data["featured_image"] = _build_media_url(instance.featured_image)
        return data


class NewsArticleCommentSerializer(serializers.ModelSerializer):
    """Comment list/create. User info for display."""
    user_name = serializers.CharField(source="user.name", read_only=True)
    user_username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = NewsArticleComment
        fields = ("id", "user", "user_name", "user_username", "content", "created_at", "updated_at")
        read_only_fields = ("id", "user", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class NewsArticleCreateSerializer(serializers.ModelSerializer):
    """Serializer for analyst to create a news article."""

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
            "tags",
            "status",
            "is_featured",
            "content_access",
            "published_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at", "published_at", "views_count")
        extra_kwargs = {
            "slug": {"required": False, "allow_blank": True},
            "summary": {"required": False, "allow_blank": True},
            "featured_image": {"required": False},
            "category": {"required": False, "allow_null": True},
            "tags": {"required": False},
            "status": {"default": "draft"},
            "is_featured": {"default": False},
            "content_access": {"default": NewsArticle.ContentAccess.PREMIUM},
        }

    def validate_status(self, value):
        if value not in ("draft", "published", "archived"):
            raise serializers.ValidationError("Status must be draft, published, or archived.")
        return value

    def create(self, validated_data):
        if validated_data.get("status") == "published":
            validated_data["published_at"] = timezone.now()

        return super().create(validated_data)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("featured_image"):
            data["featured_image"] = _build_media_url(instance.featured_image)
        return data


class LiveNewsSerializer(serializers.ModelSerializer):
    class Meta:
        model = LiveNews
        fields = (
            "id",
            "provider_event_id",
            "provider_content_id",
            "provider_revision_id",
            "original_id",
            "action",
            "news_type",
            "language",
            "title",
            "teaser",
            "body",
            "source_url",
            "authors",
            "tags",
            "securities",
            "channels",
            "images",
            "primary_image_url",
            "source_created_at",
            "source_updated_at",
            "source_timestamp",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class EconomicCalendarEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = EconomicCalendarEvent
        fields = (
            "id",
            "provider_id",
            "event_name",
            "currency_code",
            "country_name",
            "importance",
            "actual_value",
            "forecast_value",
            "previous_value",
            "release_date",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class EconomicCalendarReminderCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating an economic calendar reminder."""

    class Meta:
        model = EconomicCalendarReminder
        fields = (
            "id",
            "event",
            "reminder_type",
            "custom_minutes_before",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "reminder_time", "is_sent", "sent_at", "created_at", "updated_at")
        extra_kwargs = {
            "event": {"required": True},
            "reminder_type": {"default": EconomicCalendarReminder.ReminderType.BEFORE_15_MIN},
            "custom_minutes_before": {"required": False, "allow_null": True},
            "is_active": {"default": True},
        }

    def validate(self, data):
        """Validate that custom_minutes_before is provided when reminder_type is CUSTOM."""
        reminder_type = data.get("reminder_type")
        custom_minutes_before = data.get("custom_minutes_before")

        if reminder_type == EconomicCalendarReminder.ReminderType.CUSTOM:
            if custom_minutes_before is None or custom_minutes_before <= 0:
                raise serializers.ValidationError(
                    {"custom_minutes_before": "custom_minutes_before must be a positive integer when reminder_type is CUSTOM."}
                )
        return data

    def create(self, validated_data):
        """Create reminder and calculate reminder_time based on event and reminder_type."""
        from datetime import timedelta

        event = validated_data["event"]
        reminder_type = validated_data["reminder_type"]
        custom_minutes_before = validated_data.get("custom_minutes_before")

        # Calculate minutes before based on reminder_type
        if reminder_type == EconomicCalendarReminder.ReminderType.BEFORE_5_MIN:
            minutes_before = 5
        elif reminder_type == EconomicCalendarReminder.ReminderType.BEFORE_15_MIN:
            minutes_before = 15
        elif reminder_type == EconomicCalendarReminder.ReminderType.BEFORE_30_MIN:
            minutes_before = 30
        elif reminder_type == EconomicCalendarReminder.ReminderType.BEFORE_1_HOUR:
            minutes_before = 60
        else:  # CUSTOM
            minutes_before = custom_minutes_before

        # Calculate reminder_time
        reminder_time = event.release_date - timedelta(minutes=minutes_before)

        validated_data["reminder_time"] = reminder_time
        validated_data["user"] = self.context["request"].user

        return super().create(validated_data)


class EconomicCalendarReminderListSerializer(serializers.ModelSerializer):
    """Serializer for listing economic calendar reminders with event details."""
    event_name = serializers.CharField(source="event.event_name", read_only=True)
    event_release_date = serializers.DateTimeField(source="event.release_date", read_only=True)
    event_importance = serializers.CharField(source="event.importance", read_only=True)

    class Meta:
        model = EconomicCalendarReminder
        fields = (
            "id",
            "event",
            "event_name",
            "event_release_date",
            "event_importance",
            "reminder_type",
            "custom_minutes_before",
            "reminder_time",
            "is_sent",
            "sent_at",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class EconomicCalendarEventNotificationSerializer(serializers.ModelSerializer):
    """Serializer for tracking economic calendar event notifications sent to users."""
    event_name = serializers.CharField(source="event.event_name", read_only=True)
    user_username = serializers.CharField(source="user.username", read_only=True, allow_null=True)
    notification_type_display = serializers.CharField(source="get_notification_type_display", read_only=True)

    class Meta:
        model = EconomicCalendarEventNotification
        fields = (
            "id",
            "event",
            "event_name",
            "user",
            "user_username",
            "notification_type",
            "notification_type_display",
            "is_sent",
            "sent_at",
            "sent_to_all_users",
            "error_message",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

from django.conf import settings
from rest_framework import serializers
from .models import NewsArticle, NewsCategory, Tag, NewsArticleLike, NewsArticleComment


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


class NewsArticleListSerializer(serializers.ModelSerializer):
    """Read-only news article for list API. Includes category_name, tags, like_count, comment_count, current_user_liked."""

    category_name = serializers.SerializerMethodField()
    tags = TagSerializer(many=True, read_only=True)
    like_count = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    current_user_liked = serializers.SerializerMethodField()

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
            "published_at",
            "like_count",
            "comment_count",
            "current_user_liked",
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
        }

    def validate_status(self, value):
        if value not in ("draft", "published", "archived"):
            raise serializers.ValidationError("Status must be draft, published, or archived.")
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("featured_image"):
            data["featured_image"] = _build_media_url(instance.featured_image)
        return data

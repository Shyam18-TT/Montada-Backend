from django.conf import settings
from rest_framework import serializers
from .models import NewsArticle, NewsCategory, Tag


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
    """Read-only news article for list API. Includes category_name, tags, and full featured_image URL."""

    category_name = serializers.SerializerMethodField()
    tags = TagSerializer(many=True, read_only=True)

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

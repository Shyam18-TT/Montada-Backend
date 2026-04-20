from django.contrib import admin
from .models import NewsCategory, Tag, NewsArticle, NewsArticleLike, NewsArticleComment, LiveNews


@admin.register(NewsCategory)
class NewsCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)


@admin.register(NewsArticle)
class NewsArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "author", "status", "content_access", "is_featured", "published_at", "created_at")
    list_filter = ("status", "content_access", "is_featured", "is_deleted")
    search_fields = ("title", "summary", "content")
    prepopulated_fields = {"slug": ("title",)}
    raw_id_fields = ("author", "category")
    filter_horizontal = ("tags",)
    readonly_fields = ("created_at", "updated_at", "views_count")
    date_hierarchy = "published_at"


@admin.register(NewsArticleLike)
class NewsArticleLikeAdmin(admin.ModelAdmin):
    list_display = ("user", "article", "created_at")
    list_filter = ("created_at",)
    raw_id_fields = ("user", "article")


@admin.register(NewsArticleComment)
class NewsArticleCommentAdmin(admin.ModelAdmin):
    list_display = ("user", "article", "content_preview", "is_deleted", "created_at")
    list_filter = ("is_deleted", "created_at")
    raw_id_fields = ("user", "article")
    readonly_fields = ("created_at", "updated_at")

    def content_preview(self, obj):
        return (obj.content or "")[:60] + ("..." if len(obj.content or "") > 60 else "")
    content_preview.short_description = "Content"


@admin.register(LiveNews)
class LiveNewsAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "news_type",
        "language",
        "action",
        "provider_content_id",
        "source_updated_at",
        "is_active",
    )
    list_filter = ("news_type", "language", "action", "is_active", "source_updated_at")
    search_fields = ("title", "teaser", "body", "source_url")
    readonly_fields = ("created_at", "updated_at")

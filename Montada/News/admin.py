from django.contrib import admin
from .models import NewsCategory, Tag, NewsArticle


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
    list_display = ("title", "category", "author", "status", "is_featured", "published_at", "created_at")
    list_filter = ("status", "is_featured", "is_deleted")
    search_fields = ("title", "summary", "content")
    prepopulated_fields = {"slug": ("title",)}
    raw_id_fields = ("author", "category")
    filter_horizontal = ("tags",)
    readonly_fields = ("created_at", "updated_at", "views_count")
    date_hierarchy = "published_at"

from django.contrib import admin
from .models import Follow, Mute, AnalystReview


@admin.register(Follow)
class FollowAdmin(admin.ModelAdmin):
    list_display = ("id", "follower", "followed", "status", "is_active", "requested_at")
    list_filter = ("status", "is_active")
    search_fields = ("follower__email", "followed__email")


@admin.register(Mute)
class MuteAdmin(admin.ModelAdmin):
    list_display = ("id", "muter", "muted", "muted_at")
    search_fields = ("muter__email", "muted__email")


@admin.register(AnalystReview)
class AnalystReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'rating', 'analyst', 'reviewer', 'title', 'is_public', 'created_at')
    list_filter = ('rating', 'is_public', 'created_at')
    search_fields = ('title', 'review_text', 'analyst__email', 'reviewer__email')
    readonly_fields = ('id', 'created_at', 'updated_at')
    raw_id_fields = ('analyst', 'reviewer')

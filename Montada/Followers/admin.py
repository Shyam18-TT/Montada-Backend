from django.contrib import admin
from .models import Follow, Mute


@admin.register(Follow)
class FollowAdmin(admin.ModelAdmin):
    list_display = ("id", "follower", "followed", "status", "is_active", "requested_at")
    list_filter = ("status", "is_active")
    search_fields = ("follower__email", "followed__email")


@admin.register(Mute)
class MuteAdmin(admin.ModelAdmin):
    list_display = ("id", "muter", "muted", "muted_at")
    search_fields = ("muter__email", "muted__email")

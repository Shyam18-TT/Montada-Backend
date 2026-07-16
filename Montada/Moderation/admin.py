from django.contrib import admin, messages
from django.utils import timezone

from .models import ModerationReport, UserBlock


@admin.action(description="Remove reported chat message content")
def remove_reported_content(modeladmin, request, queryset):
    removed = 0
    for report in queryset.filter(content_type="chat_message").exclude(content_id=""):
        try:
            from chat.models import ChatMessage

            updated = ChatMessage.objects.filter(pk=report.content_id).update(is_deleted=True)
        except (ValueError, TypeError):
            updated = 0
        if updated:
            removed += updated
            report.status = ModerationReport.Status.CONTENT_REMOVED
            report.reviewed_at = timezone.now()
            report.reviewed_by = request.user
            report.save(update_fields=["status", "reviewed_at", "reviewed_by"])
    messages.info(request, f"Removed {removed} chat message(s).")


@admin.action(description="Ban reported users")
def ban_reported_users(modeladmin, request, queryset):
    banned = 0
    for report in queryset.select_related("reported_user"):
        user = report.reported_user
        if not user:
            continue
        if user.is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])
            banned += 1
        report.status = ModerationReport.Status.USER_BANNED
        report.reviewed_at = timezone.now()
        report.reviewed_by = request.user
        report.save(update_fields=["status", "reviewed_at", "reviewed_by"])
    messages.info(request, f"Banned {banned} user(s).")


@admin.register(ModerationReport)
class ModerationReportAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "content_type",
        "reason",
        "status",
        "reporter",
        "reported_user",
        "platform",
        "created_at",
    )
    list_filter = ("status", "reason", "created_at", "content_type", "platform")
    search_fields = (
        "reported_user_id_raw",
        "content_id",
        "content_excerpt",
        "details",
        "reporter__email",
        "reported_user__email",
    )
    readonly_fields = ("created_at",)
    actions = [remove_reported_content, ban_reported_users]


@admin.register(UserBlock)
class UserBlockAdmin(admin.ModelAdmin):
    list_display = ("id", "blocker", "blocked", "created_at")
    list_filter = ("created_at",)
    search_fields = ("blocker__email", "blocked__email")


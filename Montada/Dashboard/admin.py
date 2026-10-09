from django.contrib import admin

from .models import PollQuestion, PollOption, PollResponse, PollSession, PollSessionOption
from .polls import ensure_current_session


class PollOptionInline(admin.TabularInline):
    model = PollOption
    extra = 1
    fields = ("option_text", "is_deleted")
    can_delete = False  # options are soft-deleted (is_deleted) so voting history is preserved


@admin.register(PollQuestion)
class PollQuestionAdmin(admin.ModelAdmin):
    list_display = ("question_text", "question_type", "order", "is_active", "is_deleted", "duration_hours")
    list_filter = ("question_type", "is_active", "is_deleted")
    search_fields = ("question_text",)
    ordering = ("order",)
    readonly_fields = ("current_session", "created_by", "created_at", "updated_at", "deleted_at")
    inlines = [PollOptionInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Polls created here need a voting session like those created through the API.
        ensure_current_session(form.instance, request.user)

    def has_delete_permission(self, request, obj=None):
        return False  # use the admin API (DELETE /api/admin/polls/<id>/), which keeps history


class PollSessionOptionInline(admin.TabularInline):
    model = PollSessionOption
    extra = 0
    can_delete = False
    readonly_fields = ("option", "option_text", "order")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PollSession)
class PollSessionAdmin(admin.ModelAdmin):
    list_display = ("question", "session_number", "status", "start_at", "end_at", "created_reason", "closed_reason")
    list_filter = ("status", "created_reason", "closed_reason")
    search_fields = ("question__question_text",)
    raw_id_fields = ("question", "created_by", "closed_by")
    inlines = [PollSessionOptionInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PollOption)
class PollOptionAdmin(admin.ModelAdmin):
    list_display = ("option_text", "question", "is_deleted")
    list_filter = ("is_deleted",)
    search_fields = ("option_text",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PollResponse)
class PollResponseAdmin(admin.ModelAdmin):
    list_display = ("user", "question", "session", "option", "voted_at")
    list_filter = ("voted_at",)
    search_fields = ("user__email",)
    raw_id_fields = ("user", "question", "session", "option")
    date_hierarchy = "voted_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

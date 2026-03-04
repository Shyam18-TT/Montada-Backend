from django.contrib import admin

from .models import PollQuestion, PollOption, PollResponse


class PollOptionInline(admin.TabularInline):
    model = PollOption
    extra = 1


@admin.register(PollQuestion)
class PollQuestionAdmin(admin.ModelAdmin):
    list_display = ("question_text", "question_type", "order")
    list_filter = ("question_type",)
    search_fields = ("question_text",)
    ordering = ("order",)
    inlines = [PollOptionInline]


@admin.register(PollOption)
class PollOptionAdmin(admin.ModelAdmin):
    list_display = ("option_text", "question")
    list_filter = ("question",)
    search_fields = ("option_text",)


@admin.register(PollResponse)
class PollResponseAdmin(admin.ModelAdmin):
    list_display = ("user", "question", "option", "voted_at")
    list_filter = ("voted_at",)
    search_fields = ("user__email",)
    raw_id_fields = ("user", "question", "option")
    date_hierarchy = "voted_at"

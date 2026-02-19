from django.contrib import admin
from django.utils import timezone

from .models import Poll, PollQuestion, PollOption, PollResponse


class PollOptionInline(admin.TabularInline):
    model = PollOption
    extra = 1


class PollQuestionInline(admin.TabularInline):
    model = PollQuestion
    extra = 0
    show_change_link = True


@admin.register(Poll)
class PollAdmin(admin.ModelAdmin):
    list_display = ("title", "is_active", "start_date", "end_date", "allow_multiple_answers", "created_at")
    list_filter = ("is_active", "allow_multiple_answers")
    search_fields = ("title", "description")
    inlines = [PollQuestionInline]
    date_hierarchy = "created_at"


@admin.register(PollQuestion)
class PollQuestionAdmin(admin.ModelAdmin):
    list_display = ("question_text", "poll", "question_type", "order")
    list_filter = ("question_type", "poll")
    search_fields = ("question_text",)
    ordering = ("poll", "order")
    inlines = [PollOptionInline]


@admin.register(PollOption)
class PollOptionAdmin(admin.ModelAdmin):
    list_display = ("option_text", "question")
    list_filter = ("question__poll",)
    search_fields = ("option_text",)


@admin.register(PollResponse)
class PollResponseAdmin(admin.ModelAdmin):
    list_display = ("user", "poll", "question", "option", "voted_at")
    list_filter = ("poll", "voted_at")
    search_fields = ("user__email",)
    raw_id_fields = ("user", "poll", "question", "option")
    date_hierarchy = "voted_at"

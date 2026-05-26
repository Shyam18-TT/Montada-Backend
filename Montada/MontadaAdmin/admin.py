from django.contrib import admin

from .models import EconomicCalendarGlobalReminderSettings


@admin.register(EconomicCalendarGlobalReminderSettings)
class EconomicCalendarGlobalReminderSettingsAdmin(admin.ModelAdmin):
    list_display = ("id", "is_enabled", "minutes_before", "updated_at")
    fields = ("is_enabled", "minutes_before", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not EconomicCalendarGlobalReminderSettings.objects.exists()

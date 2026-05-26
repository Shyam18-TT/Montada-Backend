from django.db import models


class EconomicCalendarGlobalReminderSettings(models.Model):
    """
    Singleton (pk=1): admin-configured advance reminder for all economic calendar events.

    When enabled, every user receives a push/in-app notification N minutes before each
  event's release_date. Event-time notifications are handled separately and are not
    affected by this setting.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    is_enabled = models.BooleanField(
        default=True,
        help_text="When false, no global advance reminders are sent.",
    )
    minutes_before = models.PositiveSmallIntegerField(
        default=15,
        help_text="Notify all users this many minutes before each economic event.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Economic calendar global reminder settings"
        verbose_name_plural = "Economic calendar global reminder settings"
        db_table = "montada_admin_economic_calendar_reminder_settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        status = "on" if self.is_enabled else "off"
        return f"Global economic reminders ({status}, {self.minutes_before} min before)"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={"is_enabled": True, "minutes_before": 15},
        )
        return obj

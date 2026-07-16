from django.apps import AppConfig


class ModerationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "Moderation"

    def ready(self):
        import Moderation.signals  # noqa: F401

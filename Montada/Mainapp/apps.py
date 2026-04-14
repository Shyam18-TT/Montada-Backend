import logging

from django.apps import AppConfig
from django.conf import settings


class MainappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Mainapp'

    def ready(self):
        if not getattr(settings, "ENABLE_DB_TIMING_LOGGING", True):
            return

        try:
            from Mainapp.db_timing import setup_db_timing_logging

            setup_db_timing_logging()
        except Exception:
            logging.getLogger("db.timing").exception(
                "DB timing logging setup failed during app startup."
            )

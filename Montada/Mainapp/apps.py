from django.apps import AppConfig


class MainappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Mainapp'

    # def ready(self):
    #     from .db_timing import setup_db_timing_logging

    #     setup_db_timing_logging()

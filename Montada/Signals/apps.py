from django.apps import AppConfig


class SignalsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Signals'

    def ready(self):
        from Signals.price_move_config import connect_invalidation_signals

        connect_invalidation_signals()

"""Model signals for the Mainapp app (registered in MainappConfig.ready)."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import User


@receiver(post_save, sender=User, dispatch_uid="mainapp_drop_push_devices_when_user_disabled")
def drop_push_devices_when_user_disabled(sender, instance, **kwargs):
    """
    A banned, deactivated or soft-deleted account must stop receiving pushes at once.
    (Sending also skips inactive users; this removes the rows so nothing lingers.)
    """
    if instance.is_active and not getattr(instance, "is_soft_deleted", False):
        return
    from .push_devices import remove_devices_for_user

    remove_devices_for_user(instance, "account_disabled")

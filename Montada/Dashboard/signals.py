from django.db.models.signals import post_save
from django.dispatch import receiver

from Mainapp.models import UserNotification

from .realtime import broadcast_notification


@receiver(post_save, sender=UserNotification)
def broadcast_user_notification(sender, instance, created, **kwargs):
    event_name = "created" if created else "updated"
    broadcast_notification(instance, event_name=event_name)

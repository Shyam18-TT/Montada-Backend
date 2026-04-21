from django.urls import re_path

from .consumers import NotificationConsumer


websocket_urlpatterns = [
    re_path(r"ws/dashboard/notifications/$", NotificationConsumer.as_asgi()),
    re_path(r"dashboard/notifications/$", NotificationConsumer.as_asgi()),
]

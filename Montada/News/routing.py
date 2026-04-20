from django.urls import re_path

from .consumers import LiveNewsConsumer


websocket_urlpatterns = [
    re_path(r"ws/news/live/$", LiveNewsConsumer.as_asgi()),
    # re_path(r"news/live/$", LiveNewsConsumer.as_asgi()),
]

from django.urls import re_path

from .consumers import MarketDataConsumer


websocket_urlpatterns = [
    re_path(r"ws/signals/market-data/$", MarketDataConsumer.as_asgi()),
]

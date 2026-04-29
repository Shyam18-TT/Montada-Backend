"""
ASGI config for Montada project.
Supports HTTP and WebSocket (live chat and live news). Uses Redis as channel layer broker.
"""
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Montada.settings")

# Load Django ASGI application for HTTP before importing app code that needs ORM
django_asgi_app = get_asgi_application()

from chat.routing import websocket_urlpatterns as chat_websocket_urlpatterns
from Dashboard.routing import websocket_urlpatterns as dashboard_websocket_urlpatterns
from News.routing import websocket_urlpatterns as news_websocket_urlpatterns
from Signals.routing import websocket_urlpatterns as signals_websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            URLRouter(
                chat_websocket_urlpatterns
                + dashboard_websocket_urlpatterns
                + news_websocket_urlpatterns
                + signals_websocket_urlpatterns
            ),
        ),
    }
)

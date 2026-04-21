import json
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model

from .realtime import get_notification_group_name


logger = logging.getLogger(__name__)
User = get_user_model()


def _get_user_from_token(token):
    if not token:
        return None
    try:
        from rest_framework_simplejwt.exceptions import InvalidToken
        from rest_framework_simplejwt.tokens import AccessToken

        access = AccessToken(token)
        user_id = access.get("user_id")
        if not user_id:
            return None
        return User.objects.get(pk=user_id)
    except (InvalidToken, User.DoesNotExist, Exception):
        return None


@database_sync_to_async
def _resolve_notification_user(token):
    return _get_user_from_token(token)


class NotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self._joined_group = False

        query = parse_qs(self.scope.get("query_string", b"").decode())
        tokens = query.get("token", [])
        token = tokens[0] if tokens else None

        user = await _resolve_notification_user(token)
        if not user:
            await self.close(code=4401)
            return

        self.scope["user"] = user
        self.room_group_name = get_notification_group_name(user.id)
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        self._joined_group = True
        await self.accept()
        await self.send(
            text_data=json.dumps(
                {
                    "type": "notifications.connected",
                    "message": "Notification stream connected.",
                }
            )
        )

    async def disconnect(self, close_code):
        if getattr(self, "_joined_group", False):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
            if data.get("type") == "ping":
                await self.send(text_data=json.dumps({"type": "pong"}))
        except (json.JSONDecodeError, Exception):
            logger.debug("Ignoring invalid dashboard notification websocket payload.")

    async def dashboard_notification(self, event):
        notification = event.get("notification")
        if notification is not None:
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "notifications.update",
                        "event": event.get("event", "created"),
                        "notification": notification,
                    }
                )
            )

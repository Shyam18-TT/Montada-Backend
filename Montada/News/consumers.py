import json
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model

from News.live_news_service import LIVE_NEWS_GROUP_NAME
from Subscriptions.access import check_active_subscription


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
def _resolve_live_news_user(token):
    user = _get_user_from_token(token)
    if not user:
        return None, False
    return user, check_active_subscription(user) is None


class LiveNewsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_group_name = LIVE_NEWS_GROUP_NAME
        self._joined_group = False

        query = parse_qs(self.scope.get("query_string", b"").decode())
        tokens = query.get("token", [])
        token = tokens[0] if tokens else None

        user, has_access = await _resolve_live_news_user(token)
        if not user:
            await self.close(code=4401)
            return
        if not has_access:
            await self.close(code=4403)
            return

        self.scope["user"] = user
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        self._joined_group = True
        await self.accept()
        await self.send(
            text_data=json.dumps(
                {
                    "type": "news.connected",
                    "message": "Live news stream connected.",
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
            logger.debug("Ignoring invalid live news websocket payload.")

    async def news_update(self, event):
        item = event.get("item")
        if item is not None:
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "news.update",
                        "event": event.get("event", "updated"),
                        "language": item.get("language"),
                        "item": item,
                    }
                )
            )

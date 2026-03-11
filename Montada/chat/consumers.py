"""
WebSocket consumer for live chat. Clients connect to a conversation room
and receive new messages in real time when someone sends via REST (or via WS).
"""
import json
import logging
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)
User = get_user_model()


def _get_user_from_token(token):
    """Validate JWT and return User or None."""
    if not token:
        return None
    try:
        from rest_framework_simplejwt.tokens import AccessToken
        from rest_framework_simplejwt.exceptions import InvalidToken
        access = AccessToken(token)
        user_id = access.get("user_id")
        if not user_id:
            return None
        return User.objects.get(pk=user_id)
    except (InvalidToken, User.DoesNotExist, Exception):
        return None


@database_sync_to_async
def _check_participant(conversation_id, user):
    if not user:
        return False
    from .models import ConversationParticipant
    return ConversationParticipant.objects.filter(
        conversation_id=conversation_id,
        user=user,
    ).exists()


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket connection to a conversation room.
    Connect: ws://host/ws/chat/<conversation_id>/?token=<access_token>
    Server sends: { "type": "chat.message", "message": { ... } } when a new message is broadcast.
    """

    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.room_group_name = f"chat_conversation_{self.conversation_id}"
        self._joined_group = False

        # Auth from query string ?token=...
        query = parse_qs(self.scope.get("query_string", b"").decode())
        tokens = query.get("token", [])
        token = tokens[0] if tokens else None
        user = await database_sync_to_async(_get_user_from_token)(token)
        if not user:
            await self.close(code=4401)
            return

        is_participant = await _check_participant(self.conversation_id, user)
        if not is_participant:
            await self.close(code=4403)
            return

        self.scope["user"] = user
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        self._joined_group = True
        await self.accept()

    async def disconnect(self, close_code):
        if getattr(self, "_joined_group", False):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        """Optional: client can send JSON to ping or trigger actions. We only forward broadcasts from REST."""
        if not text_data:
            return
        try:
            data = json.loads(text_data)
            if data.get("type") == "ping":
                await self.send(text_data=json.dumps({"type": "pong"}))
        except (json.JSONDecodeError, Exception):
            pass

    async def chat_message(self, event):
        """Handle broadcast from channel_layer: send message payload to WebSocket."""
        message = event.get("message")
        if message is not None:
            await self.send(text_data=json.dumps({"type": "chat.message", "message": message}))

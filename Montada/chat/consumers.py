"""
WebSocket consumer for live chat. Clients connect to a conversation room
and receive new messages in real time when someone sends via REST (or via WS).
"""
import asyncio
import json
import logging
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)
User = get_user_model()
AUTH_LOOKUP_TIMEOUT_SECONDS = 5


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


@database_sync_to_async
def _check_conversation_not_blocked(conversation_id, user):
    if not user:
        return False
    try:
        from Moderation.models import users_are_blocked
        from .models import Conversation

        conversation = Conversation.objects.prefetch_related("participants").get(pk=conversation_id)
        for participant in conversation.participants.exclude(pk=user.pk):
            if users_are_blocked(user, participant):
                return False
        return True
    except Exception:
        logger.exception(
            "Failed to evaluate chat websocket block status for conversation_id=%s user_id=%s.",
            conversation_id,
            getattr(user, "id", None),
        )
        return False


def get_chat_notification_group_name(user_id):
    return f"chat_notifications_{user_id}"


async def _resolve_user_for_socket(scope, socket_name):
    query = parse_qs(scope.get("query_string", b"").decode())
    tokens = query.get("token", [])
    token = tokens[0] if tokens else None
    if not token:
        logger.warning("%s websocket rejected because token query param is missing.", socket_name)
        return None

    try:
        return await asyncio.wait_for(
            database_sync_to_async(_get_user_from_token)(token),
            timeout=AUTH_LOOKUP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("%s websocket auth lookup timed out.", socket_name)
        return None


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

        user = await _resolve_user_for_socket(self.scope, "chat")
        if not user:
            await self.close(code=4401)
            return

        try:
            is_participant = await asyncio.wait_for(
                _check_participant(self.conversation_id, user),
                timeout=AUTH_LOOKUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "chat websocket participant lookup timed out for conversation_id=%s user_id=%s.",
                self.conversation_id,
                getattr(user, "id", None),
            )
            await self.close(code=4403)
            return
        if not is_participant:
            await self.close(code=4403)
            return
        try:
            is_not_blocked = await asyncio.wait_for(
                _check_conversation_not_blocked(self.conversation_id, user),
                timeout=AUTH_LOOKUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "chat websocket block lookup timed out for conversation_id=%s user_id=%s.",
                self.conversation_id,
                getattr(user, "id", None),
            )
            await self.close(code=4403)
            return
        if not is_not_blocked:
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


class ChatNotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self._joined_group = False

        user = await _resolve_user_for_socket(self.scope, "chat.notifications")
        if not user:
            await self.close(code=4401)
            return

        self.scope["user"] = user
        self.room_group_name = get_chat_notification_group_name(user.id)
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        self._joined_group = True
        await self.accept()
        await self.send(
            text_data=json.dumps(
                {
                    "type": "chat.notifications.connected",
                    "message": "Chat notification stream connected.",
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
            logger.debug("Ignoring invalid chat notification websocket payload.")

    async def chat_notification(self, event):
        notification = event.get("notification")
        if notification is not None:
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "chat.notification",
                        "event": event.get("event", "message.created"),
                        "notification": notification,
                    }
                )
            )

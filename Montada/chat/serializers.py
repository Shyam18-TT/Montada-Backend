"""
Serializers for chat API.
"""
from rest_framework import serializers
from django.contrib.auth import get_user_model

from .models import Conversation, ChatMessage

User = get_user_model()


class UserChatSerializer(serializers.ModelSerializer):
    """Minimal user info for chat UI."""
    class Meta:
        model = User
        fields = ("id", "username", "name", "profile_picture")
        read_only_fields = fields


class ChatMessageSerializer(serializers.ModelSerializer):
    """Message list/detail; sender as nested object."""
    sender = UserChatSerializer(read_only=True)
    sender_id = serializers.UUIDField(write_only=True, required=False)

    class Meta:
        model = ChatMessage
        fields = (
            "id",
            "conversation",
            "sender",
            "sender_id",
            "content",
            "created_at",
            "read_at",
            "is_deleted",
        )
        read_only_fields = ("id", "sender", "created_at", "read_at", "is_deleted")


class ChatMessageCreateSerializer(serializers.ModelSerializer):
    """Create message (content only; sender from request.user)."""
    class Meta:
        model = ChatMessage
        fields = ("content",)
        extra_kwargs = {"content": {"allow_blank": False, "trim_whitespace": True}}


class AnalystBroadcastSerializer(serializers.Serializer):
    """Analyst mass message: same text to all followers or all active plan subscribers."""
    content = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True)
    audience = serializers.ChoiceField(
        choices=[("followers", "followers"), ("subscribers", "subscribers")],
        required=False,
        help_text="Can also be sent as query param ?audience=followers|subscribers",
    )


class ConversationListSerializer(serializers.ModelSerializer):
    """Conversation in list: id, participants, last_message, unread_count, updated_at."""
    participants = UserChatSerializer(many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = (
            "id",
            "participants",
            "last_message",
            "unread_count",
            "updated_at",
            "created_at",
        )

    def get_last_message(self, obj):
        last = obj.messages.filter(is_deleted=False).order_by("-created_at").first()
        if not last:
            return None
        return {
            "id": str(last.id),
            "sender_id": str(last.sender_id),
            "content": last.content[:100] + ("..." if len(last.content) > 100 else ""),
            "created_at": last.created_at,
        }

    def get_unread_count(self, obj):
        request = self.context.get("request")
        if not request or not request.user:
            return 0
        return obj.messages.filter(
            is_deleted=False,
            read_at__isnull=True,
        ).exclude(sender=request.user).count()


class ConversationDetailSerializer(serializers.ModelSerializer):
    """Single conversation with participants only."""
    participants = UserChatSerializer(many=True, read_only=True)

    class Meta:
        model = Conversation
        fields = ("id", "participants", "created_at", "updated_at")

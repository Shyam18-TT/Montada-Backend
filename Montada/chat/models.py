"""
Live chat models: conversations (1-1 or group) and messages.
"""
import uuid
from django.conf import settings
from django.db import models


class Conversation(models.Model):
    """
    A chat conversation. For 1-1 chats there are exactly 2 participants;
    for group chats (future) there can be more. Use get_or_create_direct()
    to find or create a 1-1 conversation between two users.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="ConversationParticipant",
        related_name="chat_conversations",
        blank=True,
    )

    class Meta:
        ordering = ["-updated_at"]
        db_table = "chat_conversation"

    def __str__(self):
        return f"Conversation {self.id}"

    @classmethod
    def get_or_create_direct(cls, user1, user2):
        """
        Get or create a 1-1 conversation between user1 and user2.
        If a conversation already exists for this pair, return it (same conversation_id).
        Returns (conversation, created).
        """
        if user1.pk == user2.pk:
            raise ValueError("Cannot create conversation with yourself.")
        # Lookup via through table: conversation IDs where user1 and user2 are both participants
        conv_ids_user1 = set(
            ConversationParticipant.objects.filter(user=user1).values_list("conversation_id", flat=True)
        )
        conv_ids_user2 = set(
            ConversationParticipant.objects.filter(user=user2).values_list("conversation_id", flat=True)
        )
        common_ids = conv_ids_user1 & conv_ids_user2
        if common_ids:
            # Only 1-1 conversations (exactly 2 participants); return oldest for consistency
            from django.db.models import Count
            conv = (
                cls.objects.filter(id__in=common_ids)
                .annotate(c=Count("participant_links"))
                .filter(c=2)
                .order_by("created_at")
                .first()
            )
            if conv:
                return conv, False
        conv = cls.objects.create()
        ConversationParticipant.objects.create(conversation=conv, user=user1)
        ConversationParticipant.objects.create(conversation=conv, user=user2)
        return conv, True


class ConversationParticipant(models.Model):
    """Links a user to a conversation (for M2M with ordering/extra data)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="participant_links",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_participant_links",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("conversation", "user")]
        db_table = "chat_conversation_participant"

    def __str__(self):
        return f"{self.user} in {self.conversation_id}"


class ChatMessage(models.Model):
    """A single message in a conversation."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_messages_sent",
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]
        db_table = "chat_message"
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
        ]

    def __str__(self):
        return f"{self.sender} @ {self.created_at}"

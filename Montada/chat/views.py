"""
REST API for live chat: conversations and messages.
Sending a message also broadcasts it via the Channel layer (Redis) to connected WebSocket clients.
"""
from django.utils import timezone
from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model

from .models import Conversation, ConversationParticipant, ChatMessage
from .serializers import (
    ConversationListSerializer,
    ConversationDetailSerializer,
    ChatMessageSerializer,
    ChatMessageCreateSerializer,
)

User = get_user_model()


def _user_can_access_conversation(user, conversation):
    return ConversationParticipant.objects.filter(
        conversation=conversation,
        user=user,
    ).exists()


def _broadcast_new_message(conversation_id, message_payload):
    """Send message payload to the conversation's WebSocket group via Redis."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        group_name = f"chat_conversation_{conversation_id}"
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "chat.message",
                "message": message_payload,
            },
        )
    except Exception:
        pass


class ConversationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class ConversationListCreateView(APIView):
    """
    GET: List conversations for the current user (with last message and unread count).
    POST: Get or create a direct conversation with another user.
         Body: { "other_user_id": "<uuid>" }
    """
    permission_classes = [IsAuthenticated]
    pagination_class = ConversationPagination

    def get(self, request):
        convos = (
            Conversation.objects.filter(participants=request.user)
            .prefetch_related("participants", "participant_links")
            .order_by("-updated_at")
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(convos, request)
        serializer = ConversationListSerializer(
            page or convos,
            many=True,
            context={"request": request},
        )
        if page is not None:
            return paginator.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def post(self, request):
        other_id = request.data.get("other_user_id")
        if not other_id:
            return Response(
                {"error": "other_user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            other = User.objects.get(pk=other_id)
        except User.DoesNotExist:
            return Response(
                {"error": "User not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if other.pk == request.user.pk:
            return Response(
                {"error": "Cannot start a conversation with yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            conv, created = Conversation.get_or_create_direct(request.user, other)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        serializer = ConversationDetailSerializer(conv)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ConversationDetailView(APIView):
    """GET: Retrieve a conversation by id (must be a participant)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        conv = get_object_or_404(Conversation, pk=conversation_id)
        if not _user_can_access_conversation(request.user, conv):
            return Response(
                {"error": "You are not part of this conversation."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = ConversationDetailSerializer(conv)
        return Response(serializer.data)


class MessageListCreateView(APIView):
    """
    GET: List messages in a conversation (paginated, newest first for display reverse).
    POST: Send a new message; broadcasts to WebSocket group.
    """
    permission_classes = [IsAuthenticated]
    pagination_class = ConversationPagination

    def get_conversation(self, conversation_id):
        conv = get_object_or_404(Conversation, pk=conversation_id)
        if not _user_can_access_conversation(self.request.user, conv):
            return None
        return conv

    def get(self, request, conversation_id):
        conv = self.get_conversation(conversation_id)
        if conv is None:
            return Response(
                {"error": "You are not part of this conversation."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Chronological order (oldest first) so the list is natural chat order
        messages = (
            conv.messages.filter(is_deleted=False)
            .select_related("sender")
            .order_by("created_at")
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(messages, request)
        serializer = ChatMessageSerializer(
            page if page is not None else messages,
            many=True,
            context={"request": request},
        )
        if page is not None:
            return paginator.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def post(self, request, conversation_id):
        conv = self.get_conversation(conversation_id)
        if conv is None:
            return Response(
                {"error": "You are not part of this conversation."},
                status=status.HTTP_403_FORBIDDEN,
            )
        ser = ChatMessageCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        msg = ChatMessage.objects.create(
            conversation=conv,
            sender=request.user,
            content=ser.validated_data["content"].strip(),
        )
        conv.updated_at = timezone.now()
        conv.save(update_fields=["updated_at"])
        payload = ChatMessageSerializer(msg).data
        _broadcast_new_message(str(conv.id), payload)
        return Response(payload, status=status.HTTP_201_CREATED)


class MessageMarkReadView(APIView):
    """POST: Mark messages in a conversation as read (read_at = now) for the current user."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conv = get_object_or_404(Conversation, pk=conversation_id)
        if not _user_can_access_conversation(request.user, conv):
            return Response(
                {"error": "You are not part of this conversation."},
                status=status.HTTP_403_FORBIDDEN,
            )
        now = timezone.now()
        updated = conv.messages.filter(
            is_deleted=False,
            read_at__isnull=True,
        ).exclude(sender=request.user).update(read_at=now)
        return Response({"marked_read": updated})

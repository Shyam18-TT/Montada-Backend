"""
Serializers for chat API.
"""
from rest_framework import serializers
from django.contrib.auth import get_user_model

from .models import Conversation, ConversationParticipant, ChatMessage

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


def _icon_url(conversation, request):
    if not conversation.icon:
        return None
    url = conversation.icon.url
    return request.build_absolute_uri(url) if request else url


class ConversationListSerializer(serializers.ModelSerializer):
    """Conversation in list: id, participants, last_message, unread_count, updated_at."""
    participants = UserChatSerializer(many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    # Additive group fields (is_group=False and empty values for 1-1 chats).
    icon = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = (
            "id",
            "participants",
            "last_message",
            "unread_count",
            "updated_at",
            "created_at",
            "is_group",
            "name",
            "icon",
            "member_count",
            "only_admins_can_send",
        )

    def get_icon(self, obj):
        return _icon_url(obj, self.context.get("request"))

    def get_member_count(self, obj):
        # participant_links is prefetched by the list view.
        return len(obj.participant_links.all())

    def get_last_message(self, obj):
        # Prefer the annotated fields the view attaches via a correlated
        # subquery (no extra query here). Falls back to a live lookup so
        # this serializer still works if used somewhere without them.
        if hasattr(obj, "last_message_id"):
            last_id = obj.last_message_id
            if not last_id:
                return None
            content = obj.last_message_content or ""
            return {
                "id": str(last_id),
                "sender_id": str(obj.last_message_sender_id) if obj.last_message_sender_id else None,
                "content": content[:100] + ("..." if len(content) > 100 else ""),
                "created_at": obj.last_message_created_at,
            }
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
        if hasattr(obj, "group_unread_count"):
            return obj.group_unread_count
        if hasattr(obj, "unread_count_annotated"):
            return obj.unread_count_annotated or 0
        request = self.context.get("request")
        if not request or not request.user:
            return 0
        return obj.messages.filter(
            is_deleted=False,
            read_at__isnull=True,
        ).exclude(sender=request.user).count()


class ConversationDetailSerializer(serializers.ModelSerializer):
    """Single conversation with participants (plus group info for group chats)."""
    participants = UserChatSerializer(many=True, read_only=True)
    icon = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = (
            "id", "participants", "created_at", "updated_at",
            "is_group", "name", "description", "icon", "only_admins_can_send",
        )

    def get_icon(self, obj):
        return _icon_url(obj, self.context.get("request"))


class GroupMemberSerializer(serializers.ModelSerializer):
    """A group member: chat user fields plus their role."""
    id = serializers.UUIDField(source="user.id", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    name = serializers.CharField(source="user.name", read_only=True)
    profile_picture = serializers.ImageField(source="user.profile_picture", read_only=True)
    user_type = serializers.CharField(source="user.user_type", read_only=True)

    class Meta:
        model = ConversationParticipant
        fields = ("id", "username", "name", "profile_picture", "user_type", "role", "joined_at")
        read_only_fields = fields


class GroupDetailSerializer(serializers.ModelSerializer):
    """Full group view for members: info, settings, members and the caller's own state."""
    icon = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    created_by = UserChatSerializer(read_only=True)
    my_role = serializers.SerializerMethodField()
    is_muted = serializers.SerializerMethodField()
    can_send = serializers.SerializerMethodField()
    can_edit_info = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = (
            "id", "name", "description", "icon", "created_by", "created_at", "updated_at",
            "is_group", "only_admins_can_send", "only_admins_can_edit_info",
            "member_count", "members", "my_role", "is_muted", "can_send", "can_edit_info",
        )

    def _me(self, obj):
        request = self.context.get("request")
        if not request:
            return None
        for link in obj.participant_links.all():
            if link.user_id == request.user.pk:
                return link
        return None

    def get_icon(self, obj):
        return _icon_url(obj, self.context.get("request"))

    def get_members(self, obj):
        links = sorted(
            obj.participant_links.all(),
            key=lambda l: (l.role != ConversationParticipant.Role.ADMIN, l.joined_at),
        )
        return GroupMemberSerializer(links, many=True, context=self.context).data

    def get_member_count(self, obj):
        return len(obj.participant_links.all())

    def get_my_role(self, obj):
        me = self._me(obj)
        return me.role if me else None

    def get_is_muted(self, obj):
        me = self._me(obj)
        return bool(me and me.is_muted)

    def get_can_send(self, obj):
        from .groups import can_send_message
        return can_send_message(obj, self._me(obj))

    def get_can_edit_info(self, obj):
        from .groups import can_edit_info
        return can_edit_info(obj, self._me(obj))


def _member_ids_from(data):
    # multipart sends repeated keys (member_ids=a&member_ids=b); QueryDict -> plain dict.
    if hasattr(data, "getlist"):
        plain = {k: data.get(k) for k in data.keys() if k != "member_ids"}
        plain["member_ids"] = data.getlist("member_ids")
        return plain
    return data


class GroupCreateSerializer(serializers.Serializer):
    """Analyst creates a group. member_ids must be followers / followed users of the analyst."""
    name = serializers.CharField(max_length=100, trim_whitespace=True)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    icon = serializers.ImageField(required=False, allow_null=True)
    member_ids = serializers.ListField(
        child=serializers.CharField(), allow_empty=False, max_length=255
    )
    only_admins_can_send = serializers.BooleanField(required=False, default=False)
    only_admins_can_edit_info = serializers.BooleanField(required=False, default=True)

    def to_internal_value(self, data):
        return super().to_internal_value(_member_ids_from(data))


class GroupUpdateSerializer(serializers.Serializer):
    """PATCH: every field optional. Settings fields are admin-only (checked in the view)."""
    name = serializers.CharField(max_length=100, required=False, trim_whitespace=True)
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    icon = serializers.ImageField(required=False, allow_null=True)
    only_admins_can_send = serializers.BooleanField(required=False)
    only_admins_can_edit_info = serializers.BooleanField(required=False)


class GroupMembersSerializer(serializers.Serializer):
    member_ids = serializers.ListField(child=serializers.CharField(), allow_empty=False, max_length=255)

    def to_internal_value(self, data):
        return super().to_internal_value(_member_ids_from(data))

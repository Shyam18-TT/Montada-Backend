"""
Group chat rules: who an analyst may add, membership changes and live group events.

Views stay thin; everything that decides "may this user do that in a group" lives here.
"""
import json
import logging
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Conversation, ConversationParticipant

logger = logging.getLogger(__name__)
User = get_user_model()

MAX_GROUP_MEMBERS = 256


def group_event_name(conversation_id):
    return f"chat_conversation_{conversation_id}"


def eligible_member_queryset(analyst):
    """
    Users an analyst may put in a group: accepted, active follow in either direction
    (they follow the analyst, or the analyst follows them), excluding the analyst,
    inactive/deleted users and anyone with a block relationship with the analyst.
    """
    from Followers.models import Follow

    accepted = Follow.objects.filter(status=Follow.Status.ACCEPTED, is_active=True)
    followers = accepted.filter(followed=analyst).values("follower_id")
    following = accepted.filter(follower=analyst).values("followed_id")

    qs = (
        User.objects.filter(Q(pk__in=followers) | Q(pk__in=following))
        .exclude(pk=analyst.pk)
        .filter(is_active=True, is_soft_deleted=False)
    )
    try:
        from Moderation.models import UserBlock

        blocked = set()
        for blocker_id, blocked_id in UserBlock.objects.filter(
            Q(blocker_id=analyst.pk) | Q(blocked_id=analyst.pk)
        ).values_list("blocker_id", "blocked_id"):
            blocked.add(blocked_id if blocker_id == analyst.pk else blocker_id)
        if blocked:
            qs = qs.exclude(pk__in=blocked)
    except Exception:
        logger.exception("Failed to load block relationships for analyst %s.", analyst.pk)
    return qs


def split_valid_member_ids(analyst, user_ids):
    """Return (valid_users, invalid_ids) for the requested ids."""
    wanted = set()
    invalid = set()
    for raw in user_ids:
        try:
            wanted.add(uuid.UUID(str(raw).strip()))
        except ValueError:
            invalid.add(str(raw))
    wanted.discard(analyst.pk)
    valid = list(eligible_member_queryset(analyst).filter(pk__in=wanted))
    invalid.update(str(u) for u in wanted - {u.pk for u in valid})
    return valid, sorted(invalid)


@transaction.atomic
def create_group(analyst, name, members, description="", icon=None,
                 only_admins_can_send=False, only_admins_can_edit_info=True):
    conv = Conversation.objects.create(
        is_group=True,
        name=name,
        description=description,
        icon=icon,
        created_by=analyst,
        only_admins_can_send=only_admins_can_send,
        only_admins_can_edit_info=only_admins_can_edit_info,
    )
    ConversationParticipant.objects.create(
        conversation=conv, user=analyst, role=ConversationParticipant.Role.ADMIN
    )
    ConversationParticipant.objects.bulk_create(
        [ConversationParticipant(conversation=conv, user=u) for u in members]
    )
    return conv


def get_participant(conversation, user):
    return ConversationParticipant.objects.filter(conversation=conversation, user=user).first()


def can_send_message(conversation, participant):
    """participant: the sender's ConversationParticipant row, or None if not a member."""
    if participant is None:
        return False
    if not conversation.is_group:
        return True
    if not conversation.is_active:
        return False
    return participant.is_admin or not conversation.only_admins_can_send


def can_edit_info(conversation, participant):
    if participant is None or not conversation.is_active:
        return False
    return participant.is_admin or not conversation.only_admins_can_edit_info


def member_count(conversation):
    return conversation.participant_links.count()


def add_members(conversation, users):
    """Add users who are not yet members. Returns the users actually added."""
    existing = set(
        conversation.participant_links.values_list("user_id", flat=True)
    )
    fresh = [u for u in users if u.pk not in existing]
    ConversationParticipant.objects.bulk_create(
        [ConversationParticipant(conversation=conversation, user=u) for u in fresh]
    )
    return fresh


def remove_member(conversation, user):
    ConversationParticipant.objects.filter(conversation=conversation, user=user).delete()


def mark_group_read(conversation, user):
    ConversationParticipant.objects.filter(
        conversation=conversation, user=user
    ).update(last_read_at=timezone.now())


def group_unread_count(conversation, participant):
    """Messages from others newer than the member's last read (or since they joined)."""
    since = participant.last_read_at or participant.joined_at
    return (
        conversation.messages.filter(is_deleted=False, created_at__gt=since)
        .exclude(sender_id=participant.user_id)
        .count()
    )


def broadcast_group_event(conversation_id, event, **fields):
    """
    Tell connected sockets of a group something changed ("group.updated",
    "group.members_added", "group.member_removed", "group.closed").
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if not layer:
            return
        payload = json.loads(json.dumps(fields, default=str))
        async_to_sync(layer.group_send)(
            group_event_name(conversation_id),
            {
                "type": "chat.group_event",
                "event": event,
                "conversation_id": str(conversation_id),
                "data": payload,
            },
        )
    except Exception:
        logger.exception("Group event broadcast failed for conversation %s.", conversation_id)

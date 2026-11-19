"""
REST API for analyst-created group chats.

Analysts create a group from the people they follow / who follow them, choose whether
only admins can message and whether only admins can edit group info, and manage members.
Messaging inside a group reuses the conversation message endpoints (see views.py).
"""
import logging

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from Mainapp.audit import audit

from . import groups
from .models import Conversation, ConversationParticipant
from .serializers import (
    GroupCreateSerializer,
    GroupDetailSerializer,
    GroupMembersSerializer,
    GroupUpdateSerializer,
    UserChatSerializer,
)
from .views import ConversationPagination

logger = logging.getLogger(__name__)
User = get_user_model()

_NOT_ANALYST = {"error": "Only analysts can create and manage groups."}
_NOT_MEMBER = {"error": "You are not part of this group."}
_NOT_ADMIN = {"error": "Only group admins can do this."}
_CLOSED = {"error": "This group has been closed."}


def _is_analyst(user):
    return getattr(user, "user_type", None) == "analyst"


def _load_group(conversation_id):
    """Group conversation with the rows the detail serializer reads, or 404."""
    return get_object_or_404(
        Conversation.objects.prefetch_related("participant_links__user").select_related("created_by"),
        pk=conversation_id,
        is_group=True,
    )


def _detail_response(conv, request, http_status=status.HTTP_200_OK):
    # Re-fetch so members added/removed in this request show up in the prefetch.
    conv = _load_group(conv.pk)
    data = GroupDetailSerializer(conv, context={"request": request}).data
    data = dict(data)
    data["conversation_id"] = str(conv.id)
    return Response(data, status=http_status)


def _member_link(conv, user):
    for link in conv.participant_links.all():
        if link.user_id == user.pk:
            return link
    return None


def _notify_added_to_group(conv, adder, users):
    """Push "you were added to <group>" to newly added members (never blocks the request)."""
    try:
        from firebase import send_push_to_users_in_background

        users = [u for u in users if u.pk != adder.pk]
        if not users:
            return
        adder_name = getattr(adder, "name", None) or getattr(adder, "username", None) or "An analyst"
        group_name = conv.name or "a group"
        send_push_to_users_in_background(
            users=users,
            title=group_name,
            body=f"{adder_name} added you to the group {group_name}"[:140],
            data={
                # Same type as group messages so the app opens the group chat on tap.
                "type": "chat_message",
                "notification_id": f"group_added:{conv.pk}",
                "conversation_id": str(conv.pk),
                "sender_id": str(adder.pk),
                "sender_name": str(adder_name),
                "is_group": "true",
                "group_name": group_name,
                "event": "added_to_group",
            },
        )
    except Exception:
        logger.exception("Group added push failed for conversation %s.", conv.pk)


class GroupEligibleMembersView(APIView):
    """
    GET: users the analyst can add to a group (followers and followed users).
    Query: ?search=<text> (name/username), ?user_type=analyst|trader, ?page, ?page_size.
    Each row has `relation`: "follower" | "following" | "both".
    """
    permission_classes = [IsAuthenticated]
    pagination_class = ConversationPagination

    def get(self, request):
        if not _is_analyst(request.user):
            return Response(_NOT_ANALYST, status=status.HTTP_403_FORBIDDEN)

        from Followers.models import Follow

        qs = groups.eligible_member_queryset(request.user)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(username__icontains=search))
        user_type = (request.query_params.get("user_type") or "").strip().lower()
        if user_type:
            if user_type not in ("analyst", "trader"):
                return Response(
                    {"error": "user_type must be 'analyst' or 'trader'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(user_type=user_type)
        qs = qs.order_by("name", "username")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request)
        users = page if page is not None else list(qs)

        ids = [u.pk for u in users]
        accepted = Follow.objects.filter(status=Follow.Status.ACCEPTED, is_active=True)
        follower_ids = set(accepted.filter(followed=request.user, follower_id__in=ids)
                           .values_list("follower_id", flat=True))
        following_ids = set(accepted.filter(follower=request.user, followed_id__in=ids)
                            .values_list("followed_id", flat=True))

        rows = []
        for user in users:
            row = dict(UserChatSerializer(user, context={"request": request}).data)
            row["user_type"] = user.user_type
            is_follower, is_following = user.pk in follower_ids, user.pk in following_ids
            row["relation"] = "both" if is_follower and is_following else (
                "follower" if is_follower else "following"
            )
            rows.append(row)
        if page is not None:
            return paginator.get_paginated_response(rows)
        return Response(rows)


class GroupCreateView(APIView):
    """
    POST: analyst creates a group (JSON or multipart when sending an icon).
      {
        "name": "Gold Traders",
        "description": "optional",
        "member_ids": ["<uuid>", ...],          # followers / followed users of the analyst
        "only_admins_can_send": false,          # true = announcement-only group
        "only_admins_can_edit_info": true,      # false = members may edit name/description/icon
        "icon": <file, optional>
      }
    The creator becomes the group admin.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_analyst(request.user):
            return Response(_NOT_ANALYST, status=status.HTTP_403_FORBIDDEN)

        ser = GroupCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        members, invalid = groups.split_valid_member_ids(request.user, data["member_ids"])
        if invalid:
            return Response(
                {
                    "error": "Some users cannot be added. You can only add people who follow you "
                             "or whom you follow.",
                    "invalid_member_ids": invalid,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not members:
            return Response(
                {"error": "Select at least one member."}, status=status.HTTP_400_BAD_REQUEST
            )
        if len(members) + 1 > groups.MAX_GROUP_MEMBERS:
            return Response(
                {"error": f"A group can have at most {groups.MAX_GROUP_MEMBERS} members."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        conv = groups.create_group(
            request.user,
            name=data["name"],
            members=members,
            description=data.get("description", ""),
            icon=data.get("icon"),
            only_admins_can_send=data["only_admins_can_send"],
            only_admins_can_edit_info=data["only_admins_can_edit_info"],
        )
        audit("chat.group_created", request=request, conversation=conv.pk,
              members=len(members) + 1, only_admins_can_send=conv.only_admins_can_send)
        _notify_added_to_group(conv, request.user, members)
        return _detail_response(conv, request, status.HTTP_201_CREATED)


class GroupDetailView(APIView):
    """
    GET: group info, settings, members and the caller's own role (members only).
    PATCH: update info and settings.
      name / description / icon: admins, or any member when only_admins_can_edit_info is false.
      only_admins_can_send / only_admins_can_edit_info: admins only.
    DELETE: admin closes the group (messages are kept, nobody can send any more).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        conv = _load_group(conversation_id)
        if _member_link(conv, request.user) is None:
            return Response(_NOT_MEMBER, status=status.HTTP_403_FORBIDDEN)
        if not conv.is_active:
            return Response(_CLOSED, status=status.HTTP_410_GONE)
        return _detail_response(conv, request)

    def patch(self, request, conversation_id):
        conv = _load_group(conversation_id)
        link = _member_link(conv, request.user)
        if link is None:
            return Response(_NOT_MEMBER, status=status.HTTP_403_FORBIDDEN)
        if not conv.is_active:
            return Response(_CLOSED, status=status.HTTP_410_GONE)

        ser = GroupUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if not data:
            return Response({"error": "No changes supplied."}, status=status.HTTP_400_BAD_REQUEST)

        settings_fields = {"only_admins_can_send", "only_admins_can_edit_info"} & set(data)
        if settings_fields and not link.is_admin:
            return Response(_NOT_ADMIN, status=status.HTTP_403_FORBIDDEN)
        info_fields = {"name", "description", "icon"} & set(data)
        if info_fields and not groups.can_edit_info(conv, link):
            return Response(
                {"error": "Only group admins can edit the group info."},
                status=status.HTTP_403_FORBIDDEN,
            )

        for field in ("name", "description", "only_admins_can_send", "only_admins_can_edit_info"):
            if field in data:
                setattr(conv, field, data[field])
        if "icon" in data:
            conv.icon = data["icon"]  # None clears it
        conv.save()

        audit("chat.group_updated", request=request, conversation=conv.pk, fields=",".join(sorted(data)))
        groups.broadcast_group_event(
            conv.pk, "group.updated", changed=sorted(data), name=conv.name,
            only_admins_can_send=conv.only_admins_can_send,
            only_admins_can_edit_info=conv.only_admins_can_edit_info,
        )
        return _detail_response(conv, request)

    def delete(self, request, conversation_id):
        conv = _load_group(conversation_id)
        link = _member_link(conv, request.user)
        if link is None:
            return Response(_NOT_MEMBER, status=status.HTTP_403_FORBIDDEN)
        if not link.is_admin:
            return Response(_NOT_ADMIN, status=status.HTTP_403_FORBIDDEN)
        if conv.is_active:
            conv.is_active = False
            conv.save(update_fields=["is_active", "updated_at"])
            audit("chat.group_closed", request=request, conversation=conv.pk)
            groups.broadcast_group_event(conv.pk, "group.closed")
        return Response(status=status.HTTP_204_NO_CONTENT)


class GroupMembersView(APIView):
    """POST: admin adds members. Body: { "member_ids": ["<uuid>", ...] } (same rules as create)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conv = _load_group(conversation_id)
        link = _member_link(conv, request.user)
        if link is None:
            return Response(_NOT_MEMBER, status=status.HTTP_403_FORBIDDEN)
        if not link.is_admin:
            return Response(_NOT_ADMIN, status=status.HTTP_403_FORBIDDEN)
        if not conv.is_active:
            return Response(_CLOSED, status=status.HTTP_410_GONE)

        ser = GroupMembersSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        users, invalid = groups.split_valid_member_ids(request.user, ser.validated_data["member_ids"])
        if invalid:
            return Response(
                {
                    "error": "Some users cannot be added. You can only add people who follow you "
                             "or whom you follow.",
                    "invalid_member_ids": invalid,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        existing = {l.user_id for l in conv.participant_links.all()}
        new_users = [u for u in users if u.pk not in existing]
        if len(existing) + len(new_users) > groups.MAX_GROUP_MEMBERS:
            return Response(
                {"error": f"A group can have at most {groups.MAX_GROUP_MEMBERS} members."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        added = groups.add_members(conv, new_users)
        if added:
            audit("chat.group_members_added", request=request, conversation=conv.pk, added=len(added))
            groups.broadcast_group_event(
                conv.pk, "group.members_added", user_ids=[str(u.pk) for u in added]
            )
            _notify_added_to_group(conv, request.user, added)
        response = _detail_response(conv, request)
        response.data["added"] = len(added)
        return response


class GroupMemberDetailView(APIView):
    """DELETE: admin removes a member. The group admin cannot be removed (close the group instead)."""
    permission_classes = [IsAuthenticated]

    def delete(self, request, conversation_id, user_id):
        conv = _load_group(conversation_id)
        link = _member_link(conv, request.user)
        if link is None:
            return Response(_NOT_MEMBER, status=status.HTTP_403_FORBIDDEN)
        if not link.is_admin:
            return Response(_NOT_ADMIN, status=status.HTTP_403_FORBIDDEN)
        if not conv.is_active:
            return Response(_CLOSED, status=status.HTTP_410_GONE)

        target = next((l for l in conv.participant_links.all() if l.user_id == user_id), None)
        if target is None:
            return Response({"error": "User is not in this group."}, status=status.HTTP_404_NOT_FOUND)
        if target.is_admin:
            return Response(
                {"error": "A group admin cannot be removed. Close the group instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target.delete()
        audit("chat.group_member_removed", request=request, conversation=conv.pk, removed=str(user_id))
        groups.broadcast_group_event(conv.pk, "group.member_removed", user_id=str(user_id))
        return Response(status=status.HTTP_204_NO_CONTENT)


class GroupLeaveView(APIView):
    """POST: a member leaves the group. The admin cannot leave; they close the group."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conv = _load_group(conversation_id)
        link = _member_link(conv, request.user)
        if link is None:
            return Response(_NOT_MEMBER, status=status.HTTP_403_FORBIDDEN)
        if link.is_admin:
            return Response(
                {"error": "The group admin cannot leave. Close the group instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        link.delete()
        audit("chat.group_left", request=request, conversation=conv.pk)
        groups.broadcast_group_event(conv.pk, "group.member_removed", user_id=str(request.user.pk))
        return Response({"left": True})


class GroupMuteView(APIView):
    """POST: mute/unmute push notifications for this group. Body: { "muted": true|false }."""
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conv = _load_group(conversation_id)
        link = _member_link(conv, request.user)
        if link is None:
            return Response(_NOT_MEMBER, status=status.HTTP_403_FORBIDDEN)
        muted = request.data.get("muted")
        if isinstance(muted, str):
            muted = muted.strip().lower() in ("true", "1", "yes")
        if not isinstance(muted, bool):
            return Response({"error": "muted (true/false) is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        ConversationParticipant.objects.filter(pk=link.pk).update(is_muted=muted)
        return Response({"muted": muted})

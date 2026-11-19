from django.urls import path
from . import group_views, views

app_name = "chat"

urlpatterns = [
    path(
        "broadcast/",
        views.AnalystBroadcastMessageView.as_view(),
        name="analyst_broadcast",
    ),
    # Analyst group chats
    path("groups/", group_views.GroupCreateView.as_view(), name="group_create"),
    path("groups/eligible-members/", group_views.GroupEligibleMembersView.as_view(), name="group_eligible_members"),
    path("groups/<uuid:conversation_id>/", group_views.GroupDetailView.as_view(), name="group_detail"),
    path("groups/<uuid:conversation_id>/members/", group_views.GroupMembersView.as_view(), name="group_members"),
    path(
        "groups/<uuid:conversation_id>/members/<uuid:user_id>/",
        group_views.GroupMemberDetailView.as_view(),
        name="group_member_detail",
    ),
    path("groups/<uuid:conversation_id>/leave/", group_views.GroupLeaveView.as_view(), name="group_leave"),
    path("groups/<uuid:conversation_id>/mute/", group_views.GroupMuteView.as_view(), name="group_mute"),
    path("conversations/", views.ConversationListCreateView.as_view(), name="conversation_list_create"),
    path("conversations/<uuid:conversation_id>/", views.ConversationDetailView.as_view(), name="conversation_detail"),
    path(
        "conversations/<uuid:conversation_id>/messages/",
        views.MessageListCreateView.as_view(),
        name="message_list_create",
    ),
    path(
        "conversations/<uuid:conversation_id>/read/",
        views.MessageMarkReadView.as_view(),
        name="message_mark_read",
    ),
]

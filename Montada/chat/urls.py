from django.urls import path
from . import views

app_name = "chat"

urlpatterns = [
    path(
        "broadcast/",
        views.AnalystBroadcastMessageView.as_view(),
        name="analyst_broadcast",
    ),
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

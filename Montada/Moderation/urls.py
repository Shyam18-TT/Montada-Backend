from django.urls import path

from .views import (
    BlockedUserListView,
    ModerationReportCreateView,
    UserBlockCreateView,
    UserUnblockView,
)


app_name = "Moderation"

urlpatterns = [
    path("reports/", ModerationReportCreateView.as_view(), name="report_create"),
    path("blocks/", UserBlockCreateView.as_view(), name="block_create"),
    path("blocks/list/", BlockedUserListView.as_view(), name="block_list"),
    path("blocks/<uuid:blocked_user_id>/", UserUnblockView.as_view(), name="block_delete"),
]


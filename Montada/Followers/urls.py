from django.urls import path
from . import views

app_name = "Followers"

urlpatterns = [
    # Follow actions
    path("request/", views.FollowRequestView.as_view(), name="follow_request"),
    path("accept/", views.FollowAcceptView.as_view(), name="follow_accept"),
    path("reject/", views.FollowRejectView.as_view(), name="follow_reject"),
    path("unfollow/", views.UnfollowView.as_view(), name="unfollow"),
    path("block/", views.BlockUserView.as_view(), name="block"),
    path("unblock/", views.UnblockUserView.as_view(), name="unblock"),
    # Mute
    path("mute/", views.MuteUserView.as_view(), name="mute"),
    path("unmute/", views.UnmuteUserView.as_view(), name="unmute"),
    # Lists
    path("analysts/", views.AnalystsListView.as_view(), name="analysts_list"),
    path("followers/", views.FollowersListView.as_view(), name="followers_list"),
    path("following/", views.FollowingListView.as_view(), name="following_list"),
    path("pending/received/", views.PendingReceivedListView.as_view(), name="pending_received"),
    path("pending/sent/", views.PendingSentListView.as_view(), name="pending_sent"),
    path("muted/", views.MutedListView.as_view(), name="muted_list"),
    # Counts & status
    path("counts/", views.CountsView.as_view(), name="counts"),
    path("status/", views.FollowStatusView.as_view(), name="follow_status"),
]

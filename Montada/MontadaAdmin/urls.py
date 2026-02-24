from django.urls import path
from .views import (
    AdminDashboardStatsView,
    WinRateByPeriodView,
    AdminDashboardGraphsView,
    TopAnalystLeaderboardView,
    AdminAnalystListView,
    AdminTraderListView,
)

app_name = "MontadaAdmin"

urlpatterns = [
    path("dashboard/stats/", AdminDashboardStatsView.as_view(), name="admin_dashboard_stats"),
    path("dashboard/winrate/", WinRateByPeriodView.as_view(), name="admin_winrate_by_period"),
    path("dashboard/graphs/", AdminDashboardGraphsView.as_view(), name="admin_dashboard_graphs"),
    path("dashboard/leaderboard/", TopAnalystLeaderboardView.as_view(), name="admin_top_analyst_leaderboard"),

    
    path("users/analysts/", AdminAnalystListView.as_view(), name="admin_analyst_list"),
    path("users/traders/", AdminTraderListView.as_view(), name="admin_trader_list"),
]


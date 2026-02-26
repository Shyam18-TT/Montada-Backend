from django.urls import path
from .views import (
    AdminLoginView,
    AdminCreateAnalystView,
    AdminCreateTraderView,
    AdminDashboardStatsView,
    AdminSignalsStatsView,
    AdminSignalsListView,
    AdminSignalStatusesView,
    AdminSignalAssetsView,
    AdminSignalTimeframesView,
    WinRateByPeriodView,
    AdminDashboardGraphsView,
    TopAnalystLeaderboardView,
    AdminAnalystListView,
    AdminTraderListView,
)

app_name = "MontadaAdmin"

urlpatterns = [
    path("login/", AdminLoginView.as_view(), name="admin_login"),
    path("dashboard/stats/", AdminDashboardStatsView.as_view(), name="admin_dashboard_stats"),
    path("dashboard/winrate/", WinRateByPeriodView.as_view(), name="admin_winrate_by_period"),
    path("dashboard/graphs/", AdminDashboardGraphsView.as_view(), name="admin_dashboard_graphs"),
    path("dashboard/leaderboard/", TopAnalystLeaderboardView.as_view(), name="admin_top_analyst_leaderboard"),


    path("users/analysts/", AdminAnalystListView.as_view(), name="admin_analyst_list"),
    path("users/analysts/create/", AdminCreateAnalystView.as_view(), name="admin_create_analyst"),
    path("users/traders/", AdminTraderListView.as_view(), name="admin_trader_list"),
    path("users/traders/create/", AdminCreateTraderView.as_view(), name="admin_create_trader"),

    path("signals/signals-stats/", AdminSignalsStatsView.as_view(), name="admin_signals_stats"),
    path("signals/", AdminSignalsListView.as_view(), name="admin_signals_list"),
    path("signals/filters/statuses/", AdminSignalStatusesView.as_view(), name="admin_signal_statuses"),
    path("signals/filters/assets/", AdminSignalAssetsView.as_view(), name="admin_signal_assets"),
    path("signals/filters/timeframes/", AdminSignalTimeframesView.as_view(), name="admin_signal_timeframes"),
]


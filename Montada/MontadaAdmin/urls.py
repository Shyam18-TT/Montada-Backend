from django.urls import path
from .views import (
    AdminLoginView,
    AdminCreateAnalystView,
    AdminCreateTraderView,
    AdminChangeUserPasswordView,
    AdminSuspendUserView,
    AdminUserProfileView,
    AdminDashboardStatsView,
    AdminSignalsStatsView,
    AdminSignalsListView,
    AdminSignalStatusesView,
    AdminSignalAssetsView,
    AdminSignalTimeframesView,
    AdminNewsCategoryListView,
    AdminNewsCategoryCreateView,
    AdminNewsArticleCreateView,
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
    path("users/change-password/", AdminChangeUserPasswordView.as_view(), name="admin_change_user_password"),
    path("users/suspend/", AdminSuspendUserView.as_view(), name="admin_suspend_user"),
    path("users/<uuid:user_id>/", AdminUserProfileView.as_view(), name="admin_user_profile"),

    path("signals/signals-stats/", AdminSignalsStatsView.as_view(), name="admin_signals_stats"),
    path("signals/", AdminSignalsListView.as_view(), name="admin_signals_list"),
    path("signals/filters/statuses/", AdminSignalStatusesView.as_view(), name="admin_signal_statuses"),
    path("signals/filters/assets/", AdminSignalAssetsView.as_view(), name="admin_signal_assets"),
    path("signals/filters/timeframes/", AdminSignalTimeframesView.as_view(), name="admin_signal_timeframes"),

    path("news/categories/", AdminNewsCategoryListView.as_view(), name="admin_news_category_list"),
    path("news/categories/create/", AdminNewsCategoryCreateView.as_view(), name="admin_news_category_create"),
    path("news/articles/create/", AdminNewsArticleCreateView.as_view(), name="admin_news_article_create"),
]


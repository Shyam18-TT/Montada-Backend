from django.urls import path
from .views import AdminDashboardStatsView, WinRateByPeriodView

app_name = "MontadaAdmin"

urlpatterns = [
    path("dashboard/stats/", AdminDashboardStatsView.as_view(), name="admin_dashboard_stats"),
    path("dashboard/winrate/", WinRateByPeriodView.as_view(), name="admin_winrate_by_period"),
]


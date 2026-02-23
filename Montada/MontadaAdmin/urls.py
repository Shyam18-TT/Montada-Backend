from django.urls import path
from .views import AdminDashboardStatsView

app_name = "MontadaAdmin"

urlpatterns = [
    path("dashboard/stats/", AdminDashboardStatsView.as_view(), name="admin_dashboard_stats"),
]



from django.urls import path
from django.conf import settings
from django.conf.urls.static import static

from . import views

urlpatterns = [
    path('performance/', views.AnalystPerformanceOverviewView.as_view(), name='analyst_performance_overview'),
    path('summary/', views.AnalystDashboardSummaryView.as_view(), name='analyst_dashboard_summary'),
    path('activities/', views.AnalystActivityLogListView.as_view(), name='analyst_activity_log_list'),

    # Analytics
    path('analytics/graphs/', views.AnalyticsGraphView.as_view(), name='analytics_graphs'),

    # Polls (active list with options and vote counts)
    path('polls/active/', views.ActivePollsListView.as_view(), name='active_polls_list'),
    path('polls/vote/', views.PollVoteView.as_view(), name='poll_vote'),

    path('marketdata/live', views.GetMarketDataFromMT5.as_view(),name='get_market_Data'),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

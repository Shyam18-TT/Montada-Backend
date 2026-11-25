from django.urls import path
from . import views

app_name = "News"

urlpatterns = [
    path("live-news/", views.LiveNewsListView.as_view(), name="live_news_list"),
    path("articles/", views.NewsArticleListView.as_view(), name="article_list"),
    path("articles/create/", views.AnalystNewsArticleCreateView.as_view(), name="article_create"),
    path("articles/<uuid:pk>/", views.AnalystNewsArticleDetailView.as_view(), name="article_detail"),
    path("articles/<uuid:pk>/like/", views.ArticleLikeView.as_view(), name="article_like"),
    path("articles/<uuid:pk>/comments/", views.ArticleCommentListCreateView.as_view(), name="article_comments"),
    path("articles/<uuid:pk>/comments/<uuid:comment_pk>/", views.ArticleCommentDestroyView.as_view(), name="article_comment_delete"),
    path("categories/", views.NewsCategoryListView.as_view(), name="category_list"),

    path('market-news/', views.MarketNewsList.as_view(), name='market_news'),
    path('events/', views.ForexEventsView.as_view(), name='forex_events'),
    path('events/<str:event_id>/', views.ForexEventDetailView.as_view(), name='forex_event_detail'),
    path('trending-headlines/', views.ForexTrendingHeadlinesView.as_view(), name='forex_trending_headlines'),
    path('category-news/', views.ForexCategoryNewsView.as_view(), name='forex_category_news'),
    path('economic-calendar/', views.TradaysEconomicCalendarView.as_view(), name='economic_calendar'),
    
    # Economic Calendar Reminders
    path('reminders/', views.EconomicCalendarReminderListView.as_view(), name='reminder_list'),
    path('reminders/create/', views.EconomicCalendarReminderCreateView.as_view(), name='reminder_create'),
    path('reminders/<uuid:pk>/', views.EconomicCalendarReminderDetailView.as_view(), name='reminder_detail'),
    
    # Economic Calendar Event Notifications (tracking)
    path('event-notifications/', views.EconomicCalendarEventNotificationListView.as_view(), name='event_notification_list'),
]

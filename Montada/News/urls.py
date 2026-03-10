from django.urls import path
from . import views

app_name = "News"

urlpatterns = [
    path("articles/", views.NewsArticleListView.as_view(), name="article_list"),
    path("articles/create/", views.AnalystNewsArticleCreateView.as_view(), name="article_create"),
    path("articles/<uuid:pk>/", views.AnalystNewsArticleDetailView.as_view(), name="article_detail"),
    path("categories/", views.NewsCategoryListView.as_view(), name="category_list"),


    path('market-news/',views.MarketNewsList.as_view(), name='market_news'),
    path('events/', views.ForexEventsView.as_view(), name='forex_events'),
]

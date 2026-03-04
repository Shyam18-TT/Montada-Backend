from django.urls import path
from . import views

app_name = "News"

urlpatterns = [
    path("articles/", views.NewsArticleListView.as_view(), name="article_list"),
    path("articles/create/", views.AnalystNewsArticleCreateView.as_view(), name="article_create"),
    path("categories/", views.NewsCategoryListView.as_view(), name="category_list"),
]

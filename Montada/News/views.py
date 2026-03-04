from django.db.models import Q
from rest_framework import status, generics, permissions
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .models import NewsArticle, NewsCategory
from .serializers import NewsArticleCreateSerializer, NewsArticleListSerializer, NewsCategorySerializer

try:
    from Signals.views import IsAnalystPermission
except ImportError:
    IsAnalystPermission = permissions.BasePermission  # no-op fallback


class AnalystNewsArticleCreateView(generics.CreateAPIView):
    """
    POST: Create a news article. Only analysts can create.
    Sets author to the authenticated user. Body fields per NewsArticle model.
    """
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    serializer_class = NewsArticleCreateSerializer
    queryset = NewsArticle.objects.all()

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)

    def create(self, request, *args, **kwargs):
        if getattr(request.user, "user_type", None) != "analyst":
            return Response(
                {"error": "Only analysts can create news articles."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            {
                "message": "News article created successfully.",
                "article": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )


class NewsArticleListPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


class NewsArticleListView(generics.ListAPIView):
    """
    GET: List news articles. Paginated.
    - Trader: only published articles.
    - Analyst: only articles created by himself; optional ?status=draft|published|archived to filter.
    Query params: search, category (UUID), status (analyst only), page, page_size.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NewsArticleListSerializer
    pagination_class = NewsArticleListPagination

    def get_queryset(self):
        user_type = getattr(self.request.user, "user_type", "trader")
        qs = NewsArticle.objects.filter(is_deleted=False).select_related("author", "category").order_by("-created_at")

        if user_type == "trader":
            qs = qs.filter(status="published")
        else:
            # Analyst: only articles created by himself; optional status filter
            qs = qs.filter(author=self.request.user)
            status_param = (self.request.query_params.get("status") or "").strip().lower()
            if status_param in ("draft", "published", "archived"):
                qs = qs.filter(status=status_param)

        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(summary__icontains=search)
                | Q(content__icontains=search)
            )
        category_id = self.request.query_params.get("category")
        if category_id:
            qs = qs.filter(category_id=category_id)
        return qs


class AnalystNewsArticleDetailView(generics.RetrieveUpdateAPIView):
    """
    GET: Retrieve a news article. PUT/PATCH: Update the article.
    Only the analyst who created the article (author) can retrieve or update it.
    """
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    serializer_class = NewsArticleCreateSerializer
    lookup_url_kwarg = "pk"
    lookup_field = "pk"

    def get_queryset(self):
        return (
            NewsArticle.objects.filter(is_deleted=False, author=self.request.user)
            .select_related("author", "category")
            .prefetch_related("tags")
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"message": "Article updated successfully.", "article": serializer.data},
            status=status.HTTP_200_OK,
        )


class NewsCategoryListView(generics.ListAPIView):
    """
    GET: List all news categories (id, name, slug). Ordered by name.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NewsCategorySerializer
    queryset = NewsCategory.objects.all().order_by("name")

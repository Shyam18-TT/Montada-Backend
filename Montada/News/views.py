from rest_framework import status, generics, permissions
from rest_framework.response import Response

from .models import NewsArticle, NewsCategory
from .serializers import NewsArticleCreateSerializer, NewsCategorySerializer

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


class NewsCategoryListView(generics.ListAPIView):
    """
    GET: List all news categories (id, name, slug). Ordered by name.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NewsCategorySerializer
    queryset = NewsCategory.objects.all().order_by("name")

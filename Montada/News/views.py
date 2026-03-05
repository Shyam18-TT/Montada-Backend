import urllib.request
import urllib.parse
import json
from django.conf import settings
from django.db.models import Q
from rest_framework import status, generics, permissions
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

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


# Allowed query params to forward to Marketaux API (see https://www.marketaux.com/documentation)
MARKETAUX_ALLOWED_PARAMS = {
    "symbols", "entity_types", "industries", "countries", "sentiment_gte", "sentiment_lte",
    "min_match_score", "filter_entities", "must_have_entities", "group_similar", "search",
    "domains", "exclude_domains", "source_ids", "exclude_source_ids", "language",
    "published_before", "published_after", "published_on", "sort", "sort_order", "limit", "page",
}

# Category -> comma-separated symbols for Marketaux (same style as working URL).
# "all" = no symbols; others pass symbol list so API returns news for that category.
MARKET_NEWS_SYMBOLS_BY_CATEGORY = {
    "all": [],
    "forex": [
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURGBP", "EURJPY",
        "GBPJPY", "AUDJPY", "USDCNH", "EURAUD", "EURCAD", "GBPAUD", "AUDNZD",
    ],
    "currencies": [
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURGBP", "EURJPY",
        "GBPJPY", "AUDJPY", "USDCNH", "EURAUD", "EURCAD", "GBPAUD", "AUDNZD",
    ],
    "shares": [
        "AAPL", "AMZN", "MSFT", "GOOGL", "TSLA", "NVDA", "META", "JPM", "JNJ", "V", "WMT",
        "PG", "MA", "HD", "DIS", "BAC", "XOM", "PFE", "CSCO", "NFLX",
    ],
    "equity": [
        "AAPL", "AMZN", "MSFT", "GOOGL", "TSLA", "NVDA", "META", "JPM", "JNJ", "V", "WMT",
        "PG", "MA", "HD", "DIS", "BAC", "XOM", "PFE", "CSCO", "NFLX",
    ],
    "stocks": [
        "AAPL", "AMZN", "MSFT", "GOOGL", "TSLA", "NVDA", "META", "JPM", "JNJ", "V", "WMT",
        "PG", "MA", "HD", "DIS", "BAC", "XOM", "PFE", "CSCO", "NFLX",
    ],
    "metals": ["GOLD", "SILVER", "XAUEUR", "PLATINUM", "PALLADIUM", "COPPER"],
    "indices": ["US30", "US100", "US500", "US2000", "GER40", "FRA40", "UK100", "JAP225", "NASDAQ", "DOW"],
    "index": ["US30", "US100", "US500", "US2000", "GER40", "FRA40", "UK100", "JAP225", "NASDAQ", "DOW"],
    "commodity": ["SOYBEAN", "COCOA", "COFFEE"],
    "commodities": ["GOLD", "SILVER", "COPPER", "CL", "BRENT", "USOIL", "UKOIL", "SOYBEAN", "COCOA", "COFFEE"],
    "energy": ["CL", "USOIL", "BRENT", "UKOIL", "NATGAS"],
    "menashares": [
        "Emaar.Propt", "ADCB", "ADIB", "FAB.Bank", "DEWA", "TAKREER", "NMDC", "IHC", "GULFNAV",
    ],
}


class MarketNewsList(APIView):
    """
    GET: Fetch market/finance news from Marketaux API (same URL style as official docs).
    Query param: category = all | forex | shares | equity | stocks | metals | indices |
                 commodity | commodities | energy | menashares.
    Optional: language, limit, page, symbols (overrides category symbols), etc.
    Example: ?category=forex&filter_entities=true&language=en&limit=10
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        api_token = getattr(settings, "MARKETAUX_API_TOKEN", None)
        if not api_token:
            return Response(
                {"error": "Market news API is not configured (MARKETAUX_API_TOKEN missing)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        # Build params like: api_token, filter_entities=true, language, symbols, ...
        params = {
            "api_token": api_token,
            "filter_entities": "true",
        }
        category = (request.query_params.get("category") or "all").strip().lower()
        if category not in MARKET_NEWS_SYMBOLS_BY_CATEGORY:
            return Response(
                {
                    "error": f"Invalid category: {category}.",
                    "valid_categories": list(MARKET_NEWS_SYMBOLS_BY_CATEGORY.keys()),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        category_symbols = MARKET_NEWS_SYMBOLS_BY_CATEGORY[category]
        if category_symbols:
            params["symbols"] = ",".join(category_symbols)
        for key in MARKETAUX_ALLOWED_PARAMS:
            val = request.query_params.get(key)
            if val is not None:
                params[key] = val
        url = "https://api.marketaux.com/v1/news/all?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else "{}"
            try:
                err_data = json.loads(body)
            except Exception:
                err_data = {"error": body or str(e)}
            if "1010" in str(err_data.get("error", "")):
                return Response(
                    {
                        "error": "Market news provider is blocking this server's region or network (Cloudflare 1010). Try calling the API from a different network, or use a proxy.",
                        "code": "1010",
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            return Response(err_data, status=e.code)
        except Exception as e:
            return Response(
                {"error": "Failed to fetch market news.", "detail": str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(data, status=status.HTTP_200_OK)

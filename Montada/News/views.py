import urllib.request
import urllib.parse
import json
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q, Count, Exists, OuterRef
from django.utils import timezone as django_timezone
from django.shortcuts import get_object_or_404
from rest_framework import status, generics, permissions
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .live_news_service import FRONTEND_LIVE_NEWS_LANGUAGES
from .models import NewsArticle, NewsCategory, NewsArticleLike, NewsArticleComment, LiveNews, EconomicCalendarEvent, EconomicCalendarReminder, EconomicCalendarEventNotification
from Subscriptions.models import AnalystContentPlan, UserAnalystPlanSubscription
from .serializers import (
    NewsArticleCreateSerializer,
    NewsArticleListSerializer,
    NewsCategorySerializer,
    NewsArticleCommentSerializer,
    LiveNewsSerializer,
    EconomicCalendarEventSerializer,
    EconomicCalendarReminderCreateSerializer,
    EconomicCalendarReminderListSerializer,
    EconomicCalendarEventNotificationSerializer,
)
from Subscriptions.access import check_active_subscription
from Subscriptions.analyst_plan_access import analyst_subscription_free_access_enabled

try:
    from Signals.views import IsAnalystPermission
except ImportError:
    IsAnalystPermission = permissions.BasePermission  # no-op fallback


class AnalystNewsArticleCreateView(generics.CreateAPIView):
    """
    POST: Create a news article. Only analysts can create.
    Sets author to the authenticated user. Body fields per NewsArticle model.
    Optional content_access: "free" (any authenticated trader) or "premium" (plan required); default premium.
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


class LiveNewsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class NewsArticleListView(generics.ListAPIView):
    """
    GET: List news articles. Paginated.
    - Trader: published articles; analyst-authored posts require an active per-analyst
      subscription (articles or all), unless the article is content_access=free.
      Non-analyst authors' articles stay visible.
    - Analyst: only articles created by himself; optional ?status=draft|published|archived to filter.
    Query params: search, category (UUID), status (analyst only), page, page_size.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NewsArticleListSerializer
    pagination_class = NewsArticleListPagination

    def get_queryset(self):
        user_type = getattr(self.request.user, "user_type", "trader")
        qs = NewsArticle.objects.filter(is_deleted=False).select_related("author", "category").prefetch_related("tags").order_by("-created_at")

        if user_type == "trader":
            qs = qs.filter(status="published")
            # Analyst-authored articles: only if trader has an active per-analyst subscription
            # (articles or all). Non-analyst authors remain visible without a plan.
            if analyst_subscription_free_access_enabled():
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

                qs = qs.annotate(
                    like_count=Count("likes", distinct=True),
                    comment_count=Count("comments", filter=Q(comments__is_deleted=False), distinct=True),
                )
                if self.request.user and self.request.user.is_authenticated:
                    qs = qs.annotate(
                        current_user_liked=Exists(NewsArticleLike.objects.filter(article=OuterRef("pk"), user=self.request.user)),
                    )
                return qs
            User = get_user_model()
            now = django_timezone.now()
            is_analyst_author = Exists(
                User.objects.filter(pk=OuterRef("author_id"), user_type="analyst")
            )
            article_access = UserAnalystPlanSubscription.objects.filter(
                subscriber=self.request.user,
                status=UserAnalystPlanSubscription.Status.ACTIVE,
                end_date__gte=now,
                plan__is_active=True,
                plan__analyst_id=OuterRef("author_id"),
                plan__scope__in=[
                    AnalystContentPlan.Scope.ARTICLES,
                    AnalystContentPlan.Scope.ALL,
                ],
            )
            is_free_article = Q(content_access=NewsArticle.ContentAccess.FREE)
            qs = qs.filter(~is_analyst_author | is_free_article | Exists(article_access))
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

        # Annotate like_count, comment_count, current_user_liked for list
        qs = qs.annotate(
            like_count=Count("likes", distinct=True),
            comment_count=Count("comments", filter=Q(comments__is_deleted=False), distinct=True),
        )
        if self.request.user and self.request.user.is_authenticated:
            qs = qs.annotate(
                current_user_liked=Exists(NewsArticleLike.objects.filter(article=OuterRef("pk"), user=self.request.user)),
            )
        return qs


class AnalystNewsArticleDetailView(generics.RetrieveUpdateAPIView):
    """
    GET: Retrieve a news article. PUT/PATCH: Update the article.
    Only the analyst who created the article (author) can retrieve or update it.
    GET response includes like_count, comment_count.
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

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = dict(serializer.data)
        data["like_count"] = instance.likes.count()
        data["comment_count"] = instance.comments.filter(is_deleted=False).count()
        return Response(data)

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


def _get_article_for_engagement(request, pk):
    """Return article if user can like/comment (published or author). Else None."""
    from Subscriptions.analyst_plan_access import user_has_analyst_article_access

    article = get_object_or_404(NewsArticle.objects.filter(is_deleted=False), pk=pk)
    if article.status == "published":
        uid = getattr(request.user, "id", None)
        if uid and article.author_id != uid:
            article_is_free = article.content_access == NewsArticle.ContentAccess.FREE
            if not user_has_analyst_article_access(
                request.user, article.author_id, article_is_free=article_is_free
            ):
                return None
        return article
    if getattr(request.user, "id", None) and article.author_id == request.user.id:
        return article
    return None


class ArticleLikeView(APIView):
    """
    POST: Like the article (idempotent).
    DELETE: Unlike the article.
    URL: .../articles/<pk>/like/
    Article must be published or be the author.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        article = _get_article_for_engagement(request, pk)
        if not article:
            return Response(
                {"error": "Article not found or not available for likes."},
                status=status.HTTP_404_NOT_FOUND,
            )
        _, created = NewsArticleLike.objects.get_or_create(user=request.user, article=article)
        return Response(
            {"message": "Liked." if created else "Already liked.", "liked": True},
            status=status.HTTP_200_OK,
        )

    def delete(self, request, pk):
        article = _get_article_for_engagement(request, pk)
        if not article:
            return Response(
                {"error": "Article not found or not available."},
                status=status.HTTP_404_NOT_FOUND,
            )
        deleted, _ = NewsArticleLike.objects.filter(user=request.user, article=article).delete()
        return Response(
            {"message": "Unliked." if deleted else "Was not liked.", "liked": False},
            status=status.HTTP_200_OK,
        )


class ArticleCommentListCreateView(APIView):
    """
    GET: List comments for the article (paginated, excludes deleted).
    POST: Add a comment. Body: { "content": "..." }
    URL: .../articles/<pk>/comments/
    Article must be published or be the author.
    """
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = NewsArticleListPagination

    def get_article(self, request, pk):
        return _get_article_for_engagement(request, pk)

    def get(self, request, pk):
        article = self.get_article(request, pk)
        if not article:
            return Response(
                {"error": "Article not found or not available for comments."},
                status=status.HTTP_404_NOT_FOUND,
            )
        qs = NewsArticleComment.objects.filter(article=article, is_deleted=False).select_related("user").order_by("created_at")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request)
        serializer = NewsArticleCommentSerializer(page if page is not None else qs, many=True)
        if page is not None:
            return paginator.get_paginated_response(serializer.data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, pk):
        article = self.get_article(request, pk)
        if not article:
            return Response(
                {"error": "Article not found or not available for comments."},
                status=status.HTTP_404_NOT_FOUND,
            )
        content = (request.data.get("content") or "").strip()
        if not content:
            return Response(
                {"error": "content is required and cannot be empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        comment = NewsArticleComment.objects.create(
            user=request.user,
            article=article,
            content=content,
        )
        serializer = NewsArticleCommentSerializer(comment)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ArticleCommentDestroyView(APIView):
    """
    DELETE: Remove own comment. Soft-delete (is_deleted=True).
    URL: .../articles/<article_pk>/comments/<comment_pk>/
    """
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk, comment_pk):
        article = _get_article_for_engagement(request, pk)
        if not article:
            return Response(
                {"error": "Article not found or not available."},
                status=status.HTTP_404_NOT_FOUND,
            )
        comment = NewsArticleComment.objects.filter(
            article=article,
            user=request.user,
            is_deleted=False,
        ).filter(pk=comment_pk).first()
        if not comment:
            return Response(
                {"error": "Comment not found or you cannot delete it."},
                status=status.HTTP_404_NOT_FOUND,
            )
        comment.is_deleted = True
        comment.save(update_fields=["is_deleted"])
        return Response({"message": "Comment removed."}, status=status.HTTP_200_OK)


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
    "crypto": ["BTC", "ETH", "BNB", "XRP", "ADA", "SOL", "DOGE", "AVAX", "DOT", "MATIC", "LINK", "UNI", "ATOM", "LTC", "ETC", "XLM", "NEAR", "APT", "ARB", "OP"],
    "cryptocurrency": ["BTC", "ETH", "BNB", "XRP", "ADA", "SOL", "DOGE", "AVAX", "DOT", "MATIC", "LINK", "UNI", "ATOM", "LTC", "ETC", "XLM", "NEAR", "APT", "ARB", "OP"],
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
    Requires an active subscription (market news is a premium feature).

    Query param: category = all | forex | shares | equity | stocks | crypto | cryptocurrency |
                 metals | indices | commodity | commodities | energy | menashares.
    Optional: language, limit, page, symbols (overrides category symbols), etc.
    Example: ?category=forex&filter_entities=true&language=en&limit=10
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        denied = check_active_subscription(request.user)
        if denied is not None:
            return denied

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
            "language":"en"
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





class ForexEventsView(APIView):
    """
    GET  /api/dashboard/events/

    Returns upcoming/recent economic events from ForexNewsAPI.

    Query params:
        page      : page number (default 1)
        currency  : comma-separated currency codes to filter e.g. USD,EUR
        date      : specific date YYYY-MM-DD
        from_date : start date YYYY-MM-DD
        to_date   : end date YYYY-MM-DD

    Response shape (mirrors ForexNewsAPI):
    {
        "total_pages": 5555,
        "events": [
            {
                "event_id"   : "AAD978",
                "event_name" : "China trade surges...",
                "event_text" : "...",
                "news_items" : 10,
                "date"       : "Mon, 09 Mar 2026 23:20:46 -0400",
                "currency"   : []
            },
            ...
        ]
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    _ALLOWED_PARAMS = {"page", "currency", "date", "from_date", "to_date"}

    def get(self, request):
        denied = check_active_subscription(request.user)
        if denied is not None:
            return denied

        token = getattr(settings, "FOREXNEWS_API_TOKEN", "ix9zm1aqfxqzclsusns6cqsaufji9k3lpdcy0ybs")
        base_url = getattr(settings, "FOREXNEWS_EVENTS_URL", "https://forexnewsapi.com/api/v1/events")

        if not token:
            return Response(
                {"error": "ForexNewsAPI token is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        params = {"token": token}
        for key in self._ALLOWED_PARAMS:
            val = request.query_params.get(key)
            if val:
                params[key] = val

        # Default to page 1
        params.setdefault("page", "1")

        url = base_url + "?" + urllib.parse.urlencode(params)

        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if exc.fp else "{}"
            try:
                err_data = json.loads(body)
            except Exception:
                err_data = {"error": body or str(exc)}
            return Response(err_data, status=exc.code)
        except urllib.error.URLError as exc:
            return Response(
                {"error": "Could not reach ForexNewsAPI.", "detail": str(exc.reason)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:
            return Response(
                {"error": "Failed to fetch events.", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        events = raw.get("data", [])
        total_pages = raw.get("total_pages", 1)

        return Response(
            {
                "total_pages": total_pages,
                "page": int(params.get("page", 1)),
                "events": events,
            },
            status=status.HTTP_200_OK,
        )


class ForexEventDetailView(APIView):
    """
    GET  /api/news/events/<event_id>/

    Fetches the details of a single economic event by its event_id
    (e.g. AAD978) from ForexNewsAPI, including all related news articles.

    Query params:
        page      : page number for the article list (default 1)

    Response shape:
    {
        "event_id"   : "AAD978",
        "event_name" : "China trade surges...",
        "event_text" : "...",
        "page"       : 1,
        "articles"   : [
            {
                "title"       : "...",
                "news_url"    : "...",
                "image_url"   : "...",
                "text"        : "...",
                "sentiment"   : "Positive",
                "type"        : "Article",
                "source_name" : "Reuters",
                "date"        : "...",
                "currency"    : [],
                "topics"      : ["China"]
            },
            ...
        ]
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, event_id):
        denied = check_active_subscription(request.user)
        if denied is not None:
            return denied

        token = getattr(settings, "FOREXNEWS_API_TOKEN", "ix9zm1aqfxqzclsusns6cqsaufji9k3lpdcy0ybs")
        base_url = getattr(settings, "FOREXNEWS_EVENTS_URL", "https://forexnewsapi.com/api/v1/events")

        if not token:
            return Response(
                {"error": "ForexNewsAPI token is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        event_id = event_id.strip().upper()
        if not event_id:
            return Response(
                {"error": "event_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        page = request.query_params.get("page", "1")

        params = {
            "eventid": event_id,
            "page":    page,
            "token":   token,
        }
        url = base_url + "?" + urllib.parse.urlencode(params)

        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if exc.fp else "{}"
            try:
                err_data = json.loads(body)
            except Exception:
                err_data = {"error": body or str(exc)}
            return Response(err_data, status=exc.code)
        except urllib.error.URLError as exc:
            return Response(
                {"error": "Could not reach ForexNewsAPI.", "detail": str(exc.reason)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:
            return Response(
                {"error": "Failed to fetch event detail.", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "event_id":   event_id,
                "event_name": raw.get("event_name", ""),
                "event_text": raw.get("event_text", ""),
                "page":       int(page),
                "articles":   raw.get("data", []),
            },
            status=status.HTTP_200_OK,
        )


class ForexTrendingHeadlinesView(APIView):
    """
    GET  /api/news/trending-headlines/

    Returns trending forex/market headlines from ForexNewsAPI.

    Query params:
        page      : page number (default 1)
        currency  : comma-separated currency codes to filter e.g. USD,EUR
        date      : specific date YYYY-MM-DD
        from_date : start date YYYY-MM-DD
        to_date   : end date YYYY-MM-DD
        sentiment : Positive | Negative | Neutral

    Response shape:
    {
        "total_pages": 101,
        "page": 1,
        "headlines": [
            {
                "id"        : 5116,
                "headline"  : "South Africa GDP Growth Slows...",
                "text"      : "...",
                "news_id"   : 289186,
                "sentiment" : "Negative",
                "date"      : "Tue, 10 Mar 2026 05:30:46 -0400",
                "currency"  : []
            },
            ...
        ]
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    _ALLOWED_PARAMS = {"page", "currency", "date", "from_date", "to_date", "sentiment"}

    def get(self, request):
        denied = check_active_subscription(request.user)
        if denied is not None:
            return denied

        token    = getattr(settings, "FOREXNEWS_API_TOKEN", "")
        base_url = getattr(settings, "FOREXNEWS_TRENDING_URL",
                           "https://forexnewsapi.com/api/v1/trending-headlines")

        if not token:
            return Response(
                {"error": "ForexNewsAPI token is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        params = {"token": token}
        for key in self._ALLOWED_PARAMS:
            val = request.query_params.get(key)
            if val:
                params[key] = val

        params.setdefault("page", "1")

        url = base_url + "?" + urllib.parse.urlencode(params)

        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if exc.fp else "{}"
            try:
                err_data = json.loads(body)
            except Exception:
                err_data = {"error": body or str(exc)}
            return Response(err_data, status=exc.code)
        except urllib.error.URLError as exc:
            return Response(
                {"error": "Could not reach ForexNewsAPI.", "detail": str(exc.reason)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:
            return Response(
                {"error": "Failed to fetch trending headlines.", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "total_pages": raw.get("total_pages", 1),
                "page":        int(params.get("page", 1)),
                "headlines":   raw.get("data", []),
            },
            status=status.HTTP_200_OK,
        )


def _normalize_eodhd_category_article(article, idx):
    """
    Map one EODHD /api/news article to the legacy category-news item shape
    (aligned with ForexTrendingHeadlinesView headline fields).
    """
    sent = article.get("sentiment") or {}
    polarity = sent.get("polarity") if isinstance(sent, dict) else None
    if polarity is not None:
        if polarity > 0.1:
            sentiment = "Positive"
        elif polarity < -0.1:
            sentiment = "Negative"
        else:
            sentiment = "Neutral"
    else:
        sentiment = "Neutral"

    title = article.get("title") or ""
    content = article.get("content") or ""
    link = article.get("link") or ""
    date = article.get("date") or ""
    symbols = article.get("symbols") or []
    tags = article.get("tags") or []
    h = hash((link, date, title)) % (10**9)
    news_id = h if h else idx + 1

    return {
        "id": idx + 1,
        "headline": title,
        "text": content,
        "news_id": news_id,
        "sentiment": sentiment,
        "date": date,
        "currency": symbols,
        "tags": tags,
        "link": link,
    }


def fetch_eodhd_category_news_dict(section, items, page, api_token):
    """
    Call EODHD ``GET /api/news`` and return the same envelope as the old ForexNewsAPI
    category endpoint: ``{ total_pages, page, section, news }``.

    https://eodhd.com/financial-apis/stock-market-financial-news-api/

    Returns:
        (payload_dict, None) on success, or (None, error_dict) when the API returns an error body.

    Raises:
        urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError — caller should handle.
    """
    limit = min(max(int(items), 1), 1000)
    page_i = max(int(page), 1)
    offset = (page_i - 1) * limit
    params = {
        "api_token": api_token,
        "fmt": "json",
        "limit": str(limit),
        "offset": str(offset),
    }
    sec = (section or "general").strip().lower()
    if sec == "general":
        params["s"] = "EURUSD.FOREX"
    else:
        params["t"] = str(section).strip()

    base_url = getattr(settings, "EODHD_NEWS_URL", "https://eodhd.com/api/news")
    url = base_url + "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, method="GET")
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode())

    if isinstance(raw, dict) and (raw.get("error") or raw.get("errors")):
        return None, raw if isinstance(raw, dict) else {"error": str(raw)}

    if isinstance(raw, dict):
        items_list = raw.get("data") or raw.get("news") or []
    elif isinstance(raw, list):
        items_list = raw
    else:
        items_list = []

    news = [_normalize_eodhd_category_article(a, i) for i, a in enumerate(items_list)]
    has_more = len(items_list) >= limit
    total_pages = max(1, page_i + (1 if has_more else 0))

    return (
        {
            "total_pages": total_pages,
            "page": page_i,
            "section": section or "general",
            "news": news,
        },
        None,
    )


def eodhd_category_news_response_for_request(request):
    """
    Shared GET handler for category news (EODHD). Same JSON shape for app and admin:
    ``{ total_pages, page, section, news }``.
    """
    _allowed = {"section", "category", "items", "page"}
    token = getattr(settings, "EODHD_API_TOKEN", None) or getattr(
        settings, "FOREXNEWS_API_TOKEN", ""
    )
    if not token:
        return Response(
            {
                "error": "EODHD API token is not configured (set EODHD_API_TOKEN or FOREXNEWS_API_TOKEN).",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    params = {}
    for key in _allowed:
        val = request.query_params.get(key)
        if val:
            params[key] = val
    selected_category = (
        params.get("category")
        or params.get("section")
        or "general"
    )
    params.setdefault("items", "50")
    params.setdefault("page", "1")

    try:
        payload, err = fetch_eodhd_category_news_dict(
            selected_category,
            params.get("items", "50"),
            params.get("page", "1"),
            token,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else "{}"
        try:
            err_data = json.loads(body)
        except Exception:
            err_data = {"error": body or str(exc)}
        return Response(err_data, status=exc.code)
    except urllib.error.URLError as exc:
        return Response(
            {"error": "Could not reach EODHD API.", "detail": str(exc.reason)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except Exception as exc:
        return Response(
            {"error": "Failed to fetch category news.", "detail": str(exc)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if err is not None:
        return Response(err, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload, status=status.HTTP_200_OK)


class EconomicCalendarReminderCreateView(generics.CreateAPIView):
    """
    POST: Create an economic calendar reminder for the authenticated user.
    Body: { "event": <uuid>, "reminder_type": "5_min_before|15_min_before|30_min_before|1_hour_before|custom", "custom_minutes_before": <int> }
    
    The reminder_time is automatically calculated based on the event's release_date and the reminder_type.
    If reminder_type is CUSTOM, custom_minutes_before must be provided.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = EconomicCalendarReminderCreateSerializer
    queryset = EconomicCalendarReminder.objects.all()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            {
                "message": "Economic calendar reminder created successfully.",
                "reminder": EconomicCalendarReminderListSerializer(serializer.instance, context={"request": request}).data,
            },
            status=status.HTTP_201_CREATED,
        )


class EconomicCalendarReminderListView(generics.ListAPIView):
    """
    GET: List all economic calendar reminders for the authenticated user.
    Paginated, ordered by reminder_time (soonest first).
    Query params: is_active (true/false), is_sent (true/false), page, page_size.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = EconomicCalendarReminderListSerializer
    pagination_class = NewsArticleListPagination

    def get_queryset(self):
        qs = EconomicCalendarReminder.objects.filter(user=self.request.user).select_related("event").order_by("reminder_time")
        
        # Optional filters
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            is_active_bool = is_active.lower() in ("true", "1", "yes")
            qs = qs.filter(is_active=is_active_bool)
        
        is_sent = self.request.query_params.get("is_sent")
        if is_sent is not None:
            is_sent_bool = is_sent.lower() in ("true", "1", "yes")
            qs = qs.filter(is_sent=is_sent_bool)
        
        return qs


class EconomicCalendarReminderDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET: Retrieve a specific reminder.
    PUT/PATCH: Update reminder (reminder_type, custom_minutes_before, is_active).
    DELETE: Delete the reminder.
    Only the user who created the reminder can access/modify it.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = EconomicCalendarReminderCreateSerializer
    lookup_url_kwarg = "pk"
    lookup_field = "pk"

    def get_queryset(self):
        return EconomicCalendarReminder.objects.filter(user=self.request.user).select_related("event")

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = EconomicCalendarReminderListSerializer(instance, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        
        # Recalculate reminder_time if reminder_type or custom_minutes_before changed
        from datetime import timedelta
        reminder_type = serializer.validated_data.get("reminder_type", instance.reminder_type)
        custom_minutes_before = serializer.validated_data.get("custom_minutes_before", instance.custom_minutes_before)
        
        if reminder_type == EconomicCalendarReminder.ReminderType.BEFORE_5_MIN:
            minutes_before = 5
        elif reminder_type == EconomicCalendarReminder.ReminderType.BEFORE_15_MIN:
            minutes_before = 15
        elif reminder_type == EconomicCalendarReminder.ReminderType.BEFORE_30_MIN:
            minutes_before = 30
        elif reminder_type == EconomicCalendarReminder.ReminderType.BEFORE_1_HOUR:
            minutes_before = 60
        else:  # CUSTOM
            minutes_before = custom_minutes_before or instance.custom_minutes_before
        
        reminder_time = instance.event.release_date - timedelta(minutes=minutes_before)
        serializer.validated_data["reminder_time"] = reminder_time
        
        self.perform_update(serializer)
        return Response(
            {
                "message": "Reminder updated successfully.",
                "reminder": EconomicCalendarReminderListSerializer(serializer.instance, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(
            {"message": "Reminder deleted successfully."},
            status=status.HTTP_204_NO_CONTENT,
        )


class EconomicCalendarEventNotificationListView(generics.ListAPIView):
    """
    GET: List economic calendar event notification tracking records.
    Shows which events had notifications sent and to whom.
    
    Query params: event_id (UUID), notification_type (reminder|event|broadcast), is_sent (true/false), page, page_size.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = EconomicCalendarEventNotificationSerializer
    pagination_class = NewsArticleListPagination

    def get_queryset(self):
        qs = EconomicCalendarEventNotification.objects.select_related('event', 'user').order_by('-sent_at')
        
        # Optional filters
        event_id = self.request.query_params.get("event_id")
        if event_id:
            qs = qs.filter(event_id=event_id)
        
        notification_type = self.request.query_params.get("notification_type")
        if notification_type:
            qs = qs.filter(notification_type=notification_type)
        
        is_sent = self.request.query_params.get("is_sent")
        if is_sent is not None:
            is_sent_bool = is_sent.lower() in ("true", "1", "yes")
            qs = qs.filter(is_sent=is_sent_bool)
        
        return qs


class ForexCategoryNewsView(APIView):
    """
    GET  /api/news/category-news/

    Fetches news by category/section from the EODHD Financial News API
    (``https://eodhd.com/api/news``). Response shape unchanged from the legacy
    ForexNewsAPI category endpoint.

    Query params:
        category: category / topic mapped to EODHD ``t`` parameter
        section : backward-compatible alias for ``category``
        items   : number of items per page (default 50, max 1000)
        page    : page number (default 1)

    Example: ?category=CRYPTO&items=10&page=1
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        denied = check_active_subscription(request.user)
        if denied is not None:
            return denied
        return eodhd_category_news_response_for_request(request)


class LiveNewsListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LiveNewsSerializer
    pagination_class = LiveNewsPagination

    def _get_default_live_news_languages(self):
        user = getattr(self.request, "user", None)
        selected_languages = []
        if getattr(user, "news_notify_ar", False):
            selected_languages.append("ar")
        if getattr(user, "news_notify_en", False):
            selected_languages.append("en")
        if getattr(user, "news_notify_zh", False):
            selected_languages.append("zh")
        return selected_languages or ["ar", "en"]

    def _get_requested_live_news_languages(self):
        language = (self.request.query_params.get("language") or "").strip().lower()
        if language in FRONTEND_LIVE_NEWS_LANGUAGES:
            return [language]
        return self._get_default_live_news_languages()

    def get_queryset(self):
        qs = LiveNews.objects.filter(
            is_active=True,
            language__in=self._get_requested_live_news_languages(),
        )

        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(teaser__icontains=search)
                | Q(body__icontains=search)
            )

        news_type = (self.request.query_params.get("news_type") or "").strip()
        if news_type:
            qs = qs.filter(news_type__iexact=news_type)

        channel = (self.request.query_params.get("channel") or "").strip()
        if channel:
            qs = qs.filter(channels__icontains=channel)

        symbol = (self.request.query_params.get("symbol") or "").strip()
        if symbol:
            qs = qs.filter(securities__icontains=symbol)

        return qs.order_by(
            "-source_updated_at",
            "-source_created_at",
            "-created_at",
        )

    def list(self, request, *args, **kwargs):
        denied = check_active_subscription(request.user)
        if denied is not None:
            return denied
        return super().list(request, *args, **kwargs)


class EconomicCalendarPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class TradaysEconomicCalendarView(generics.ListAPIView):
    """
    GET /api/news/economic-calendar/
    Returns economic calendar events stored in the database.
    Events are synced periodically via: python manage.py fetch_economic_calendar

    Query params:
        date_from   : ISO date string YYYY-MM-DD — filter events on or after this date
        date_to     : ISO date string YYYY-MM-DD — filter events on or before this date
        importance  : none | low | medium | high — filter by importance level
        currency    : e.g. USD, EUR — filter by currency code (case-insensitive)
        search      : free-text search against event_name and country_name
        page        : page number (default 1)
        page_size   : items per page (default 50, max 200)
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = EconomicCalendarEventSerializer
    pagination_class = EconomicCalendarPagination

    def get_queryset(self):
        from django.utils.dateparse import parse_date
        from django.utils import timezone as tz
        import datetime

        qs = EconomicCalendarEvent.objects.all().order_by("release_date")

        # Date filters
        date_from = self.request.query_params.get("date_from")
        date_to = self.request.query_params.get("date_to")

        if date_from:
            parsed = parse_date(date_from)
            if parsed:
                qs = qs.filter(release_date__date__gte=parsed)

        if date_to:
            parsed = parse_date(date_to)
            if parsed:
                qs = qs.filter(release_date__date__lte=parsed)

        # If no date filters, default to today + 20 days
        if not date_from and not date_to:
            now = tz.now()
            qs = qs.filter(
                release_date__gte=now - datetime.timedelta(days=1),
                release_date__lte=now + datetime.timedelta(days=20),
            )

        # Importance filter
        importance = self.request.query_params.get("importance", "").strip().lower()
        if importance in ("none", "low", "medium", "high"):
            qs = qs.filter(importance=importance)

        # Currency filter
        currency = self.request.query_params.get("currency", "").strip().upper()
        if currency:
            qs = qs.filter(currency_code__iexact=currency)

        # Search filter
        search = self.request.query_params.get("search", "").strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(event_name__icontains=search) | Q(country_name__icontains=search)
            )

        return qs


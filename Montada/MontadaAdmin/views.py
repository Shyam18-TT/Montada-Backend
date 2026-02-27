"""
Admin dashboard views: stats cards with date range and percentage change.
"""
from calendar import monthrange
from datetime import timedelta, datetime
from django.utils import timezone
from django.db.models import Avg, Count, Q, OuterRef, Subquery
from django.db.models.functions import Coalesce
from rest_framework import status, generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.contrib.auth import authenticate, get_user_model
from django.shortcuts import get_object_or_404

User = get_user_model()

try:
    from rest_framework_simplejwt.tokens import RefreshToken
except ImportError:
    RefreshToken = None

try:
    from Signals.models import TradingSignal
except ImportError:
    TradingSignal = None

try:
    from Subscriptions.models import Subscription
except ImportError:
    Subscription = None

try:
    from Signals.models import AssetClass, Timeframe
except ImportError:
    AssetClass = None
    Timeframe = None

try:
    from Followers.models import Follow
except ImportError:
    Follow = None

try:
    from Signals.models import AppliedSignal
except ImportError:
    AppliedSignal = None

try:
    from Signals.serializers import TradingSignalSerializer
except ImportError:
    TradingSignalSerializer = None

try:
    from News.models import NewsCategory, NewsArticle
except ImportError:
    NewsCategory = None
    NewsArticle = None

try:
    from News.serializers import NewsArticleCreateSerializer
except ImportError:
    NewsArticleCreateSerializer = None

from .serializers import (
    AdminAnalystListSerializer,
    AdminTraderListSerializer,
    AdminLoginSerializer,
    AdminCreateAnalystSerializer,
    AdminCreateTraderSerializer,
    AdminNewsCategoryCreateSerializer,
    AdminChangeUserPasswordSerializer,
    AdminSuspendUserSerializer,
)


class AdminLoginView(APIView):
    """
    POST: Admin-only login. Accepts email and password.
    Only users with is_staff or is_superuser can log in. Returns JWT tokens.
    Body: { "email": "...", "password": "..." }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = AdminLoginSerializer(data=request.data, context={"request": request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = serializer.validated_data["user"]
        if not (user.is_staff or user.is_superuser):
            return Response(
                {"error": "Admin access only. Your account does not have staff privileges."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if RefreshToken is None:
            return Response(
                {"error": "JWT is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "message": "Admin login successful.",
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "name": user.name or "",
                    "is_staff": user.is_staff,
                    "is_superuser": user.is_superuser,
                },
                "tokens": {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                },
            },
            status=status.HTTP_200_OK,
        )


class AdminChangeUserPasswordView(APIView):
    """
    POST: Admin sets a new password for a user. Body: user_id (UUID), new_password.
    Cannot change password of superuser (to avoid locking out admins).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request):
        serializer = AdminChangeUserPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user_id = serializer.validated_data["user_id"]
        new_password = serializer.validated_data["new_password"]
        user = get_object_or_404(User, id=user_id)
        if user.is_superuser:
            return Response(
                {"error": "Cannot change password of a superuser account."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user.set_password(new_password)
        user.save(update_fields=["password"])
        return Response(
            {"message": "Password updated successfully."},
            status=status.HTTP_200_OK,
        )


class AdminSuspendUserView(APIView):
    """
    POST: Admin suspend or unsuspend a user. Body: user_id (UUID), suspend (true/false).
    Suspended users cannot log in (is_active=False). Cannot suspend superuser.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request):
        serializer = AdminSuspendUserSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user_id = serializer.validated_data["user_id"]
        suspend = serializer.validated_data["suspend"]
        user = get_object_or_404(User, id=user_id)
        if user.is_superuser:
            return Response(
                {"error": "Cannot suspend a superuser account."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user.is_active = not suspend
        user.save(update_fields=["is_active"])
        return Response(
            {
                "message": "User suspended." if suspend else "User activated.",
                "user_id": str(user.id),
                "is_active": user.is_active,
            },
            status=status.HTTP_200_OK,
        )


class AdminCreateAnalystView(APIView):
    """
    POST: Create an analyst from admin. Body: email, password, name (optional), phone_number (optional), is_verified (optional).
    Admin only.
    """
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = AdminCreateAnalystSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user = serializer.save()
        return Response(
            {
                "message": "Analyst created successfully.",
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "name": user.name or "",
                    "user_type": user.user_type,
                    "is_verified": user.is_verified,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class AdminCreateTraderView(APIView):
    """
    POST: Create a trader from admin. Body: email, password, name (optional), phone_number (optional),
    is_verified (optional), subscription_plan (basic | free_trial | premium), trial_days (optional, default 7),
    premium_plan (monthly | yearly, optional when subscription_plan=premium). Admin only.
    """
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = AdminCreateTraderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user = serializer.save()
        subscription_label = "basic"
        try:
            sub = user.subscription
        except Exception:
            sub = None
        if sub:
            if sub.plan_type == "free_trial" or sub.is_trial:
                subscription_label = "free_trial"
            else:
                subscription_label = "premium"
        return Response(
            {
                "message": "Trader created successfully.",
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "name": user.name or "",
                    "user_type": user.user_type,
                    "is_verified": user.is_verified,
                    "subscription": subscription_label,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class AdminPageNumberPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


def _parse_date_range(request):
    """
    Parse query params into (start, end) datetime range and preset label.
    Params: range=today|last_7_days|last_30_days|custom
    For custom: from_date=YYYY-MM-DD&to_date=YYYY-MM-DD
    Returns (start, end, preset, error_response).
    """
    now = timezone.now()
    preset = (request.query_params.get("range") or "last_30_days").strip().lower()

    if preset == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        return start, end, "today", None

    if preset == "last_7_days":
        end = now
        start = end - timedelta(days=7)
        return start, end, "last_7_days", None

    if preset == "last_30_days":
        end = now
        start = end - timedelta(days=30)
        return start, end, "last_30_days", None

    if preset == "custom":
        from_date_s = request.query_params.get("from_date", "").strip()
        to_date_s = request.query_params.get("to_date", "").strip()
        if not from_date_s or not to_date_s:
            return None, None, None, Response(
                {"error": "Custom range requires from_date and to_date (YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            from datetime import datetime
            start = timezone.make_aware(datetime.strptime(from_date_s, "%Y-%m-%d"))
            end_dt = datetime.strptime(to_date_s, "%Y-%m-%d")
            end = timezone.make_aware(end_dt.replace(hour=23, minute=59, second=59, microsecond=999999))
            if start > end:
                return None, None, None, Response(
                    {"error": "from_date must be before or equal to to_date."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return start, end, "custom", None
        except ValueError:
            return None, None, None, Response(
                {"error": "Dates must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # default
    end = now
    start = end - timedelta(days=30)
    return start, end, "last_30_days", None


def _prev_period(start, end):
    """Return (prev_start, prev_end) for the same length before start."""
    delta = end - start
    prev_end = start
    prev_start = prev_end - delta
    return prev_start, prev_end


def _pct_change(current, previous):
    """Return percentage change; 0 if previous is 0."""
    if previous == 0:
        return 0.0 if current == 0 else 100.0
    return round(((current - previous) / previous) * 100, 2)


def _win_rate_for_period(start, end):
    """Return win rate % (wins / (wins+losses)) for closed signals in [start, end], or 0 if no outcomes."""
    if TradingSignal is None:
        return 0
    qs = TradingSignal.active.filter(
        status=TradingSignal.Status.CLOSED,
        updated_at__gte=start,
        updated_at__lte=end,
    )
    wins = qs.filter(is_win=True).count()
    losses = qs.filter(is_loss=True).count()
    total = wins + losses
    if total == 0:
        return 0
    return round((wins / total) * 100, 2)


class WinRateByPeriodView(APIView):
    """
    GET: Win rate (percentage) for last 24 hours, last 7 days, and last 30 days.
    Win rate = (wins / (wins + losses)) * 100 for signals closed in each period.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        now = timezone.now()
        last_24h_end = now
        last_24h_start = now - timedelta(hours=24)
        last_7d_end = now
        last_7d_start = now - timedelta(days=7)
        last_30d_end = now
        last_30d_start = now - timedelta(days=30)

        return Response(
            {
                "win_rate_last_24_hours": _win_rate_for_period(last_24h_start, last_24h_end),
                "win_rate_last_7_days": _win_rate_for_period(last_7d_start, last_7d_end),
                "win_rate_last_30_days": _win_rate_for_period(last_30d_start, last_30d_end),
            },
            status=status.HTTP_200_OK,
        )


class AdminDashboardStatsView(APIView):
    """
    GET: Stats for admin dashboard with date range and increase percentage.
    Query params:
      - range: today | last_7_days | last_30_days (default) | custom
      - from_date, to_date: required when range=custom (YYYY-MM-DD)
    Returns total analysts, total traders, active signals, closed signals, win rate,
    each with current value and increase_pct vs previous period of same length.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        start, end, preset, err = _parse_date_range(request)
        if err is not None:
            return err

        prev_start, prev_end = _prev_period(start, end)

        # User stats: new in period (created_at in range)
        new_users = User.objects.filter(created_at__gte=start, created_at__lte=end).count()
        new_users_prev = User.objects.filter(created_at__gte=prev_start, created_at__lte=prev_end).count()
        analysts_in_period = User.objects.filter(user_type="analyst", created_at__gte=start, created_at__lte=end).count()
        analysts_prev = User.objects.filter(user_type="analyst", created_at__gte=prev_start, created_at__lte=prev_end).count()
        traders_in_period = User.objects.filter(user_type="trader", created_at__gte=start, created_at__lte=end).count()
        traders_prev = User.objects.filter(user_type="trader", created_at__gte=prev_start, created_at__lte=prev_end).count()

        # Active subscriptions: count that were active during the period (overlapped [start, end])
        if Subscription is not None:
            active_subs = Subscription.objects.filter(
                status="active",
                start_date__lte=end,
                end_date__gte=start,
            ).count()
            active_subs_prev = Subscription.objects.filter(
                status="active",
                start_date__lte=prev_end,
                end_date__gte=prev_start,
            ).count()
        else:
            active_subs = 0
            active_subs_prev = 0

        if TradingSignal is None:
            active_created_in_period = 0
            active_created_prev = 0
            closed_in_period = 0
            closed_prev = 0
            win_rate = None
            win_rate_previous = None
            win_rate_increase_pct = None
        else:
            # Signals created in period (for date filter)
            active_created_in_period = TradingSignal.active.filter(created_at__gte=start, created_at__lte=end).count()
            active_created_prev = TradingSignal.active.filter(created_at__gte=prev_start, created_at__lte=prev_end).count()
            # Closed in period (status=CLOSED, updated_at in range)
            closed_in_period = TradingSignal.active.filter(
                status=TradingSignal.Status.CLOSED,
                updated_at__gte=start,
                updated_at__lte=end,
            ).count()
            closed_prev = TradingSignal.active.filter(
                status=TradingSignal.Status.CLOSED,
                updated_at__gte=prev_start,
                updated_at__lte=prev_end,
            ).count()
            # Win rate: closed in period, wins / (wins + losses) * 100
            closed_in_period_qs = TradingSignal.active.filter(
                status=TradingSignal.Status.CLOSED,
                updated_at__gte=start,
                updated_at__lte=end,
            )
            wins = closed_in_period_qs.filter(is_win=True).count()
            losses = closed_in_period_qs.filter(is_loss=True).count()
            total_outcome = wins + losses
            win_rate = round((wins / total_outcome) * 100, 2) if total_outcome else None

            closed_prev_qs = TradingSignal.active.filter(
                status=TradingSignal.Status.CLOSED,
                updated_at__gte=prev_start,
                updated_at__lte=prev_end,
            )
            wins_prev = closed_prev_qs.filter(is_win=True).count()
            losses_prev = closed_prev_qs.filter(is_loss=True).count()
            total_prev = wins_prev + losses_prev
            win_rate_previous = round((wins_prev / total_prev) * 100, 2) if total_prev else None

            if win_rate is not None and win_rate_previous is not None:
                win_rate_increase_pct = round(win_rate - win_rate_previous, 2)  # percentage point change
            else:
                win_rate_increase_pct = None

        return Response(
            {
                "date_range": {
                    "preset": preset,
                    "from_date": start.date().isoformat() if start else None,
                    "to_date": end.date().isoformat() if end else None,
                },
                "stats": {
                    "new_users": new_users,
                    "new_users_increase_pct": _pct_change(new_users, new_users_prev),
                    "total_analysts": analysts_in_period,
                    "total_analysts_increase_pct": _pct_change(analysts_in_period, analysts_prev),
                    "total_traders": traders_in_period,
                    "total_traders_increase_pct": _pct_change(traders_in_period, traders_prev),
                    "active_subscriptions": active_subs,
                    "active_subscriptions_increase_pct": _pct_change(active_subs, active_subs_prev),
                    "active_signals": active_created_in_period if TradingSignal else 0,
                    "active_signals_increase_pct": _pct_change(
                        active_created_in_period if TradingSignal else 0,
                        active_created_prev if TradingSignal else 0,
                    ),
                    "closed_signals": closed_in_period if TradingSignal else 0,
                    "closed_signals_increase_pct": _pct_change(
                        closed_in_period if TradingSignal else 0,
                        closed_prev if TradingSignal else 0,
                    ),
                    "win_rate": win_rate,
                    "win_rate_increase_pct": win_rate_increase_pct,
                },
            },
            status=status.HTTP_200_OK,
        )


class AdminSignalsStatsView(APIView):
    """
    GET: Single API for platform-wide signal stats.
    Returns: total_signals, open_signals, win_rate, average_confidence.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if TradingSignal is None:
            return Response(
                {
                    "total_signals": 0,
                    "open_signals": 0,
                    "win_rate": None,
                    "average_confidence": None,
                },
                status=status.HTTP_200_OK,
            )
        signals = TradingSignal.active.all()
        total_signals = signals.count()
        open_signals = signals.filter(status=TradingSignal.Status.OPEN).count()
        win_count = signals.filter(is_win=True).count()
        loss_count = signals.filter(is_loss=True).count()
        closed_with_outcome = win_count + loss_count
        win_rate = round((win_count / closed_with_outcome) * 100, 2) if closed_with_outcome > 0 else None
        avg_result = signals.aggregate(avg_confidence=Avg("confidence_level"))
        average_confidence = (
            round(avg_result["avg_confidence"], 2) if avg_result["avg_confidence"] is not None else None
        )
        return Response(
            {
                "total_signals": total_signals,
                "open_signals": open_signals,
                "win_rate": win_rate,
                "average_confidence": average_confidence,
            },
            status=status.HTTP_200_OK,
        )


def _last_six_months_ranges():
    """Yield (start, end, year, month, label) for the last 6 calendar months (oldest first)."""
    now = timezone.now()
    for i in range(5, -1, -1):
        year = now.year
        month = now.month - i
        while month <= 0:
            month += 12
            year -= 1
        last_day = monthrange(year, month)[1]
        start = timezone.make_aware(datetime(year, month, 1))
        end = timezone.make_aware(datetime(year, month, last_day, 23, 59, 59, 999999))
        if end > now:
            end = now
        label = f"{year}-{month:02d}"
        yield start, end, year, month, label


class AdminDashboardGraphsView(APIView):
    """
    GET: Graph data for admin dashboard.
    Returns:
    1. user_growth: last 6 months - new analysts and new traders per month.
    2. signals_per_day: last 7 days - total signals created and win signals (closed as win) per day.
    3. signals_by_asset_class: pie chart - count of signals per asset class.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        now = timezone.now()

        # 1. User growth (analyst, trader) last 6 months
        user_growth = []
        for start, end, year, month, label in _last_six_months_ranges():
            analysts = User.objects.filter(
                user_type="analyst",
                created_at__gte=start,
                created_at__lte=end,
            ).count()
            traders = User.objects.filter(
                user_type="trader",
                created_at__gte=start,
                created_at__lte=end,
            ).count()
            user_growth.append({
                "month": label,
                "year": year,
                "month_number": month,
                "analysts": analysts,
                "traders": traders,
            })

        # 2. Signals per day (last 7 days): total created and win (closed as win) per day
        signals_per_day = []
        for i in range(6, -1, -1):  # 7 days ago .. yesterday, today
            day_end = now - timedelta(days=i)
            day_start = day_end.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
            if day_end > now:
                day_end = now
            label = day_start.date().isoformat()
            if TradingSignal is not None:
                total = TradingSignal.active.filter(
                    created_at__gte=day_start,
                    created_at__lte=day_end,
                ).count()
                win = TradingSignal.active.filter(
                    status=TradingSignal.Status.CLOSED,
                    is_win=True,
                    updated_at__gte=day_start,
                    updated_at__lte=day_end,
                ).count()
            else:
                total = 0
                win = 0
            signals_per_day.append({
                "date": label,
                "total": total,
                "win": win,
            })

        # 3. Signals by asset class (pie chart)
        if TradingSignal is not None and AssetClass is not None:
            by_asset = dict(
                TradingSignal.active.values("asset_class_id")
                .annotate(count=Count("id"))
                .values_list("asset_class_id", "count")
            )
            all_assets = AssetClass.objects.filter(is_active=True).order_by("name")
            signals_by_asset_class = [
                {"asset_class": ac.name, "count": by_asset.get(ac.id, 0)}
                for ac in all_assets
            ]
        else:
            signals_by_asset_class = []

        return Response(
            {
                "user_growth": user_growth,
                "signals_per_day": signals_per_day,
                "signals_by_asset_class": signals_by_asset_class,
            },
            status=status.HTTP_200_OK,
        )


class TopAnalystLeaderboardView(APIView):
    """
    GET: Top analyst leaderboard for admin dashboard, ordered by win rate (desc).
    Returns for each analyst: id, name, email, total_signals, followers, win_rate (overall %).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if Follow is None or TradingSignal is None:
            return Response(
                {"leaderboard": []},
                status=status.HTTP_200_OK,
            )

        followers_subq = (
            Follow.objects.filter(
                followed_id=OuterRef("pk"),
                status=Follow.Status.ACCEPTED,
                is_active=True,
            )
            .values("followed_id")
            .annotate(c=Count("id"))
            .values("c")
        )
        signals_subq = (
            TradingSignal.active.filter(analyst_id=OuterRef("pk"))
            .values("analyst_id")
            .annotate(c=Count("id"))
            .values("c")
        )
        wins_subq = (
            TradingSignal.active.filter(
                analyst_id=OuterRef("pk"),
                status=TradingSignal.Status.CLOSED,
                is_win=True,
            )
            .values("analyst_id")
            .annotate(c=Count("id"))
            .values("c")
        )
        losses_subq = (
            TradingSignal.active.filter(
                analyst_id=OuterRef("pk"),
                status=TradingSignal.Status.CLOSED,
                is_loss=True,
            )
            .values("analyst_id")
            .annotate(c=Count("id"))
            .values("c")
        )

        analysts = (
            User.objects.filter(user_type="analyst", is_active=True)
            .annotate(
                followers_count=Coalesce(Subquery(followers_subq), 0),
                total_signals=Coalesce(Subquery(signals_subq), 0),
                wins=Coalesce(Subquery(wins_subq), 0),
                losses=Coalesce(Subquery(losses_subq), 0),
            )
        )

        # Build list with win_rate and sort by win_rate desc (then by total_signals desc as tiebreaker)
        rows = []
        for a in analysts:
            wins = getattr(a, "wins", 0) or 0
            losses = getattr(a, "losses", 0) or 0
            total_outcome = wins + losses
            win_rate = round((wins / total_outcome) * 100, 2) if total_outcome else 0
            rows.append({
                "analyst_id": str(a.id),
                "name": a.name or a.email or str(a.id),
                "email": a.email,
                "total_signals": getattr(a, "total_signals", 0) or 0,
                "followers": getattr(a, "followers_count", 0) or 0,
                "win_rate": win_rate,
                "_sort_key": (win_rate, getattr(a, "total_signals", 0) or 0),
            })
        rows.sort(key=lambda x: x["_sort_key"], reverse=True)
        for r in rows:
            del r["_sort_key"]

        return Response(
            {"leaderboard": rows},
            status=status.HTTP_200_OK,
        )


class AdminAnalystListView(generics.ListAPIView):
    """
    GET: Paginated list of analysts for admin dashboard.
    Query params: page, page_size (optional), search (name or email), status (active|suspended|pending).
    Each item: id, name, email, status (active/inactive), signals_count, followers, win_rate, registered_at, is_verified.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = AdminAnalystListSerializer

    def get_queryset(self):
        qs = User.objects.filter(user_type="analyst").order_by("-created_at")

        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(email__icontains=search)
            )

        status_param = (self.request.query_params.get("status") or "").strip().lower()
        if status_param == "active":
            qs = qs.filter(is_active=True)
        elif status_param == "suspended":
            qs = qs.filter(is_active=False)
        elif status_param == "pending":
            qs = qs.filter(is_verified=False)

        if Follow is not None:
            followers_subq = (
                Follow.objects.filter(
                    followed_id=OuterRef("pk"),
                    status=Follow.Status.ACCEPTED,
                    is_active=True,
                )
                .values("followed_id")
                .annotate(c=Count("id"))
                .values("c")
            )
            qs = qs.annotate(followers_count=Coalesce(Subquery(followers_subq), 0))

        if TradingSignal is not None:
            signals_subq = (
                TradingSignal.active.filter(analyst_id=OuterRef("pk"))
                .values("analyst_id")
                .annotate(c=Count("id"))
                .values("c")
            )
            wins_subq = (
                TradingSignal.active.filter(
                    analyst_id=OuterRef("pk"),
                    status=TradingSignal.Status.CLOSED,
                    is_win=True,
                )
                .values("analyst_id")
                .annotate(c=Count("id"))
                .values("c")
            )
            losses_subq = (
                TradingSignal.active.filter(
                    analyst_id=OuterRef("pk"),
                    status=TradingSignal.Status.CLOSED,
                    is_loss=True,
                )
                .values("analyst_id")
                .annotate(c=Count("id"))
                .values("c")
            )
            qs = qs.annotate(
                signals_count=Coalesce(Subquery(signals_subq), 0),
                wins=Coalesce(Subquery(wins_subq), 0),
                losses=Coalesce(Subquery(losses_subq), 0),
            )

        return qs


class AdminTraderListView(generics.ListAPIView):
    """
    GET: Paginated list of traders for admin dashboard.
    Query params: page, page_size (optional), search (name or email), plan (basic|trial|subscribed).
    Each item: id, name, email, status (active/inactive), subscription (trial|subscribed|basic), signals_applied, registered_at, is_verified.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = AdminTraderListSerializer

    def get_queryset(self):
        qs = User.objects.filter(user_type="trader").order_by("-created_at")

        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(email__icontains=search)
            )

        plan_param = (self.request.query_params.get("plan") or "").strip().lower()
        if plan_param and Subscription is not None:
            now = timezone.now()
            if plan_param == "basic":
                qs = qs.filter(
                    Q(subscription__isnull=True)
                    | ~Q(subscription__status="active")
                    | Q(subscription__end_date__lt=now)
                )
            elif plan_param == "trial":
                qs = qs.filter(
                    subscription__status="active",
                    subscription__end_date__gte=now,
                ).filter(
                    Q(subscription__is_trial=True) | Q(subscription__plan_type="free_trial")
                )
            elif plan_param == "subscribed":
                qs = qs.filter(
                    subscription__status="active",
                    subscription__end_date__gte=now,
                    subscription__plan_type__in=["monthly", "yearly"],
                    subscription__is_trial=False,
                )

        if AppliedSignal is not None:
            applied_subq = (
                AppliedSignal.objects.filter(trader_id=OuterRef("pk"))
                .values("trader_id")
                .annotate(c=Count("id"))
                .values("c")
                .order_by()
            )
            qs = qs.annotate(signals_applied_count=Coalesce(Subquery(applied_subq), 0))

        if Subscription is not None:
            qs = qs.select_related("subscription")

        return qs


class AdminSignalsListView(generics.ListAPIView):
    """
    GET: Paginated list of all signals for admin.
    Query params: page, page_size (optional, default 10), search, status, asset_class, timeframe.
    - search: search in instrument symbol/name, analyst note, analyst email/name.
    - status: OPEN | CLOSED | DRAFT
    - asset_class: UUID of asset class
    - timeframe: UUID of timeframe
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = TradingSignalSerializer

    def get_queryset(self):
        qs = (
            TradingSignal.active
            .select_related("analyst", "asset_class", "instrument", "timeframe")
            .order_by("-created_at")
        )

        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(instrument__symbol__icontains=search)
                | Q(instrument__name__icontains=search)
                | Q(analyst_note__icontains=search)
                | Q(analyst__email__icontains=search)
                | Q(analyst__name__icontains=search)
            )

        status_param = (self.request.query_params.get("status") or "").strip().upper()
        if status_param in ("OPEN", "CLOSED", "DRAFT"):
            qs = qs.filter(status=status_param)

        asset_param = self.request.query_params.get("asset_class") or self.request.query_params.get("asset")
        if asset_param:
            qs = qs.filter(asset_class_id=asset_param)

        timeframe_param = self.request.query_params.get("timeframe")
        if timeframe_param:
            qs = qs.filter(timeframe_id=timeframe_param)

        return qs


class AdminSignalStatusesView(APIView):
    """
    GET: List of signal status choices for filter dropdowns.
    Returns: [{ "value": "OPEN", "label": "Open" }, ...]
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if TradingSignal is None:
            return Response({"statuses": []}, status=status.HTTP_200_OK)
        statuses = [
            {"value": choice[0], "label": choice[1]}
            for choice in TradingSignal.Status.choices
        ]
        return Response({"statuses": statuses}, status=status.HTTP_200_OK)


class AdminSignalAssetsView(APIView):
    """
    GET: List of all asset classes for filter dropdowns.
    Returns: [{ "id": "<uuid>", "name": "..." }, ...]
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if AssetClass is None:
            return Response({"assets": []}, status=status.HTTP_200_OK)
        qs = AssetClass.objects.all().order_by("name")
        assets = [{"id": str(a.id), "name": a.name} for a in qs]
        return Response({"assets": assets}, status=status.HTTP_200_OK)


class AdminSignalTimeframesView(APIView):
    """
    GET: List of all timeframes for filter dropdowns.
    Returns: [{ "id": "<uuid>", "code": "...", "name": "..." }, ...]
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if Timeframe is None:
            return Response({"timeframes": []}, status=status.HTTP_200_OK)
        qs = Timeframe.objects.all().order_by("code")
        timeframes = [
            {"id": str(t.id), "code": t.code, "name": t.name}
            for t in qs
        ]
        return Response({"timeframes": timeframes}, status=status.HTTP_200_OK)


class AdminNewsCategoryListView(generics.ListAPIView):
    """
    GET: List all news categories (id, name, slug) for admin. Ordered by name.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminNewsCategoryCreateSerializer
    queryset = NewsCategory.objects.all().order_by("name") if NewsCategory else []

    def get_queryset(self):
        if NewsCategory is None:
            return []
        return NewsCategory.objects.all().order_by("name")

    def list(self, request, *args, **kwargs):
        if NewsCategory is None or AdminNewsCategoryCreateSerializer is None:
            return Response(
                {"error": "News app is not available.", "categories": []},
                status=status.HTTP_200_OK,
            )
        return super().list(request, *args, **kwargs)


class AdminNewsCategoryCreateView(generics.CreateAPIView):
    """
    POST: Create a news category. Admin only.
    Body: { "name": "Category Name", "slug": "optional-slug" }
    Slug is optional; if omitted, it is generated from name.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminNewsCategoryCreateSerializer
    queryset = NewsCategory.objects.all() if NewsCategory else []

    def create(self, request, *args, **kwargs):
        if NewsCategory is None or AdminNewsCategoryCreateSerializer is None:
            return Response(
                {"error": "News app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            {
                "message": "News category created successfully.",
                "category": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminNewsArticleCreateView(generics.CreateAPIView):
    """
    POST: Create a news article. Admin only. Same fields as analyst create.
    Sets author to the authenticated admin user.
    Body: title, content, summary (optional), slug (optional), featured_image, category, tags, status, is_featured.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = NewsArticleCreateSerializer
    queryset = NewsArticle.objects.all() if NewsArticle else []

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)

    def create(self, request, *args, **kwargs):
        if NewsArticle is None or NewsArticleCreateSerializer is None:
            return Response(
                {"error": "News app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
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

"""
Admin dashboard views: stats cards with date range and percentage change.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from calendar import monthrange
from datetime import timedelta, datetime
from django.conf import settings as django_settings
from django.utils import timezone
from django.db import connections
from django.db.models import Avg, Count, Q, OuterRef, Subquery, Prefetch
from django.db.models.functions import Coalesce
from rest_framework import status, generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.contrib.auth import authenticate, get_user_model
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

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
    from Signals.models import AssetClass, Instrument, Timeframe
except ImportError:
    AssetClass = None
    Instrument = None
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

try:
    from Dashboard.models import PollQuestion, PollOption, PollResponse
except ImportError:
    PollQuestion = None
    PollOption = None
    PollResponse = None

from .serializers import (
    AdminAnalystListSerializer,
    AdminTraderListSerializer,
    AdminLoginSerializer,
    AdminCreateAnalystSerializer,
    AdminCreateTraderSerializer,
    AdminCreateSignalSerializer,
    AdminNewsCategoryCreateSerializer,
    AdminNewsArticleListSerializer,
    AdminChangeUserPasswordSerializer,
    AdminSuspendUserSerializer,
    AdminUserProfileSerializer,
)


@method_decorator(csrf_exempt, name="dispatch")
class AdminLoginView(APIView):
    """
    POST: Admin-only login. Accepts email and password.
    Only users with is_staff or is_superuser can log in. Returns JWT tokens.
    Body: { "email": "...", "password": "..." }
    """
    permission_classes = [AllowAny]
    authentication_classes = []

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


class AdminUserProfileView(generics.RetrieveAPIView):
    """
    GET: View profile details of a user by id. Admin only.
    URL: user_id in path (UUID).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminUserProfileSerializer
    queryset = User.objects.all()
    lookup_url_kwarg = "user_id"
    lookup_field = "id"


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
    GET: Stats for admin dashboard.
    - total_traders and total_analysts are all-time counts (no date filter).
    - Date range (range / from_date / to_date) is used only for increase_pct:
      total_traders_increase_pct and total_analysts_increase_pct compare new signups
      in the selected period vs the previous period of same length.
    Query params: range (today | last_7_days | last_30_days | custom), from_date, to_date.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        start, end, preset, err = _parse_date_range(request)
        if err is not None:
            return err

        prev_start, prev_end = _prev_period(start, end)

        # Total traders/analysts: all-time counts (no date filter)
        total_traders = User.objects.filter(user_type="trader").count()
        total_analysts = User.objects.filter(user_type="analyst").count()

        # New users in period (for new_users stat and for increase_pct)
        new_users = User.objects.filter(created_at__gte=start, created_at__lte=end).count()
        new_users_prev = User.objects.filter(created_at__gte=prev_start, created_at__lte=prev_end).count()
        # New analysts/traders in range (used only for increase_pct vs previous period)
        new_analysts_in_period = User.objects.filter(user_type="analyst", created_at__gte=start, created_at__lte=end).count()
        new_analysts_prev = User.objects.filter(user_type="analyst", created_at__gte=prev_start, created_at__lte=prev_end).count()
        new_traders_in_period = User.objects.filter(user_type="trader", created_at__gte=start, created_at__lte=end).count()
        new_traders_prev = User.objects.filter(user_type="trader", created_at__gte=prev_start, created_at__lte=prev_end).count()

        # Active subscriptions: paid (subscribed) users only, excluding trial (is_trial=False)
        # Active trial users: users currently on trial (is_trial=True)
        if Subscription is not None:
            active_subs = Subscription.objects.filter(
                status="active",
                is_trial=False,
                start_date__lte=end,
                end_date__gte=start,
            ).count()
            active_subs_prev = Subscription.objects.filter(
                status="active",
                is_trial=False,
                start_date__lte=prev_end,
                end_date__gte=prev_start,
            ).count()
            active_trial_users = Subscription.objects.filter(
                status="active",
                is_trial=True,
                start_date__lte=end,
                end_date__gte=start,
            ).count()
            active_trial_users_prev = Subscription.objects.filter(
                status="active",
                is_trial=True,
                start_date__lte=prev_end,
                end_date__gte=prev_start,
            ).count()
        else:
            active_subs = 0
            active_subs_prev = 0
            active_trial_users = 0
            active_trial_users_prev = 0

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
                    "total_analysts": total_analysts,
                    "total_analysts_increase_pct": _pct_change(new_analysts_in_period, new_analysts_prev),
                    "total_traders": total_traders,
                    "total_traders_increase_pct": _pct_change(new_traders_in_period, new_traders_prev),
                    "active_subscriptions": active_subs,
                    "active_subscriptions_increase_pct": _pct_change(active_subs, active_subs_prev),
                    "active_trial_users": active_trial_users,
                    "active_trial_users_increase_pct": _pct_change(active_trial_users, active_trial_users_prev),
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


class AdminSignalInstrumentsView(APIView):
    """
    GET: List of instruments for admin (e.g. signal create form).
    Query param: asset_class (UUID) to filter by asset class.
    Returns: [{ "id": "<uuid>", "symbol": "...", "name": "...", "asset_class": "<uuid>", "asset_class_name": "..." }, ...]
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if Instrument is None:
            return Response({"instruments": []}, status=status.HTTP_200_OK)
        qs = Instrument.objects.filter(is_active=True).select_related("asset_class").order_by("asset_class__name", "symbol")
        asset_class_id = request.query_params.get("asset_class")
        if asset_class_id:
            qs = qs.filter(asset_class_id=asset_class_id)
        instruments = [
            {
                "id": str(i.id),
                "symbol": i.symbol,
                "name": i.name or i.symbol,
                "asset_class": str(i.asset_class_id),
                "asset_class_name": i.asset_class.name if i.asset_class else None,
            }
            for i in qs
        ]
        return Response({"instruments": instruments}, status=status.HTTP_200_OK)


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


class AdminCreateSignalView(generics.CreateAPIView):
    """
    POST: Create a trading signal from admin. Analyst (created_by) is sent in the body.
    Body: analyst (UUID of analyst user), asset_class, instrument, direction, entry_price,
    stop_loss, take_profit, timeframe, confidence_level, analyst_note (optional), status (optional).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminCreateSignalSerializer

    def create(self, request, *args, **kwargs):
        if TradingSignal is None or AdminCreateSignalSerializer is None:
            return Response(
                {"error": "Signals app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "message": "Signal created successfully.",
                "signal": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )


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


class AdminNewsArticleStatsView(APIView):
    """
    GET: News article stats for admin. One response: total_articles, draft_count, published_count (and archived_count).
    Counts exclude soft-deleted (is_deleted=True).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if NewsArticle is None:
            return Response(
                {"error": "News app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        base = NewsArticle.objects.filter(is_deleted=False)
        stats = base.aggregate(
            total=Count("id"),
            draft_count=Count("id", filter=Q(status="draft")),
            published_count=Count("id", filter=Q(status="published")),
            archived_count=Count("id", filter=Q(status="archived")),
        )
        return Response(
            {
                "total_articles": stats["total"] or 0,
                "draft_count": stats["draft_count"] or 0,
                "published_count": stats["published_count"] or 0,
                "archived_count": stats["archived_count"] or 0,
            },
            status=status.HTTP_200_OK,
        )


class AdminPollStatsView(APIView):
    """
    GET: Poll stats for admin. One response: total_polls, active_polls, closed_polls_count, total_votes.
    active_polls = questions with is_active=True, closed_polls_count = is_active=False.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if PollQuestion is None or PollResponse is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        total_polls = PollQuestion.objects.count()
        active_polls = PollQuestion.objects.filter(is_active=True).count()
        closed_polls_count = PollQuestion.objects.filter(is_active=False).count()
        total_votes = PollResponse.objects.count()
        return Response(
            {
                "total_polls": total_polls,
                "active_polls": active_polls,
                "closed_polls_count": closed_polls_count,
                "total_votes": total_votes,
            },
            status=status.HTTP_200_OK,
        )


class AdminPollsListView(APIView):
    """
    GET: List poll questions for admin. Each result is a question with its options and vote counts.
    Query params: search, status (all|active|closed|unpublished), page, page_size.
    - all: all polls (default)
    - active: is_active=True (published, accepting votes)
    - closed: is_active=False
    - unpublished: is_active=False (same as closed in current model)
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get_queryset(self):
        if PollQuestion is None or PollOption is None:
            return PollQuestion.objects.none() if PollQuestion else []
        options_with_votes = PollOption.objects.annotate(vote_count=Count("responses"))
        qs = (
            PollQuestion.objects.all()
            .order_by("order")
            .prefetch_related(Prefetch("options", queryset=options_with_votes))
        )
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(question_text__icontains=search))
        status_param = (self.request.query_params.get("status") or "all").strip().lower()
        if status_param == "active":
            qs = qs.filter(is_active=True)
        elif status_param in ("closed", "unpublished"):
            qs = qs.filter(is_active=False)
        # "all" or any other value: no filter
        return qs

    def get(self, request):
        if PollQuestion is None or PollOption is None:
            return Response(
                {"error": "Dashboard/Poll app is not available.", "results": [], "count": 0},
                status=status.HTTP_200_OK,
            )
        qs = self.get_queryset()
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        if page is None:
            page = list(qs)
            paginated = False
        else:
            paginated = True
        result = []
        for q in page:
            opts = list(q.options.all())
            total_votes_q = sum(getattr(opt, "vote_count", 0) for opt in opts)
            options_data = [
                {
                    "id": str(opt.id),
                    "option_text": opt.option_text,
                    "vote_count": getattr(opt, "vote_count", 0),
                    "vote_percentage": round(
                        (getattr(opt, "vote_count", 0) / total_votes_q) * 100, 2
                    ) if total_votes_q else 0,
                }
                for opt in opts
            ]
            result.append({
                "id": str(q.id),
                "question_text": q.question_text,
                "question_type": q.question_type,
                "order": q.order,
                "is_active": getattr(q, "is_active", True),
                "options": options_data,
                "total_votes": total_votes_q,
            })
        if paginated:
            return paginator.get_paginated_response(result)
        return Response({"results": result, "count": len(result)}, status=status.HTTP_200_OK)


class AdminPollCreateView(APIView):
    """
    POST: Create a new poll question with options. Admin only.
    Body: question_text (required), question_type (optional: "single"|"multiple", default "single"),
          order (optional, default 0), is_active (optional, default True),
          options (optional list of { "option_text": "..." }).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def _question_to_data(self, q):
        opts = list(q.options.all())
        total_votes_q = sum(getattr(opt, "vote_count", 0) for opt in opts)
        options_data = [
            {
                "id": str(opt.id),
                "option_text": opt.option_text,
                "vote_count": getattr(opt, "vote_count", 0),
                "vote_percentage": round(
                    (getattr(opt, "vote_count", 0) / total_votes_q) * 100, 2
                ) if total_votes_q else 0,
            }
            for opt in opts
        ]
        return {
            "id": str(q.id),
            "question_text": q.question_text,
            "question_type": q.question_type,
            "order": q.order,
            "is_active": getattr(q, "is_active", True),
            "options": options_data,
            "total_votes": total_votes_q,
        }

    def post(self, request):
        if PollQuestion is None or PollOption is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        data = request.data
        question_text = (data.get("question_text") or "").strip()
        if not question_text:
            return Response(
                {"error": "question_text is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        question_type = (data.get("question_type") or "single").strip().lower()
        if question_type not in ("single", "multiple"):
            return Response(
                {"error": "question_type must be 'single' or 'multiple'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            order = int(data.get("order", 0))
        except (TypeError, ValueError):
            order = 0
        is_active = data.get("is_active", True)
        if isinstance(is_active, str):
            is_active = is_active.lower() in ("true", "1", "yes")

        question = PollQuestion.objects.create(
            question_text=question_text,
            question_type=question_type,
            order=order,
            is_active=is_active,
        )
        options_payload = data.get("options") or []
        if isinstance(options_payload, list):
            for item in options_payload:
                if isinstance(item, dict):
                    opt_text = (item.get("option_text") or "").strip()
                elif isinstance(item, str):
                    opt_text = item.strip()
                else:
                    continue
                if opt_text:
                    PollOption.objects.create(question=question, option_text=opt_text)

        qs = PollQuestion.objects.prefetch_related(
            Prefetch("options", queryset=PollOption.objects.annotate(vote_count=Count("responses")))
        )
        question = qs.get(pk=question.pk)
        return Response(
            {
                "message": "Poll created successfully.",
                "question": self._question_to_data(question),
            },
            status=status.HTTP_201_CREATED,
        )


class AdminPollQuestionDetailView(APIView):
    """
    GET: Retrieve a single poll question by id with options and vote counts.
    PUT/PATCH: Update question (question_text, question_type, order) and options.
    Body for update: { "question_text": "...", "question_type": "single"|"multiple", "order": 0, "options": [ {"id": "uuid", "option_text": "..."}, {"option_text": "..."} ] }
    Options with id are updated; options without id are created; options not in the list are deleted.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get_question_queryset(self):
        if PollQuestion is None or PollOption is None:
            return PollQuestion.objects.none() if PollQuestion else []
        options_with_votes = PollOption.objects.annotate(vote_count=Count("responses"))
        return PollQuestion.objects.prefetch_related(
            Prefetch("options", queryset=options_with_votes)
        )

    def _question_to_data(self, q):
        opts = list(q.options.all())
        total_votes_q = sum(getattr(opt, "vote_count", 0) for opt in opts)
        options_data = [
            {
                "id": str(opt.id),
                "option_text": opt.option_text,
                "vote_count": getattr(opt, "vote_count", 0),
                "vote_percentage": round(
                    (getattr(opt, "vote_count", 0) / total_votes_q) * 100, 2
                ) if total_votes_q else 0,
            }
            for opt in opts
        ]
        return {
            "id": str(q.id),
            "question_text": q.question_text,
            "question_type": q.question_type,
            "order": q.order,
            "is_active": getattr(q, "is_active", True),
            "options": options_data,
            "total_votes": total_votes_q,
        }

    def get(self, request, pk):
        if PollQuestion is None or PollOption is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        qs = self.get_question_queryset()
        question = get_object_or_404(qs, pk=pk)
        return Response(self._question_to_data(question), status=status.HTTP_200_OK)

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, partial):
        if PollQuestion is None or PollOption is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        question = get_object_or_404(PollQuestion, pk=pk)
        data = request.data

        if "question_text" in data:
            question.question_text = data["question_text"]
        if "question_type" in data:
            qtype = data["question_type"]
            if qtype not in ("single", "multiple"):
                return Response(
                    {"error": "question_type must be 'single' or 'multiple'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            question.question_type = qtype
        if "order" in data:
            try:
                question.order = int(data["order"])
            except (TypeError, ValueError):
                return Response(
                    {"error": "order must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        question.save()

        if "options" in data:
            options_payload = data["options"]
            if not isinstance(options_payload, list):
                return Response(
                    {"error": "options must be a list."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            seen_ids = set()
            for item in options_payload:
                if not isinstance(item, dict):
                    continue
                opt_text = (item.get("option_text") or "").strip()
                if not opt_text:
                    continue
                opt_id = item.get("id")
                if opt_id:
                    try:
                        o_uuid = uuid.UUID(str(opt_id)) if isinstance(opt_id, str) else opt_id
                    except (ValueError, TypeError):
                        continue
                    if PollOption.objects.filter(question=question, id=o_uuid).exists():
                        PollOption.objects.filter(id=o_uuid).update(option_text=opt_text)
                        seen_ids.add(o_uuid)
                else:
                    new_opt = PollOption.objects.create(question=question, option_text=opt_text)
                    seen_ids.add(new_opt.id)
            question.options.exclude(id__in=seen_ids).delete()

        qs = self.get_question_queryset()
        question = qs.get(pk=question.pk)
        return Response(
            {"message": "Poll question updated.", "question": self._question_to_data(question)},
            status=status.HTTP_200_OK,
        )

    def delete(self, request, pk):
        """DELETE: Permanently delete a poll question (and its options and responses via CASCADE)."""
        if PollQuestion is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        question = get_object_or_404(PollQuestion, pk=pk)
        question_id = str(question.id)
        question.delete()
        return Response(
            {"message": "Poll deleted.", "id": question_id},
            status=status.HTTP_200_OK,
        )


class AdminPollUnpublishView(APIView):
    """
    POST: Unpublish a poll (set is_active=False). Poll will no longer appear in active list or accept votes.
    URL: polls/<pk>/unpublish/
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, pk):
        if PollQuestion is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        question = get_object_or_404(PollQuestion, pk=pk)
        question.is_active = False
        question.save(update_fields=["is_active"])
        return Response(
            {
                "message": "Poll unpublished.",
                "id": str(question.id),
                "is_active": False,
            },
            status=status.HTTP_200_OK,
        )


class AdminPollOptionAddView(APIView):
    """
    POST: Add one or more options to a poll question.
    URL: polls/<question_pk>/options/
    Body (single): { "option_text": "New option" }
    Body (multiple): { "options": [ {"option_text": "Option A"}, {"option_text": "Option B"} ] }
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, question_pk):
        if PollQuestion is None or PollOption is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        question = get_object_or_404(PollQuestion, pk=question_pk)
        data = request.data

        to_create = []
        if "options" in data and isinstance(data["options"], list):
            for item in data["options"]:
                if isinstance(item, dict):
                    opt_text = (item.get("option_text") or "").strip()
                    if opt_text:
                        to_create.append(opt_text)
                elif isinstance(item, str) and item.strip():
                    to_create.append(item.strip())
        elif "option_text" in data:
            opt_text = (data.get("option_text") or "").strip()
            if opt_text:
                to_create.append(opt_text)

        if not to_create:
            return Response(
                {"error": "Provide 'option_text' or 'options' (list of { option_text } or strings)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = []
        for opt_text in to_create:
            opt = PollOption.objects.create(question=question, option_text=opt_text)
            created.append({"id": str(opt.id), "option_text": opt.option_text})

        return Response(
            {"message": f"Added {len(created)} option(s).", "options": created},
            status=status.HTTP_201_CREATED,
        )


class AdminPollOptionDeleteView(APIView):
    """
    DELETE: Remove an option from a poll question.
    URL: polls/<question_pk>/options/<option_pk>/
    Option must belong to the question. Responses for this option are deleted (CASCADE).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def delete(self, request, question_pk, option_pk):
        if PollQuestion is None or PollOption is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        get_object_or_404(PollQuestion, pk=question_pk)
        option = get_object_or_404(PollOption, pk=option_pk, question_id=question_pk)
        option.delete()
        return Response(
            {"message": "Option deleted."},
            status=status.HTTP_200_OK,
        )


class AdminPollQuestionCloseView(APIView):
    """
    POST: Close or reopen a poll question.
    URL: polls/<pk>/close/
    Body (optional): { "reopen": true } to reopen; otherwise closes the poll (is_active=False).
    Closed polls are excluded from active list and reject new votes.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, pk):
        if PollQuestion is None:
            return Response(
                {"error": "Dashboard/Poll app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        question = get_object_or_404(PollQuestion, pk=pk)
        reopen = request.data.get("reopen") is True
        question.is_active = reopen
        question.save(update_fields=["is_active"])
        return Response(
            {
                "message": "Poll reopened." if reopen else "Poll closed.",
                "id": str(question.id),
                "is_active": question.is_active,
            },
            status=status.HTTP_200_OK,
        )


class AdminNewsArticleListView(generics.ListAPIView):
    """
    GET: List news articles for admin. Paginated.
    Query params: search (title/summary/content), category (UUID of category), page, page_size.
    Excludes soft-deleted (is_deleted=True). Ordered by created_at desc. Response excludes tags and is_featured.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminNewsArticleListSerializer
    pagination_class = AdminPageNumberPagination

    def get_queryset(self):
        if NewsArticle is None:
            return []
        qs = (
            NewsArticle.objects.filter(is_deleted=False)
            .select_related("author", "category")
            .order_by("-created_at")
        )
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

    def list(self, request, *args, **kwargs):
        if NewsArticle is None or AdminNewsArticleListSerializer is None:
            return Response(
                {"error": "News app is not available.", "results": [], "count": 0},
                status=status.HTTP_200_OK,
            )
        return super().list(request, *args, **kwargs)


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


class AdminNewsArticleDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET   : Retrieve a single news article by id.
    PUT   : Full update of article details.
    PATCH : Partial update of article details.
    DELETE: Soft-delete the article (sets is_deleted=True, keeps the DB row).
    Admin only.  URL: news/articles/<id>/
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = NewsArticleCreateSerializer
    lookup_url_kwarg = "pk"
    lookup_field = "pk"

    def get_queryset(self):
        if NewsArticle is None:
            return User.objects.none()
        return NewsArticle.objects.filter(is_deleted=False).select_related("author", "category").prefetch_related("tags")

    def retrieve(self, request, *args, **kwargs):
        if NewsArticle is None or NewsArticleCreateSerializer is None:
            return Response(
                {"error": "News app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return super().retrieve(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        if NewsArticle is None or NewsArticleCreateSerializer is None:
            return Response(
                {"error": "News app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        partial = kwargs.get("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"message": "Article updated successfully.", "article": serializer.data},
            status=status.HTTP_200_OK,
        )

    def destroy(self, request, *args, **kwargs):
        if NewsArticle is None:
            return Response(
                {"error": "News app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        instance = self.get_object()
        instance.is_deleted = True
        instance.save(update_fields=["is_deleted"])
        return Response(
            {"message": "Article deleted successfully."},
            status=status.HTTP_200_OK,
        )


class AdminNewsArticleUnpublishView(APIView):
    """
    POST: Unpublish a published news article (sets status back to 'draft').
    Admin only.  URL: news/articles/<pk>/unpublish/
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, pk):
        if NewsArticle is None:
            return Response(
                {"error": "News app is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        article = get_object_or_404(NewsArticle, pk=pk, is_deleted=False)
        if article.status != "published":
            return Response(
                {"error": f"Article is already '{article.status}' and cannot be unpublished."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        article.status = "draft"
        article.published_at = None
        article.save(update_fields=["status", "published_at"])
        return Response(
            {"message": "Article unpublished successfully.", "status": article.status},
            status=status.HTTP_200_OK,
        )


# Market data symbols by category (same as Dashboard; used with ?category=forex|shares|metals|...)
MARKET_DATA_SYMBOLS_BY_CATEGORY = {
    'forex': [
        'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'USDCAD', 'AUDUSD', 'NZDUSD', 'EURAUD', 'EURCAD', 'EURCHF',
        'EURGBP', 'EURJPY', 'EURNZD', 'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPJPY', 'AUDCAD', 'AUDCHF', 'AUDNZD',
        'NZDCAD', 'NZDCHF', 'CADCHF', 'CADJPY', 'CHFJPY', 'AUDNOK', 'AUDSEK', 'AUDSGD', 'CADSGD', 'CHFNOK',
        'CHFSGD', 'EURCZK', 'EURHUF', 'EURNOK', 'EURPLN', 'EURSEK', 'EURSGD', 'EURTRY', 'EURZAR', 'GBPHUF',
        'GBPMXN', 'GBPNOK', 'GBPPLN', 'GBPSEK', 'GBPSGD', 'NOKJPY', 'NOKSEK', 'SGDJPY', 'TRYJPY', 'USDCNH',
        'USDCZK', 'USDHKD', 'USDHUF', 'USDMXN', 'USDNOK', 'USDPLN', 'USDRON', 'USDSEK', 'USDSGD', 'USDTHB',
        'USDTRY', 'USDZAR', 'ZARJPY',
    ],
    'shares': [
        'AAL', 'AAPL', 'ABNB', 'ADBE', 'AIG', 'AMZN', 'AXP', 'BA', 'BABA', 'BAC', 'BK', 'BKNG', 'BMRN', 'BMY',
        'CAT', 'CME', 'COST', 'CSCO', 'DAL', 'DELL', 'DIS', 'EBAY', 'FDX', 'GE', 'GM', 'GOOG', 'GOOGL', 'GPRO',
        'GS', 'GT', 'HD', 'HLT', 'HOG', 'HPQ', 'IBM', 'INTC', 'JNJ', 'JPM', 'KMI', 'KO', 'MA', 'MCD', 'MCO',
        'MMM', 'MO', 'MRK', 'MRVL', 'MS', 'MSFT', 'NFLX', 'NKE', 'NVDA', 'ORCL', 'PEP', 'PFE', 'PM', 'PYPL',
        'QCOM', 'RACE', 'ROKU', 'SBUX', 'SHOP', 'SONY', 'SPOT', 'SQ', 'TMUS', 'TSLA', 'UA', 'UAL', 'UBER', 'UPS',
        'VALE', 'VZ', 'WFC', 'WMT', 'XOM', 'YUM', 'ZM', 'ADSGn', 'AIRF', 'ALVG', 'BAYGn', 'BMWG', 'BNPP', 'CBKG',
        'DAIGn', 'DANO', 'DBKGn', 'DPWGn', 'EONGn', 'IBE', 'LHAG', 'LVMH', 'MAP', 'SAN', 'SIEGn', 'SOGN', 'TEF',
        'TOTF', 'VOWG',
    ],
    'metals': ['GOLD', 'SILVER', 'XAUEUR', 'PLATINUM', 'PALLADIUM', 'COPPER'],
    'indices': [
        'US30', 'US100', 'US500', 'US2000', 'GER40', 'FRA40', 'NETH25', 'SPA35', 'EU50', 'SWI20', 'UK100',
        'JAP225', 'AUS200', 'HKIND', 'CHINAAS', 'USDIDX', 'DOW', 'NASDAQ', 'S&P', 'DAX', 'CAC', 'FTSE', 'AUS',
    ],
    'commodity': ['SOYBEAN', 'COCOA', 'COFFEE'],
    'energy': ['CL', 'USOIL', 'BRENT', 'UKOIL', 'NATGAS'],
    'menashares': [
        'CBD', 'DEWA', 'DIB', 'DU', 'Emaar.Devel', 'Emaar.Propt', 'GULFNAV', 'NBD.Bank', 'Parkin', 'Salik',
        'Taaleem', 'Tecom.Group', 'AD.Aviation', 'AD.Insuranc', 'AD.Natl.Tak', 'AD.Ship', 'ADCB', 'ADIB',
        'ADNOC.Drill', 'ADNOC.Gas', 'ADNOC.Logis', 'Agthia.Grp', 'Alpha.Dhabi', 'Apex', 'Chimera', 'FAB.Bank',
        'Ghitha.Hold', 'IHC', 'Modon', 'NMDC', 'Palms.Sport', 'Pure.Health', 'RAK.Bank', 'RPH',
    ],
}

# Categories for which flag is set to "" when returning all (no category filter)
MARKET_DATA_EXCLUDE_FLAGS = ('shares', 'commodity', 'metals', 'indices', 'energy')

# Keys returned per symbol (same structure as Dashboard GetMarketDataFromMT5)
LIVE_QUOTE_KEYS = ('dir', 'bid', 'ask', 'digits', 'flag', 'ask_today', 'bid_today', 'change', 'change_percentage')


def _admin_serialize_market_value(val):
    """Convert DB values to JSON-serializable types (e.g. Decimal, datetime)."""
    from decimal import Decimal
    from datetime import date, datetime, time
    if val is None:
        return None
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (datetime, date, time)):
        return val.isoformat()
    if hasattr(val, '__iter__') and not isinstance(val, (str, bytes)):
        try:
            return [_admin_serialize_market_value(v) for v in val]
        except Exception:
            pass
    return val


def _admin_row_val(row, *keys):
    """Get value from row dict by first matching key (case-insensitive)."""
    key_map = {k.lower(): k for k in row.keys()}
    for key in keys:
        k_lower = key.lower()
        if k_lower in key_map:
            return row.get(key_map[k_lower])
    return None


def _admin_enrich_market_row(row, default_round_digits=4):
    """
    Add extra fields to DB row to match Dashboard GetMarketDataFromMT5: dir, bid, ask, digits,
    flag, ask_today, bid_today, change, change_percentage.
    dir from AskDir (0=down, else up); change = ask_current - ask_today;
    change_symbol = "+" if change >= 0 else ""; percentage_symbol = "" if change >= 0 else "-".
    """
    symbol_val = _admin_row_val(row, 'Symbol', 'symbol')
    symbol = (symbol_val or '').strip() if symbol_val is not None else ''
    ask_last_val = _admin_row_val(row, 'AskLast')
    bid_last_val = _admin_row_val(row, 'BidLast')
    ask_dir_val = _admin_row_val(row, 'AskDir')
    ask_today_val = _admin_row_val(row, 'AskOpen', 'Open', 'ask_open')
    if ask_today_val is None:
        ask_today_val = _admin_row_val(row, 'AskLow')
    if ask_today_val is None:
        ask_today_val = ask_last_val
    bid_today_val = _admin_row_val(row, 'BidOpen', 'bid_open')
    if bid_today_val is None:
        bid_today_val = _admin_row_val(row, 'BidLow')
    if bid_today_val is None:
        bid_today_val = bid_last_val

    digits_val = _admin_row_val(row, 'Digits')
    try:
        round_digits = int(digits_val) if digits_val is not None else default_round_digits
    except (TypeError, ValueError):
        round_digits = default_round_digits

    try:
        bid_last = float(bid_last_val) if bid_last_val is not None else None
        ask_last = float(ask_last_val) if ask_last_val is not None else None
        ask_today = float(ask_today_val) if ask_today_val is not None else None
        bid_today = float(bid_today_val) if bid_today_val is not None else None
    except (TypeError, ValueError):
        bid_last = ask_last = ask_today = bid_today = None

    bid = round(bid_last, round_digits) if bid_last is not None else None
    ask = round(ask_last, round_digits) if ask_last is not None else None
    ask_today_rounded = round(ask_today, round_digits) if ask_today is not None else None

    row['bid'] = bid
    row['ask'] = ask
    row['digits'] = round_digits
    row['flag'] = f"{symbol[0:2]}|{symbol[3:5]}" if len(symbol) >= 6 else ""

    row['ask_today'] = ask_today_rounded
    row['bid_today'] = round(bid_today, round_digits) if bid_today is not None else None

    try:
        ask_dir = ask_dir_val if ask_dir_val is None else int(float(ask_dir_val))
    except (TypeError, ValueError):
        ask_dir = None
    if ask_dir is not None:
        row['dir'] = 'down' if ask_dir == 0 else 'up'
    else:
        row['dir'] = 'up'

    if ask is None or ask_today_rounded is None:
        row['change'] = '0'
        row['change_percentage'] = '0'
        return row

    ask_current = ask
    change = ask_current - ask_today_rounded
    change_absolute = abs(change)
    change_percentage = (change_absolute / ask_today_rounded) * 100 if ask_today_rounded else 0

    change_symbol = "+" if change >= 0 else ""
    percentage_symbol = "" if change >= 0 else "-"
    row['change'] = f"{change_symbol}{round(change, 4)}"
    row['change_percentage'] = f"{percentage_symbol}{round(change_percentage, 2)}"
    return row


class AdminGetMarketDataFromMT5(APIView):
    """
    Returns mt5_prices data as a single object:
    - live_quote: dict of symbol -> { dir, bid, ask, digits, flag, ask_today, bid_today, change, change_percentage }
    Query param category (optional): forex | shares | metals | indices | commodity | energy | menashares
    When category is passed: only that category's symbols; exclude_flags not applied.
    When category is omitted: all symbols; exclude_flags applied.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        category = (request.query_params.get('category') or '').strip().lower()
        if category:
            if category not in MARKET_DATA_SYMBOLS_BY_CATEGORY:
                return Response(
                    {
                        'error': f'Invalid category: {category}.',
                        'valid_categories': list(MARKET_DATA_SYMBOLS_BY_CATEGORY.keys()),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            symbols = list(MARKET_DATA_SYMBOLS_BY_CATEGORY[category])
            apply_exclude_flags = False
        else:
            all_symbols = set()
            for syms in MARKET_DATA_SYMBOLS_BY_CATEGORY.values():
                all_symbols.update(syms)
            symbols = list(all_symbols)
            apply_exclude_flags = True

        if not symbols:
            return Response({'live_quote': {}}, status=status.HTTP_200_OK)
        placeholders = ','.join(['%s'] * len(symbols))
        sql = "SELECT * FROM mt5_prices WHERE Symbol IN (%s)" % placeholders
        try:
            with connections['mt5clients'].cursor() as cursor:
                cursor.execute(sql, symbols)
                columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()
        except Exception as e:
            return Response(
                {'error': 'Failed to fetch market data.', 'detail': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        arr_symbols = {}
        for row in rows:
            row_dict = dict(zip(columns, row))
            item = {k: _admin_serialize_market_value(v) for k, v in row_dict.items()}
            _admin_enrich_market_row(item, default_round_digits=4)
            symbol = _admin_row_val(item, 'Symbol', 'symbol') or ''
            symbol = symbol.strip() if isinstance(symbol, str) else str(symbol)
            if not symbol:
                continue
            out = {k: item.get(k) for k in LIVE_QUOTE_KEYS if k in item}
            arr_symbols[symbol] = out

        if not category and apply_exclude_flags:
            for category_code, instruments in MARKET_DATA_SYMBOLS_BY_CATEGORY.items():
                if category_code not in MARKET_DATA_EXCLUDE_FLAGS:
                    continue
                for symbol in instruments:
                    if symbol in arr_symbols:
                        arr_symbols[symbol]['flag'] = ''

        return Response({'live_quote': arr_symbols}, status=status.HTTP_200_OK)


# --- Market news (Marketaux API) - same as News.MarketNewsList but admin-only ---
MARKETAUX_ALLOWED_PARAMS = {
    "symbols", "entity_types", "industries", "countries", "sentiment_gte", "sentiment_lte",
    "min_match_score", "filter_entities", "must_have_entities", "group_similar", "search",
    "domains", "exclude_domains", "source_ids", "exclude_source_ids", "language",
    "published_before", "published_after", "published_on", "sort", "sort_order", "limit", "page",
}
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


class AdminMarketNewsList(APIView):
    """
    GET: Fetch market/finance news from Marketaux API (admin-only).
    Same query params as News MarketNewsList: category, language, limit, page, symbols, etc.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        api_token = getattr(django_settings, "MARKETAUX_API_TOKEN", None)
        if not api_token:
            return Response(
                {"error": "Market news API is not configured (MARKETAUX_API_TOKEN missing)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
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
                        "error": "Market news provider is blocking this server's region or network (Cloudflare 1010).",
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


# ===========================================================================
# Subscriptions & Revenue – Admin Stats
# ===========================================================================

def _sub_period_filter(field, start, end):
    """Return a Q that filters a DateTimeField between start and end."""
    return Q(**{f"{field}__gte": start, f"{field}__lte": end})


def _revenue_for_qs(qs):
    """Sum amount for non-null, non-trial paid subscriptions in a queryset."""
    from django.db.models import Sum
    result = qs.filter(is_trial=False, amount__isnull=False).aggregate(total=Sum("amount"))
    return float(result["total"] or 0)


class AdminSubscriptionStatsView(APIView):
    """
    GET  /api/admin/subscriptions/stats/

    Returns high-level subscription & revenue metrics (no date-range filtering).
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if Subscription is None:
            return Response({"error": "Subscriptions app not available."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        now = timezone.now()
        all_subs = Subscription.objects.all()

        # ── Totals (all-time) ───────────────────────────────────────────────
        total = all_subs.count()
        active = all_subs.filter(status="active", end_date__gte=now, is_trial=False).count()
        trial = all_subs.filter(status="active", end_date__gte=now, is_trial=True).count()
        monthly = all_subs.filter(status="active", end_date__gte=now, plan_type="monthly", is_trial=False).count()
        yearly = all_subs.filter(status="active", end_date__gte=now, plan_type="yearly", is_trial=False).count()
        expired = all_subs.filter(status="expired").count()
        cancelled = all_subs.filter(status="cancelled").count()

        # ── Revenue ─────────────────────────────────────────────────────────
        currency_mode = (
            all_subs.filter(is_trial=False, amount__isnull=False)
            .values("currency")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")
            .first()
        )
        dominant_currency = currency_mode["currency"] if currency_mode else "usd"

        return Response(
            {
                "total_subscriptions": total,
                "active_subscriptions": active,
                "trial_subscriptions": trial,
                "monthly_subscriptions": monthly,
                "yearly_subscriptions": yearly,
                "expired_subscriptions": expired,
                "cancelled_subscriptions": cancelled,
                "all_time_revenue": {
                    "value": round(_revenue_for_qs(all_subs), 2),
                    "currency": dominant_currency,
                },
            },
            status=status.HTTP_200_OK,
        )


class AdminMRRView(APIView):
    """
    GET  /api/admin/subscriptions/mrr/
    Returns MRR for the last 6 calendar months: month key, month name, and value.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        if Subscription is None:
            return Response({"error": "Subscriptions app not available."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        import decimal
        import calendar

        now = timezone.now()
        mrr_data = []

        for i in range(5, -1, -1):
            year = now.year
            month = now.month - i
            while month <= 0:
                month += 12
                year -= 1
            last_day = monthrange(year, month)[1]
            m_start = timezone.make_aware(timezone.datetime(year, month, 1))
            m_end = timezone.make_aware(timezone.datetime(year, month, last_day, 23, 59, 59, 999999))
            if m_end > now:
                m_end = now

            qs = Subscription.objects.filter(
                is_trial=False,
                amount__isnull=False,
                status__in=["active", "expired", "cancelled"],
                start_date__lte=m_end,
                end_date__gte=m_start,
            ).values("plan_type", "amount")

            total = decimal.Decimal("0")
            for sub in qs:
                amt = sub["amount"] or 0
                total += amt / 12 if sub["plan_type"] == "yearly" else amt

            mrr_data.append({
                "month": f"{year}-{month:02d}",
                "month_name": calendar.month_name[month],
                "value": float(round(total, 2)),
            })

        return Response({"mrr": mrr_data}, status=status.HTTP_200_OK)


class AdminPaymentHistoryView(generics.ListAPIView):
    """
    GET  /api/admin/subscriptions/payments/

    Paginated payment history — only subscriptions that have an amount recorded.
    Each row: username, plan, amount, currency, status, date (start_date).

    Query params:
        plan   : free_trial | monthly | yearly
        status : active | expired | cancelled
        search : name or email (icontains)
        page / page_size
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    pagination_class = AdminPageNumberPagination

    def get_queryset(self):
        if Subscription is None:
            return User.objects.none()
        qs = (
            Subscription.objects
            .filter(amount__isnull=False)
            .select_related("user")
            .order_by("-start_date")
        )
        plan = self.request.query_params.get("plan", "").strip().lower()
        if plan:
            qs = qs.filter(plan_type=plan)
        sub_status = self.request.query_params.get("status", "").strip().lower()
        if sub_status:
            qs = qs.filter(status=sub_status)
        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(user__name__icontains=search) | Q(user__email__icontains=search)
            )
        return qs

    def list(self, request, *args, **kwargs):
        if Subscription is None:
            return Response({"error": "Subscriptions app not available."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        items = page if page is not None else qs
        data = [
            {
                "id": str(sub.id),
                "username": sub.user.name or sub.user.email,
                "email": sub.user.email,
                "plan": sub.plan_type,
                "amount": float(sub.amount),
                "currency": sub.currency or "usd",
                "status": sub.status,
                "date": sub.start_date.isoformat() if sub.start_date else None,
            }
            for sub in items
        ]
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)


# ===========================================================================
# Admin FCM Push Notification Broadcast
# ===========================================================================

# Segment → Q filter mapping (resolved at request time so `now` is fresh)
_SEGMENT_LABELS = {
    "all":       "All users",
    "analysts":  "Analysts",
    "traders":   "Traders",
    "premium":   "Premium (active monthly/yearly)",
    "trial":     "Trial users",
    "basic":     "Basic (no active subscription)",
    "inactive":  "Inactive users",
}

# Notification categories shown to the admin (stored in UserNotification.notification_type)
# Maps admin-facing category → UserNotification notification_type
_PUSH_CATEGORY_MAP = {
    "broadcast":             "INFO",
    "targeted":              "INFO",
    "subscription_reminder": "WARNING",
    "system_alert":          "WARNING",
    "promotional":           "INFO",
}

_PUSH_CATEGORIES = list(_PUSH_CATEGORY_MAP.keys())


def _build_segment_qs(segment: str, now):
    """Return a User queryset for the requested segment."""
    base = User.objects.filter(is_active=True)

    if segment == "analysts":
        return base.filter(user_type="analyst")

    if segment == "traders":
        return base.filter(user_type="trader")

    if Subscription is None:
        # No subscription app — fall back to all active users
        return base

    if segment == "premium":
        return base.filter(
            subscription__status="active",
            subscription__end_date__gte=now,
            subscription__is_trial=False,
            subscription__plan_type__in=["monthly", "yearly"],
        )

    if segment == "trial":
        return base.filter(
            subscription__status="active",
            subscription__end_date__gte=now,
            subscription__is_trial=True,
        )

    if segment == "basic":
        # Traders with no active subscription at all
        active_sub_ids = User.objects.filter(
            subscription__status="active",
            subscription__end_date__gte=now,
        ).values_list("id", flat=True)
        return base.filter(user_type="trader").exclude(id__in=active_sub_ids)

    if segment == "inactive":
        return User.objects.filter(is_active=False)

    # default: "all"
    return base


class AdminFCMBroadcastView(APIView):
    """
    POST  /api/admin/notifications/broadcast/

    Send a push notification + save UserNotification for each matched user.

    Body
    ----
    {
        "title"            : "Server maintenance tonight",      // required
        "message"          : "We will be down at 02:00 UTC.",   // required
        "segment"          : "all",                             // required
        "category"         : "system_alert",                    // required
        "redirect_url"     : "https://...",                     // optional
        "user_ids"         : ["uuid1", "uuid2"]                 // required only when segment="targeted"
    }

    segment options
    ---------------
    all        – every active user
    analysts   – users with user_type='analyst'
    traders    – users with user_type='trader'
    premium    – traders on an active monthly/yearly subscription
    trial      – traders on an active free_trial subscription
    basic      – traders with no active subscription
    inactive   – users where is_active=False
    targeted   – exact list of user IDs in user_ids

    category options
    ----------------
    broadcast             – general announcement to all
    targeted              – personalised message to specific users
    subscription_reminder – remind users about expiring/upgrade
    system_alert          – maintenance, downtime, urgent info
    promotional           – offers, new features, campaigns
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request):
        title    = (request.data.get("title") or "").strip()
        message  = (request.data.get("message") or "").strip()
        segment  = (request.data.get("segment") or "").strip().lower()
        category = (request.data.get("category") or "").strip().lower()
        redirect_url = (request.data.get("redirect_url") or "").strip() or None
        user_ids = request.data.get("user_ids") or []

        # ── Validate ────────────────────────────────────────────────────────
        errors = {}
        if not title:
            errors["title"] = "This field is required."
        if not message:
            errors["message"] = "This field is required."
        valid_segments = list(_SEGMENT_LABELS.keys()) + ["targeted"]
        if segment not in valid_segments:
            errors["segment"] = f"Must be one of: {', '.join(valid_segments)}."
        if category not in _PUSH_CATEGORIES:
            errors["category"] = f"Must be one of: {', '.join(_PUSH_CATEGORIES)}."
        if segment == "targeted" and not user_ids:
            errors["user_ids"] = "Required when segment is 'targeted'."
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        # ── Build recipient queryset ─────────────────────────────────────────
        now = timezone.now()
        if segment == "targeted":
            recipients = User.objects.filter(id__in=user_ids)
        else:
            recipients = _build_segment_qs(segment, now)

        recipient_list = list(recipients.distinct())
        recipient_count = len(recipient_list)

        if recipient_count == 0:
            return Response(
                {
                    "message": "No recipients found for the selected segment.",
                    "segment": segment,
                    "category": category,
                    "recipient_count": 0,
                    "fcm_success": 0,
                    "fcm_failure": 0,
                },
                status=status.HTTP_200_OK,
            )

        # ── Save UserNotification rows ───────────────────────────────────────
        notification_type = _PUSH_CATEGORY_MAP.get(category, "INFO")
        try:
            from Mainapp.models import UserNotification
            UserNotification.objects.bulk_create([
                UserNotification(
                    user=user,
                    title=title,
                    message=message,
                    notification_type=notification_type,
                    redirect_url=redirect_url,
                )
                for user in recipient_list
            ])
        except Exception as db_exc:
            return Response(
                {"error": f"Failed to save notifications to DB: {db_exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ── Send FCM push ────────────────────────────────────────────────────
        fcm_success = 0
        fcm_failure = 0
        try:
            from firebase import send_push_to_users
            fcm_result = send_push_to_users(
                users=recipient_list,
                title=title,
                body=message,
                data={
                    "type": "admin_broadcast",
                    "category": category,
                    "redirect_url": redirect_url or "",
                },
            )
            fcm_success = fcm_result.get("success_count", 0)
            fcm_failure = fcm_result.get("failure_count", 0)
        except Exception:
            # FCM failure is non-fatal — DB notifications already saved
            fcm_failure = recipient_count

        return Response(
            {
                "message": "Notification sent successfully.",
                "segment": segment,
                "category": category,
                "title": title,
                "recipient_count": recipient_count,
                "fcm_success": fcm_success,
                "fcm_failure": fcm_failure,
            },
            status=status.HTTP_200_OK,
        )

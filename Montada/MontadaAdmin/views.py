"""
Admin dashboard views: stats cards with date range and percentage change.
"""
from datetime import timedelta
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.contrib.auth import get_user_model

User = get_user_model()

try:
    from Signals.models import TradingSignal
except ImportError:
    TradingSignal = None

try:
    from Subscriptions.models import Subscription
except ImportError:
    Subscription = None


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

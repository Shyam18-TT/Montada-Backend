from datetime import timedelta
from calendar import monthrange

from django.db.models import Avg, Count, Q
from django.utils import timezone
from rest_framework import status, generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from Mainapp.models import ActivityLog
from Followers.models import Follow
from Signals.models import TradingSignal, AssetClass, Timeframe
from Signals.views import IsAnalystPermission

from .serializers import ActivityLogSerializer


class DashboardPageNumberPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class AnalystPerformanceOverviewView(APIView):
    """
    Single API for analyst dashboard: win rate %, average confidence,
    win count, loss count, and active (OPEN) signals count.
    Only for authenticated analyst users.
    """
    permission_classes = [IsAuthenticated, IsAnalystPermission]

    def get(self, request):
        if request.user.user_type != 'analyst':
            return Response(
                {'error': 'Only analysts can view this dashboard.'},
                status=status.HTTP_403_FORBIDDEN
            )

        signals = TradingSignal.active.filter(analyst=request.user)

        # Counts
        win_count = signals.filter(is_win=True).count()
        loss_count = signals.filter(is_loss=True).count()
        active_signals_count = signals.filter(status=TradingSignal.Status.OPEN).count()

        # Win rate: wins / (wins + losses) * 100 (exclude neutrals)
        closed_with_outcome = win_count + loss_count
        if closed_with_outcome > 0:
            win_rate_percentage = round((win_count / closed_with_outcome) * 100, 2)
        else:
            win_rate_percentage = None  # or 0 if you prefer

        # Average confidence over all posted (non-deleted) signals
        avg_result = signals.aggregate(avg_confidence=Avg('confidence_level'))
        average_confidence = round(avg_result['avg_confidence'], 2) if avg_result['avg_confidence'] is not None else None

        return Response({
            'win_rate_percentage': win_rate_percentage,
            'average_confidence': average_confidence,
            'win_count': win_count,
            'loss_count': loss_count,
            'active_signals_count': active_signals_count,
        }, status=status.HTTP_200_OK)


class AnalystDashboardSummaryView(APIView):
    """
    Dashboard summary for analyst: win rate %, followers count, total signals posted.
    Only for authenticated analyst users.
    """
    permission_classes = [IsAuthenticated, IsAnalystPermission]

    def get(self, request):
        if request.user.user_type != 'analyst':
            return Response(
                {'error': 'Only analysts can view this dashboard.'},
                status=status.HTTP_403_FORBIDDEN
            )

        signals = TradingSignal.active.filter(analyst=request.user)
        total_signals_posted = signals.count()

        win_count = signals.filter(is_win=True).count()
        loss_count = signals.filter(is_loss=True).count()
        closed_with_outcome = win_count + loss_count
        if closed_with_outcome > 0:
            win_rate_percentage = round((win_count / closed_with_outcome) * 100, 2)
        else:
            win_rate_percentage = None

        followers_count = Follow.objects.filter(
            followed=request.user,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        ).count()

        return Response({
            'win_rate_percentage': win_rate_percentage,
            'followers_count': followers_count,
            'total_signals_posted': total_signals_posted,
        }, status=status.HTTP_200_OK)


class AnalystActivityLogListView(generics.ListAPIView):
    """
    List recent activities from ActivityLog for the authenticated analyst.
    Paginated; ordered by created_at descending (most recent first).
    """
    permission_classes = [IsAuthenticated, IsAnalystPermission]
    serializer_class = ActivityLogSerializer
    pagination_class = DashboardPageNumberPagination

    def get_queryset(self):
        if getattr(self.request.user, 'user_type', None) != 'analyst':
            return ActivityLog.objects.none()
        return ActivityLog.objects.filter(user=self.request.user).order_by('-created_at')


def _last_six_months_ranges():
    """Yield (start_date, end_date, year, month, label) for the last 6 calendar months (oldest first)."""
    now = timezone.now()
    for i in range(5, -1, -1):  # 5 months ago, 4, 3, 2, 1, current
        # month we're at: now.month - i, handle year rollover
        year = now.year
        month = now.month - i
        while month <= 0:
            month += 12
            year -= 1
        last_day = monthrange(year, month)[1]
        start = timezone.make_aware(timezone.datetime(year, month, 1))
        end = timezone.make_aware(timezone.datetime(year, month, last_day, 23, 59, 59, 999999))
        if end > now:
            end = now
        label = f"{year}-{month:02d}"
        yield start, end, year, month, label


class AnalyticsGraphView(APIView):
    """
    Analytics graph data for analyst dashboard.
    Query param: type=winrate | growthrate
    - winrate: last 6 months win rate per month (month, win_rate, win_count, loss_count).
    - growthrate: last 6 months followers count at end of each month (month, followers_count).
    """
    permission_classes = [IsAuthenticated, IsAnalystPermission]

    def get(self, request):
        if request.user.user_type != 'analyst':
            return Response(
                {'error': 'Only analysts can view this.'},
                status=status.HTTP_403_FORBIDDEN
            )
        graph_type = (request.query_params.get('type') or '').strip().lower()
        if graph_type not in ('winrate', 'growthrate'):
            return Response(
                {'error': 'Query param "type" must be "winrate" or "growthrate".'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if graph_type == 'winrate':
            return Response(self._get_winrate_data(request.user), status=status.HTTP_200_OK)
        return Response(self._get_growthrate_data(request.user), status=status.HTTP_200_OK)

    def _get_winrate_data(self, user):
        """Last 6 months: win rate per month (closed signals in that month).
        Also returns signals_by_asset_class (percentage per asset class) and
        signals_by_timeframe (count per timeframe) for all signals posted by the analyst.
        """
        signals_base = TradingSignal.active.filter(analyst=user, status=TradingSignal.Status.CLOSED)
        result = []
        for start, end, year, month, label in _last_six_months_ranges():
            month_signals = signals_base.filter(updated_at__gte=start, updated_at__lte=end)
            wins = month_signals.filter(is_win=True).count()
            losses = month_signals.filter(is_loss=True).count()
            total = wins + losses
            win_rate = round((wins / total) * 100, 2) if total > 0 else None
            result.append({
                'month': label,
                'year': year,
                'month_number': month,
                'win_rate': win_rate,
                'win_count': wins,
                'loss_count': losses,
                'total_closed': total,
            })

        # All signals posted by the analyst (for breakdowns)
        analyst_signals = TradingSignal.active.filter(analyst=user)
        total_signals = analyst_signals.count()

        # Counts per asset_class (for lookup)
        by_asset_counts = dict(
            analyst_signals.values('asset_class_id').annotate(count=Count('id')).values_list('asset_class_id', 'count')
        )
        # All active asset classes: include every one with count 0 if no signals
        all_asset_classes = AssetClass.objects.filter(is_active=True).order_by('name')
        signals_by_asset_class = [
            {
                'asset_class': ac.name,
                'count': by_asset_counts.get(ac.id, 0),
                'percentage': round((by_asset_counts.get(ac.id, 0) / total_signals) * 100, 2) if total_signals else 0,
            }
            for ac in all_asset_classes
        ]

        # Counts per timeframe (for lookup)
        by_timeframe_counts = dict(
            analyst_signals.values('timeframe_id').annotate(count=Count('id')).values_list('timeframe_id', 'count')
        )
        # All active timeframes: include every one with count 0 if no signals
        all_timeframes = Timeframe.objects.filter(is_active=True).order_by('code')
        signals_by_timeframe = [
            {
                'timeframe_code': tf.code,
                'timeframe_name': tf.name or tf.code,
                'count': by_timeframe_counts.get(tf.id, 0),
            }
            for tf in all_timeframes
        ]

        return {
            'type': 'winrate',
            'data': result,
            'signals_by_asset_class': signals_by_asset_class,
            'signals_by_timeframe': signals_by_timeframe,
        }

    def _get_growthrate_data(self, user):
        """Last 6 months: cumulative followers count at end of each month."""
        result = []
        for start, end, year, month, label in _last_six_months_ranges():
            # Followers that were following at end of this month: accepted before end, not unfollowed before end
            count = Follow.objects.filter(
                followed=user,
                status=Follow.Status.ACCEPTED,
                accepted_at__lte=end,
            ).filter(
                Q(unfollowed_at__isnull=True) | Q(unfollowed_at__gt=end),
            ).count()
            result.append({
                'month': label,
                'year': year,
                'month_number': month,
                'followers_count': count,
            })
        return {'type': 'growthrate', 'data': result}

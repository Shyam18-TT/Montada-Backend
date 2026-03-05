import uuid
from datetime import timedelta
from calendar import monthrange

from django.db import connections
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

        # One growth percentage: compare first 3 months vs last 3 months (win rate improvement)
        first_half = result[:3]
        last_half = result[-3:]
        wins_first = sum(d['win_count'] for d in first_half)
        losses_first = sum(d['loss_count'] for d in first_half)
        wins_last = sum(d['win_count'] for d in last_half)
        losses_last = sum(d['loss_count'] for d in last_half)
        total_first = wins_first + losses_first
        total_last = wins_last + losses_last
        win_rate_first = (wins_first / total_first) * 100 if total_first > 0 else None
        win_rate_last = (wins_last / total_last) * 100 if total_last > 0 else None
        if win_rate_first is not None and win_rate_last is not None and win_rate_first > 0:
            growth_percentage = round(((win_rate_last - win_rate_first) / win_rate_first) * 100, 2)
        else:
            growth_percentage = None  # not enough data or no baseline

        return {
            'type': 'winrate',
            'data': result,
            'signals_by_asset_class': signals_by_asset_class,
            'signals_by_timeframe': signals_by_timeframe,
            'growth_percentage': growth_percentage,
        }

    def _get_growthrate_data(self, user):
        """Last 6 months: cumulative followers count at end of each month. One growth_percentage (first vs last month)."""
        result = []
        for start, end, year, month, label in _last_six_months_ranges():
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
        # One growth percentage: first month vs last month followers
        first_count = result[0]['followers_count'] if result else 0
        last_count = result[-1]['followers_count'] if result else 0
        if first_count > 0:
            growth_percentage = round(((last_count - first_count) / first_count) * 100, 2)
        else:
            growth_percentage = None if last_count == 0 else 100.0  # no baseline: 0→something = 100%
        return {'type': 'growthrate', 'data': result, 'growth_percentage': growth_percentage}


class ActivePollsListView(APIView):
    """
    GET: List active polls with their questions, options, and vote count per option.
    Same response shape as before: { polls: [...] }. Each question is standalone (no Poll model).
    Returns one logical "poll" containing all questions so frontend contract is unchanged.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Prefetch
        from .models import PollQuestion, PollOption, PollResponse

        options_with_votes = PollOption.objects.annotate(vote_count=Count('responses'))
        questions_qs = (
            PollQuestion.objects.filter(is_active=True)
            .order_by('order')
            .prefetch_related(Prefetch('options', queryset=options_with_votes))
        )

        voted_question_ids = set(
            PollResponse.objects.filter(user=request.user).values_list('question_id', flat=True)
        )

        questions_data = []
        for q in questions_qs:
            opts = list(q.options.all())
            total_votes = sum(getattr(opt, 'vote_count', 0) for opt in opts)
            options_data = [
                {
                    'id': str(opt.id),
                    'option_text': opt.option_text,
                    'vote_count': getattr(opt, 'vote_count', 0),
                    'vote_percentage': round((getattr(opt, 'vote_count', 0) / total_votes) * 100, 2) if total_votes else 0,
                }
                for opt in opts
            ]
            questions_data.append({
                'id': str(q.id),
                'question_text': q.question_text,
                'question_type': q.question_type,
                'order': q.order,
                'options': options_data,
                'is_voted': q.id in voted_question_ids,
            })

        # Only return a poll wrapper when there are questions; otherwise empty list
        if not questions_data:
            result = []
        else:
            result = [{'questions': questions_data}]
        return Response({'polls': result}, status=status.HTTP_200_OK)


class PollVoteView(APIView):
    """
    POST: Submit a vote for one question.
    Body: { "poll_id": "<uuid>", "question_id": "<uuid>", "option_ids": ["<uuid>"] }
    poll_id is accepted for frontend compatibility but not used (questions are standalone).
    - Single-choice: option_ids must contain exactly one option.
    - Multiple-choice: option_ids may contain one or more options.
    - User can vote only once per question.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .models import PollQuestion, PollResponse

        question_id = request.data.get('question_id')
        option_ids = request.data.get('option_ids')
        # poll_id accepted but not used (frontend can keep sending it)

        if not question_id:
            return Response(
                {'error': 'question_id is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not option_ids or not isinstance(option_ids, list):
            return Response(
                {'error': 'option_ids must be a non-empty list.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            q_uuid = uuid.UUID(str(question_id)) if isinstance(question_id, str) else question_id
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid question_id.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            question = PollQuestion.objects.get(id=q_uuid)
        except PollQuestion.DoesNotExist:
            return Response(
                {'error': 'Question not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not getattr(question, 'is_active', True):
            return Response(
                {'error': 'This poll is closed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if PollResponse.objects.filter(question=question, user=request.user).exists():
            return Response(
                {'error': 'You have already voted for this question.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_option_ids = {opt.id for opt in question.options.all()}
        if question.question_type == 'single' and len(option_ids) != 1:
            return Response(
                {'error': 'This question allows only one option (single choice).'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        to_create = []
        seen_options = set()
        for oid in option_ids:
            try:
                o_uuid = uuid.UUID(str(oid)) if isinstance(oid, str) else oid
            except (ValueError, TypeError):
                return Response(
                    {'error': f'Invalid option_id: {oid}.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if o_uuid not in valid_option_ids:
                return Response(
                    {'error': 'One or more option_ids are not valid for this question.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if o_uuid in seen_options:
                continue
            seen_options.add(o_uuid)
            to_create.append(
                PollResponse(
                    question=question,
                    option_id=o_uuid,
                    user=request.user,
                )
            )

        if not to_create:
            return Response(
                {'error': 'At least one valid option must be provided.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        PollResponse.objects.bulk_create(to_create)
        return Response(
            {'message': 'Vote recorded successfully.'},
            status=status.HTTP_201_CREATED,
        )




# Market data symbols by category (used with ?category=forex|shares|metals|indices|commodity|energy|menashares)
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


def _serialize_value(val):
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
            return [_serialize_value(v) for v in val]
        except Exception:
            pass
    return val


def _row_val(row, *keys):
    """Get value from row dict by first matching key (case-insensitive)."""
    key_map = {k.lower(): k for k in row.keys()}
    for key in keys:
        k_lower = key.lower()
        if k_lower in key_map:
            return row.get(key_map[k_lower])
    return None


def _enrich_market_row(row, default_round_digits=4):
    """
    Add computed fields using mt5_prices field names: AskLast, BidLast, AskDir,
    Digits, etc. Adds: dir, ask_today, bid_today, change, change_percentage.
    """
    # mt5_prices columns: Symbol, AskLast, BidLast, AskDir, AskHigh, AskLow, BidHigh, BidLow, ...
    # Use Open if available; else use AskLow/BidLow (session low) so change = current vs session low (non-zero)
    ask_current_val = _row_val(row, 'AskLast')
    bid_current_val = _row_val(row, 'BidLast')
    ask_today_val = _row_val(row, 'AskOpen', 'Open', 'ask_open')
    if ask_today_val is None:
        ask_today_val = _row_val(row, 'AskLow')
    if ask_today_val is None:
        ask_today_val = ask_current_val
    bid_today_val = _row_val(row, 'BidOpen', 'bid_open')
    if bid_today_val is None:
        bid_today_val = _row_val(row, 'BidLow')
    if bid_today_val is None:
        bid_today_val = bid_current_val

    digits_val = _row_val(row, 'Digits')
    try:
        round_digits = int(digits_val) if digits_val is not None else default_round_digits
    except (TypeError, ValueError):
        round_digits = default_round_digits

    try:
        ask_current = float(ask_current_val) if ask_current_val is not None else None
        ask_today = float(ask_today_val) if ask_today_val is not None else None
        bid_current = float(bid_current_val) if bid_current_val is not None else None
        bid_today = float(bid_today_val) if bid_today_val is not None else None
    except (TypeError, ValueError):
        ask_current = ask_today = bid_current = bid_today = None

    row['ask_today'] = round(ask_today, round_digits) if ask_today is not None else None
    row['bid_today'] = round(bid_today, round_digits) if bid_today is not None else None

    if ask_current is None or ask_today is None:
        row['dir'] = 'up'
        row['change'] = '0'
        row['change_percentage'] = '0'
        return row

    # change = ask_current - ask_today
    change = ask_current - ask_today
    change_absolute = abs(change)

    change_percentage = 0
    if ask_today != 0:
        change_percentage = (change_absolute / ask_today) * 100

    # dir must match change: "up" when change >= 0, "down" when change < 0
    row['dir'] = 'down' if change < 0 else 'up'
    change_symbol = '+' if change >= 0 else ''
    percent_symbol = '' if change >= 0 else '-'
    row['change'] = f"{change_symbol}{round(change, 4)}"
    row['change_percentage'] = f"{percent_symbol}{round(change_percentage, 2)}"
    return row


class GetMarketDataFromMT5(APIView):
    """
    Returns mt5_prices data from mt5clients DB as JSON, filtered by category.
    Query param: category = forex | shares | metals | indices | commodity | energy | menashares
    Example: GET /marketdata/live?category=forex
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        category = (request.query_params.get('category') or '').strip().lower()
        if not category:
            return Response(
                {
                    'error': 'Missing category.',
                    'valid_categories': list(MARKET_DATA_SYMBOLS_BY_CATEGORY.keys()),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if category not in MARKET_DATA_SYMBOLS_BY_CATEGORY:
            return Response(
                {
                    'error': f'Invalid category: {category}.',
                    'valid_categories': list(MARKET_DATA_SYMBOLS_BY_CATEGORY.keys()),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        symbols = MARKET_DATA_SYMBOLS_BY_CATEGORY[category]
        if not symbols:
            return Response({'data': [], 'count': 0, 'category': category}, status=status.HTTP_200_OK)
        placeholders = ','.join(['%s'] * len(symbols))
        sql = "SELECT * FROM mt5_prices WHERE symbol IN (%s)" % placeholders
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
        data = []
        for row in rows:
            row_dict = dict(zip(columns, row))
            item = {k: _serialize_value(v) for k, v in row_dict.items()}
            _enrich_market_row(item, default_round_digits=4)
            data.append(item)
        return Response(
            {'data': data, 'count': len(data), 'category': category},
            status=status.HTTP_200_OK,
        )


import uuid
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
    GET: List active polls with their questions, options, and vote count per option in one response.
    Polls are active when is_active=True and current time is within start_date/end_date if set.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Prefetch
        from .models import Poll, PollQuestion, PollOption, PollResponse

        now = timezone.now()
        options_with_votes = PollOption.objects.annotate(vote_count=Count('responses'))
        questions_ordered = PollQuestion.objects.order_by('order').prefetch_related(
            Prefetch('options', queryset=options_with_votes)
        )
        polls_qs = (
            Poll.objects.filter(is_active=True)
            .filter(
                Q(start_date__isnull=True) | Q(start_date__lte=now),
                Q(end_date__isnull=True) | Q(end_date__gte=now),
            )
            .prefetch_related(Prefetch('questions', queryset=questions_ordered))
            .order_by('-created_at')
        )

        # Question ids the current user (request.user) has voted on in these active polls
        voted_question_ids = set(
            PollResponse.objects.filter(
                user=request.user,
                question__poll__in=polls_qs,
            ).values_list('question_id', flat=True)
        )

        result = []
        for poll in polls_qs:
            questions_data = []
            for q in poll.questions.all():
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
            result.append({
                'id': str(poll.id),
                'title': poll.title,
                'description': poll.description or '',
                'allow_multiple_answers': poll.allow_multiple_answers,
                'start_date': poll.start_date.isoformat() if poll.start_date else None,
                'end_date': poll.end_date.isoformat() if poll.end_date else None,
                'created_at': poll.created_at.isoformat(),
                'questions': questions_data,
            })

        return Response({'polls': result}, status=status.HTTP_200_OK)


class PollVoteView(APIView):
    """
    POST: Submit a vote for one question in a poll.
    Body: { "poll_id": "<uuid>", "question_id": "<uuid>", "option_ids": ["<uuid>"] }
    - Single question and its option(s) per request; not all questions need to be answered.
    - Single-choice: option_ids must contain exactly one option.
    - Multiple-choice: option_ids may contain one or more options.
    - User can vote only once per question (no re-voting for that question).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .models import Poll, PollQuestion, PollResponse

        poll_id = request.data.get('poll_id')
        question_id = request.data.get('question_id')
        option_ids = request.data.get('option_ids')

        if not poll_id:
            return Response(
                {'error': 'poll_id is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
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

        now = timezone.now()
        try:
            poll = Poll.objects.get(id=poll_id)
        except (Poll.DoesNotExist, ValueError):
            return Response(
                {'error': 'Poll not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not poll.is_active:
            return Response(
                {'error': 'This poll is not active.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if poll.start_date and poll.start_date > now:
            return Response(
                {'error': 'This poll has not started yet.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if poll.end_date and poll.end_date < now:
            return Response(
                {'error': 'This poll has ended.'},
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
            question = PollQuestion.objects.get(id=q_uuid, poll=poll)
        except PollQuestion.DoesNotExist:
            return Response(
                {'error': 'Question not found or does not belong to this poll.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # User can vote only once per question
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
                    poll=poll,
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

import logging

from django.utils import timezone
from django.db.models import Count, Q
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from Followers.models import Follow
from Dashboard.realtime import broadcast_notifications
from Subscriptions.analyst_plan_access import (
    filter_visible_analyst_ids_for_signals,
    user_has_analyst_signal_access,
)
from .models import TradingSignal, AssetClass, Instrument, Timeframe, AppliedSignal, PriceAlert

try:
    from Mainapp.models import ActivityLog
except ImportError:
    ActivityLog = None
from .serializers import (
    TradingSignalSerializer,
    AssetClassSerializer,
    InstrumentSerializer,
    AssetClassWithInstrumentsSerializer,
    TimeframeSerializer,
    TimeframeSimpleSerializer,
    ApplySignalSerializer,
    AppliedSignalSerializer,
    PriceAlertCreateSerializer,
    PriceAlertSerializer,
)


logger = logging.getLogger(__name__)
User = get_user_model()


def _log_signal_closed(user, signal):
    """Create an ActivityLog entry when an analyst closes a signal (win/loss/neutral)."""
    if not ActivityLog:
        return
    instrument_symbol = signal.instrument.symbol if signal.instrument else "N/A"
    subtitle = f"{instrument_symbol} {signal.direction}"
    if signal.is_win:
        log_type = ActivityLog.ActivityType.TAKE_PROFIT
        title = "Closed signal as win"
        icon = "trending-up"
    elif signal.is_loss:
        log_type = ActivityLog.ActivityType.STOP_LOSS
        title = "Closed signal as loss"
        icon = "trending-down"
    else:
        log_type = ActivityLog.ActivityType.GENERAL
        title = "Closed signal (neutral)"
        icon = "minus"
    ActivityLog.objects.create(
        user=user,
        type=log_type,
        title=title,
        subtitle=subtitle[:255],
        icon=icon,
        entity_type=ActivityLog.EntityType.SIGNAL,
        metadata={"signal_id": str(signal.id), "instrument": instrument_symbol, "direction": signal.direction},
    )


def _reset_signal_lifecycle_if_needed(signal, *, old_status, old_direction, old_entry_price, old_instrument_id):
    """
    Clear auto-close lifecycle state when an open signal is edited or reopened.
    """
    if signal.status != TradingSignal.Status.OPEN:
        return

    needs_reset = (
        old_status != TradingSignal.Status.OPEN
        or old_direction != signal.direction
        or old_entry_price != signal.entry_price
        or old_instrument_id != signal.instrument_id
    )
    if not needs_reset:
        return

    signal.entry_triggered_at = None
    signal.entry_watch_direction = None
    signal.price_alert_fcm_sent = False
    signal.is_win = None
    signal.is_loss = None
    signal.is_neutral = None
    signal.updated_at = timezone.now()
    signal.save(
        update_fields=[
            "entry_triggered_at",
            "entry_watch_direction",
            "price_alert_fcm_sent",
            "is_win",
            "is_loss",
            "is_neutral",
            "updated_at",
        ]
    )


def _create_and_broadcast_notifications(users, *, title, body, notification_type="INFO", redirect_url=None):
    if not users:
        return
    try:
        from Mainapp.models import UserNotification

        created_notifications = [
            UserNotification(
                user=user,
                title=title,
                message=body,
                notification_type=notification_type,
                redirect_url=redirect_url,
            )
            for user in users
        ]
        UserNotification.objects.bulk_create(created_notifications)
        broadcast_notifications(
            UserNotification.objects.filter(
                id__in=[notification.id for notification in created_notifications]
            ),
            event_name="created",
        )
    except Exception:
        logger.exception("Failed to create/broadcast in-app notifications.")


def _send_push_notifications(users, *, title, body, data):
    if not users:
        return
    try:
        from firebase import send_push_to_users

        send_push_to_users(
            users=users,
            title=title,
            body=body,
            data=data,
        )
    except Exception:
        logger.exception("Failed to send push notifications.")


def _get_signal_notification_recipients(analyst):
    recipient_ids = (
        Follow.objects.filter(
            followed=analyst,
            status=Follow.Status.ACCEPTED,
            is_active=True,
            follower__is_active=True,
        )
        .values_list("follower_id", flat=True)
        .distinct()
    )
    return list(User.objects.filter(id__in=recipient_ids).distinct())


def _notify_analyst_signal_applied(applied):
    signal = applied.signal
    analyst = signal.analyst if signal else None
    trader = applied.trader
    if not analyst or not trader:
        return

    trader_name = getattr(trader, "name", None) or getattr(trader, "username", None) or getattr(trader, "email", "A trader")
    symbol = signal.instrument.symbol if signal.instrument else "signal"
    timeframe = signal.timeframe.code if signal.timeframe else ""
    title = "Signal applied by trader"
    body = (
        f"{trader_name} applied your {signal.direction} signal for {symbol}"
        + (f" ({timeframe})." if timeframe else ".")
    )
    data_payload = {
        "type": "signal_applied",
        "signal_id": str(signal.id),
        "applied_signal_id": str(applied.id),
        "trader_id": str(trader.id),
        "symbol": symbol,
        "direction": signal.direction or "",
        "timeframe": timeframe,
    }

    _create_and_broadcast_notifications([analyst], title=title, body=body, notification_type="INFO")
    _send_push_notifications([analyst], title=title, body=body, data=data_payload)


def _notify_signal_published(signal, *, old_status=None):
    if not signal or signal.status != TradingSignal.Status.OPEN or old_status == TradingSignal.Status.OPEN:
        return

    recipients = _get_signal_notification_recipients(signal.analyst)
    if not recipients:
        return

    analyst_name = (
        getattr(signal.analyst, "name", None)
        or getattr(signal.analyst, "username", None)
        or getattr(signal.analyst, "email", "Analyst")
    )
    symbol = signal.instrument.symbol if signal.instrument else "signal"
    timeframe = signal.timeframe.code if signal.timeframe else ""
    title = f"{analyst_name} published a new signal"
    body = (
        f"{analyst_name} published a {signal.direction} signal for {symbol}"
        + (f" ({timeframe})." if timeframe else ".")
    )
    data_payload = {
        "type": "signal_published",
        "signal_id": str(signal.id),
        "analyst_id": str(signal.analyst_id),
        "symbol": symbol,
        "direction": signal.direction or "",
        "status": signal.status or "",
        "timeframe": timeframe,
    }

    _create_and_broadcast_notifications(recipients, title=title, body=body, notification_type="INFO")
    _send_push_notifications(recipients, title=title, body=body, data=data_payload)


def _notify_signal_closed(signal, *, old_status=None):
    if (
        not signal
        or signal.status != TradingSignal.Status.CLOSED
        or old_status == TradingSignal.Status.CLOSED
    ):
        return

    recipients = _get_signal_notification_recipients(signal.analyst)
    if not recipients:
        return

    analyst_name = (
        getattr(signal.analyst, "name", None)
        or getattr(signal.analyst, "username", None)
        or getattr(signal.analyst, "email", "Analyst")
    )
    symbol = signal.instrument.symbol if signal.instrument else "signal"
    timeframe = signal.timeframe.code if signal.timeframe else ""
    if signal.is_win:
        outcome = "profit"
    elif signal.is_loss:
        outcome = "loss"
    else:
        outcome = "neutral"

    title = f"{analyst_name} closed a signal"
    body = (
        f"{analyst_name} closed the {signal.direction} signal for {symbol} as {outcome}"
        + (f" ({timeframe})." if timeframe else ".")
    )
    data_payload = {
        "type": "signal_closed",
        "signal_id": str(signal.id),
        "analyst_id": str(signal.analyst_id),
        "symbol": symbol,
        "direction": signal.direction or "",
        "status": signal.status or "",
        "close_outcome": outcome,
        "timeframe": timeframe,
    }

    _create_and_broadcast_notifications(recipients, title=title, body=body, notification_type="INFO")
    _send_push_notifications(recipients, title=title, body=body, data=data_payload)


class IsAnalystPermission(permissions.BasePermission):
    """
    Custom permission to only allow analyst users to post signals
    """
    def has_permission(self, request, view):
        # Check if user is authenticated
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Check if user is an analyst
        return request.user.user_type == 'analyst'


class AnalystSignalPagination(PageNumberPagination):
    """
    Custom pagination for analyst signals list
    Returns 10 signals per page
    """
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class CreateTradingSignalView(generics.CreateAPIView):
    """
    API endpoint for analysts to create trading signals
    Only analyst users can post signals
    """
    queryset = TradingSignal.active.all()
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    
    def perform_create(self, serializer):
        # Automatically set the analyst to the current authenticated user
        serializer.save(analyst=self.request.user)
    
    def create(self, request, *args, **kwargs):
        # Check if user is an analyst
        if request.user.user_type != 'analyst':
            return Response({
                'error': 'Only analyst users can post trading signals.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        signal = serializer.instance
        if ActivityLog and signal:
            instrument_symbol = signal.instrument.symbol if signal.instrument else "N/A"
            subtitle = f"{instrument_symbol} {signal.direction}"
            ActivityLog.objects.create(
                user=request.user,
                type=ActivityLog.ActivityType.GENERAL,
                title="Posted a new signal",
                subtitle=subtitle[:255],
                icon="chart-up",
                entity_type=ActivityLog.EntityType.SIGNAL,
                metadata={"signal_id": str(signal.id), "instrument": instrument_symbol, "direction": signal.direction},
            )
        _notify_signal_published(signal)
        return Response({
            'message': 'Trading signal created successfully.',
            'signal': serializer.data
        }, status=status.HTTP_201_CREATED)


class AssetClassListView(generics.ListAPIView):
    """
    API endpoint to list all active asset classes
    """
    queryset = AssetClass.objects.filter(is_active=True)
    serializer_class = AssetClassSerializer
    permission_classes = [permissions.IsAuthenticated]


class InstrumentListView(generics.ListAPIView):
    """
    API endpoint to list all active instruments
    Can be filtered by asset_class
    """
    serializer_class = InstrumentSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        queryset = Instrument.objects.filter(is_active=True)
        asset_class_id = self.request.query_params.get('asset_class', None)
        
        if asset_class_id:
            queryset = queryset.filter(asset_class_id=asset_class_id)
        
        return queryset


class TimeframeListView(generics.ListAPIView):
    """
    API endpoint to list all active timeframes
    Returns only id, code, and name without pagination
    """
    queryset = Timeframe.objects.filter(is_active=True)
    serializer_class = TimeframeSimpleSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None  # Disable pagination


class AssetClassWithInstrumentsView(generics.ListAPIView):
    """
    API endpoint to get all asset classes with their related instruments in a single response
    No pagination - returns all results at once
    """
    queryset = AssetClass.objects.filter(is_active=True).prefetch_related('instruments')
    serializer_class = AssetClassWithInstrumentsSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None  # Disable pagination for this view
    
    def get_queryset(self):
        """
        Optimize query by prefetching instruments
        """
        return AssetClass.objects.filter(
            is_active=True
        ).prefetch_related('instruments').order_by('name')


class AnalystSignalListView(generics.ListAPIView):
    """
    API endpoint for analysts to view all signals created by them
    Only analyst users can access this endpoint
    Returns all signals created by the authenticated analyst user
    Paginated to 10 signals per page
    """
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    pagination_class = AnalystSignalPagination
    
    def get_queryset(self):
        """
        Filter signals to only return those created by the current analyst user
        Excludes soft-deleted signals
        """
        if not self.request.user.is_authenticated or self.request.user.user_type != 'analyst':
            return TradingSignal.active.none()

        queryset = TradingSignal.active.filter(
            analyst=self.request.user
        ).select_related(
            'analyst', 'asset_class', 'instrument', 'timeframe'
        ).annotate(
            applied_count=Count('applications')
        )

        # Optional filter by status query parameter: ?status=OPEN/CLOSED/DRAFT
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        return queryset.order_by('-created_at')

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        items = page if page is not None else queryset
        serializer = self.get_serializer(items, many=True)
        data = []
        for signal_obj, signal_data in zip(items, serializer.data):
            entry = dict(signal_data)
            entry['applied_count'] = signal_obj.applied_count
            data.append(entry)
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)


class AnalystSignalUpdateView(generics.RetrieveUpdateAPIView):
    """
    API endpoint for analysts to retrieve and update a specific signal
    Only the analyst who created the signal can edit it
    """
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    
    def get_queryset(self):
        """
        Filter signals to only return those created by the current analyst user
        Excludes soft-deleted signals
        """
        if not self.request.user.is_authenticated or self.request.user.user_type != 'analyst':
            return TradingSignal.active.none()
        
        return TradingSignal.active.filter(
            analyst=self.request.user
        ).select_related(
            'analyst', 'asset_class', 'instrument', 'timeframe'
        )
    
    def update(self, request, *args, **kwargs):
        """
        Handle signal update with proper response
        """
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        old_status = instance.status
        old_direction = instance.direction
        old_entry_price = instance.entry_price
        old_instrument_id = instance.instrument_id
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        _reset_signal_lifecycle_if_needed(
            instance,
            old_status=old_status,
            old_direction=old_direction,
            old_entry_price=old_entry_price,
            old_instrument_id=old_instrument_id,
        )
        _notify_signal_published(instance, old_status=old_status)
        _notify_signal_closed(instance, old_status=old_status)
        if ActivityLog and old_status != TradingSignal.Status.CLOSED and instance.status == TradingSignal.Status.CLOSED:
            _log_signal_closed(request.user, instance)
        return Response({
            'message': 'Trading signal updated successfully.',
            'signal': serializer.data
        }, status=status.HTTP_200_OK)
    
    def partial_update(self, request, *args, **kwargs):
        """
        Handle PATCH request to update signal (e.g. status and outcome).
        When setting status to CLOSED, exactly one of is_win, is_loss, or is_neutral must be true.
        """
        instance = self.get_object()
        old_status = instance.status
        old_direction = instance.direction
        old_entry_price = instance.entry_price
        old_instrument_id = instance.instrument_id
        # Allow only status and outcome fields for PATCH
        allowed = {'status', 'is_win', 'is_loss', 'is_neutral'}
        data = {k: request.data[k] for k in allowed if k in request.data}
        if not data:
            return Response({
                'error': 'Provide at least one of: status, is_win, is_loss, is_neutral.'
            }, status=status.HTTP_400_BAD_REQUEST)
        serializer = self.get_serializer(instance, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        _reset_signal_lifecycle_if_needed(
            instance,
            old_status=old_status,
            old_direction=old_direction,
            old_entry_price=old_entry_price,
            old_instrument_id=old_instrument_id,
        )
        _notify_signal_published(instance, old_status=old_status)
        _notify_signal_closed(instance, old_status=old_status)
        if ActivityLog and old_status != TradingSignal.Status.CLOSED and instance.status == TradingSignal.Status.CLOSED:
            _log_signal_closed(request.user, instance)
        return Response({
            'message': 'Signal updated successfully.',
            'signal': serializer.data
        }, status=status.HTTP_200_OK)


class AnalystSignalSoftDeleteView(generics.RetrieveAPIView):
    """
    API endpoint for analysts to soft delete a specific signal
    Only the analyst who created the signal can delete it
    """
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]
    
    def get_queryset(self):
        """
        Filter signals to only return those created by the current analyst user
        Excludes already soft-deleted signals
        """
        if not self.request.user.is_authenticated or self.request.user.user_type != 'analyst':
            return TradingSignal.active.none()
        
        return TradingSignal.active.filter(
            analyst=self.request.user
        ).select_related(
            'analyst', 'asset_class', 'instrument', 'timeframe'
        )
    
    def delete(self, request, *args, **kwargs):
        """
        Soft delete the signal by setting deleted_at timestamp
        """
        instance = self.get_object()
        instance.soft_delete()
        
        return Response({
            'message': 'Trading signal deleted successfully.'
        }, status=status.HTTP_200_OK)




class TraderSignalListView(generics.ListAPIView):
    """
    API endpoint for traders to view signals from analysts they follow.
    Only analysts where the trader has an **active per-analyst subscription**
    (plan scope signals or all) are included—follow alone is not enough.

    Query params:
        status — optional OPEN | CLOSED | DRAFT
        search or q — optional free-text match across instrument symbol/name, asset class
        name, timeframe code/name, direction, status, analyst note, analyst name/email
        (case-insensitive, partial match).

    Each signal includes close_outcome for CLOSED rows: "profit" | "loss" | "neutral"
    (from is_win / is_loss / is_neutral); null for OPEN/DRAFT.
    """
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = AnalystSignalPagination

    def get_queryset(self):
        """
        Return signals from followed analysts who also have an active analyst-plan
        subscription (signals or all) for the current user.
        """
        if not self.request.user.is_authenticated:
            return TradingSignal.active.none()

        user = self.request.user

        # Analysts the trader is following (accepted and active follow)
        following_analyst_ids = list(
            Follow.objects.filter(
                follower=user,
                status=Follow.Status.ACCEPTED,
                is_active=True,
            ).values_list("followed_id", flat=True)
        )
        visible_analyst_ids = filter_visible_analyst_ids_for_signals(
            user, following_analyst_ids
        )

        # Traders must never see DRAFT signals; only the creating analyst sees drafts
        queryset = TradingSignal.active.filter(
            analyst_id__in=visible_analyst_ids
        ).exclude(
            status=TradingSignal.Status.DRAFT
        ).select_related(
            'analyst', 'asset_class', 'instrument', 'timeframe',
        )

        status_param = (self.request.query_params.get('status') or "").strip().upper()
        if status_param in (TradingSignal.Status.OPEN, TradingSignal.Status.CLOSED):
            queryset = queryset.filter(status=status_param)

        search = (
            self.request.query_params.get("search")
            or self.request.query_params.get("q")
            or ""
        ).strip()
        if search:
            queryset = queryset.filter(
                Q(instrument__symbol__icontains=search)
                | Q(instrument__name__icontains=search)
                | Q(asset_class__name__icontains=search)
                | Q(timeframe__code__icontains=search)
                | Q(timeframe__name__icontains=search)
                | Q(direction__icontains=search)
                | Q(status__icontains=search)
                | Q(analyst_note__icontains=search)
                | Q(analyst__name__icontains=search)
                | Q(analyst__email__icontains=search)
            )

        return queryset.order_by('-created_at')

    def list(self, request, *args, **kwargs):
        """
        Return paginated signals with is_applied on each row.
        """
        response = super().list(request, *args, **kwargs)

        # Add is_applied field to each signal in results
        if 'results' in response.data and response.data['results']:
            signal_ids = [signal['id'] for signal in response.data['results']]
            
            # Get applied signals for current user (convert to str for comparison with serialized id)
            applied_signal_ids = set(
                str(sid) for sid in AppliedSignal.objects.filter(
                    trader=request.user,
                    signal_id__in=signal_ids
                ).values_list('signal_id', flat=True)
            )
            
            # Add is_applied field to each signal
            for signal in response.data['results']:
                signal['is_applied'] = str(signal['id']) in applied_signal_ids
        
        return response


class SubscribedAnalystSignalsListView(generics.ListAPIView):
    """
    GET: Signals from a **single** analyst (paginated).

    Access: the viewer must have an active **per-analyst** subscription with scope
    ``signals`` or ``all`` to that analyst. The analyst themself may list without
    a subscription (same data as ``my-signals``).

    Query params (all optional):
        status       — OPEN | CLOSED | DRAFT
        asset_class  — UUID of asset class
        instrument   — UUID of instrument
        timeframe    — UUID of timeframe
        direction    — BUY | SELL
        symbol       — partial match on instrument symbol (case-insensitive)
    """

    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = AnalystSignalPagination

    def list(self, request, *args, **kwargs):
        analyst_id = self.kwargs["analyst_id"]
        User = get_user_model()
        analyst = get_object_or_404(User, pk=analyst_id)
        if getattr(analyst, "user_type", "") != "analyst":
            return Response(
                {
                    "error": "No analyst found with this id.",
                    "code": "not_analyst",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        if request.user.id != analyst.id and not user_has_analyst_signal_access(
            request.user, analyst.id
        ):
            return Response(
                {
                    "error": (
                        "You need an active subscription to this analyst's signals "
                        "(or all-access) plan to view their signals."
                    ),
                    "code": "analyst_subscription_required",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        response = super().list(request, *args, **kwargs)
        if (
            isinstance(response.data, dict)
            and response.data.get("results")
            and request.user.is_authenticated
        ):
            signal_ids = [row["id"] for row in response.data["results"]]
            applied_signal_ids = set(
                str(sid)
                for sid in AppliedSignal.objects.filter(
                    trader=request.user,
                    signal_id__in=signal_ids,
                ).values_list("signal_id", flat=True)
            )
            for row in response.data["results"]:
                row["is_applied"] = str(row["id"]) in applied_signal_ids
        return response

    def get_queryset(self):
        analyst_id = self.kwargs["analyst_id"]
        qs = (
            TradingSignal.active.filter(analyst_id=analyst_id)
            .select_related("analyst", "asset_class", "instrument", "timeframe")
        )
        params = self.request.query_params
        st = (params.get("status") or "").strip().upper()
        if st in ("OPEN", "CLOSED", "DRAFT"):
            qs = qs.filter(status=st)
        asset_class = (params.get("asset_class") or "").strip()
        if asset_class:
            qs = qs.filter(asset_class_id=asset_class)
        instrument = (params.get("instrument") or "").strip()
        if instrument:
            qs = qs.filter(instrument_id=instrument)
        timeframe = (params.get("timeframe") or "").strip()
        if timeframe:
            qs = qs.filter(timeframe_id=timeframe)
        direction = (params.get("direction") or "").strip().upper()
        if direction in ("BUY", "SELL"):
            qs = qs.filter(direction=direction)
        symbol = (params.get("symbol") or "").strip()
        if symbol:
            qs = qs.filter(instrument__symbol__icontains=symbol)
        return qs.order_by("-created_at")


def _get_trader_visible_signals_queryset(user):
    """
    Return queryset of signals the trader is allowed to see (and thus apply).
    Same rules as TraderSignalListView: follow + active per-analyst subscription (signals or all).
    """
    following_analyst_ids = list(
        Follow.objects.filter(
            follower=user,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        ).values_list("followed_id", flat=True)
    )
    visible_analyst_ids = filter_visible_analyst_ids_for_signals(
        user, following_analyst_ids
    )
    return TradingSignal.active.filter(
        analyst_id__in=visible_analyst_ids
    ).select_related('analyst', 'asset_class', 'instrument', 'timeframe')


class TraderApplySignalView(generics.GenericAPIView):
    """
    API endpoint for traders to apply (take) a signal.
    POST with { "signal": "<uuid>", "note": "optional" }.
    Trader must follow the analyst, have an active per-analyst subscription covering signals,
    and the signal must be OPEN.
    Each signal can be applied only once per trader.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ApplySignalSerializer

    def post(self, request, *args, **kwargs):
        serializer = ApplySignalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        signal_id = serializer.validated_data['signal']
        note = serializer.validated_data.get('note') or ''

        allowed_signals = _get_trader_visible_signals_queryset(request.user)
        try:
            signal = allowed_signals.get(id=signal_id)
        except TradingSignal.DoesNotExist:
            return Response(
                {'error': 'Signal not found or you do not have access to apply it.'},
                status=status.HTTP_404_NOT_FOUND
            )

        if signal.status != TradingSignal.Status.OPEN:
            return Response(
                {'error': 'Only OPEN signals can be applied.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if AppliedSignal.objects.filter(trader=request.user, signal=signal).exists():
            return Response(
                {'error': 'You have already applied this signal.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        applied = AppliedSignal.objects.create(
            trader=request.user,
            signal=signal,
            note=note or None
        )
        _notify_analyst_signal_applied(applied)
        out_serializer = AppliedSignalSerializer(applied)
        return Response(
            {
                'message': 'Signal applied successfully.',
                'applied_signal': out_serializer.data
            },
            status=status.HTTP_201_CREATED
        )


class TraderAppliedSignalsListView(generics.ListAPIView):
    """
    API endpoint for traders to list signals they have applied.
    """
    serializer_class = AppliedSignalSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = AnalystSignalPagination

    def get_queryset(self):
        return AppliedSignal.objects.filter(
            trader=self.request.user
        ).select_related('signal', 'signal__analyst', 'signal__asset_class', 'signal__instrument', 'signal__timeframe').order_by('-applied_at')


class PriceAlertCreateView(generics.CreateAPIView):
    """
    POST: Create a price alert for the authenticated user.
    Body: { "instrument": "<uuid>", "target_price": <decimal>, "condition": "above"|"below", "label": "optional" }

    Price fields keep the decimal precision sent by the client (see PriceAlertCreateSerializer).
    """
    serializer_class = PriceAlertCreateSerializer
    permission_classes = [permissions.IsAuthenticated]


class PriceAlertListView(generics.ListAPIView):
    """
    GET: List price alerts for the authenticated user (active and triggered).
    """
    serializer_class = PriceAlertSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = AnalystSignalPagination

    def get_queryset(self):
        return (
            PriceAlert.objects.filter(user=self.request.user)
            .select_related("instrument")
            .order_by("-created_at")
        )


class PriceAlertDestroyView(generics.DestroyAPIView):
    """
    DELETE: Remove a price alert (only if it belongs to the user and not yet triggered).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return PriceAlert.objects.filter(user=self.request.user, is_triggered=False)


# ---------------------------------------------------------------------------
# Signal notification categories and their preset title/body builders
# ---------------------------------------------------------------------------

_NOTIFICATION_CATEGORIES = {
    "buy_now": {
        "label": "Buy Now",
        "title_tpl": "BUY {symbol} – Act Now",
        "body_tpl": "Buy {symbol} now. Entry: {entry} | TP: {tp} | SL: {sl}",
    },
    "sell_now": {
        "label": "Sell Now",
        "title_tpl": "SELL {symbol} – Act Now",
        "body_tpl": "Sell {symbol} now. Entry: {entry} | TP: {tp} | SL: {sl}",
    },
    "cancel_trade": {
        "label": "Cancel Trade",
        "title_tpl": "Cancel {symbol} Trade",
        "body_tpl": "Cancel your {symbol} {direction} trade. The setup is no longer valid.",
    },
    "exit": {
        "label": "Exit Signal",
        "title_tpl": "Exit {symbol} Position – Target Reached",
        "body_tpl": "Close {symbol} position. Target reached. Entry: {entry} | TP: {tp}",
    },
}

_AUDIENCE_CHOICES = ("followers", "applied")


def _build_notification_content(signal, category: str, custom_title: str | None, custom_body: str | None):
    """Build title and body for a signal push notification."""
    symbol = signal.instrument.symbol if signal.instrument else "Signal"
    direction = signal.direction or ""
    entry = str(signal.entry_price) if signal.entry_price else "N/A"
    tp = str(signal.take_profit) if signal.take_profit else "N/A"
    sl = str(signal.stop_loss) if signal.stop_loss else "N/A"
    timeframe = signal.timeframe.code if signal.timeframe else ""

    tpl = _NOTIFICATION_CATEGORIES.get(category, {})
    title = custom_title or tpl.get("title_tpl", "Signal Alert").format(
        symbol=symbol, direction=direction, entry=entry, tp=tp, sl=sl, timeframe=timeframe
    )
    body = custom_body or tpl.get("body_tpl", "{symbol} signal update.").format(
        symbol=symbol, direction=direction, entry=entry, tp=tp, sl=sl, timeframe=timeframe
    )
    return title, body


class SignalPushNotificationView(generics.GenericAPIView):
    """
    POST: Analyst sends a push notification to users for one of their signals.

    URL: signals/<pk>/notify/

    Body (JSON):
    {
        "audience"     : "followers" | "applied",   // required
        "category"     : "buy_now" | "sell_now" | "cancel_trade" | "exit",  // required
        "custom_title" : "Override title",           // optional
        "custom_body"  : "Override message body",    // optional
    }

    - audience=followers : notify all accepted followers of the analyst.
    - audience=applied   : notify only traders who applied this specific signal.
    - category           : determines the pre-built title and body (can be overridden).
    - The notification data payload always includes signal_id, symbol, direction, category.
    """
    permission_classes = [permissions.IsAuthenticated, IsAnalystPermission]

    def post(self, request, pk):
        if request.user.user_type != "analyst":
            return Response(
                {"error": "Only analysts can send signal notifications."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Fetch the signal; must belong to this analyst
        try:
            signal = (
                TradingSignal.active
                .select_related("instrument", "asset_class", "timeframe", "analyst")
                .get(pk=pk, analyst=request.user)
            )
        except TradingSignal.DoesNotExist:
            return Response(
                {"error": "Signal not found or does not belong to you."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Validate required fields
        audience = (request.data.get("audience") or "").strip().lower()
        category = (request.data.get("category") or "").strip().lower()
        custom_title = (request.data.get("custom_title") or "").strip() or None
        custom_body = (request.data.get("custom_body") or "").strip() or None

        if audience not in _AUDIENCE_CHOICES:
            return Response(
                {"error": f"'audience' must be one of: {', '.join(_AUDIENCE_CHOICES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if category not in _NOTIFICATION_CATEGORIES:
            return Response(
                {"error": f"'category' must be one of: {', '.join(_NOTIFICATION_CATEGORIES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Build recipients queryset
        from django.contrib.auth import get_user_model
        UserModel = get_user_model()

        if audience == "followers":
            recipient_ids = (
                Follow.objects.filter(
                    followed=request.user,
                    status=Follow.Status.ACCEPTED,
                    is_active=True,
                ).values_list("follower_id", flat=True)
            )
            recipients = UserModel.objects.filter(id__in=recipient_ids)
        else:  # applied
            recipient_ids = (
                AppliedSignal.objects.filter(signal=signal)
                .values_list("trader_id", flat=True)
            )
            recipients = UserModel.objects.filter(id__in=recipient_ids)

        # Evaluate once so the same list is reused for DB save + FCM send
        recipient_list = list(recipients)
        recipient_count = len(recipient_list)
        if recipient_count == 0:
            return Response(
                {
                    "message": "No recipients found for the selected audience. No notification sent.",
                    "audience": audience,
                    "category": category,
                    "recipient_count": 0,
                    "device_tokens_found": 0,
                    "success_count": 0,
                    "failure_count": 0,
                },
                status=status.HTTP_200_OK,
            )

        # Build title / body from signal details
        title, body = _build_notification_content(signal, category, custom_title, custom_body)

        # Data payload (always strings)
        symbol = signal.instrument.symbol if signal.instrument else ""
        data_payload = {
            "type": "signal_alert",
            "signal_id": str(signal.id),
            "category": category,
            "symbol": symbol,
            "direction": signal.direction or "",
            "entry_price": str(signal.entry_price) if signal.entry_price else "",
            "take_profit": str(signal.take_profit) if signal.take_profit else "",
            "stop_loss": str(signal.stop_loss) if signal.stop_loss else "",
            "timeframe": signal.timeframe.code if signal.timeframe else "",
            "signal_status": signal.status or "",
        }

        # ── Save to UserNotification (non-fatal) ────────────────────────────
        db_error = None
        try:
            from Mainapp.models import UserNotification
            created_notifications = [
                UserNotification(
                    user=recipient,
                    title=title,
                    message=body,
                    notification_type="INFO",
                )
                for recipient in recipient_list
            ]
            UserNotification.objects.bulk_create(created_notifications)
            broadcast_notifications(
                UserNotification.objects.filter(
                    id__in=[notification.id for notification in created_notifications]
                ),
                event_name="created",
            )
        except Exception as exc:
            db_error = str(exc)

        # ── Resolve FCM device tokens (non-fatal) ────────────────────────────
        token_strings = []
        device_tokens_found = 0
        tokens_error = None
        try:
            from firebase import get_push_tokens_for_users
            token_strings = get_push_tokens_for_users(recipient_list)
            device_tokens_found = len(token_strings)
        except Exception as exc:
            tokens_error = str(exc)

        # ── Send via Firebase (non-fatal) ────────────────────────────────────
        fcm_success = 0
        fcm_failure = 0
        fcm_error = None
        if token_strings:
            try:
                from firebase import send_push_to_tokens
                result = send_push_to_tokens(
                    tokens=token_strings,
                    title=title,
                    body=body,
                    data=data_payload,
                )
                fcm_success = result.get("success_count", 0)
                fcm_failure = result.get("failure_count", 0)
            except Exception as exc:
                fcm_error = str(exc)
                fcm_failure = device_tokens_found

        # ── Build response message ───────────────────────────────────────────
        if db_error and tokens_error:
            msg = "Both DB save and token lookup failed."
        elif db_error:
            msg = "DB save failed; push attempted."
        elif device_tokens_found == 0:
            msg = "Notification saved. No device tokens registered for recipients."
        elif fcm_error:
            msg = "Notification saved. FCM push failed."
        else:
            msg = "Notification saved and push sent."

        response_data = {
            "message": msg,
            "audience": audience,
            "category": category,
            "title": title,
            "body": body,
            "recipient_count": recipient_count,
            "device_tokens_found": device_tokens_found,
            "success_count": fcm_success,
            "failure_count": fcm_failure,
        }
        if db_error:
            response_data["db_error"] = db_error
        if tokens_error:
            response_data["tokens_error"] = tokens_error
        if fcm_error:
            response_data["fcm_error"] = fcm_error

        return Response(response_data, status=status.HTTP_200_OK)


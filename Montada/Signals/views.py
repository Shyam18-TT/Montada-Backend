from django.utils import timezone
from django.db.models import Count
from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from Subscriptions.models import Subscription
from Followers.models import Follow
from .models import TradingSignal, AssetClass, Instrument, Timeframe, AppliedSignal

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
)


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
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
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
    - Active subscription: all signals from followed analysts.
    - Expired subscription: only signals created on or before subscription end_date,
      plus message to subscribe for new signals.
    - No subscription: no signals, with message.
    """
    serializer_class = TradingSignalSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = AnalystSignalPagination

    def _get_subscription_status(self, user):
        """
        Return (is_active, end_date).
        is_active: True if subscription is active (before end_date).
        end_date: subscription end_date or None if no subscription.
        """
        try:
            subscription = Subscription.objects.get(user=user)
            return (subscription.is_active(), subscription.end_date)
        except Subscription.DoesNotExist:
            return (False, None)

    def get_queryset(self):
        """
        Return signals from analysts the trader follows.
        Active subscription: all such signals.
        Expired subscription: only signals with created_at <= subscription.end_date.
        No subscription: none.
        """
        if not self.request.user.is_authenticated:
            return TradingSignal.active.none()

        user = self.request.user
        is_active, end_date = self._get_subscription_status(user)

        # Analysts the trader is following (accepted and active follow)
        following_analyst_ids = Follow.objects.filter(
            follower=user,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        ).values_list('followed_id', flat=True)

        queryset = TradingSignal.active.filter(
            analyst_id__in=following_analyst_ids
        ).select_related(
            'analyst', 'asset_class', 'instrument', 'timeframe', 
        )

        if not is_active:
            if end_date is None:
                return TradingSignal.active.none()
            # Expired: only signals created on or before subscription end_date
            queryset = queryset.filter(created_at__lte=end_date)

        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        return queryset.order_by('-created_at')

    def list(self, request, *args, **kwargs):
        """
        Return paginated signals. For expired (or missing) subscription, add message
        to subscribe for new signals. Also adds is_applied field to each signal.
        """
        response = super().list(request, *args, **kwargs)
        is_active, _ = self._get_subscription_status(request.user)
        if not is_active:
            response.data['message'] = 'To view new signals you have to subscribe.'
        
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


def _get_trader_visible_signals_queryset(user):
    """
    Return queryset of signals the trader is allowed to see (and thus apply).
    Same rules as TraderSignalListView: follow + subscription.
    """
    is_active, end_date = _get_subscription_status_for_trader(user)
    following_analyst_ids = Follow.objects.filter(
        follower=user,
        status=Follow.Status.ACCEPTED,
        is_active=True,
    ).values_list('followed_id', flat=True)

    queryset = TradingSignal.active.filter(
        analyst_id__in=following_analyst_ids
    ).select_related('analyst', 'asset_class', 'instrument', 'timeframe')

    if not is_active:
        if end_date is None:
            return TradingSignal.active.none()
        queryset = queryset.filter(created_at__lte=end_date)

    return queryset


def _get_subscription_status_for_trader(user):
    """Return (is_active, end_date) for the user's subscription."""
    try:
        subscription = Subscription.objects.get(user=user)
        return (subscription.is_active(), subscription.end_date)
    except Subscription.DoesNotExist:
        return (False, None)


class TraderApplySignalView(generics.GenericAPIView):
    """
    API endpoint for traders to apply (take) a signal.
    POST with { "signal": "<uuid>", "note": "optional" }.
    Trader must follow the analyst, have subscription access to the signal,
    and the signal must be OPEN. Each signal can be applied only once per trader.
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
            UserNotification.objects.bulk_create([
                UserNotification(
                    user=recipient,
                    title=title,
                    message=body,
                    notification_type="INFO",
                )
                for recipient in recipient_list
            ])
        except Exception as exc:
            db_error = str(exc)

        # ── Resolve FCM device tokens (non-fatal) ────────────────────────────
        token_strings = []
        device_tokens_found = 0
        tokens_error = None
        try:
            from Mainapp.models import DeviceToken
            token_strings = list(
                DeviceToken.objects.filter(user__in=recipient_list)
                .values_list("fcm_token", flat=True)
                .distinct()
            )
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


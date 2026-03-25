import logging
from django.db.models import Count, Sum, Max
from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from .models import Subscription, UserAnalystPlanSubscription, AnalystPlanPurchase
from .serializers import (
    SubscriptionSerializer,
    SubscribeSerializer,
    ConfirmSubscriptionSerializer,
)
from .analyst_plan_serializers import UserAnalystPlanSubscriptionSerializer
from .analyst_plan_views import IsAnalystUser

User = get_user_model()
logger = logging.getLogger(__name__)


class SubscriptionStatusView(generics.RetrieveAPIView):
    """
    API endpoint to get current subscription status
    """
    serializer_class = SubscriptionSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_object(self):
        user = self.request.user
        subscription, created = Subscription.objects.get_or_create(
            user=user,
            defaults={
                'plan_type': 'free_trial',
                'status': 'active',
                'end_date': timezone.now() + timedelta(days=7),
                'is_trial': True
            }
        )
        
        # Check if subscription has expired and update status
        if subscription.is_active() == False and subscription.status == 'active':
            subscription.status = 'expired'
            subscription.save()
            user.is_subscribed = False
            user.save()
        
        return subscription


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def subscribe_view(request):
    """
    API endpoint to subscribe to a paid plan
    """
    user = request.user
    
    # Get or create subscription
    try:
        subscription = Subscription.objects.get(user=user)
    except Subscription.DoesNotExist:
        # Create a new subscription if it doesn't exist
        subscription = Subscription.create_free_trial(user)
    
    serializer = SubscribeSerializer(data=request.data)
    
    if serializer.is_valid():
        plan_type = serializer.validated_data['plan_type']
        months = serializer.validated_data.get('months', 1)
        
        # Upgrade to paid plan
        subscription.upgrade_to_paid(plan_type=plan_type, months=months)
        
        return Response({
            'message': f'Successfully subscribed to {plan_type} plan.',
            'subscription': SubscriptionSerializer(subscription).data
        }, status=status.HTTP_200_OK)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def cancel_subscription_view(request):
    """
    API endpoint to cancel subscription
    """
    user = request.user
    
    try:
        subscription = Subscription.objects.get(user=user)
        
        if subscription.status == 'cancelled':
            return Response({
                'message': 'Subscription is already cancelled.'
            }, status=status.HTTP_200_OK)
        
        subscription.cancel()
        
        return Response({
            'message': 'Subscription cancelled successfully.',
            'subscription': SubscriptionSerializer(subscription).data
        }, status=status.HTTP_200_OK)
    
    except Subscription.DoesNotExist:
        return Response({
            'error': 'No subscription found for this user.'
        }, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def check_subscription_status_view(request):
    """
    API endpoint to check if user has active subscription
    """
    user = request.user
    
    try:
        subscription = Subscription.objects.get(user=user)
        
        # If end_date has passed, mark as Expired and return
        if subscription.end_date and subscription.end_date < timezone.now():
            if subscription.status != 'expired':
                subscription.status = 'expired'
                subscription.save()
                user.is_subscribed = False
                user.save()
            return Response({
                'has_active_subscription': False,
                'subscription': SubscriptionSerializer(subscription).data
            }, status=status.HTTP_200_OK)
        
        is_active = subscription.is_active()
        
        # Update user's is_subscribed status based on subscription
        if user.is_subscribed != is_active:
            user.is_subscribed = is_active
            user.save()
        
        return Response({
            'has_active_subscription': is_active,
            'subscription': SubscriptionSerializer(subscription).data
        }, status=status.HTTP_200_OK)
    
    except Subscription.DoesNotExist:
        return Response({
            'has_active_subscription': False,
            'message': 'No subscription found. Free trial will be created on first access.'
        }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def confirm_subscription_view(request):
    """
    Confirm and save subscription after successful payment.
    Frontend sends: plan_id, payment_intent_id, amount, currency, subscribed_at, status.
    Only status=succeeded is accepted. Updates or creates the user's subscription and
    sets start_date/end_date from subscribed_at and plan_id.
    """
    serializer = ConfirmSubscriptionSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    user = request.user
    plan_id = data['plan_id']
    subscribed_at = data['subscribed_at']
    if subscribed_at.tzinfo is None:
        subscribed_at = timezone.make_aware(subscribed_at)

    duration = timedelta(days=365) if plan_id == 'yearly' else timedelta(days=30)
    end_date = subscribed_at + duration

    subscription = None
    try:
        subscription = Subscription.objects.get(user=user)
    except Subscription.DoesNotExist:
        pass

    if subscription:
        subscription.plan_type = plan_id
        subscription.status = 'active'
        subscription.is_trial = False
        subscription.start_date = subscribed_at
        subscription.end_date = end_date
        subscription.payment_intent_id = data.get('payment_intent_id') or ''
        subscription.amount = data.get('amount')
        subscription.currency = data.get('currency', 'usd') or 'usd'
        subscription.save()
    else:
        subscription = Subscription.objects.create(
            user=user,
            plan_type=plan_id,
            status='active',
            start_date=subscribed_at,
            end_date=end_date,
            is_trial=False,
            payment_intent_id=data.get('payment_intent_id') or '',
            amount=data.get('amount'),
            currency=data.get('currency', 'usd') or 'usd',
        )

    user.is_subscribed = True
    user.save(update_fields=['is_subscribed'])

    return Response(
        {
            'message': 'Subscription confirmed and saved.',
            'subscription': SubscriptionSerializer(subscription).data,
        },
        status=status.HTTP_200_OK,
    )


class StripeAnalyticsView(APIView):
    """
    GET: Stripe payment analytics (balance, recent payment intents, charges, payouts).
    Requires admin/staff. Set STRIPE_SECRET_KEY in settings or env.
    Query params (optional): limit_pi=50, limit_charges=50, limit_payouts=20
    """
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        secret_key = getattr(settings, "STRIPE_SECRET_KEY", None) or ""
        if not secret_key or not secret_key.strip():
            return Response(
                {"error": "STRIPE_SECRET_KEY is not configured. Set it in environment or settings."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        limit_pi = min(int(request.query_params.get("limit_pi", 50)), 100)
        limit_charges = min(int(request.query_params.get("limit_charges", 50)), 100)
        limit_payouts = min(int(request.query_params.get("limit_payouts", 20)), 100)
        try:
            import stripe
            stripe.api_key = secret_key
            # Balance
            balance = stripe.Balance.retrieve()
            balance_data = {
                "available": [{"amount": b.amount, "currency": b.currency} for b in (balance.available or [])],
                "pending": [{"amount": b.amount, "currency": b.currency} for b in (balance.pending or [])],
            }
            # Recent payment intents (succeeded)
            pi_list = stripe.PaymentIntent.list(limit=limit_pi)
            payment_intents = [
                {
                    "id": pi.id,
                    "amount": pi.amount,
                    "currency": (pi.currency or "usd").lower(),
                    "status": pi.status,
                    "created": pi.created,
                }
                for pi in (pi_list.data or [])
            ]
            # Recent charges
            charges_list = stripe.Charge.list(limit=limit_charges)
            charges = [
                {
                    "id": c.id,
                    "amount": c.amount,
                    "currency": (c.currency or "usd").lower(),
                    "status": c.status,
                    "paid": getattr(c, "paid", None),
                    "created": c.created,
                }
                for c in (charges_list.data or [])
            ]
            # Recent payouts
            payouts_list = stripe.Payout.list(limit=limit_payouts)
            payouts = [
                {
                    "id": p.id,
                    "amount": p.amount,
                    "currency": (p.currency or "usd").lower(),
                    "status": p.status,
                    "arrival_date": getattr(p, "arrival_date", None),
                    "created": p.created,
                }
                for p in (payouts_list.data or [])
            ]
            return Response(
                {
                    "balance": balance_data,
                    "payment_intents": payment_intents,
                    "charges": charges,
                    "payouts": payouts,
                },
                status=status.HTTP_200_OK,
            )
        except stripe.error.StripeError as e:
            logger.warning("Stripe analytics API error: %s", e)
            return Response(
                {"error": "Stripe API error.", "detail": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception as e:
            logger.exception("Stripe analytics failed: %s", e)
            return Response(
                {"error": "Failed to fetch Stripe analytics.", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AnalystPlanRevenueView(APIView):
    """
    GET: Revenue from all purchases of this analyst's plans.

    Totals use ``AnalystPlanPurchase`` (immutable snapshot at checkout), not current plan prices.

    Query params:
    - ``status``: ``active`` | ``expired`` | ``cancelled`` | ``all`` (default ``all``)
    - ``limit`` / ``offset``: paginate the ``subscriptions`` list (default limit 50, max 200)
    """

    permission_classes = [permissions.IsAuthenticated, IsAnalystUser]

    def get(self, request):
        status_param = (request.query_params.get("status") or "all").lower()
        if status_param != "all":
            allowed = {
                UserAnalystPlanSubscription.Status.ACTIVE,
                UserAnalystPlanSubscription.Status.EXPIRED,
                UserAnalystPlanSubscription.Status.CANCELLED,
            }
            if status_param not in allowed:
                return Response(
                    {
                        "error": "Invalid status. Use active, expired, cancelled, or all.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        qs = (
            UserAnalystPlanSubscription.objects.filter(plan__analyst=request.user)
            .select_related("plan", "subscriber", "purchase")
            .order_by("-created_at")
        )
        if status_param != "all":
            qs = qs.filter(status=status_param)

        purchase_qs = AnalystPlanPurchase.objects.filter(analyst=request.user)
        if status_param != "all":
            purchase_qs = purchase_qs.filter(subscription__status=status_param)

        by_currency = (
            purchase_qs.values("currency")
            .annotate(
                total_revenue=Sum("amount"),
                subscription_count=Count("id"),
            )
            .order_by()
        )

        by_plan = (
            purchase_qs.values("subscription__plan_id")
            .annotate(
                subscription_count=Count("id"),
                revenue=Sum("amount"),
                plan_title=Max("plan_title"),
                currency=Max("currency"),
            )
            .order_by("-revenue")
        )

        try:
            limit = min(int(request.query_params.get("limit", 50)), 200)
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            offset = 0

        page_qs = qs[offset : offset + limit]
        sub_ser = UserAnalystPlanSubscriptionSerializer(
            page_qs, many=True, context={"request": request}
        )

        return Response(
            {
                "summary": {
                    "total_subscriptions": qs.count(),
                    "by_currency": [
                        {
                            "currency": (row["currency"] or "usd").lower(),
                            "subscription_count": row["subscription_count"],
                            "total_revenue": str(row["total_revenue"] or 0),
                        }
                        for row in by_currency
                    ],
                },
                "by_plan": [
                    {
                        "plan_id": str(row["subscription__plan_id"]),
                        "plan_title": row["plan_title"],
                        "currency": (row["currency"] or "usd").lower(),
                        "subscription_count": row["subscription_count"],
                        "revenue": str(row["revenue"] or 0),
                    }
                    for row in by_plan
                ],
                "subscriptions": sub_ser.data,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "returned": len(sub_ser.data),
                    "total": qs.count(),
                },
            },
            status=status.HTTP_200_OK,
        )

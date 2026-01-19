from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from .models import Subscription
from .serializers import SubscriptionSerializer, SubscribeSerializer

User = get_user_model()


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

from rest_framework import serializers
from .models import Subscription
from django.contrib.auth import get_user_model

User = get_user_model()


class SubscriptionSerializer(serializers.ModelSerializer):
    """
    Serializer for subscription details
    """
    days_remaining = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source='user.email', read_only=True)
    
    class Meta:
        model = Subscription
        fields = (
            'id', 'user', 'user_email', 'plan_type', 'status',
            'start_date', 'end_date', 'is_trial', 'is_active',
            'days_remaining', 'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'user', 'start_date', 'created_at', 'updated_at')
    
    def get_days_remaining(self, obj):
        """Get days remaining in subscription"""
        return obj.days_remaining()
    
    def get_is_active(self, obj):
        """Check if subscription is active"""
        return obj.is_active()


class SubscribeSerializer(serializers.Serializer):
    """
    Serializer for subscribing to a plan
    """
    PLAN_CHOICES = [
        ('monthly', 'Monthly'),
        ('yearly', 'Yearly'),
    ]
    
    plan_type = serializers.ChoiceField(
        choices=PLAN_CHOICES,
        required=True,
        help_text='Subscription plan type: monthly or yearly'
    )
    months = serializers.IntegerField(
        required=False,
        default=1,
        min_value=1,
        max_value=12,
        help_text='Number of months for monthly plan (1-12)'
    )
    
    def validate(self, attrs):
        plan_type = attrs.get('plan_type')
        months = attrs.get('months', 1)
        
        # For yearly plan, months should be 1 (or ignored)
        if plan_type == 'yearly' and months != 1:
            attrs['months'] = 1
        
        return attrs


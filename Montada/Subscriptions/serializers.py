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
            'days_remaining', 'payment_intent_id', 'amount', 'currency',
            'created_at', 'updated_at'
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


class ConfirmSubscriptionSerializer(serializers.Serializer):
    """
    Serializer for confirming a subscription after successful payment (frontend sends after payment).
    """
    plan_id = serializers.ChoiceField(
        choices=[('monthly', 'Monthly'), ('yearly', 'Yearly')],
        required=True,
        help_text='Plan identifier: monthly or yearly'
    )
    payment_intent_id = serializers.CharField(required=True, allow_blank=False)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=True)
    currency = serializers.CharField(default='usd', max_length=10, required=False)
    subscribed_at = serializers.DateTimeField(required=True)
    status = serializers.ChoiceField(
        choices=[('succeeded', 'Succeeded'), ('failed', 'Failed'), ('pending', 'Pending')],
        required=True
    )

    def validate_plan_id(self, value):
        if value not in ('monthly', 'yearly'):
            raise serializers.ValidationError("plan_id must be 'monthly' or 'yearly'.")
        return value

    def validate_status(self, value):
        if value != 'succeeded':
            raise serializers.ValidationError("Only succeeded payments can confirm a subscription.")
        return value


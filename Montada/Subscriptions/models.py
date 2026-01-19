from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta

User = get_user_model()


class Subscription(models.Model):
    """
    Model to manage user subscriptions
    """
    PLAN_CHOICES = [
        ('free_trial', 'Free Trial'),
        ('monthly', 'Monthly'),
        ('yearly', 'Yearly'),
    ]
    
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    ]
    
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='subscription',
        help_text='User associated with this subscription'
    )
    plan_type = models.CharField(
        max_length=20,
        choices=PLAN_CHOICES,
        default='free_trial',
        help_text='Type of subscription plan'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='active',
        help_text='Current status of the subscription'
    )
    start_date = models.DateTimeField(
        auto_now_add=True,
        help_text='When the subscription started'
    )
    end_date = models.DateTimeField(
        help_text='When the subscription ends or expires'
    )
    is_trial = models.BooleanField(
        default=True,
        help_text='Whether this is a trial subscription'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Subscription'
        verbose_name_plural = 'Subscriptions'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.email} - {self.plan_type} ({self.status})"
    
    def is_active(self):
        """Check if subscription is currently active"""
        if self.status != 'active':
            return False
        return timezone.now() <= self.end_date
    
    def days_remaining(self):
        """Calculate days remaining in subscription"""
        if not self.is_active():
            return 0
        remaining = self.end_date - timezone.now()
        return max(0, remaining.days)
    
    @staticmethod
    def create_free_trial(user):
        """Create a 7-day free trial subscription for a new user"""
        end_date = timezone.now() + timedelta(days=7)
        subscription = Subscription.objects.create(
            user=user,
            plan_type='free_trial',
            status='active',
            end_date=end_date,
            is_trial=True
        )
        # Update user's subscription status
        user.is_subscribed = True
        user.save()
        return subscription
    
    def upgrade_to_paid(self, plan_type='monthly', months=1):
        """Upgrade from trial to paid subscription"""
        if plan_type == 'yearly':
            duration = timedelta(days=365)
        else:
            duration = timedelta(days=30 * months)
        
        # If subscription is still active, extend from end_date, otherwise start now
        if self.is_active():
            self.end_date = self.end_date + duration
        else:
            self.end_date = timezone.now() + duration
        
        self.plan_type = plan_type
        self.is_trial = False
        self.status = 'active'
        self.save()
        
        # Update user's subscription status
        self.user.is_subscribed = True
        self.user.save()
        
        return self
    
    def cancel(self):
        """Cancel the subscription"""
        self.status = 'cancelled'
        self.save()
        
        # Update user's subscription status
        self.user.is_subscribed = False
        self.user.save()
        
        return self

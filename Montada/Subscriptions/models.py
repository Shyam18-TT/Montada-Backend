from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
import uuid

User = get_user_model()


class Subscription(models.Model):
    """
    Paid (or trial) access to **market news** (third-party feeds) and **live market data**
    (e.g. MT5 quotes via the dashboard). Analyst trading signals and follow/social features
    do not depend on this subscription.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
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
    payment_intent_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text='Stripe (or provider) payment intent ID for the last payment'
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Amount paid for the subscription'
    )
    currency = models.CharField(
        max_length=10,
        default='usd',
        blank=True,
        help_text='Currency code of the payment'
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


# ---------------------------------------------------------------------------
# Per-analyst content plans (separate from app-wide Subscription above).
# Analysts offer paid access to their articles and/or signals; traders subscribe
# per plan. Unrelated to market data / Marketaux app subscription.
# ---------------------------------------------------------------------------


class AnalystContentPlan(models.Model):
    """
    A plan created by an analyst: access to their articles only, signals only, or both.
    """

    class Scope(models.TextChoices):
        ARTICLES = "articles", "Articles only"
        SIGNALS = "signals", "Signals only"
        ALL = "all", "Articles and signals"

    class BillingPeriod(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"
        ONE_TIME = "one_time", "One time"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analyst = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="content_plans",
        help_text="Analyst who owns and receives this offering",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    scope = models.CharField(
        max_length=20,
        choices=Scope.choices,
        help_text="What this plan unlocks: articles, signals, or both",
    )
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Display/charge amount (0 for free tier)",
    )
    currency = models.CharField(max_length=10, default="usd", blank=True)
    billing_period = models.CharField(
        max_length=20,
        choices=BillingPeriod.choices,
        default=BillingPeriod.MONTHLY,
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive plans are hidden from catalog and do not enforce paywall",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["analyst", "is_active"]),
            models.Index(fields=["scope"]),
        ]
        verbose_name = "Analyst content plan"
        verbose_name_plural = "Analyst content plans"

    def __str__(self):
        return f"{self.title} ({self.get_scope_display()}) — {self.analyst_id}"


class UserAnalystPlanSubscription(models.Model):
    """
    A user's subscription to a specific AnalystContentPlan (one analyst offering).
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="analyst_content_subscriptions",
        help_text="User who purchased / activated this plan",
    )
    plan = models.ForeignKey(
        AnalystContentPlan,
        on_delete=models.CASCADE,
        related_name="user_subscriptions",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    start_date = models.DateTimeField(auto_now_add=True)
    end_date = models.DateTimeField()
    payment_intent_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Optional Stripe payment intent for this purchase",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["subscriber", "status"]),
            models.Index(fields=["plan", "status"]),
            models.Index(fields=["end_date"]),
        ]
        verbose_name = "User analyst plan subscription"
        verbose_name_plural = "User analyst plan subscriptions"

    def __str__(self):
        return f"{self.subscriber_id} → {self.plan_id} ({self.status})"

    def is_effective(self):
        """True if subscription currently grants access."""
        if self.status != self.Status.ACTIVE:
            return False
        return timezone.now() <= self.end_date

    def cancel(self):
        self.status = self.Status.CANCELLED
        self.save(update_fields=["status", "updated_at"])
        return self


class AnalystPlanPurchase(models.Model):
    """
    Immutable revenue snapshot for one analyst-plan subscription checkout.

    Amount and plan metadata are copied from ``AnalystContentPlan`` at purchase time so
    analyst revenue and purchase history stay correct if the plan price or title changes later.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.OneToOneField(
        UserAnalystPlanSubscription,
        on_delete=models.CASCADE,
        related_name="purchase",
        help_text="The subscription row this payment belongs to.",
    )
    analyst = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="analyst_plan_purchases",
        help_text="Analyst who received this purchase (denormalized for reporting).",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Price charged for this purchase (snapshot).",
    )
    currency = models.CharField(max_length=10, default="usd")
    plan_title = models.CharField(max_length=200)
    plan_scope = models.CharField(max_length=20)
    billing_period = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["analyst", "created_at"]),
            models.Index(fields=["analyst", "currency"]),
        ]
        verbose_name = "Analyst plan purchase"
        verbose_name_plural = "Analyst plan purchases"

    def __str__(self):
        return f"{self.amount} {self.currency} — sub {self.subscription_id}"


def create_analyst_plan_purchase(subscription: UserAnalystPlanSubscription, plan: AnalystContentPlan):
    """Persist an immutable purchase record from the current plan fields."""
    return AnalystPlanPurchase.objects.create(
        subscription=subscription,
        analyst_id=plan.analyst_id,
        amount=plan.price,
        currency=(plan.currency or "usd").strip().lower() or "usd",
        plan_title=plan.title,
        plan_scope=plan.scope,
        billing_period=plan.billing_period,
    )

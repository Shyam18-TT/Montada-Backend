from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from datetime import timedelta
import random
import uuid


class User(AbstractUser):
    """
    Custom User model extending Django's AbstractUser
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    USER_TYPE_CHOICES = [
        ('trader', 'Trader'),
        ('analyst', 'Analyst'),
    ]
    
    email = models.EmailField(_('email address'), unique=True)
    name = models.CharField(_('full name'), max_length=255, blank=True, null=True)
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    profile_picture = models.ImageField(upload_to='profile_pictures/', blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    user_type = models.CharField(
        max_length=10,
        choices=USER_TYPE_CHOICES,
        default='trader',
        help_text='Whether the user is a trader or analyst'
    )
    is_subscribed = models.BooleanField(
        default=False,
        help_text='True when the user has an active subscription for market news and live market data (includes in-date free trial).',
    )
    free_trial_eligible = models.BooleanField(
        default=True,
        help_text='Whether this user may still receive their first in-app free trial automatically.',
    )
    admin_granted_in_app_access = models.BooleanField(
        default=False,
        help_text='When True, user receives full in-app access (market news & live data) without a paid/trial Subscription row.',
    )
    admin_in_app_access_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='If set, admin-granted in-app access ends at this time (null = no expiry until revoked).',
    )
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Analyst profile (optional; used when user_type='analyst')
    experience = models.TextField(
        blank=True,
        null=True,
        help_text="Years or description of trading/finance experience",
    )
    company = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Current company or institution",
    )
    contact_details = models.TextField(
        blank=True,
        null=True,
        help_text="Additional contact info (e.g. secondary phone, address)",
    )
    social_links = models.JSONField(
        blank=True,
        null=True,
        help_text='Social media URLs e.g. {"twitter": "...", "linkedin": "...", "facebook": "...", "instagram": "..."}',
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        verbose_name = _('user')
        verbose_name_plural = _('users')
        ordering = ['-created_at']

    def __str__(self):
        return self.email


class PasswordResetOTP(models.Model):
    """
    Model to store OTP codes for password reset
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)
    
    class Meta:
        verbose_name = 'Password Reset OTP'
        verbose_name_plural = 'Password Reset OTPs'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"OTP for {self.email}"
    
    @staticmethod
    def generate_otp():
        """Generate a 6-digit OTP"""
        return str(random.randint(100000, 999999))
    
    def is_expired(self, expiry_minutes=10):
        """Check if OTP has expired (default 10 minutes)"""
        expiry_time = self.created_at + timedelta(minutes=expiry_minutes)
        return timezone.now() > expiry_time
    
    def is_valid(self):
        """Check if OTP is valid (not used and not expired)"""
        return not self.is_used and not self.is_expired()


class EmailVerificationOTP(models.Model):
    """
    Model to store OTP codes for email verification
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)
    
    class Meta:
        verbose_name = 'Email Verification OTP'
        verbose_name_plural = 'Email Verification OTPs'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Verification OTP for {self.email}"
    
    @staticmethod
    def generate_otp():
        """Generate a 6-digit OTP"""
        return str(random.randint(100000, 999999))
    
    def is_expired(self, expiry_minutes=10):
        """Check if OTP has expired (default 10 minutes)"""
        expiry_time = self.created_at + timedelta(minutes=expiry_minutes)
        return timezone.now() > expiry_time
    
    def is_valid(self):
        """Check if OTP is valid (not used and not expired)"""
        return not self.is_used and not self.is_expired()


class ActivityLog(models.Model):
    class ActivityType(models.TextChoices):
        FOLLOW = "FOLLOW", "Follow"
        SIGNAL_APPLIED = "SIGNAL_APPLIED", "Signal Applied"
        TAKE_PROFIT = "TAKE_PROFIT", "Take Profit"
        STOP_LOSS = "STOP_LOSS", "Stop Loss"
        GENERAL = "GENERAL", "General"

    class EntityType(models.TextChoices):
        USER = "USER", "User"
        SIGNAL = "SIGNAL", "Signal"
        PAIR = "PAIR", "Trading Pair"
        SYSTEM = "SYSTEM", "System"

    # Owner of the activity feed
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="activity_logs"
    )

    # Activity info
    type = models.CharField(
        max_length=30,
        choices=ActivityType.choices
    )

    title = models.CharField(
        max_length=255,
        help_text="Main text shown in the activity widget"
    )

    subtitle = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    # UI helpers
    icon = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Icon key used by frontend (e.g. user, chart-up)"
    )

    # Optional relation info
    entity_type = models.CharField(
        max_length=30,
        choices=EntityType.choices,
        blank=True,
        null=True
    )

    entity_id = models.BigIntegerField(
        blank=True,
        null=True
    )

    # Flexible extra data
    metadata = models.JSONField(
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["type"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.type} - {self.title}"




class UserNotification(models.Model):
    NOTIFICATION_TYPES = (
        ("INFO", "Information"),
        ("SUCCESS", "Success"),
        ("WARNING", "Warning"),
        ("ERROR", "Error"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications"
    )

    title = models.CharField(max_length=255)
    message = models.TextField()

    notification_type = models.CharField(
        max_length=20,
        choices=NOTIFICATION_TYPES,
        default="INFO"
    )

    is_read = models.BooleanField(default=False)

    redirect_url = models.URLField(
        null=True,
        blank=True,
        help_text="Optional URL to redirect when clicked"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "is_read"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.title}"



# firebase token model

class DeviceToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    fcm_token = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.fcm_token[:20]}"
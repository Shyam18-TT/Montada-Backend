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
    news_notify_ar = models.BooleanField(
        default=True,
        help_text='Whether the user wants Arabic live news notifications.',
    )
    news_notify_en = models.BooleanField(
        default=True,
        help_text='Whether the user wants English live news notifications.',
    )
    news_notify_zh = models.BooleanField(
        default=False,
        help_text='Whether the user wants Chinese live news notifications.',
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
    is_soft_deleted = models.BooleanField(
        default=False,
        help_text='Marks the account as soft deleted without removing database history.',
    )
    soft_deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the account was soft deleted.',
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


class AccountDeletionOTP(models.Model):
    """
    Model to store OTP codes for account deletion confirmation.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'Account Deletion OTP'
        verbose_name_plural = 'Account Deletion OTPs'
        ordering = ['-created_at']

    def __str__(self):
        return f"Deletion OTP for {self.email}"

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

    class NotificationCategory(models.TextChoices):
        NEWS = "NEWS", "News"
        ECONOMIC_EVENT = "ECONOMIC_EVENT", "Economic Event"
        MESSAGE = "MESSAGE", "Message"
        PRICE_ALERT = "PRICE_ALERT", "Price Alert"
        TRADING_SIGNAL = "TRADING_SIGNAL", "Trading Signal"
        SYSTEM = "SYSTEM", "System"
        COMPLIANCE = "COMPLIANCE", "Compliance"
        CHAT = "CHAT", "Chat"
        PROMOTION = "PROMOTION", "Promotion"
        FOLLOWER = "FOLLOWER", "Follower"


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

    category = models.CharField(
        max_length=50,
        choices=NotificationCategory.choices,
        default=NotificationCategory.SYSTEM
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
        # Match the notification list query (user + is_deleted [+ is_read], newest first) so
        # SQL Server can seek straight to the page instead of sorting every row for the user.
        indexes = [
            models.Index(fields=["user", "is_deleted", "is_read", "-created_at"], name="notif_user_del_read_created"),
            models.Index(fields=["user", "is_deleted", "-created_at"], name="notif_user_del_created"),
        ]

    def __str__(self):
        return f"{self.user} - {self.title}"



class NotificationPreference(models.Model):
    """
    A user's push choice for one notification category (see Mainapp.notification_preferences).
    Only categories the user changed have a row; a missing row means the default (sound).
    """

    class Mode(models.TextChoices):
        SOUND = "sound", "Notify with sound"
        SILENT = "silent", "Notify without sound"
        OFF = "off", "Do not notify"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    category = models.CharField(max_length=50)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.SOUND)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "category"], name="uniq_notification_pref_user_category"),
        ]
        indexes = [
            # Broadcast filtering reads every non-default row of one category.
            models.Index(fields=["category", "mode"], name="notif_pref_category_mode"),
        ]

    def __str__(self):
        return f"{self.user} - {self.category}: {self.mode}"


# firebase token model

def hash_push_token(token):
    """sha256 hex of an FCM token: unique-indexable (MSSQL cannot index a text column)."""
    import hashlib

    return hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()


class DeviceToken(models.Model):
    """
    One signed-in device of one account (an FCM registration).

    - A physical device belongs to at most one account at a time (unique ``device_id``).
    - A token belongs to exactly one row (unique ``token_hash``).
    - An account can have many devices; every active one receives every push.
    """

    PLATFORM_ANDROID = "android"
    PLATFORM_IOS = "ios"
    PLATFORM_UNKNOWN = "unknown"
    PLATFORM_CHOICES = [
        (PLATFORM_ANDROID, "Android"),
        (PLATFORM_IOS, "iOS"),
        (PLATFORM_UNKNOWN, "Unknown"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    fcm_token = models.TextField()
    # sha256 of fcm_token; maintained by save(). Carries the unique constraint.
    token_hash = models.CharField(max_length=64, blank=True, default="")
    device_id = models.CharField(max_length=191, blank=True, default="")
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES, default=PLATFORM_UNKNOWN)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # token / owner last changed
    last_seen_at = models.DateTimeField(default=timezone.now)  # last save-fcm-token call
    last_success_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        constraints = [
            # One row per physical device, across ALL users.
            models.UniqueConstraint(
                fields=["device_id"],
                condition=models.Q(device_id__gt=""),
                name="uniq_push_device_device_id",
            ),
            # One token can only ever belong to one row.
            models.UniqueConstraint(
                fields=["token_hash"],
                condition=models.Q(token_hash__gt=""),
                name="uniq_push_device_token",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"], name="devicetoken_user_active_idx"),
        ]

    def save(self, *args, **kwargs):
        self.device_id = (self.device_id or "").strip()
        self.fcm_token = (self.fcm_token or "").strip()
        self.token_hash = hash_push_token(self.fcm_token) if self.fcm_token else ""
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            if "fcm_token" in fields:
                fields.add("token_hash")
            if "device_id" in fields or "fcm_token" in fields:
                fields.add("updated_at")
            kwargs["update_fields"] = list(fields)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} - {self.fcm_token[:20]}"


class PushDelivery(models.Model):
    """
    One push attempt to one device. Lets support answer "did this notification reach the
    user's devices?". Kept 30 days (``manage.py cleanup_push_devices``).
    """

    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_DELETED_TOKEN = "deleted_token"
    STATUS_SKIPPED_PREF = "skipped_pref"
    STATUS_CHOICES = [
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
        (STATUS_DELETED_TOKEN, "Token deleted"),
        (STATUS_SKIPPED_PREF, "Skipped (preference off)"),
    ]

    id = models.BigAutoField(primary_key=True)
    notification_id = models.CharField(max_length=64, blank=True, default="")
    # Plain UUID, not a FK: the log must survive and never block user deletion.
    user_id = models.UUIDField(null=True, blank=True)
    device_id = models.CharField(max_length=191, blank=True, default="")
    platform = models.CharField(max_length=16, blank=True, default="")
    push_type = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    fcm_message_id = models.CharField(max_length=255, blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user_id", "created_at"], name="pushdelivery_user_created_idx"),
            models.Index(fields=["notification_id"], name="pushdelivery_notification_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.push_type} {self.status} {self.device_id[:12]}"


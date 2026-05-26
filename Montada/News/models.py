import uuid
from django.db import models
from django.conf import settings
from django.utils.text import slugify
from django.utils import timezone


class NewsCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=150, unique=True)
    slug = models.SlugField(max_length=160, unique=True)

    class Meta:
        verbose_name_plural = "Categories"
        indexes = [
            models.Index(fields=['slug']),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Tag(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class NewsArticle(models.Model):

    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('published', 'Published'),
        ('archived', 'Archived'),
    )

    class ContentAccess(models.TextChoices):
        FREE = "free", "Free"
        PREMIUM = "premium", "Premium"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=270, unique=True, blank=True, null=True)

    summary = models.TextField(blank=True, null=True)
    content = models.TextField()

    featured_image = models.ImageField(upload_to='news/', blank=True, null=True)

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='news_articles'
    )

    category = models.ForeignKey(
        NewsCategory,
        on_delete=models.SET_NULL,
        null=True,
        related_name='articles'
    )

    tags = models.ManyToManyField(Tag, blank=True, related_name='articles')

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft'
    )

    is_featured = models.BooleanField(default=False)

    content_access = models.CharField(
        max_length=20,
        choices=ContentAccess.choices,
        default=ContentAccess.PREMIUM,
        help_text="Free: visible to all authenticated traders. Premium: requires an active analyst plan covering articles.",
    )

    published_at = models.DateTimeField(blank=True, null=True)


    # Tracking
    views_count = models.PositiveIntegerField(default=0)

    # Audit Fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ['-published_at', '-created_at']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['status']),
            models.Index(fields=['published_at']),
            models.Index(fields=['content_access']),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)

        if self.status == 'published' and not self.published_at:
            self.published_at = timezone.now()

        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class EconomicCalendarEvent(models.Model):
    """
    Stores economic calendar events fetched from external providers (e.g., Tradays).
    """
    class Importance(models.TextChoices):
        NONE = "none", "None"
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider_id = models.BigIntegerField(unique=True, help_text="Unique ID from the provider (e.g. Tradays)")
    
    event_name = models.CharField(max_length=255)
    currency_code = models.CharField(max_length=10, blank=True, null=True)
    country_name = models.CharField(max_length=100, blank=True, null=True)
    
    importance = models.CharField(
        max_length=10, 
        choices=Importance.choices, 
        default=Importance.NONE
    )
    
    actual_value = models.CharField(max_length=100, blank=True, null=True)
    forecast_value = models.CharField(max_length=100, blank=True, null=True)
    previous_value = models.CharField(max_length=100, blank=True, null=True)
    
    release_date = models.DateTimeField(help_text="When the event happens")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-release_date"]
        indexes = [
            models.Index(fields=["provider_id"]),
            models.Index(fields=["release_date"]),
            models.Index(fields=["importance"]),
        ]
        db_table = "economic_calendar_event"

    def __str__(self):
        return f"{self.event_name} - {self.release_date}"
        
class NewsArticleLike(models.Model):
    """User like on an analyst news article. One like per user per article."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="news_article_likes",
    )
    article = models.ForeignKey(
        NewsArticle,
        on_delete=models.CASCADE,
        related_name="likes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("user", "article")]
        indexes = [
            models.Index(fields=["article"]),
        ]
        db_table = "news_article_like"

    def __str__(self):
        return f"{self.user_id} likes {self.article_id}"


class NewsArticleComment(models.Model):
    """Comment on an analyst news article."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="news_article_comments",
    )
    article = models.ForeignKey(
        NewsArticle,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["article"]),
        ]
        db_table = "news_article_comment"

    def __str__(self):
        return f"{self.user_id} on {self.article_id}"


class LiveNews(models.Model):
    """
    Compact live news item stored from websocket/provider payloads.
    Keeps only the fields the frontend is likely to render directly.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    provider_event_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Outer websocket event id (data.id).",
    )
    provider_content_id = models.BigIntegerField(
        unique=True,
        help_text="Provider article/content id (data.content.id).",
    )
    provider_revision_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Provider content revision id.",
    )
    original_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Original provider content id if supplied.",
    )

    action = models.CharField(max_length=30, blank=True, null=True)
    news_type = models.CharField(max_length=100, blank=True, null=True)
    language = models.CharField(
        max_length=20,
        default="unknown",
        help_text="Detected language code for the news content, e.g. ar, en, unknown.",
    )
    title = models.CharField(max_length=500)
    teaser = models.TextField(blank=True, null=True)
    body = models.TextField(blank=True, null=True)
    source_url = models.URLField(max_length=1000, blank=True, null=True, unique=True)

    authors = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)
    securities = models.JSONField(
        default=list,
        blank=True,
        help_text="List of relevant security dictionaries like symbol/exchange/primary.",
    )
    channels = models.JSONField(default=list, blank=True)
    images = models.JSONField(
        default=list,
        blank=True,
        help_text="List of image dictionaries like size/url.",
    )
    primary_image_url = models.URLField(max_length=1000, blank=True, null=True)

    source_created_at = models.DateTimeField(blank=True, null=True)
    source_updated_at = models.DateTimeField(blank=True, null=True)
    source_timestamp = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Outer websocket event timestamp.",
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-source_updated_at", "-source_created_at", "-created_at"]
        indexes = [
            models.Index(fields=["provider_content_id"]),
            models.Index(fields=["source_updated_at"]),
            models.Index(fields=["news_type"]),
            models.Index(fields=["language"]),
            models.Index(fields=["is_active"]),
            models.Index(
                fields=["is_active", "language", "source_updated_at", "source_created_at", "created_at"],
                name="live_news_listing_idx",
            ),
        ]
        db_table = "live_news"

    def __str__(self):
        return self.title



class EconomicCalendarReminder(models.Model):
    """
    Stores user reminders for economic calendar events.
    """

    class ReminderType(models.TextChoices):
        BEFORE_5_MIN = "5_min_before", "5 Minutes Before"
        BEFORE_15_MIN = "15_min_before", "15 Minutes Before"
        BEFORE_30_MIN = "30_min_before", "30 Minutes Before"
        BEFORE_1_HOUR = "1_hour_before", "1 Hour Before"
        CUSTOM = "custom", "Custom"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Relation to event
    event = models.ForeignKey(
        EconomicCalendarEvent,
        on_delete=models.CASCADE,
        related_name="reminders"
    )

    # User who created the reminder
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="economic_event_reminders"
    )

    # Reminder configuration
    reminder_type = models.CharField(
        max_length=20,
        choices=ReminderType.choices,
        default=ReminderType.BEFORE_15_MIN
    )

    # Used only when reminder_type = CUSTOM
    custom_minutes_before = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Custom reminder time in minutes before the event"
    )

    # Calculated reminder trigger time
    reminder_time = models.DateTimeField(
        help_text="Exact datetime when reminder should be triggered"
    )

    # Notification tracking
    is_sent = models.BooleanField(default=False)
    sent_at = models.DateTimeField(blank=True, null=True)

    # Status
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "economic_calendar_reminder"
        ordering = ["reminder_time"]
        unique_together = ("event", "user", "reminder_time")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["event"]),
            models.Index(fields=["reminder_time"]),
            models.Index(fields=["is_sent"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.event.event_name} reminder"


class EconomicCalendarEventNotification(models.Model):
    """
    Tracks economic calendar event notifications sent to users.
    Prevents duplicate notifications for the same event to the same user or group.
    
    Used by management command to track which notifications have been sent:
    - Event-time notifications (sent to all subscribed users when event occurs)
    - Reminder notifications (tracked separately but can reference this model)
    """
    
    class NotificationType(models.TextChoices):
        REMINDER = "reminder", "Reminder Notification"
        EVENT = "event", "Event-Time Notification"
        BROADCAST = "broadcast", "Broadcast to All Users"
        ADMIN_ADVANCE = "admin_advance", "Admin Global Advance Reminder"
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Reference to the event
    event = models.ForeignKey(
        EconomicCalendarEvent,
        on_delete=models.CASCADE,
        related_name="event_notifications",
        help_text="The economic calendar event"
    )
    
    # User who received the notification (nullable for broadcast to all)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="economic_event_notifications",
        blank=True,
        null=True,
        help_text="Specific user. NULL for broadcast to all subscribed users."
    )
    
    # Type of notification
    notification_type = models.CharField(
        max_length=20,
        choices=NotificationType.choices,
        default=NotificationType.EVENT,
        help_text="Type of notification sent"
    )
    
    # Notification tracking
    is_sent = models.BooleanField(default=True, help_text="Whether notification was successfully sent")
    sent_at = models.DateTimeField(auto_now_add=True, help_text="When notification was sent")
    
    # Optional: Track if it was an all-users broadcast
    sent_to_all_users = models.BooleanField(
        default=False,
        help_text="True if this notification was sent to all subscribed users"
    )
    
    # For audit trail
    error_message = models.TextField(
        blank=True,
        null=True,
        help_text="Error message if notification failed to send"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = "economic_calendar_event_notification"
        ordering = ["-sent_at"]
        # Prevent duplicate event notifications to same user
        unique_together = ("event", "user", "notification_type", "sent_to_all_users")
        indexes = [
            models.Index(fields=["event"]),
            models.Index(fields=["user"]),
            models.Index(fields=["notification_type"]),
            models.Index(fields=["sent_at"]),
            models.Index(fields=["is_sent"]),
            models.Index(fields=["event", "notification_type"]),
            models.Index(fields=["user", "notification_type"]),
        ]
    
    def __str__(self):
        user_str = self.user.username if self.user else "All Users"
        return f"{user_str} - {self.event.event_name} ({self.get_notification_type_display()})"
    
    @classmethod
    def check_notification_sent(cls, event, user=None, notification_type=NotificationType.EVENT):
        """
        Check if a notification has already been sent for this event/user combo.
        
        Args:
            event: EconomicCalendarEvent instance
            user: User instance (None for broadcast to all)
            notification_type: Type of notification
        
        Returns:
            bool: True if notification already sent, False otherwise
        """
        qs = cls.objects.filter(
            event=event,
            user=user,
            notification_type=notification_type,
            is_sent=True,
        )
        if notification_type == cls.NotificationType.ADMIN_ADVANCE and user is None:
            qs = qs.filter(sent_to_all_users=True)
        return qs.exists()

    @classmethod
    def claim_admin_advance_notification(cls, event):
        """
        Atomically reserve the right to send the global admin advance reminder once.

        Inserts a tracking row before push/in-app delivery so concurrent scheduler runs
        and repeated ticks inside the catch-up window cannot duplicate notifications.

        Returns:
            bool: True if this caller should send; False if already claimed/sent.
        """
        from django.db import IntegrityError

        try:
            cls.objects.create(
                event=event,
                user=None,
                notification_type=cls.NotificationType.ADMIN_ADVANCE,
                sent_to_all_users=True,
                is_sent=True,
            )
            return True
        except IntegrityError:
            return False
    
    @classmethod
    def create_notification_record(cls, event, user=None, notification_type=NotificationType.EVENT, 
                                   sent_to_all=False, is_sent=True, error_message=None):
        """
        Create a notification tracking record.
        
        Args:
            event: EconomicCalendarEvent instance
            user: User instance (None for broadcast)
            notification_type: Type of notification
            sent_to_all: Whether sent to all users
            is_sent: Whether send was successful
            error_message: Error details if failed
        
        Returns:
            tuple: (notification_record, created)
        """
        return cls.objects.get_or_create(
            event=event,
            user=user,
            notification_type=notification_type,
            sent_to_all_users=sent_to_all,
            defaults={
                'is_sent': is_sent,
                'error_message': error_message,
            }
        )


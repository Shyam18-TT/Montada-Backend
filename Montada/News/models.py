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
        ]
        db_table = "live_news"

    def __str__(self):
        return self.title


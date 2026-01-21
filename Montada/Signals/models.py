from django.db import models
from django.conf import settings
from django.utils import timezone
import uuid


class ActiveSignalManager(models.Manager):
    """
    Custom manager that excludes soft-deleted signals
    """
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AssetClass(models.Model):
    """
    Example:
    - Forex
    - Commodities
    - Indices
    - Crypto
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(
        max_length=50,
        unique=True
    )

    description = models.TextField(
        blank=True,
        null=True
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Asset Class"
        verbose_name_plural = "Asset Classes"
        ordering = ['name']

    def __str__(self):
        return self.name


class Instrument(models.Model):
    """
    Example:
    Forex      -> EUR/USD, GBP/USD
    Commodity  -> XAU/USD, CL
    Indices    -> NAS100, US30
    Crypto     -> BTC/USD, ETH/USD
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    asset_class = models.ForeignKey(
        AssetClass,
        on_delete=models.CASCADE,
        related_name='instruments'
    )

    symbol = models.CharField(
        max_length=20,
        help_text="e.g. EUR/USD, XAU/USD, NAS100"
    )

    name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Optional full name"
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('asset_class', 'symbol')
        ordering = ['asset_class__name', 'symbol']

    def __str__(self):
        return f"{self.symbol} ({self.asset_class.name})"


class Timeframe(models.Model):
    """
    Trading timeframes for signals
    Example:
    - M1 (1 minute)
    - M5 (5 minutes)
    - M15 (15 minutes)
    - M30 (30 minutes)
    - H1 (1 hour)
    - H4 (4 hours)
    - D1 (1 day)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    code = models.CharField(
        max_length=5,
        unique=True,
        help_text="Timeframe code (e.g. M1, M5, H1, D1)"
    )

    name = models.CharField(
        max_length=50,
        help_text="Display name for the timeframe"
    )

    description = models.TextField(
        blank=True,
        null=True,
        help_text="Optional description of the timeframe"
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Timeframe"
        verbose_name_plural = "Timeframes"
        ordering = ['code']

    def __str__(self):
        return self.code


class TradingSignal(models.Model):
    # -------------------------
    # PRIMARY KEY
    # -------------------------
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # -------------------------
    # ENUM / CHOICES
    # -------------------------
    class Direction(models.TextChoices):
        BUY = 'BUY', 'Buy'
        SELL = 'SELL', 'Sell'
    
    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        CLOSED = 'CLOSED', 'Closed'
        DRAFT = 'DRAFT','Draft'

    # -------------------------
    # CORE FIELDS
    # -------------------------
    analyst = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='posted_signals'
    )

    asset_class = models.ForeignKey(
        AssetClass,
        on_delete=models.CASCADE,
        related_name='trading_signals',
        help_text="Asset class for this trading signal"
    )

    instrument = models.ForeignKey(
        Instrument,
        on_delete=models.CASCADE,
        related_name='trading_signals',
        help_text="Instrument for this trading signal"
    )

    direction = models.CharField(
        max_length=4,
        choices=Direction.choices
    )

    entry_price = models.DecimalField(
        max_digits=12,
        decimal_places=5
    )

    stop_loss = models.DecimalField(
        max_digits=12,
        decimal_places=5
    )

    take_profit = models.DecimalField(
        max_digits=12,
        decimal_places=5
    )

    timeframe = models.ForeignKey(
        Timeframe,
        on_delete=models.CASCADE,
        related_name='trading_signals',
        help_text="Timeframe for this trading signal"
    )

    confidence_level = models.PositiveSmallIntegerField(
        help_text="Confidence percentage (0–100)"
    )

    analyst_note = models.TextField(
        blank=True,
        null=True
    )

    # -------------------------
    # META / STATUS
    # -------------------------
    is_active = models.BooleanField(default=True)
    
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.OPEN,
        help_text="Signal status: OPEN or CLOSED"
    )
    
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when signal was soft deleted"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Custom managers
    objects = models.Manager()  # Default manager (includes all signals)
    active = ActiveSignalManager()  # Manager that excludes soft-deleted signals

    # -------------------------
    # VALIDATION
    # -------------------------
    def clean(self):
        """Validate that instrument belongs to the selected asset_class and confidence level"""
        if self.asset_class and self.instrument:
            if self.instrument.asset_class != self.asset_class:
                raise ValueError(
                    f"Instrument '{self.instrument.symbol}' does not belong to "
                    f"asset class '{self.asset_class.name}'"
                )
        if self.confidence_level and self.confidence_level > 100:
            raise ValueError("Confidence level cannot exceed 100%")

    def soft_delete(self):
        """Soft delete the signal by setting deleted_at timestamp"""
        self.deleted_at = timezone.now()
        self.save(update_fields=['deleted_at'])
    
    def restore(self):
        """Restore a soft-deleted signal"""
        self.deleted_at = None
        self.save(update_fields=['deleted_at'])
    
    @property
    def is_deleted(self):
        """Check if signal is soft deleted"""
        return self.deleted_at is not None

    def __str__(self):
        instrument_symbol = self.instrument.symbol if self.instrument else "N/A"
        timeframe_code = self.timeframe.code if self.timeframe else "N/A"
        return f"{instrument_symbol} | {self.direction} | {timeframe_code}"
from django.db import models
from django.conf import settings

class AssetClass(models.Model):
    """
    Example:
    - Forex
    - Commodities
    - Indices
    - Crypto
    """

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
        
class TradingSignal(models.Model):
    # -------------------------
    # ENUM / CHOICES
    # -------------------------
    class Direction(models.TextChoices):
        BUY = 'BUY', 'Buy'
        SELL = 'SELL', 'Sell'

    class Timeframe(models.TextChoices):
        M1 = 'M1', 'M1'
        M5 = 'M5', 'M5'
        M15 = 'M15', 'M15'
        M30 = 'M30', 'M30'
        H1 = 'H1', 'H1'
        H4 = 'H4', 'H4'
        D1 = 'D1', 'D1'

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

    timeframe = models.CharField(
        max_length=5,
        choices=Timeframe.choices
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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

    def __str__(self):
        instrument_symbol = self.instrument.symbol if self.instrument else "N/A"
        return f"{instrument_symbol} | {self.direction} | {self.timeframe}"
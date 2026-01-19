from django.db import models
from django.conf import settings


class TradingSignal(models.Model):
    # -------------------------
    # ENUM / CHOICES
    # -------------------------
    class AssetClass(models.TextChoices):
        FOREX = 'FOREX', 'Forex'
        COMMODITY = 'COMMODITY', 'Commodity'
        INDICES = 'INDICES', 'Indices'
        CRYPTO = 'CRYPTO', 'Crypto'

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

    asset_class = models.CharField(
        max_length=20,
        choices=AssetClass.choices
    )

    instrument = models.CharField(
        max_length=20,
        help_text="e.g. EUR/USD, XAU/USD, NAS100, BTC/USD"
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
        if self.confidence_level > 100:
            raise ValueError("Confidence level cannot exceed 100%")

    def __str__(self):
        return f"{self.instrument} | {self.direction} | {self.timeframe}"
from decimal import Decimal

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

    is_win = models.BooleanField(null=True, blank=True)
    is_loss = models.BooleanField(null=True, blank=True)
    is_neutral = models.BooleanField(null=True, blank=True)
    entry_triggered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When market price first reached the entry price and the signal became active.",
    )
    entry_watch_direction = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        help_text="Internal direction used to determine whether price must move up or down to reach entry.",
    )

    price_alert_fcm_sent = models.BooleanField(
        default=False,
        help_text="True after the price-alert FCM push has been sent for this signal (TP/SL hit). Ensures we only send once."
    )

    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the signal was closed. Set automatically in save(); analytics bucket closed signals by it.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Custom managers
    objects = models.Manager()  # Default manager (includes all signals)
    active = ActiveSignalManager()  # Manager that excludes soft-deleted signals

    class Meta:
        # AnalystSignalListView / TraderSignalListView both filter on
        # analyst (+ optional status) and order by -created_at; without a
        # composite index Django only has the implicit single-column FK
        # index on analyst, so MSSQL falls back to a scan/bookmark lookup
        # for the filtered+ordered read instead of a covering seek.
        indexes = [
            models.Index(fields=["analyst", "status", "-created_at"], name="signal_analyst_status_created"),
        ]

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

    def save(self, *args, **kwargs):
        # Stamp closed_at on every path that closes a signal (price alerts, analyst/admin
        # edits, Django admin) and clear it if the signal is reopened. Later edits to a
        # closed signal keep the original close time, unlike updated_at.
        is_closed = self.status == self.Status.CLOSED
        if is_closed != (self.closed_at is not None):
            self.closed_at = timezone.now() if is_closed else None
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "closed_at"}
        super().save(*args, **kwargs)

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


class AppliedSignal(models.Model):
    """
    Records when a trader applies (takes) a trading signal.
    One trader can apply a signal only once; one signal can be applied by many traders.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    trader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='applied_signals'
    )

    signal = models.ForeignKey(
        TradingSignal,
        on_delete=models.CASCADE,
        related_name='applications'
    )

    applied_at = models.DateTimeField(auto_now_add=True)

    note = models.TextField(blank=True, null=True, help_text="Optional note when applying the signal")

    class Meta:
        verbose_name = "Applied Signal"
        verbose_name_plural = "Applied Signals"
        ordering = ['-applied_at']
        constraints = [
            models.UniqueConstraint(
                fields=['trader', 'signal'],
                name='unique_trader_signal_application'
            )
        ]

    def __str__(self):
        return f"{self.trader} applied {self.signal}"


class PriceAlert(models.Model):
    """
    User-set price alert: notify when market price reaches a target level.
    Checked by run_price_alerts management command; when triggered, user gets
    in-app notification and FCM push.
    """
    class Condition(models.TextChoices):
        ABOVE = "above", "Price goes above target"
        BELOW = "below", "Price goes below target"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="price_alerts",
    )
    instrument = models.ForeignKey(
        Instrument,
        on_delete=models.CASCADE,
        related_name="price_alerts",
        help_text="Market/symbol to watch (e.g. EUR/USD, GOLD)",
    )
    target_price = models.DecimalField(
        max_digits=18,
        decimal_places=5,
        null=True,
        blank=True,
        help_text="Alert when price reaches this level (use this OR target_percentage + reference_price)",
    )
    target_percentage = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Alert when price moves this %% from reference (e.g. 5 = 5%%)",
    )
    reference_price = models.DecimalField(
        max_digits=18,
        decimal_places=5,
        null=True,
        blank=True,
        help_text="Base price for percentage; required when target_percentage is set",
    )
    condition = models.CharField(
        max_length=10,
        choices=Condition.choices,
        default=Condition.ABOVE,
        help_text="Trigger when price goes above or below target",
    )
    label = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        help_text="Optional label for the alert",
    )
    activation_price = models.DecimalField(
        max_digits=18,
        decimal_places=5,
        null=True,
        blank=True,
        help_text="Price level the market must touch before the alert becomes active.",
    )
    activation_watch_direction = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        help_text="Internal direction used to determine whether price must move up or down to activate the alert.",
    )
    armed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the alert became active and started watching the target price.",
    )
    is_triggered = models.BooleanField(default=False)
    triggered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        db_table = "price_alert"
        indexes = [
            models.Index(fields=["user", "is_triggered"]),
        ]

    def get_effective_target_price(self):
        """
        Return the price level that triggers the alert.
        - If target_price is set: return it.
        - If target_percentage and reference_price are set: return reference * (1 ± pct/100).
        - Otherwise return None.
        """
        from decimal import Decimal
        if self.target_price is not None:
            return self.target_price
        if self.target_percentage is not None and self.reference_price is not None:
            pct = Decimal(str(self.target_percentage)) / Decimal("100")
            reference = Decimal(str(self.reference_price))
            if (self.condition or "").lower() == "below":
                return reference * (Decimal("1") - pct)
            return reference * (Decimal("1") + pct)
        return None

    def __str__(self):
        sym = self.instrument.symbol if self.instrument else "?"
        if self.target_price is not None:
            return f"{self.user_id} {sym} {self.condition} {self.target_price}"
        if self.target_percentage is not None and self.reference_price is not None:
            return f"{self.user_id} {sym} {self.condition} {self.target_percentage}% of {self.reference_price}"
        return f"{self.user_id} {sym} (incomplete)"

class PriceMoveNotificationSettings(models.Model):
    """
    Singleton row (pk=1): global defaults for the symbol %-move push notifications sent
    by poll_signal_change_notifications ("EURUSD up 0.5%"). Per-asset-class and per-symbol
    overrides live in PriceMoveNotificationRule.
    """
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    is_enabled = models.BooleanField(
        default=True,
        help_text="Global switch. When off, no move notifications are sent for any symbol.",
    )
    default_threshold_percent = models.DecimalField(
        max_digits=7, decimal_places=3, default=Decimal("0.5"),
        help_text="First notification level, e.g. 0.5 -> notify at 0.5%.",
    )
    default_step_percent = models.DecimalField(
        max_digits=7, decimal_places=3, null=True, blank=True,
        help_text="Gap between later levels. Empty = same as the threshold (0.5, 1, 1.5, ...).",
    )
    reset_percent = models.DecimalField(
        max_digits=7, decimal_places=3, default=Decimal("0.3"),
        help_text="When the daily change falls back to this %, the symbol can notify the same levels again.",
    )
    cooldown_seconds = models.PositiveIntegerField(
        default=300,
        help_text="Minimum seconds before the same direction/level is notified again.",
    )
    poll_interval_seconds = models.PositiveIntegerField(
        default=30,
        help_text="Seconds between price polls.",
    )
    max_notification_batches = models.PositiveIntegerField(
        default=200,
        help_text="Maximum symbol notifications sent in a single poll.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Price move notification settings"
        verbose_name_plural = "Price move notification settings"
        db_table = "signals_price_move_settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Price move notifications ({'on' if self.is_enabled else 'off'}, {self.default_threshold_percent}%)"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PriceMoveNotificationRule(models.Model):
    """
    Admin override for one asset class or one symbol: its own threshold/step/reset/cooldown,
    or muted (is_enabled=False). Empty fields inherit from the asset-class rule (for a symbol
    rule) and then from PriceMoveNotificationSettings.
    """
    class Scope(models.TextChoices):
        ASSET_CLASS = "asset_class", "Asset class"
        SYMBOL = "symbol", "Symbol"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=20, choices=Scope.choices)
    asset_class = models.ForeignKey(
        AssetClass,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="price_move_rules",
    )
    symbol = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="Feed symbol, stored normalized (EUR/USD -> EURUSD). Also matches aliases, "
                  "futures contracts (WHEAT -> WHEAT_Z26) and broker variants (AUDNOK.p).",
    )
    is_enabled = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        help_text="Empty = inherit, False = muted, True = notify even if the asset class is muted.",
    )
    threshold_percent = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    step_percent = models.DecimalField(
        max_digits=7, decimal_places=3, null=True, blank=True,
        help_text="Empty = same as this rule's threshold.",
    )
    reset_percent = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    cooldown_seconds = models.PositiveIntegerField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "signals_price_move_rule"
        ordering = ["scope", "symbol"]
        # Filtered unique indexes: a plain unique (asset_class, symbol) would allow only one
        # NULL per column on SQL Server.
        constraints = [
            models.UniqueConstraint(
                fields=["asset_class"],
                condition=models.Q(scope="asset_class"),
                name="uniq_price_move_rule_asset_class",
            ),
            models.UniqueConstraint(
                fields=["symbol"],
                condition=models.Q(scope="symbol"),
                name="uniq_price_move_rule_symbol",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(scope="asset_class", asset_class__isnull=False, symbol__isnull=True)
                    | models.Q(scope="symbol", asset_class__isnull=True, symbol__isnull=False)
                ),
                name="price_move_rule_scope_target",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.symbol is not None:
            self.symbol = str(self.symbol).replace("/", "").replace(" ", "").upper() or None
        super().save(*args, **kwargs)

    @property
    def target_label(self):
        if self.scope == self.Scope.ASSET_CLASS:
            return getattr(self.asset_class, "name", None) or str(self.asset_class_id)
        return self.symbol

    def __str__(self):
        state = {None: "inherit", True: "on", False: "muted"}[self.is_enabled]
        return f"{self.get_scope_display()} {self.target_label} ({state})"

"""
High-water-mark state for the symbol %-move notifications ("EURUSD up 0.5%").

A level is notified once per symbol and direction in a period. The stored level only ever
rises inside the period, so a price that oscillates around a level (0.5% -> 0.4% -> 0.5%) is
notified once, and a jump over several levels is notified once, for the level reached.

    level = floor((abs(change) - threshold) / step) + 1   (0 below the threshold)

With the default step (= threshold) that is floor(abs(change) / threshold): 0.49% is level 0,
0.5% level 1, 1.49% level 2, 1.5% level 3. A separate step (admin "step" setting) keeps the
first level at the threshold and spaces the later ones by the step.

Scope: the notification is a broadcast to every active user, so state is per
(symbol, direction), never per user. Per-user choices only change how the push is delivered
(Mainapp.notification_preferences).

Period: state belongs to the reference price the feed's change_percentage is measured against.
Every quote carries it as `bid_today`, so the period key is that price: when the website rolls
its daily reference, the key changes and both directions start again from level 0, at exactly
the moment the % itself restarts. This matters because the feed clock is broker time (UTC+3),
so a UTC-midnight reset would not line up with the website's daily roll. Quotes without a
usable reference fall back to a calendar day (PRICE_MOVE_PERIOD_TIMEZONE, starting at
PRICE_MOVE_PERIOD_START_HOUR); PRICE_MOVE_PERIOD_SOURCE = "calendar" uses the calendar day
for every symbol.

Atomicity: advance() is one conditional UPDATE (`... WHERE last_level < :level`), or an INSERT
protected by a unique constraint. Whoever changes a row owns that level and is the only one
that sends the push.
"""
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

UP = "up"
DOWN = "down"
DIRECTIONS = (UP, DOWN)

# Percentages are compared at this precision, so 0.49999999 (float noise) is the 0.5000 level.
PERCENT_PRECISION = Decimal("0.0001")
# A calendar-day period that every symbol shares, used when the feed gives no reference.
NO_RESET_PERIOD = "all"


def opposite(direction):
    return DOWN if direction == UP else UP


def quantize_percent(value):
    """abs(value) as a Decimal rounded to PERCENT_PRECISION (half up)."""
    return abs(Decimal(value)).quantize(PERCENT_PRECISION, rounding=ROUND_HALF_UP)


def level_for(change_percentage, threshold, step):
    """Level reached by `change_percentage` (signed or not): 0 below the threshold."""
    value = quantize_percent(change_percentage)
    if value < threshold:
        return 0
    return int((value - threshold) // step) + 1


def level_percentage(level, threshold, step):
    """The % at which `level` is reached (level 1 = threshold)."""
    return threshold + step * Decimal(int(level) - 1)


def normalize_reference(value):
    """Reference price as a canonical string, or None when missing / not a positive number."""
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return format(number.normalize(), "f")


def parse_tick_time(value):
    """
    Timestamp of a feed quote ("2026-10-16 09:52:39.000", broker time) as an aware datetime.
    The offset is unknown and does not matter: it is only used to order ticks of one symbol.
    """
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=dt_timezone.utc)
        except ValueError:
            continue
    return None


def calendar_period(now=None):
    """Calendar-day period key, in PRICE_MOVE_PERIOD_TIMEZONE, starting at PRICE_MOVE_PERIOD_START_HOUR."""
    try:
        tz = ZoneInfo(str(getattr(settings, "PRICE_MOVE_PERIOD_TIMEZONE", "UTC") or "UTC"))
    except Exception:
        logger.warning("Invalid PRICE_MOVE_PERIOD_TIMEZONE; using UTC.")
        tz = ZoneInfo("UTC")
    try:
        start_hour = int(getattr(settings, "PRICE_MOVE_PERIOD_START_HOUR", 0) or 0)
    except (TypeError, ValueError):
        start_hour = 0
    local = (now or timezone.now()).astimezone(tz) - timedelta(hours=start_hour)
    return "day:" + local.date().isoformat()


def period_key(reference=None, *, now=None, reset=True):
    """
    Key of the period a quote belongs to. `reference` is the normalized bid_today of the quote.
    reset=False (the --no-daily-reset flag) never resets.
    """
    if not reset:
        return NO_RESET_PERIOD
    source = str(getattr(settings, "PRICE_MOVE_PERIOD_SOURCE", "reference") or "reference").lower()
    if source != "calendar" and reference:
        return "ref:" + reference
    return calendar_period(now)


def notification_id(symbol, direction, level, period):
    """
    Idempotency key of one alert: (instrument, direction, level, period). Hashed so it fits the
    64 characters iOS keeps of a collapse id. Passed to firebase as data["notification_id"],
    which then skips devices that already received it.
    """
    raw = "%s|%s|%s|%s" % (symbol, direction, int(level), period)
    return "pm-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


@dataclass(frozen=True)
class LevelState:
    level: int
    period: str
    config_sig: str = ""
    tick_at: object = None


def _model():
    from Signals.models import PriceMoveLevelState

    return PriceMoveLevelState


def load_states():
    """{(symbol, direction): LevelState} for every stored row (two per symbol)."""
    return {
        (symbol, direction): LevelState(level, period, sig, tick_at)
        for symbol, direction, period, level, sig, tick_at in _model().objects.values_list(
            "symbol", "direction", "period", "last_level", "config_sig", "last_tick_at"
        )
    }


def has_states():
    return _model().objects.exists()


def advance(symbol, direction, *, period, level, percentage, config_sig, tick_at=None):
    """
    Atomically raise the high-water mark to `level`. True only for the caller that raised it;
    False when the stored level for this period is already >= `level` (already notified, or
    another worker got there first) or the tick is older than the last one that advanced.
    """
    Model = _model()
    values = {
        "period": period,
        "last_level": int(level),
        "last_percentage": percentage,
        "config_sig": config_sig,
        "updated_at": timezone.now(),
    }
    if tick_at is not None:
        values["last_tick_at"] = tick_at
    rows = Model.objects.filter(symbol=symbol, direction=direction)
    if tick_at is not None:
        rows = rows.filter(Q(last_tick_at__isnull=True) | Q(last_tick_at__lte=tick_at))
    # A row from another period is overwritten, which is the period reset.
    can_raise = Q(period=period, last_level__lt=int(level)) | ~Q(period=period)

    # Two passes: when the INSERT loses to a concurrent INSERT of the same row, that row may
    # still be below `level`, so the conditional UPDATE gets one more chance.
    for _ in range(2):
        if rows.filter(can_raise).update(**values):
            return True
        try:
            with transaction.atomic():
                Model.objects.create(symbol=symbol, direction=direction, **values)
            return True
        except IntegrityError:
            continue
    return False


def rebase(symbol, direction, *, period, level, percentage, config_sig, tick_at=None):
    """
    Set the level unconditionally, for a symbol whose level grid changed (threshold/step edited,
    muted, unmuted). Levels from two different grids are never compared.
    """
    Model = _model()
    values = {
        "period": period,
        "last_level": int(level),
        "last_percentage": percentage,
        "config_sig": config_sig,
        "updated_at": timezone.now(),
    }
    if tick_at is not None:
        values["last_tick_at"] = tick_at
    for _ in range(2):
        if Model.objects.filter(symbol=symbol, direction=direction).update(**values):
            return
        try:
            with transaction.atomic():
                Model.objects.create(symbol=symbol, direction=direction, **values)
            return
        except IntegrityError:
            continue


def revert(symbol, direction, *, period, claimed_level, restore_level, restore_percentage):
    """
    Give back a claimed level when nothing could be delivered, so the next poll tries again.
    Only undoes our own claim: a row that has moved on since (higher level) is left alone.
    """
    return bool(
        _model().objects.filter(
            symbol=symbol, direction=direction, period=period, last_level=int(claimed_level)
        ).update(
            last_level=int(restore_level),
            last_percentage=restore_percentage,
            updated_at=timezone.now(),
        )
    )

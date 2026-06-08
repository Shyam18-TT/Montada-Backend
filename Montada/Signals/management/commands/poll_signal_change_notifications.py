import json
import logging
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone


logger = logging.getLogger(__name__)

DEFAULT_PRICE_URL = "https://uat.trustcapital.com/api/get-MT5-price"
DEFAULT_THRESHOLD_PERCENT = Decimal("0.5")
DEFAULT_STATE_TTL_SECONDS = 60 * 60 * 24 * 7
DEFAULT_LOCK_TTL_SECONDS = 60
DEFAULT_MAX_NOTIFICATION_BATCHES_PER_POLL = 200
STATE_CACHE_KEY_PREFIX = "signals:change-notifications:state"
LOCK_CACHE_KEY_PREFIX = "signals:change-notifications:lock"


def _normalize_symbol(symbol):
    return str(symbol or "").replace("/", "").replace(" ", "").upper()


def _parse_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace("+", ""))
    except (InvalidOperation, ValueError):
        return None


def _format_percent(value):
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.to_integral())
    return format(normalized, "f").rstrip("0").rstrip(".")


def _level_key(value):
    return _format_percent(abs(value))


def _extract_live_quotes(payload):
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") or {}
    live_quote = data.get("live_quote") or {}
    if not isinstance(live_quote, dict):
        return {}
    return live_quote


def _fetch_live_quotes(url, timeout):
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        raw_body = response.read().decode("utf-8")
    return _extract_live_quotes(json.loads(raw_body))


class Command(BaseCommand):
    help = (
        "Poll TrustCapital MT5 live quotes and notify all users when a symbol "
        "crosses each percentage-change threshold."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default=DEFAULT_PRICE_URL,
            help=f"Price API URL. Default: {DEFAULT_PRICE_URL}",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=30.0,
            help="Seconds between polls. Default: 30.",
        )
        parser.add_argument(
            "--threshold",
            default=str(DEFAULT_THRESHOLD_PERCENT),
            help="Percentage step that triggers notifications. Default: 0.5.",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=15.0,
            help="HTTP timeout in seconds. Default: 15.",
        )
        parser.add_argument(
            "--symbols",
            default="",
            help="Optional comma-separated normalized symbols to watch, e.g. EURUSD,XAUUSD.",
        )
        parser.add_argument(
            "--reset-threshold",
            default="",
            help=(
                "Absolute percentage below which a symbol resets and can notify the same "
                "direction/level again. Default: 60%% of --threshold."
            ),
        )
        parser.add_argument(
            "--state-ttl",
            type=int,
            default=DEFAULT_STATE_TTL_SECONDS,
            help="Seconds to keep inactive symbol state in cache. Default: 604800.",
        )
        parser.add_argument(
            "--lock-ttl",
            type=int,
            default=DEFAULT_LOCK_TTL_SECONDS,
            help="Seconds to hold the per-symbol cache lock. Default: 60.",
        )
        parser.add_argument(
            "--max-notification-batches",
            type=int,
            default=DEFAULT_MAX_NOTIFICATION_BATCHES_PER_POLL,
            help="Maximum symbol/level notification batches per poll. Default: 200.",
        )
        parser.add_argument(
            "--no-daily-reset",
            action="store_true",
            help="Do not reset milestone state when the local trading date changes.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one poll and exit.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print per-symbol threshold state.",
        )

    def handle(self, *args, **options):
        self.url = str(options["url"]).strip()
        self.interval = max(1.0, float(options["interval"] or 30.0))
        self.timeout = max(1.0, float(options["timeout"] or 15.0))
        self.run_once = bool(options["once"])
        self.verbose = bool(options["verbose"])
        self.threshold = _parse_decimal(options.get("threshold")) or DEFAULT_THRESHOLD_PERCENT
        if self.threshold <= 0:
            self.stderr.write(self.style.ERROR("--threshold must be greater than 0."))
            return
        self.reset_threshold = _parse_decimal(options.get("reset_threshold"))
        if self.reset_threshold is None:
            self.reset_threshold = self.threshold * Decimal("0.6")
        if self.reset_threshold < 0:
            self.stderr.write(self.style.ERROR("--reset-threshold cannot be negative."))
            return
        if self.reset_threshold >= self.threshold:
            self.stderr.write(self.style.ERROR("--reset-threshold must be less than --threshold."))
            return
        self.state_ttl = max(60, int(options.get("state_ttl") or DEFAULT_STATE_TTL_SECONDS))
        self.lock_ttl = max(1, int(options.get("lock_ttl") or DEFAULT_LOCK_TTL_SECONDS))
        self.max_notification_batches = max(
            1,
            int(options.get("max_notification_batches") or DEFAULT_MAX_NOTIFICATION_BATCHES_PER_POLL),
        )
        self.reset_daily = not bool(options.get("no_daily_reset"))

        self.symbol_filter = {
            _normalize_symbol(symbol)
            for symbol in str(options.get("symbols") or "").split(",")
            if _normalize_symbol(symbol)
        }
        self._fallback_state_by_symbol = {}
        self._fallback_locks = set()

        self.stdout.write(
            self.style.SUCCESS(
                "Polling %s every %ss; threshold=%s%%; reset=%s%%."
                % (
                    self.url,
                    self.interval,
                    _format_percent(self.threshold),
                    _format_percent(self.reset_threshold),
                )
            )
        )

        while True:
            try:
                self._run_poll()
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("Stopping signal change polling."))
                break
            except Exception as exc:
                logger.exception("Signal change polling failed: %s", exc)
                self.stderr.write(self.style.WARNING("Poll failed: %s" % exc))

            if self.run_once:
                break
            time.sleep(self.interval)

    def _run_poll(self):
        try:
            live_quotes = _fetch_live_quotes(self.url, self.timeout)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("Could not fetch live quotes: %s" % exc) from exc

        quotes_by_symbol = {}
        for raw_symbol, quote in live_quotes.items():
            symbol = _normalize_symbol(raw_symbol)
            if not symbol:
                continue
            if self.symbol_filter and symbol not in self.symbol_filter:
                continue
            if not isinstance(quote, dict):
                continue
            quotes_by_symbol[symbol] = quote

        if not quotes_by_symbol:
            self.stdout.write("No matching live quotes found.")
            return

        signals_by_symbol = self._load_open_signals_by_symbol(quotes_by_symbol.keys())
        if not signals_by_symbol:
            self.stdout.write("No open signals matched fetched quote symbols.")
            return

        notification_count = 0
        self._notification_batches_this_poll = 0
        for symbol, quote in quotes_by_symbol.items():
            parsed_quote = self._parse_quote_change(symbol, quote)
            if parsed_quote is None:
                continue
            direction, signed_change_percentage = parsed_quote

            signal_ids = [signal.id for signal in signals_by_symbol.get(symbol, [])]

            notification_count += self._maybe_notify_symbol(
                symbol,
                direction,
                signed_change_percentage,
                quote,
                signal_ids,
            )

        self.stdout.write(
            "Poll complete: %d matching symbol(s), %d notification batch(es) sent."
            % (len(signals_by_symbol), notification_count)
        )

    def _load_open_signals_by_symbol(self, symbols):
        from Signals.models import TradingSignal

        normalized_symbols = set(symbols)
        signals = (
            TradingSignal.active.filter(status=TradingSignal.Status.OPEN)
            .select_related("analyst", "instrument")
            .only("id", "analyst_id", "instrument_id", "instrument__symbol", "status")
        )

        result = {}
        for signal in signals:
            instrument = getattr(signal, "instrument", None)
            symbol = _normalize_symbol(getattr(instrument, "symbol", ""))
            if symbol in normalized_symbols:
                result.setdefault(symbol, []).append(signal)
        return result

    def _parse_quote_change(self, symbol, quote):
        change_percentage = _parse_decimal(quote.get("change_percentage"))
        if change_percentage is None:
            logger.warning("Skipping %s: invalid change_percentage=%r.", symbol, quote.get("change_percentage"))
            return None

        provider_direction = str(quote.get("dir") or "").strip().lower()
        if provider_direction not in {"up", "down", ""}:
            logger.warning("Ignoring invalid direction for %s: dir=%r.", symbol, quote.get("dir"))
            provider_direction = ""

        if change_percentage > 0:
            direction = "up"
        elif change_percentage < 0:
            direction = "down"
        elif provider_direction in {"up", "down"}:
            direction = provider_direction
        else:
            return None

        if provider_direction in {"up", "down"} and provider_direction != direction and change_percentage != 0:
            logger.warning(
                "Provider direction mismatch for %s: change_percentage=%s, dir=%s. Using signed percentage.",
                symbol,
                change_percentage,
                provider_direction,
            )

        signed_change_percentage = abs(change_percentage)
        if direction == "down":
            signed_change_percentage = -signed_change_percentage
        return direction, signed_change_percentage

    def _state_cache_key(self, symbol):
        return "%s:%s" % (STATE_CACHE_KEY_PREFIX, symbol)

    def _lock_cache_key(self, symbol):
        return "%s:%s" % (LOCK_CACHE_KEY_PREFIX, symbol)

    def _current_state_day(self):
        if not self.reset_daily:
            return ""
        return timezone.localdate().isoformat()

    def _get_symbol_state(self, symbol):
        try:
            state = cache.get(self._state_cache_key(symbol))
        except Exception:
            logger.exception("Signal change state cache read failed for %s; using in-process fallback.", symbol)
            state = self._fallback_state_by_symbol.get(symbol)
        if not isinstance(state, dict):
            return None
        if self.reset_daily and state.get("trading_day") != self._current_state_day():
            return None
        return state

    def _set_symbol_state(self, symbol, direction, step_count, notified_levels=None):
        state = {
            "direction": direction,
            "step_count": int(step_count),
            "trading_day": self._current_state_day(),
            "threshold": str(self.threshold),
            "notified_levels": notified_levels or {},
        }
        try:
            cache.set(self._state_cache_key(symbol), state, timeout=self.state_ttl)
        except Exception:
            logger.exception("Signal change state cache write failed for %s; using in-process fallback.", symbol)
            self._fallback_state_by_symbol[symbol] = state

    def _clear_symbol_state(self, symbol):
        try:
            cache.delete(self._state_cache_key(symbol))
        except Exception:
            logger.exception("Signal change state cache delete failed for %s; using in-process fallback.", symbol)
        self._fallback_state_by_symbol.pop(symbol, None)

    def _acquire_symbol_lock(self, symbol):
        token = uuid.uuid4().hex
        try:
            if cache.add(self._lock_cache_key(symbol), token, timeout=self.lock_ttl):
                return token
            return None
        except Exception:
            logger.exception("Signal change lock cache add failed for %s; using in-process fallback.", symbol)
            if symbol in self._fallback_locks:
                return None
            self._fallback_locks.add(symbol)
            return token

    def _release_symbol_lock(self, symbol, token):
        try:
            if cache.get(self._lock_cache_key(symbol)) == token:
                cache.delete(self._lock_cache_key(symbol))
        except Exception:
            logger.exception("Signal change lock cache release failed for %s; using in-process fallback.", symbol)
        self._fallback_locks.discard(symbol)

    def _crossed_step_counts(self, previous_step_count, current_step_count):
        start = max(1, int(previous_step_count) + 1)
        return range(start, int(current_step_count) + 1)

    def _get_notified_levels(self, previous):
        notified_levels = previous.get("notified_levels") if previous else None
        if isinstance(notified_levels, dict):
            return {
                "up": set(str(level) for level in notified_levels.get("up", [])),
                "down": set(str(level) for level in notified_levels.get("down", [])),
            }

        previous_direction = previous.get("direction") if previous else None
        previous_step_count = int(previous.get("step_count", 0)) if previous else 0
        if previous_direction not in {"up", "down"} or previous_step_count <= 0:
            return {"up": set(), "down": set()}

        return {
            "up": set(),
            "down": set(),
            previous_direction: {
                _level_key(self.threshold * Decimal(step_count))
                for step_count in range(1, previous_step_count + 1)
            },
        }

    def _serialize_notified_levels(self, notified_levels):
        return {
            "up": sorted(notified_levels.get("up", set())),
            "down": sorted(notified_levels.get("down", set())),
        }

    def _maybe_notify_symbol(self, symbol, direction, change_percentage, quote, signal_ids):
        lock_token = self._acquire_symbol_lock(symbol)
        if not lock_token:
            if self.verbose:
                self.stdout.write("Skip %s: another worker is processing this symbol." % symbol)
            return 0

        try:
            return self._maybe_notify_symbol_locked(symbol, direction, change_percentage, quote, signal_ids)
        finally:
            self._release_symbol_lock(symbol, lock_token)

    def _maybe_notify_symbol_locked(self, symbol, direction, change_percentage, quote, signal_ids):
        previous = self._get_symbol_state(symbol)
        abs_percentage = abs(change_percentage)

        if abs_percentage <= self.reset_threshold:
            if previous:
                self._clear_symbol_state(symbol)
                if self.verbose:
                    self.stdout.write(
                        "Reset %s: change fell to %s%%."
                        % (symbol, _format_percent(abs_percentage))
                    )
            return 0

        step_count = int((abs_percentage / self.threshold).to_integral_value(rounding=ROUND_FLOOR))
        if step_count <= 0:
            return 0

        previous_direction = previous.get("direction") if previous else None
        previous_step_count = int(previous.get("step_count", 0)) if previous else 0
        previous_threshold = str(previous.get("threshold") or "") if previous else ""
        same_threshold = previous_threshold == str(self.threshold)
        if previous_direction != direction or not same_threshold:
            previous_step_count = 0
        notified_levels = self._get_notified_levels(previous)

        if step_count <= previous_step_count:
            if self.verbose:
                self.stdout.write(
                    "Skip %s: already notified %s %s%%."
                    % (
                        symbol,
                        direction,
                        _format_percent(self.threshold * Decimal(previous_step_count)),
                    )
                )
            return 0

        sent_count = 0
        highest_notified_step_count = previous_step_count
        for crossed_step_count in self._crossed_step_counts(previous_step_count, step_count):
            if self._notification_batches_this_poll >= self.max_notification_batches:
                logger.warning(
                    "Signal change notification batch limit reached (%s). Last processed symbol=%s.",
                    self.max_notification_batches,
                    symbol,
                )
                break
            crossed_percentage = self.threshold * Decimal(crossed_step_count)
            crossed_level_key = _level_key(crossed_percentage)
            if crossed_level_key in notified_levels.get(direction, set()):
                highest_notified_step_count = crossed_step_count
                continue
            signed_crossed_percentage = crossed_percentage if direction == "up" else -crossed_percentage
            if self._notify_all_users(
                symbol=symbol,
                direction=direction,
                signed_crossed_percentage=signed_crossed_percentage,
                crossed_percentage=crossed_percentage,
                current_percentage=change_percentage,
                quote=quote,
                signal_ids=signal_ids,
            ):
                sent_count += 1
                self._notification_batches_this_poll += 1
                highest_notified_step_count = crossed_step_count
                notified_levels.setdefault(direction, set()).add(crossed_level_key)

        state_step_count = highest_notified_step_count if sent_count else step_count
        self._set_symbol_state(
            symbol,
            direction,
            state_step_count,
            self._serialize_notified_levels(notified_levels),
        )
        return sent_count

    def _notify_all_users(
        self,
        *,
        symbol,
        direction,
        signed_crossed_percentage,
        crossed_percentage,
        current_percentage,
        quote,
        signal_ids,
    ):
        from django.contrib.auth import get_user_model
        from Mainapp.models import UserNotification

        User = get_user_model()
        users = list(User.objects.filter(is_active=True).distinct())
        if not users:
            if self.verbose:
                self.stdout.write("No active users found for %s movement notification." % symbol)
            return False

        crossed_label = _format_percent(crossed_percentage)
        signed_crossed_label = _format_percent(signed_crossed_percentage)
        current_label = _format_percent(current_percentage)
        title = "%s %s %s%%" % (symbol, direction, crossed_label)
        message = (
            "%s passed %s%% (%s). Current change: %s%%."
            % (symbol, signed_crossed_label, direction, current_label)
        )
        notification_type = "SUCCESS" if direction == "up" else "WARNING"

        notifications = []
        for user in users:
            notifications.append(
                UserNotification(
                    user=user,
                    title=title,
                    message=message,
                    notification_type=notification_type,
                    category="TRADING_SIGNAL",
                )
            )
        UserNotification.objects.bulk_create(notifications, batch_size=1000)

        try:
            from Dashboard.realtime import broadcast_notifications

            broadcast_notifications(
                UserNotification.objects.filter(id__in=[notification.id for notification in notifications]),
                event_name="created",
            )
        except Exception:
            logger.exception("Failed to broadcast signal change notifications.")

        data = {
            "type": "signal_change_threshold",
            "symbol": symbol,
            "direction": direction,
            "crossed_percentage": str(crossed_percentage),
            "signed_crossed_percentage": str(signed_crossed_percentage),
            "current_percentage": str(current_percentage),
            "signal_ids": ",".join(str(signal_id) for signal_id in signal_ids),
            "bid": str(quote.get("bid") or ""),
            "ask": str(quote.get("ask") or ""),
        }
        try:
            from firebase import send_push_to_users

            send_push_to_users(
                users=users,
                title=title,
                body=message,
                data=data,
            )
        except Exception:
            logger.exception("Failed to send signal change push notifications.")

        self.stdout.write(
            self.style.SUCCESS(
                "Notified %d user(s): %s."
                % (len(users), title)
            )
        )
        return True

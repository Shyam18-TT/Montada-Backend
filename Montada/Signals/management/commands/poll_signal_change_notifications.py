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
from django.utils.dateparse import parse_datetime

from Signals.management.commands.run_price_alerts import _describe_push_result
from Signals.price_move_config import (
    DEFAULT_COOLDOWN_SECONDS as DEFAULT_NOTIFICATION_COOLDOWN_SECONDS,
    DEFAULT_MAX_NOTIFICATION_BATCHES as DEFAULT_MAX_NOTIFICATION_BATCHES_PER_POLL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_THRESHOLD_PERCENT,
    EXCLUDED_CRYPTO_SYMBOLS as _EXCLUDED_CRYPTO_SYMBOLS,
    EXCLUDED_FOREX_MINOR_SYMBOLS as _EXCLUDED_FOREX_MINOR_SYMBOLS,
    EXCLUDED_FX2_SYMBOLS as _EXCLUDED_FX2_SYMBOLS,
    MAX_NOTIFICATION_BATCHES,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    PriceMoveConfigProvider,
    build_fallback_config,
    format_percent as _format_percent,
    futures_base_symbol as _futures_base_symbol,
    normalize_symbol as _normalize_symbol,
    symbol_alias_candidates as _symbol_alias_candidates,
)


logger = logging.getLogger(__name__)

# Thresholds, steps, cooldowns and muted symbols come from the admin tables
# (PriceMoveNotificationSettings / PriceMoveNotificationRule, see Signals.price_move_config).
# The CLI values below are only the fallback used when those tables cannot be read.

# Seeded muted symbols and their broker variants (AUDNOK, AUDNOK.p, AUDNOK., AUDNOKp);
# the rule resolver matches the same variants.
_FOREX_MINOR_SUFFIXES = ("", ".p", ".", "p")
_EXCLUDED_STOCK_SYMBOLS = frozenset(
    {symbol.upper() for symbol in _EXCLUDED_FX2_SYMBOLS}
    | {
        (symbol + suffix).upper()
        for symbol in _EXCLUDED_FOREX_MINOR_SYMBOLS
        for suffix in _FOREX_MINOR_SUFFIXES
    }
    | {symbol.upper() for symbol in _EXCLUDED_CRYPTO_SYMBOLS}
)

DEFAULT_PRICE_URL = "https://trustcapital.com/api/get-MT5-price"
DEFAULT_STATE_TTL_SECONDS = 60 * 60 * 24 * 7
DEFAULT_LOCK_TTL_SECONDS = 60
# Market snapshot prices older than this fall back to the polled quote's bid.
MARKET_SNAPSHOT_MAX_AGE_SECONDS = 300
# Max signal ids sent in the FCM data payload (keeps the payload well under FCM's 4KB limit).
MAX_PUSH_SIGNAL_IDS = 20
# While waiting for the next poll, check this often whether the admin changed the config.
CONFIG_CHECK_SECONDS = 5
STATE_CACHE_KEY_PREFIX = "signals:change-notifications:state"
LOCK_CACHE_KEY_PREFIX = "signals:change-notifications:lock"
# Set once a poll has built symbol state. Missing means Redis was flushed (or this is the
# first run of this code): that poll records state silently instead of notifying every
# symbol already past a level.
BASELINE_DONE_CACHE_KEY = "signals:change-notifications:baseline-done"
# _get_symbol_state result when the state cannot be read at all (Redis down and nothing in
# the in-process fallback). Recorded silently, like a flush.
_UNKNOWN_STATE = object()


def _drop_duplicate_futures(symbols):
    """
    Symbols to skip because they duplicate another feed symbol: a futures contract whose
    spot symbol is also in the feed, or extra spellings of the same contract
    (GER40.Z26 and GER40_Z26). Futures-only markets (e.g. COCOA_Z26) keep one contract.
    """
    symbols = set(symbols)
    skipped = set()
    kept_base = set()
    for symbol in sorted(symbols):
        base = _futures_base_symbol(symbol)
        if base is None:
            continue
        if base in symbols or base in kept_base:
            skipped.add(symbol)
        else:
            kept_base.add(base)
    return skipped


def _parse_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace("+", ""))
    except (InvalidOperation, ValueError):
        return None


def _step_count_for(abs_percentage, start, step):
    """Number of levels crossed, where levels are start, start+step, start+2*step, ..."""
    if abs_percentage < start:
        return 0
    return int(((abs_percentage - start) / step).to_integral_value(rounding=ROUND_FLOOR)) + 1


def _step_percentage(step_count, start, step):
    return start + step * Decimal(int(step_count) - 1)


def _level_key(value):
    return _format_percent(abs(value))


def _format_price(value, digits=None):
    price = _parse_decimal(value)
    if price is None:
        return ""
    try:
        if digits is not None:
            return format(price, ".%df" % max(0, int(digits)))
    except (TypeError, ValueError):
        pass
    return format(price.normalize(), "f")


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
        "crosses each percentage-change threshold. Thresholds, poll interval and muted "
        "symbols/asset classes are managed by admins (PriceMoveNotificationSettings/Rule)."
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
            default=None,
            help="Fallback seconds between polls, used only if the admin settings cannot be read. Default: 30.",
        )
        parser.add_argument(
            "--threshold",
            default=None,
            help="Fallback percentage step, used only if the admin settings cannot be read. Default: 0.5.",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=15.0,
            help="HTTP timeout in seconds. Default: 15.",
        )
        parser.add_argument(
            "--exclude-symbols",
            default="",
            help="Optional comma-separated symbols to drop entirely (on top of admin mute rules).",
        )
        parser.add_argument(
            "--no-hardcoded-exclusions",
            action="store_true",
            help="Deprecated, ignored: exclusions are admin mute rules now.",
        )
        parser.add_argument(
            "--reset-threshold",
            default="",
            help="Fallback reset percentage, used only if the admin settings cannot be read. Default: 60%% of --threshold.",
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
            default=None,
            help="Fallback max notification batches per poll, used only if the admin settings cannot be read. Default: 200.",
        )
        parser.add_argument(
            "--max-users-per-notification",
            type=int,
            default=0,
            help="Maximum users to notify per event (0=all). Default: 0 (notify all).",
        )
        parser.add_argument(
            "--notification-cooldown-seconds",
            type=int,
            default=None,
            help="Fallback cooldown, used only if the admin settings cannot be read. Default: 300.",
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
        parser.add_argument(
            "--debug-symbols",
            action="store_true",
            help="Print all open signal symbols from database (for debugging).",
        )

    def handle(self, *args, **options):
        self.url = str(options["url"]).strip()
        self.timeout = max(1.0, float(options["timeout"] or 15.0))
        self.run_once = bool(options["once"])
        self.verbose = bool(options["verbose"])
        self.debug_symbols = bool(options.get("debug_symbols"))

        fallback_threshold = _parse_decimal(options.get("threshold")) or DEFAULT_THRESHOLD_PERCENT
        if fallback_threshold <= 0:
            self.stderr.write(self.style.ERROR("--threshold must be greater than 0."))
            return
        fallback_reset = _parse_decimal(options.get("reset_threshold"))
        if fallback_reset is None:
            fallback_reset = fallback_threshold * Decimal("0.6")
        if fallback_reset < 0:
            self.stderr.write(self.style.ERROR("--reset-threshold cannot be negative."))
            return
        if fallback_reset >= fallback_threshold:
            self.stderr.write(self.style.ERROR("--reset-threshold must be less than --threshold."))
            return
        fallback_interval = options.get("interval") or DEFAULT_POLL_INTERVAL_SECONDS
        self._fallback_config_kwargs = {
            "threshold": fallback_threshold,
            "reset": fallback_reset,
            "cooldown_seconds": max(
                0, int(options.get("notification_cooldown_seconds") or DEFAULT_NOTIFICATION_COOLDOWN_SECONDS)
            ),
            "poll_interval_seconds": int(
                min(MAX_POLL_INTERVAL_SECONDS, max(MIN_POLL_INTERVAL_SECONDS, fallback_interval))
            ),
            "max_notification_batches": int(
                min(
                    MAX_NOTIFICATION_BATCHES,
                    max(1, options.get("max_notification_batches") or DEFAULT_MAX_NOTIFICATION_BATCHES_PER_POLL),
                )
            ),
        }
        self._config_provider = PriceMoveConfigProvider(
            lambda: build_fallback_config(**self._fallback_config_kwargs)
        )

        tuning_flags = [
            flag for flag, key in (
                ("--interval", "interval"),
                ("--threshold", "threshold"),
                ("--reset-threshold", "reset_threshold"),
                ("--notification-cooldown-seconds", "notification_cooldown_seconds"),
                ("--max-notification-batches", "max_notification_batches"),
            )
            if options.get(key) not in (None, "")
        ]
        if tuning_flags:
            self.stdout.write(
                self.style.WARNING(
                    "%s now only apply if the admin settings cannot be read; edit them in the admin panel."
                    % ", ".join(tuning_flags)
                )
            )
        if options.get("no_hardcoded_exclusions"):
            self.stdout.write(
                self.style.WARNING("--no-hardcoded-exclusions is ignored: exclusions are admin mute rules now.")
            )

        self.state_ttl = max(60, int(options.get("state_ttl") or DEFAULT_STATE_TTL_SECONDS))
        self.lock_ttl = max(1, int(options.get("lock_ttl") or DEFAULT_LOCK_TTL_SECONDS))
        self.max_users_per_notification = max(0, int(options.get("max_users_per_notification") or 0))
        self.reset_daily = not bool(options.get("no_daily_reset"))

        self.symbol_filter = {
            _normalize_symbol(symbol)
            for symbol in str(options.get("symbols") or "").split(",")
            if _normalize_symbol(symbol)
        }
        self.excluded_symbols = {
            _normalize_symbol(symbol)
            for symbol in str(options.get("exclude_symbols") or "").split(",")
            if _normalize_symbol(symbol)
        }

        self._fallback_state_by_symbol = {}
        self._fallback_locks = set()
        self._fallback_state_last_cleanup = 0  # For periodic memory cleanup
        self._active_config = None

        config = self._get_config()
        self.stdout.write(
            self.style.SUCCESS(
                "Polling %s; config from %s: interval=%ss, default-threshold=%s%%, reset=%s%%, "
                "%d symbol rule(s), %d asset-class rule(s)."
                % (
                    self.url,
                    config.source,
                    config.poll_interval_seconds,
                    _format_percent(config.threshold),
                    _format_percent(config.reset),
                    len(config.symbol_rules),
                    len(config.asset_class_rules),
                )
            )
        )

        if self.max_users_per_notification > 0:
            self.stdout.write(
                self.style.WARNING(
                    "User limit: %d users per notification (to prevent spam)." % self.max_users_per_notification
                )
            )

        if self.excluded_symbols:
            self.stdout.write(
                self.style.WARNING("CLI-excluded symbols: %s." % ", ".join(sorted(self.excluded_symbols)))
            )

        if self.debug_symbols:
            self._print_open_signals_by_symbol()

        while True:
            poll_started_at = time.monotonic()
            try:
                self._cleanup_fallback_state()
                self._run_poll()
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("Stopping signal change polling."))
                break
            except Exception as exc:
                logger.exception("Signal change polling failed: %s", exc)
                self.stderr.write(self.style.WARNING("Poll failed: %s" % exc))

            if self.run_once:
                break
            try:
                self._wait_for_next_poll(poll_started_at)
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("Stopping signal change polling."))
                break

    def _get_config(self):
        """Admin config from the in-memory copy; reloads from the DB only after a change."""
        config = self._config_provider.get()
        active = getattr(self, "_active_config", None)
        if config is not active:
            if active is not None:
                self.stdout.write(
                    self.style.WARNING(
                        "Price move config reloaded from %s (interval=%ss, default-threshold=%s%%, "
                        "enabled=%s). Symbols whose levels changed are re-baselined silently."
                        % (
                            config.source,
                            config.poll_interval_seconds,
                            _format_percent(config.threshold),
                            config.enabled,
                        )
                    )
                )
            self._active_config = config
        return config

    def _wait_for_next_poll(self, poll_started_at):
        """
        Sleep until the configured interval has passed since the poll started. The interval
        is re-read every CONFIG_CHECK_SECONDS so an admin change (e.g. 3600s -> 30s) applies
        without waiting out the old interval.
        """
        while True:
            interval = self._get_config().poll_interval_seconds
            remaining = poll_started_at + interval - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, CONFIG_CHECK_SECONDS))

    def _needs_baseline_poll(self):
        try:
            return not cache.get(BASELINE_DONE_CACHE_KEY)
        except Exception:
            # Redis is down: per-symbol unknown state is handled in _get_symbol_state.
            return False

    def _mark_baseline_done(self):
        try:
            cache.set(BASELINE_DONE_CACHE_KEY, True, timeout=None)
        except Exception:
            logger.exception("Could not record the signal change baseline marker.")

    def _run_poll(self):
        self._config = self._get_config()
        try:
            live_quotes = _fetch_live_quotes(self.url, self.timeout)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("Could not fetch live quotes: %s" % exc) from exc

        quotes_by_symbol = {}
        excluded_symbols = set()
        filtered_symbols = set()

        for raw_symbol, quote in live_quotes.items():
            symbol = _normalize_symbol(raw_symbol)
            if not symbol:
                continue
            if symbol in self.excluded_symbols:
                excluded_symbols.add(symbol)
                continue
            if self.symbol_filter and symbol not in self.symbol_filter:
                filtered_symbols.add(symbol)
                continue
            if not isinstance(quote, dict):
                continue
            quotes_by_symbol[symbol] = quote

        duplicate_futures = _drop_duplicate_futures(quotes_by_symbol)
        for symbol in duplicate_futures:
            quotes_by_symbol.pop(symbol, None)

        if self.verbose:
            if duplicate_futures:
                self.stdout.write("Skipped duplicate futures: %s" % ", ".join(sorted(duplicate_futures)))
            if excluded_symbols:
                self.stdout.write("Excluded symbols: %s" % ", ".join(sorted(excluded_symbols)))
            if filtered_symbols:
                self.stdout.write("Filtered symbols (not in watchlist): %s" % ", ".join(sorted(filtered_symbols)))
            self.stdout.write("Processing symbols: %s" % ", ".join(sorted(quotes_by_symbol.keys())))

        if not quotes_by_symbol:
            self.stdout.write("No matching live quotes found.")
            return

        self._market_prices = self._load_market_prices()

        signals_by_symbol = self._load_open_signals_by_symbol(quotes_by_symbol.keys())
        if not signals_by_symbol:
            # No open signal matched any fetched quote symbol (e.g. all quotes are for
            # symbols nobody currently has an open signal on). Threshold notifications
            # are not gated on having a matching signal, so keep polling instead of
            # aborting the whole cycle.
            self.stdout.write(
                "WARNING: No open signals matched %d fetched quote symbols." % len(quotes_by_symbol)
            )

        self._baseline_poll = self._needs_baseline_poll()
        if self._baseline_poll:
            self.stdout.write(
                self.style.WARNING(
                    "No baseline marker in cache (first run or cache flushed): recording current "
                    "levels without notifying."
                )
            )
        self._silent_counts = {}
        notification_count = 0
        self._notification_batches_this_poll = 0
        for symbol, quote in quotes_by_symbol.items():
            parsed_quote = self._parse_quote_change(symbol, quote)
            if parsed_quote is None:
                continue
            direction, signed_change_percentage = parsed_quote

            signals = signals_by_symbol.get(symbol, [])

            notification_count += self._maybe_notify_symbol(
                symbol,
                direction,
                signed_change_percentage,
                quote,
                signals,
            )

        if self._baseline_poll:
            self._mark_baseline_done()

        silent_summary = ""
        if self._silent_counts:
            silent_summary = " Silent: %s." % ", ".join(
                "%s=%d" % (reason, count) for reason, count in sorted(self._silent_counts.items())
            )
        self.stdout.write(
            "Poll complete: %d matching symbol(s), %d notification batch(es) sent.%s"
            % (len(signals_by_symbol), notification_count, silent_summary)
        )

    def _load_market_prices(self):
        """Latest ticks written by run_market_data_stream, keyed by normalized symbol."""
        try:
            from Signals.market_stream import load_market_snapshot

            ticks = load_market_snapshot()
        except Exception:
            logger.exception("Could not load market snapshot; using polled quote prices.")
            return {}

        now = timezone.now()
        prices = {}
        for tick in ticks:
            if not isinstance(tick, dict) or tick.get("bid") is None:
                continue
            symbol = _normalize_symbol(tick.get("symbol"))
            if not symbol:
                continue
            try:
                received_at = parse_datetime(str(tick.get("received_at") or ""))
            except ValueError:
                received_at = None
            if received_at is None or timezone.is_naive(received_at):
                continue
            if (now - received_at).total_seconds() > MARKET_SNAPSHOT_MAX_AGE_SECONDS:
                continue
            prices[symbol] = tick
        return prices

    def _get_current_price(self, symbol, quote):
        """
        Price shown in the notification. It comes from the same quote as change_percentage,
        so "down 7.37%, trading at X" is one consistent snapshot. The MT5 stream snapshot is
        used only for the symbol's decimal digits, or as the price if the quote has no bid.
        """
        market_prices = getattr(self, "_market_prices", None) or {}
        candidates = [symbol] + sorted(_symbol_alias_candidates(symbol) - {symbol})
        tick = next((market_prices[c] for c in candidates if market_prices.get(c)), None)
        digits = tick.get("digits") if tick else None

        price = _format_price(quote.get("bid"), digits)
        if price:
            return price
        if tick:
            return _format_price(tick.get("bid"), digits)
        return ""

    def _print_open_signals_by_symbol(self):
        """Print all open signals grouped by normalized symbol for debugging."""
        from Signals.models import TradingSignal
        
        signals = (
            TradingSignal.active.filter(status=TradingSignal.Status.OPEN)
            .select_related("analyst", "instrument__asset_class")
            .only("id", "analyst_id", "instrument_id", "instrument__symbol", "instrument__asset_class", "status")
        )
        
        signals_by_symbol = {}
        for signal in signals:
            instrument = getattr(signal, "instrument", None)
            raw_symbol = getattr(instrument, "symbol", "")
            normalized = _normalize_symbol(raw_symbol)
            if normalized:
                signals_by_symbol.setdefault(normalized, {
                    "raw_symbol": raw_symbol,
                    "count": 0
                })
                signals_by_symbol[normalized]["count"] += 1
        
        self.stdout.write("\n" + self.style.SUCCESS("=== OPEN SIGNALS BY NORMALIZED SYMBOL ==="))
        if not signals_by_symbol:
            self.stdout.write("No open signals found!")
        else:
            for norm_sym in sorted(signals_by_symbol.keys()):
                info = signals_by_symbol[norm_sym]
                self.stdout.write(
                    "  %s (raw: %s) -> %d signal(s)"
                    % (norm_sym, info["raw_symbol"], info["count"])
                )
        self.stdout.write("=== END SYMBOLS ===\n")

    def _load_open_signals_by_symbol(self, symbols):
        from Signals.models import TradingSignal

        normalized_symbols = set(symbols)
        signals = (
            TradingSignal.active.filter(status=TradingSignal.Status.OPEN)
            .select_related("analyst", "instrument__asset_class")
            .only("id", "analyst_id", "instrument_id", "instrument__symbol", "instrument__asset_class", "status")
        )

        result = {}
        unmatched_symbols = {}
        for signal in signals:
            instrument = getattr(signal, "instrument", None)
            symbol = _normalize_symbol(getattr(instrument, "symbol", ""))
            matched_symbol = symbol if symbol in normalized_symbols else next(
                (
                    candidate
                    for candidate in _symbol_alias_candidates(symbol)
                    if candidate in normalized_symbols
                ),
                None,
            )
            if matched_symbol:
                result.setdefault(matched_symbol, []).append(signal)
            else:
                # Track symbols with open signals that weren't in the quote response
                unmatched_symbols.setdefault(symbol, []).append(signal)
        
        if unmatched_symbols and self.verbose:
            self.stdout.write(
                "WARNING: %d open signal(s) for symbols not in live quotes: %s"
                % (sum(len(v) for v in unmatched_symbols.values()), ", ".join(unmatched_symbols.keys()))
            )
        
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

    def _cleanup_fallback_state(self):
        """
        Periodically drop in-process fallback state from previous trading days. Today's
        entries are kept: wiping them would make every symbol notify its levels again while
        Redis is down.
        """
        current_time = time.time()
        if current_time - self._fallback_state_last_cleanup < 300:  # Cleanup every 5 minutes
            return

        self._fallback_state_last_cleanup = current_time
        today = self._current_state_day()
        old_count = len(self._fallback_state_by_symbol)
        self._fallback_state_by_symbol = {
            symbol: state
            for symbol, state in self._fallback_state_by_symbol.items()
            if isinstance(state, dict) and state.get("trading_day") == today
        }
        removed = old_count - len(self._fallback_state_by_symbol)
        if self.verbose and removed > 0:
            self.stdout.write("Cleaned up %d old fallback state entries." % removed)

    def _get_symbol_state(self, symbol):
        """
        Today's state for `symbol`: from Redis, or from the in-process fallback written while
        Redis was failing (whichever is newer, so levels notified during an outage are not
        sent again once Redis is back). Returns None when there is no state and
        _UNKNOWN_STATE when Redis cannot be read and the fallback has nothing either.
        """
        fallback = self._fallback_state_by_symbol.get(symbol)
        try:
            state = cache.get(self._state_cache_key(symbol))
        except Exception:
            logger.warning("Signal change state cache read failed for %s; using in-process fallback.", symbol)
            if not isinstance(fallback, dict):
                return _UNKNOWN_STATE
            state = None
        if isinstance(fallback, dict):
            state_updated_at = state.get("updated_at", 0) if isinstance(state, dict) else -1
            if fallback.get("updated_at", 0) > state_updated_at:
                state = fallback
        if not isinstance(state, dict):
            return None
        if self.reset_daily and state.get("trading_day") != self._current_state_day():
            return None
        return state

    def _set_symbol_state(
        self,
        symbol,
        direction,
        step_count,
        notified_levels=None,
        threshold=None,
        notification_history=None,
        config_sig=None,
    ):
        state = {
            "direction": direction,
            "step_count": int(step_count),
            "trading_day": self._current_state_day(),
            "threshold": str(threshold) if threshold is not None else "",
            "config_sig": config_sig,
            "notified_levels": notified_levels or {},
            "notification_history": notification_history or {},
            "updated_at": time.time(),
        }
        try:
            cache.set(self._state_cache_key(symbol), state, timeout=self.state_ttl)
        except Exception:
            logger.warning("Signal change state cache write failed for %s; using in-process fallback.", symbol)
            self._fallback_state_by_symbol[symbol] = state
        else:
            self._fallback_state_by_symbol.pop(symbol, None)

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

    def _is_level_notification_in_cooldown(self, direction, level_key, previous, current_time=None, cooldown_seconds=None):
        if cooldown_seconds is None:
            cooldown_seconds = getattr(self, "notification_cooldown_seconds", DEFAULT_NOTIFICATION_COOLDOWN_SECONDS)
        if cooldown_seconds <= 0:
            return False

        history = previous.get("notification_history") if previous else None
        if not isinstance(history, dict):
            return False

        direction_history = history.get(direction)
        if not isinstance(direction_history, dict):
            return False

        timestamp = direction_history.get(level_key)
        if not isinstance(timestamp, (int, float)):
            return False

        current_time = current_time if current_time is not None else int(time.time())
        return (current_time - timestamp) < cooldown_seconds

    def _get_notified_levels(self, previous):
        notified_levels = previous.get("notified_levels") if previous else None
        if not isinstance(notified_levels, dict):
            return {"up": set(), "down": set()}
        return {
            "up": set(str(level) for level in notified_levels.get("up", [])),
            "down": set(str(level) for level in notified_levels.get("down", [])),
        }

    def _serialize_notified_levels(self, notified_levels):
        return {
            "up": sorted(notified_levels.get("up", set())),
            "down": sorted(notified_levels.get("down", set())),
        }

    def _serialize_notification_history(self, previous):
        previous_history = previous.get("notification_history") if previous else None
        if not isinstance(previous_history, dict):
            previous_history = {}

        return {
            "up": dict(previous_history.get("up", {}) or {}),
            "down": dict(previous_history.get("down", {}) or {}),
        }

    def _maybe_notify_symbol(self, symbol, direction, change_percentage, quote, signals):
        lock_token = self._acquire_symbol_lock(symbol)
        if not lock_token:
            if self.verbose:
                self.stdout.write("Skip %s: another worker is processing this symbol." % symbol)
            return 0

        try:
            return self._maybe_notify_symbol_locked(symbol, direction, change_percentage, quote, signals)
        finally:
            self._release_symbol_lock(symbol, lock_token)

    def _resolve_rule(self, symbol, signals=None):
        """Admin settings for `symbol`; open signals add their instrument's asset class."""
        asset_class_ids = set()
        for signal in signals or ():
            instrument = getattr(signal, "instrument", None)
            asset_class_id = getattr(instrument, "asset_class_id", None)
            if asset_class_id:
                asset_class_ids.add(asset_class_id)
        config = getattr(self, "_config", None) or self._get_config()
        rule = config.resolve(symbol, asset_class_ids)
        if rule.warnings and self.verbose:
            self.stdout.write("Config warning for %s: %s" % (symbol, " ".join(rule.warnings)))
        return rule

    def _silent_reason(self, rule, previous, state_unknown):
        """
        Why this symbol's levels should be recorded without notifying, or None to notify
        normally. Every case would otherwise push levels the price crossed earlier:
        - muted: tracked silently, so unmuting later starts from the current level.
        - baseline: cache was flushed / first run, every symbol has lost its state.
        - state unavailable: Redis is down and this process has no state for the symbol.
        - config changed: admin changed this symbol's threshold/step or (un)muted it, so
          its levels are re-based on the current % change.
        """
        if not rule.enabled:
            return "muted"
        if getattr(self, "_baseline_poll", False):
            return "baseline"
        if state_unknown:
            return "state unavailable"
        if previous is not None and previous.get("config_sig") != rule.signature:
            return "config changed"
        return None

    def _record_silent_baseline(self, symbol, direction, step_count, rule, previous, reason):
        counts = getattr(self, "_silent_counts", None)
        if counts is None:
            counts = self._silent_counts = {}
        counts[reason] = counts.get(reason, 0) + 1

        if (
            previous is not None
            and previous.get("config_sig") == rule.signature
            and previous.get("direction") == direction
            and int(previous.get("step_count", 0)) == int(step_count)
        ):
            return  # e.g. a muted symbol still at the same level: nothing to rewrite

        notified_levels = {"up": set(), "down": set()}
        notified_levels[direction] = {
            _level_key(_step_percentage(step, rule.threshold, rule.step))
            for step in range(1, int(step_count) + 1)
        }
        self._set_symbol_state(
            symbol,
            direction,
            step_count,
            self._serialize_notified_levels(notified_levels),
            threshold=rule.threshold,
            notification_history=self._serialize_notification_history(previous),
            config_sig=rule.signature,
        )
        if self.verbose:
            self.stdout.write(
                "Silent %s (%s): %s levels up to %s recorded; next notification at %s%%."
                % (
                    symbol,
                    reason,
                    direction,
                    _format_percent(_step_percentage(step_count, rule.threshold, rule.step)) + "%"
                    if step_count > 0 else "none",
                    _format_percent(_step_percentage(int(step_count) + 1, rule.threshold, rule.step)),
                )
            )

    def _maybe_notify_symbol_locked(self, symbol, direction, change_percentage, quote, signals):
        rule = self._resolve_rule(symbol, signals)
        previous = self._get_symbol_state(symbol)
        state_unknown = previous is _UNKNOWN_STATE
        if state_unknown:
            previous = None
        abs_percentage = abs(change_percentage)
        effective_threshold = rule.threshold
        effective_step = rule.step

        if abs_percentage <= rule.reset:
            if previous:
                self._clear_symbol_state(symbol)
                if self.verbose:
                    self.stdout.write(
                        "Reset %s: change fell to %s%%."
                        % (symbol, _format_percent(abs_percentage))
                    )
            return 0

        step_count = _step_count_for(abs_percentage, effective_threshold, effective_step)

        silent_reason = self._silent_reason(rule, previous, state_unknown)
        if silent_reason:
            self._record_silent_baseline(symbol, direction, step_count, rule, previous, silent_reason)
            return 0

        if step_count <= 0:
            return 0

        # The state's config_sig matches, so its step counts use the same level grid.
        previous_direction = previous.get("direction") if previous else None
        previous_step_count = int(previous.get("step_count", 0)) if previous else 0
        if previous_direction != direction:
            previous_step_count = 0
        notified_levels = self._get_notified_levels(previous)

        if step_count <= previous_step_count:
            if self.verbose:
                self.stdout.write(
                    "Skip %s: already notified %s %s%%."
                    % (
                        symbol,
                        direction,
                        _format_percent(_step_percentage(previous_step_count, effective_threshold, effective_step)),
                    )
                )
            return 0

        sent_count = 0
        highest_notified_step_count = previous_step_count
        notification_history = self._serialize_notification_history(previous)
        now_ts = int(time.time())

        # Levels crossed since the last notification that have not been notified yet.
        pending_levels = []
        for crossed_step_count in self._crossed_step_counts(previous_step_count, step_count):
            crossed_percentage = _step_percentage(crossed_step_count, effective_threshold, effective_step)
            crossed_level_key = _level_key(crossed_percentage)
            if crossed_level_key in notified_levels.get(direction, set()):
                highest_notified_step_count = crossed_step_count
                continue
            if self._is_level_notification_in_cooldown(
                direction, crossed_level_key, previous, now_ts, cooldown_seconds=rule.cooldown_seconds
            ):
                if self.verbose:
                    self.stdout.write(
                        "Skip %s: %s %s%% already notified within %ss."
                        % (
                            symbol,
                            direction,
                            _format_percent(crossed_percentage),
                            rule.cooldown_seconds,
                        )
                    )
                highest_notified_step_count = crossed_step_count
                continue
            pending_levels.append((crossed_step_count, crossed_percentage, crossed_level_key))

        # A move that jumps several levels at once (e.g. -0.3% -> -7.37%) sends ONE
        # notification for the highest level reached; the lower levels are marked as covered
        # instead of each producing its own push with the same price.
        if pending_levels:
            max_batches = self._config.max_notification_batches
            if self._notification_batches_this_poll >= max_batches:
                logger.warning(
                    "Signal change notification batch limit reached (%s). Last processed symbol=%s.",
                    max_batches,
                    symbol,
                )
            else:
                top_step_count, top_percentage, _ = pending_levels[-1]
                signed_top_percentage = top_percentage if direction == "up" else -top_percentage
                if self._notify_all_users(
                    symbol=symbol,
                    direction=direction,
                    signed_crossed_percentage=signed_top_percentage,
                    crossed_percentage=top_percentage,
                    current_percentage=change_percentage,
                    quote=quote,
                    signal_ids=[signal.id for signal in signals],
                ):
                    sent_count = 1
                    self._notification_batches_this_poll += 1
                    highest_notified_step_count = top_step_count
                    for _, _, level_key in pending_levels:
                        notified_levels.setdefault(direction, set()).add(level_key)
                        notification_history.setdefault(direction, {})[level_key] = now_ts

        state_step_count = highest_notified_step_count if sent_count else step_count
        self._set_symbol_state(
            symbol,
            direction,
            state_step_count,
            self._serialize_notified_levels(notified_levels),
            threshold=effective_threshold,
            notification_history=notification_history,
            config_sig=rule.signature,
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
        from Mainapp.notifications import bulk_create_user_notifications

        User = get_user_model()
        # Every active user gets the in-app notification; push preferences are applied
        # in send_push_to_users.
        users = list(User.objects.filter(is_active=True).distinct())

        # Limit users if specified
        if self.max_users_per_notification > 0:
            users = users[:self.max_users_per_notification]
        
        if not users:
            if self.verbose:
                self.stdout.write("No active users found for %s movement notification." % symbol)
            return False

        crossed_label = _format_percent(crossed_percentage)
        current_price = self._get_current_price(symbol, quote)
        title = "%s %s %s%%" % (symbol, direction, crossed_label)
        # e.g. "BCHUSD is down 7.37%, trading at 308.615."
        message = "%s is %s %s%%" % (symbol, direction, _format_percent(abs(current_percentage)))
        message += ", trading at %s." % current_price if current_price else "."
        notification_type = "SUCCESS" if direction == "up" else "WARNING"

        # Check for recent duplicates to prevent re-sends if cache failed
        notification_cache_key = "signals:last_notification:%s:%s:%s" % (symbol, direction, crossed_label)
        try:
            if cache.get(notification_cache_key):
                if self.verbose:
                    self.stdout.write("Skip %s: notification already sent within last 60s." % title)
                return False
        except Exception:
            logger.exception("Could not check notification cache for deduplication.")
        
        # Set the cache key to prevent duplicate notifications for 60 seconds
        try:
            cache.set(notification_cache_key, True, timeout=60)
        except Exception:
            logger.exception("Could not set notification deduplication cache for %s." % title)

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
        bulk_create_user_notifications(notifications)

        try:
            from Dashboard.realtime import broadcast_notifications

            # bulk_create filled id/created_at, so broadcast the objects without re-reading them
            # (an id__in lookup scans the table on MSSQL and breaks past 2100 ids).
            broadcast_notifications(notifications, event_name="created")
        except Exception:
            logger.exception("Failed to broadcast signal change notifications.")

        data = {
            "type": "signal_change_threshold",
            "symbol": symbol,
            "direction": direction,
            "crossed_percentage": str(crossed_percentage),
            "signed_crossed_percentage": str(signed_crossed_percentage),
            "current_percentage": str(current_percentage),
            "current_price": current_price,
            # FCM rejects data payloads over 4KB; a busy symbol's full UUID list exceeds that.
            "signal_ids": ",".join(str(signal_id) for signal_id in signal_ids[:MAX_PUSH_SIGNAL_IDS]),
            "signal_count": str(len(signal_ids)),
            "bid": str(quote.get("bid") or ""),
            "ask": str(quote.get("ask") or ""),
            "source": "montada-app",
        }
        push_summary = "not sent"
        try:
            from firebase import send_push_to_users

            push_result = send_push_to_users(
                users=users,
                title=title,
                body=message,
                data=data,
            )
            push_summary = _describe_push_result(push_result)
        except Exception as exc:
            logger.exception("Failed to send signal change push notifications.")
            push_summary = "FAILED: %s" % exc

        self.stdout.write(
            self.style.SUCCESS(
                "Notified %d user(s): %s. FCM push: %s."
                % (len(users), title, push_summary)
            )
        )
        return True

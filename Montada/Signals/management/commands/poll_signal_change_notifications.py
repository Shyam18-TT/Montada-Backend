import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from Signals.management.commands.run_price_alerts import _describe_push_result
from Signals.price_move_levels import (
    DIRECTIONS,
    UP,
    LevelState,
    advance,
    has_states,
    level_for,
    level_percentage,
    load_states,
    normalize_reference,
    notification_id,
    parse_tick_time,
    period_key,
    rebase,
    revert,
)
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
# --state-ttl / --lock-ttl are accepted for existing command lines but no longer used: the
# level state is a durable DB table (Signals.price_move_levels) updated with an atomic
# compare-and-set, so it needs neither a TTL nor a lock.
DEFAULT_STATE_TTL_SECONDS = 60 * 60 * 24 * 7
DEFAULT_LOCK_TTL_SECONDS = 60
# Market snapshot prices older than this fall back to the polled quote's bid.
MARKET_SNAPSHOT_MAX_AGE_SECONDS = 300
# Max signal ids sent in the FCM data payload (keeps the payload well under FCM's 4KB limit).
MAX_PUSH_SIGNAL_IDS = 20
# While waiting for the next poll, check this often whether the admin changed the config.
CONFIG_CHECK_SECONDS = 5
# Set once a poll has recorded the levels of an empty state table. A poll that finds neither
# this marker nor any stored level (first run of this code, or a wiped table) records where
# every symbol is without notifying, instead of pushing every level already behind the price.
# A Redis flush alone is harmless now: the levels live in the DB.
BASELINE_DONE_CACHE_KEY = "signals:change-notifications:baseline-done:v2"


@dataclass(frozen=True)
class Tick:
    """What identifies a quote for the level state: its period and its (feed) timestamp."""
    period: str
    tick_at: datetime = None


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
    """Level reached, where levels are start, start+step, start+2*step, ... (0 below start)."""
    return level_for(abs_percentage, start, step)


def _step_percentage(step_count, start, step):
    return level_percentage(step_count, start, step)


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
            help="Deprecated, ignored: level state is a DB table now.",
        )
        parser.add_argument(
            "--lock-ttl",
            type=int,
            default=DEFAULT_LOCK_TTL_SECONDS,
            help="Deprecated, ignored: levels are claimed with an atomic compare-and-set, no lock.",
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
            help="Never reset the notified levels (by default they reset when the feed's daily reference price changes).",
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

        self._last_tick_at = {}
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
        """True when there is no stored level anywhere and no marker saying that is expected."""
        try:
            if cache.get(BASELINE_DONE_CACHE_KEY):
                return False
        except Exception:
            return False  # Redis down: do not silence a poll because of it
        return not has_states()

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

        # Without the stored levels every symbol would look new, so a read failure aborts the poll.
        self._level_states = load_states()
        self._instrument_ids = self._load_instrument_ids()
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
            tick = self._validate_tick(symbol, quote)
            if tick is None:
                continue
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
                tick=tick,
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

    # ------------------------------------------------------------------ ticks

    def _tick_meta(self, quote):
        """Period and timestamp of a quote (see Signals.price_move_levels.period_key)."""
        return Tick(
            period=period_key(
                normalize_reference(quote.get("bid_today")),
                reset=getattr(self, "reset_daily", True),
            ),
            tick_at=parse_tick_time(quote.get("time")),
        )

    def _count_silent(self, reason):
        counts = getattr(self, "_silent_counts", None)
        if counts is None:
            counts = self._silent_counts = {}
        counts[reason] = counts.get(reason, 0) + 1

    def _validate_tick(self, symbol, quote):
        """
        The Tick of a usable quote, else None. Ignored, and never allowed to move the state:
        - a missing, zero or negative price (bid, or the bid_today reference when given);
        - a tick older than one already processed for the symbol (out of order / stale feed).
        """
        bid = _parse_decimal(quote.get("bid"))
        if bid is None or bid <= 0:
            self._count_silent("bad price")
            logger.warning("Skipping %s: unusable bid=%r.", symbol, quote.get("bid"))
            return None
        reference = quote.get("bid_today")
        if reference not in (None, "") and normalize_reference(reference) is None:
            self._count_silent("bad price")
            logger.warning("Skipping %s: unusable bid_today=%r.", symbol, reference)
            return None

        tick = self._tick_meta(quote)
        last_tick_at = getattr(self, "_last_tick_at", None)
        if last_tick_at is None:
            last_tick_at = self._last_tick_at = {}
        previous = last_tick_at.get(symbol)
        if tick.tick_at is not None:
            if previous is not None and tick.tick_at < previous:
                self._count_silent("stale tick")
                if self.verbose:
                    self.stdout.write(
                        "Skip %s: tick %s is older than the last processed (%s)." % (symbol, tick.tick_at, previous)
                    )
                return None
            last_tick_at[symbol] = tick.tick_at
        return tick

    # ----------------------------------------------------------------- levels

    def _get_level_states(self):
        states = getattr(self, "_level_states", None)
        if states is None:
            states = self._level_states = load_states()
        return states

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

    def _rebase_levels(self, symbol, direction, level, rule, tick):
        """
        The level grid of `symbol` changed (threshold/step edited, muted, unmuted): start over
        from the current change without notifying the levels the new grid "missed". The other
        direction has notified nothing on the new grid, so it starts at 0.
        """
        states = self._get_level_states()
        for side in DIRECTIONS:
            target = level if side == direction else 0
            percentage = level_percentage(target, rule.threshold, rule.step) if target else Decimal("0")
            rebase(
                symbol, side, period=tick.period, level=target, percentage=percentage,
                config_sig=rule.signature, tick_at=tick.tick_at,
            )
            states[(symbol, side)] = LevelState(target, tick.period, rule.signature, tick.tick_at)
        self._count_silent("config changed")

    def _maybe_notify_symbol(self, symbol, direction, change_percentage, quote, signals, tick=None):
        """
        Notify `symbol` once for the level `change_percentage` has reached, if that level is
        higher than any already notified in this period for this direction. Returns the number
        of notifications sent (0 or 1).

        The stored level never moves down inside a period: a price that falls back and crosses
        the same level again sends nothing. The claim on a level is an atomic compare-and-set
        (price_move_levels.advance), so workers racing on the same tick send exactly one push.
        """
        rule = self._resolve_rule(symbol, signals)
        tick = tick or self._tick_meta(quote)
        states = self._get_level_states()
        period = tick.period
        level = level_for(change_percentage, rule.threshold, rule.step)

        def stored_level(side):
            state = states.get((symbol, side))
            return state.level if state is not None and state.period == period else 0

        if any(
            state is not None and state.period == period and state.config_sig != rule.signature
            for state in (states.get((symbol, side)) for side in DIRECTIONS)
        ):
            self._rebase_levels(symbol, direction, level, rule, tick)
            return 0

        if level == 0:
            return 0  # below the threshold: nothing to send, and nothing to reset
        previous_level = stored_level(direction)
        if level <= previous_level:
            if self.verbose:
                self.stdout.write(
                    "Skip %s: already notified %s %s%%."
                    % (symbol, direction, _format_percent(level_percentage(previous_level, rule.threshold, rule.step)))
                )
            return 0

        silent_reason = None
        if not rule.enabled:
            silent_reason = "muted"  # tracked, so unmuting later starts from the current level
        elif getattr(self, "_baseline_poll", False):
            silent_reason = "baseline"  # first run: record where every symbol is, send nothing
        elif getattr(self, "_notification_batches_this_poll", 0) >= self._get_config().max_notification_batches:
            logger.warning(
                "Signal change notification batch limit reached (%s). Last processed symbol=%s.",
                self._get_config().max_notification_batches, symbol,
            )
            return 0  # level not claimed: the next poll sends it

        crossed_percentage = level_percentage(level, rule.threshold, rule.step)
        if not advance(
            symbol, direction, period=period, level=level, percentage=crossed_percentage,
            config_sig=rule.signature, tick_at=tick.tick_at,
        ):
            if self.verbose:
                self.stdout.write("Skip %s: %s level %s already claimed." % (symbol, direction, level))
            states.pop((symbol, direction), None)  # re-read on the next poll
            return 0
        states[(symbol, direction)] = LevelState(level, period, rule.signature, tick.tick_at)

        if silent_reason:
            self._count_silent(silent_reason)
            return 0

        sent = self._send_level_alert(
            symbol=symbol,
            direction=direction,
            level=level,
            period=period,
            crossed_percentage=crossed_percentage,
            change_percentage=change_percentage,
            quote=quote,
            signals=signals,
        )
        if not sent:
            # Nothing reached anyone, so give the level back; the next poll retries it. The push
            # carries the (symbol, direction, level, period) key, so a retry never double-sends.
            restore_level = previous_level
            revert(
                symbol, direction, period=period, claimed_level=level, restore_level=restore_level,
                restore_percentage=level_percentage(restore_level, rule.threshold, rule.step)
                if restore_level else Decimal("0"),
            )
            states[(symbol, direction)] = LevelState(restore_level, period, rule.signature, tick.tick_at)
            return 0

        self._notification_batches_this_poll = getattr(self, "_notification_batches_this_poll", 0) + 1
        return 1

    def _instrument_id_for(self, symbol, signals):
        """Instrument id of `symbol` (as a string) for the push payload, or ""."""
        for signal in signals or ():
            if getattr(signal, "instrument_id", None):
                return str(signal.instrument_id)
        ids = getattr(self, "_instrument_ids", None) or {}
        for candidate in [symbol] + sorted(_symbol_alias_candidates(symbol) - {symbol}):
            if candidate in ids:
                return ids[candidate]
        return ""

    def _load_instrument_ids(self):
        try:
            from Signals.models import Instrument

            return {
                _normalize_symbol(symbol): str(instrument_id)
                for symbol, instrument_id in Instrument.objects.values_list("symbol", "id")
                if _normalize_symbol(symbol)
            }
        except Exception:
            logger.exception("Could not load instrument ids for move notifications.")
            return {}

    def _send_level_alert(
        self, *, symbol, direction, level, period, crossed_percentage, change_percentage, quote, signals
    ):
        """
        Deliver the notification for a claimed level. True when it went out. An exception while
        creating the in-app notifications means nothing was delivered, so it is reported as
        not sent (the caller rolls the claim back). FCM failures do not: send_push_to_users
        already retries per device and logs every attempt, and the in-app copy exists by then.
        """
        signed = crossed_percentage if direction == UP else -crossed_percentage
        try:
            return self._notify_all_users(
                symbol=symbol,
                direction=direction,
                signed_crossed_percentage=signed,
                crossed_percentage=crossed_percentage,
                current_percentage=change_percentage,
                quote=quote,
                signal_ids=[signal.id for signal in signals],
                level=level,
                instrument_id=self._instrument_id_for(symbol, signals),
                notification_id=notification_id(symbol, direction, level, period),
            )
        except Exception:
            logger.exception("Could not deliver %s %s level %s notification.", symbol, direction, level)
            return False

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
        level=None,
        instrument_id="",
        notification_id=None,
    ):
        """
        In-app notification plus push for every active user. Returns True when it went out,
        False when there was nobody to notify. `notification_id` is the idempotency key of the
        alert (symbol, direction, level, period); firebase skips devices that already got it.
        """
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

        # No duplicate guard here: the caller has already won the atomic claim on this level.

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
            "type": "market_percentage_alert",
            "symbol": symbol,
            "direction": direction,
            "alert_level": str(level) if level is not None else "",
            "instrument_id": str(instrument_id or ""),
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
        if notification_id:
            data["notification_id"] = notification_id
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

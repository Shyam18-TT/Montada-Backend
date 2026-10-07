import logging
import threading
import time

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.management.base import BaseCommand

from Signals.market_stream import (
    MARKET_DATA_GROUP_NAME,
    build_market_tick_payload,
    fetch_trustcapital_open_prices,
    load_market_snapshot_from_db,
    save_market_snapshot,
)


logger = logging.getLogger(__name__)

try:
    import MT5Manager
except ImportError:  # pragma: no cover - depends on optional package
    MT5Manager = None


ALLOWED_PATH_PREFIXES = [
    "Forex\\",
    "Forex Minors\\",
    "Spot Metals\\",
    "Metal Future CFDs\\",
    "Energy CFDs\\Spot\\",
    "Energy CFDs\\Future 2\\",
    "Index CFDs\\Cash\\",
    "Index CFDs\\Future 2\\",
    "Agricultural Comdty CFDs\\",
    "Crypto CFDs\\",
    "MENA Shares\\",
    "Share CFDs\\",
]


def _extract_symbol_name(symbol_info):
    return (
        getattr(symbol_info, "Symbol", None)
        or getattr(symbol_info, "symbol", None)
        or getattr(symbol_info, "Name", None)
        or getattr(symbol_info, "name", None)
        or ""
    )


def _extract_symbol_path(symbol_info):
    return (
        getattr(symbol_info, "Path", None)
        or getattr(symbol_info, "path", None)
        or ""
    )


def _is_allowed_symbol(symbol_info):
    path = str(_extract_symbol_path(symbol_info) or "")
    return any(path.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES)


def _extract_symbol_digits(symbol_info):
    for attr in ("Digits", "digits"):
        value = getattr(symbol_info, attr, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


# How often today's reference prices (bid_today / ask_today) are re-read from the website's
# price API, so the day rollover is picked up without restarting the stream.
OPEN_PRICES_REFRESH_SECONDS = 60

# The MT5 Manager connection can die silently (e.g. the MT5 server restarts over the weekend):
# the process keeps running but no ticks arrive, so prices freeze while bid_today keeps
# rolling over and the daily change becomes wrong. Crypto ticks around the clock, so this
# long without ANY tick means the connection is dead and the stream reconnects itself.
DEFAULT_STALE_TICK_SECONDS = 120
# Wait between reconnect attempts: doubles after each failure, capped.
RECONNECT_BACKOFF_START_SECONDS = 5
RECONNECT_BACKOFF_MAX_SECONDS = 120


class Command(BaseCommand):
    help = "Stream MT5 Manager ticks and broadcast them over the market data websocket."

    def add_arguments(self, parser):
        parser.add_argument(
            "--symbols",
            default="",
            help="Optional comma-separated symbols to subscribe to. Default subscribes to all symbols.",
        )
        parser.add_argument(
            "--timeout-ms",
            type=int,
            default=120000,
            help="MT5 Manager connect timeout in milliseconds.",
        )
        parser.add_argument(
            "--publish-interval-ms",
            type=int,
            default=250,
            help="How often buffered ticks are flushed to websocket clients.",
        )
        parser.add_argument(
            "--stale-tick-seconds",
            type=int,
            default=DEFAULT_STALE_TICK_SECONDS,
            help=(
                "Reconnect to MT5 Manager when no tick has arrived for this many seconds "
                f"(minimum 30). Default: {DEFAULT_STALE_TICK_SECONDS}."
            ),
        )

    def handle(self, *args, **options):
        if MT5Manager is None:
            self.stderr.write(
                self.style.ERROR("MT5Manager package is not installed. Install it to stream market data.")
            )
            return

        self._server = getattr(settings, "MT5_MANAGER_SERVER", "")
        self._login = int(getattr(settings, "MT5_MANAGER_LOGIN", 0) or 0)
        self._password = str(getattr(settings, "MT5_MANAGER_PASSWORD", "") or "")
        self._timeout_ms = max(1000, int(options.get("timeout_ms") or 120000))
        self._requested_symbols = [
            symbol.strip()
            for symbol in str(options.get("symbols") or "").split(",")
            if symbol.strip()
        ]
        publish_interval_ms = max(50, int(options.get("publish_interval_ms") or 250))
        stale_tick_seconds = max(30, int(options.get("stale_tick_seconds") or DEFAULT_STALE_TICK_SECONDS))

        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if not channel_layer:
            self.stderr.write(
                self.style.ERROR("No channel layer configured. Market data cannot be broadcast.")
            )
            return

        self._channel_layer = channel_layer
        self._publish_interval_seconds = publish_interval_ms / 1000.0
        self._pending_ticks = {}
        self._pending_ticks_lock = threading.Lock()
        self._latest_ticks = {}
        # Website reference prices keyed by UPPERCASE symbol: {"bid_today": .., "ask_today": ..}
        self._open_prices = {}
        # MT5 symbol digits keyed by symbol name (PHP rounds with mt5 Digits).
        self._symbol_digits = {}
        self._selected_symbols = []
        self._stop_dispatcher = threading.Event()
        self._last_broadcast_error_at = 0.0
        # Watchdog state: time of the last tick, and a flag set when MT5 reports a disconnect.
        self._last_tick_at = time.monotonic()
        self._connection_lost = threading.Event()
        self._tick_sink = self._build_tick_sink()
        self._manager_sink = self._build_manager_sink()
        self._manager = None

        dispatcher_thread = threading.Thread(
            target=self._dispatch_ticks_loop,
            name="signals-market-data-dispatcher",
            daemon=True,
        )
        dispatcher_thread.start()
        threading.Thread(
            target=self._open_prices_refresh_loop,
            name="signals-market-open-prices",
            daemon=True,
        ).start()

        backoff = RECONNECT_BACKOFF_START_SECONDS
        try:
            while True:
                if self._manager is None:
                    if self._connect_and_subscribe():
                        backoff = RECONNECT_BACKOFF_START_SECONDS
                    else:
                        self.stderr.write(self.style.WARNING(f"Retrying MT5 connection in {backoff}s..."))
                        time.sleep(backoff)
                        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)
                    continue

                time.sleep(1)
                idle_seconds = time.monotonic() - self._last_tick_at
                if self._connection_lost.is_set():
                    reason = "MT5 Manager reported a disconnect"
                elif idle_seconds >= stale_tick_seconds:
                    reason = f"no ticks for {int(idle_seconds)}s"
                else:
                    continue

                logger.warning("Market data stream reconnecting: %s.", reason)
                self.stderr.write(self.style.WARNING(f"Reconnecting to MT5 Manager ({reason})..."))
                self._disconnect()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Stopping market data stream..."))
        finally:
            self._stop_dispatcher.set()
            if dispatcher_thread.is_alive():
                dispatcher_thread.join(timeout=2.0)
            self._disconnect()
            self.stdout.write(self.style.SUCCESS("Disconnected from MT5 Manager."))

    def _connect_and_subscribe(self):
        """Open a fresh MT5 Manager connection and subscribe to ticks. True on success."""
        manager = MT5Manager.ManagerAPI()
        self._connection_lost.clear()
        self.stdout.write(
            self.style.SUCCESS(
                f"Connecting to {self._server} (login={self._login}) for market data stream..."
            )
        )

        pump_modes = getattr(MT5Manager.ManagerAPI, "EnPumpModes", None)
        pump_mode = getattr(pump_modes, "PUMP_MODE_SYMBOLS", 0)
        try:
            # Connection events (OnDisconnect) make reconnects immediate; the tick watchdog
            # still covers a connection that dies without reporting it.
            manager.Subscribe(self._manager_sink)
        except Exception:
            logger.debug("MT5 Manager connection-event subscription unavailable.", exc_info=True)
        if not manager.Connect(self._server, self._login, self._password, pump_mode, self._timeout_ms):
            self.stderr.write(
                self.style.ERROR(f"Connection failed: {getattr(MT5Manager, 'LastError', lambda: '')()}")
            )
            self._safe_disconnect(manager)
            return False

        self.stdout.write(self.style.SUCCESS("Connected to MT5 Manager successfully."))

        symbols_to_add = list(self._requested_symbols)
        if not symbols_to_add:
            try:
                raw_symbols = manager.SymbolGetArray() or []
                filtered_symbols = [
                    item for item in raw_symbols
                    if _is_allowed_symbol(item)
                ]
                symbols_to_add = [
                    symbol_name
                    for symbol_name in (_extract_symbol_name(item) for item in filtered_symbols)
                    if symbol_name
                ]
                for item in filtered_symbols:
                    digits = _extract_symbol_digits(item)
                    if digits is not None:
                        self._symbol_digits[_extract_symbol_name(item)] = digits
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Loaded {len(raw_symbols)} symbol(s) from MT5 Manager; "
                        f"allowed-path filter kept {len(symbols_to_add)}."
                    )
                )
            except Exception as exc:
                logger.exception("Failed to fetch symbols from MT5 Manager: %s", exc)
                symbols_to_add = []

        if not symbols_to_add:
            self.stderr.write(
                self.style.WARNING("No symbols available to subscribe. Disconnecting.")
            )
            self._safe_disconnect(manager)
            return False

        selected_symbols = []
        for symbol_name in symbols_to_add:
            if manager.SelectedAdd(symbol_name):
                selected_symbols.append(symbol_name)
            else:
                logger.warning(
                    "SelectedAdd failed for symbol=%s error=%s",
                    symbol_name,
                    getattr(MT5Manager, "LastError", lambda: "")(),
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Subscribed to {len(selected_symbols)}/{len(symbols_to_add)} symbol(s)."
            )
        )

        if selected_symbols:
            self._selected_symbols = selected_symbols
            try:
                self._refresh_open_prices(selected_symbols)
                initial_snapshot = load_market_snapshot_from_db(selected_symbols)
                latest_ticks = {}
                for tick in initial_snapshot:
                    symbol = tick.get("symbol")
                    if not symbol:
                        continue
                    if symbol not in self._symbol_digits and tick.get("digits") is not None:
                        self._symbol_digits[symbol] = tick["digits"]
                    # Rebuild with the website's reference prices so the change matches it.
                    latest_ticks[symbol] = self._build_tick(symbol, tick.get("bid"), tick.get("ask"))
                self._latest_ticks = latest_ticks

                save_market_snapshot(self._latest_ticks.values())
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Prepared initial market snapshot for {len(self._latest_ticks)} subscribed symbol(s)."
                    )
                )
            except Exception as exc:
                logger.exception("Failed to prepare initial market snapshot: %s", exc)

        if not manager.TickSubscribe(self._tick_sink):
            self.stderr.write(
                self.style.ERROR(
                    f"Tick subscription failed: {getattr(MT5Manager, 'LastError', lambda: '')()}"
                )
            )
            self._safe_disconnect(manager)
            return False

        self._manager = manager
        self._last_tick_at = time.monotonic()
        self.stdout.write(self.style.SUCCESS("Market data websocket broadcasting is live."))
        return True

    def _safe_disconnect(self, manager):
        """Unsubscribe and disconnect, ignoring errors from an already-dead connection."""
        steps = (
            lambda: manager.TickUnsubscribe(self._tick_sink),
            lambda: manager.Unsubscribe(self._manager_sink),
            manager.Disconnect,
        )
        for step in steps:
            try:
                step()
            except Exception:
                logger.debug("MT5 Manager cleanup step failed.", exc_info=True)

    def _disconnect(self):
        manager, self._manager = self._manager, None
        if manager is not None:
            self._safe_disconnect(manager)

    def _refresh_open_prices(self, symbols):
        """Reload bid_today / ask_today from the website's price API (keeps old values on failure)."""
        open_prices = fetch_trustcapital_open_prices(symbols)
        if open_prices:
            self._open_prices = open_prices
        else:
            logger.warning("Could not refresh today's reference prices; keeping %d previous.", len(self._open_prices))
        missing = [symbol for symbol in symbols if symbol.upper() not in self._open_prices]
        if missing:
            logger.warning(
                "No bid_today from the price API for %d symbol(s); their change is not sent: %s",
                len(missing),
                ", ".join(missing[:20]),
            )

    def _open_prices_refresh_loop(self):
        while not self._stop_dispatcher.wait(timeout=OPEN_PRICES_REFRESH_SECONDS):
            # Symbols of the current connection (they can change after a reconnect).
            symbols = list(self._selected_symbols)
            if not symbols:
                continue
            try:
                self._refresh_open_prices(symbols)
            except Exception:
                logger.exception("Refreshing today's reference prices failed.")

    def _build_tick(self, symbol, bid, ask, tick_digits=None):
        open_price = self._open_prices.get(str(symbol or "").strip().upper()) or {}
        latest = self._latest_ticks.get(symbol) or {}
        digits = self._symbol_digits.get(symbol)
        if digits is None:
            digits = tick_digits if tick_digits is not None else latest.get("digits")
        return build_market_tick_payload(
            symbol=symbol,
            bid=bid,
            ask=ask,
            ask_open=open_price.get("ask_today"),
            bid_open=open_price.get("bid_today"),
            digits=digits,
        )

    def _build_tick_sink(self):
        class TickSink:
            def OnTick(self, symbol, tick):  # noqa: N802 - MT5Manager callback naming
                # Watchdog heartbeat: any tick proves the connection is alive.
                self_outer._last_tick_at = time.monotonic()
                try:
                    tick_digits = getattr(tick, "Digits", None)
                    if tick_digits is None:
                        tick_digits = getattr(tick, "digits", None)
                    payload = self_outer._build_tick(
                        symbol,
                        getattr(tick, "bid", None),
                        getattr(tick, "ask", None),
                        tick_digits=tick_digits,
                    )
                    with self_outer._pending_ticks_lock:
                        self_outer._pending_ticks[payload["symbol"]] = payload
                except Exception as exc:  # pragma: no cover - depends on live MT5 callbacks
                    logger.exception("Failed to buffer MT5 tick for %s: %s", symbol, exc)

            def OnTickStat(self, stat):  # noqa: N802 - MT5Manager callback naming
                return None

        self_outer = self
        return TickSink()

    def _build_manager_sink(self):
        """Connection-event sink: flags a disconnect so the main loop reconnects right away."""
        class ManagerSink:
            def OnConnect(self):  # noqa: N802 - MT5Manager callback naming
                return None

            def OnDisconnect(self):  # noqa: N802 - MT5Manager callback naming
                logger.warning("MT5 Manager connection lost.")
                self_outer._connection_lost.set()

        self_outer = self
        return ManagerSink()

    def _dispatch_ticks_loop(self):
        while not self._stop_dispatcher.is_set():
            self._stop_dispatcher.wait(timeout=self._publish_interval_seconds)
            self._flush_pending_ticks()

        self._flush_pending_ticks()

    def _flush_pending_ticks(self):
        with self._pending_ticks_lock:
            if not self._pending_ticks:
                return
            ticks = list(self._pending_ticks.values())
            self._pending_ticks.clear()

        self._broadcast_ticks(ticks)

    def _broadcast_ticks(self, ticks):
        for tick in ticks:
            symbol = str((tick or {}).get("symbol") or "").strip()
            if symbol:
                self._latest_ticks[symbol] = tick

        try:
            save_market_snapshot(self._latest_ticks.values())
        except Exception as exc:
            logger.exception("Failed to persist market snapshot: %s", exc)

        for chunk_start in range(0, len(ticks), 200):
            chunk = ticks[chunk_start:chunk_start + 200]
            try:
                async_to_sync(self._channel_layer.group_send)(
                    MARKET_DATA_GROUP_NAME,
                    {
                        "type": "market.ticks",
                        "ticks": chunk,
                    },
                )
            except Exception as exc:
                now = time.monotonic()
                if now - self._last_broadcast_error_at >= 5:
                    logger.exception(
                        "Failed to broadcast buffered MT5 ticks (chunk_size=%s): %s",
                        len(chunk),
                        exc,
                    )
                    self._last_broadcast_error_at = now

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

    def handle(self, *args, **options):
        if MT5Manager is None:
            self.stderr.write(
                self.style.ERROR("MT5Manager package is not installed. Install it to stream market data.")
            )
            return

        server = getattr(settings, "MT5_MANAGER_SERVER", "")
        login = int(getattr(settings, "MT5_MANAGER_LOGIN", 0) or 0)
        password = str(getattr(settings, "MT5_MANAGER_PASSWORD", "") or "")
        timeout_ms = max(1000, int(options.get("timeout_ms") or 120000))
        requested_symbols = [
            symbol.strip()
            for symbol in str(options.get("symbols") or "").split(",")
            if symbol.strip()
        ]
        publish_interval_ms = max(50, int(options.get("publish_interval_ms") or 250))

        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if not channel_layer:
            self.stderr.write(
                self.style.ERROR("No channel layer configured. Market data cannot be broadcast.")
            )
            return

        manager = MT5Manager.ManagerAPI()
        self._channel_layer = channel_layer
        self._publish_interval_seconds = publish_interval_ms / 1000.0
        self._pending_ticks = {}
        self._pending_ticks_lock = threading.Lock()
        self._latest_ticks = {}
        # Website reference prices keyed by UPPERCASE symbol: {"bid_today": .., "ask_today": ..}
        self._open_prices = {}
        # MT5 symbol digits keyed by symbol name (PHP rounds with mt5 Digits).
        self._symbol_digits = {}
        self._stop_dispatcher = threading.Event()
        self._last_broadcast_error_at = 0.0
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_ticks_loop,
            name="signals-market-data-dispatcher",
            daemon=True,
        )
        sink = self._build_tick_sink()

        self.stdout.write(
            self.style.SUCCESS(
                f"Connecting to {server} (login={login}) for market data stream..."
            )
        )

        pump_modes = getattr(MT5Manager.ManagerAPI, "EnPumpModes", None)
        pump_mode = getattr(pump_modes, "PUMP_MODE_SYMBOLS", 0)
        if not manager.Connect(server, login, password, pump_mode, timeout_ms):
            self.stderr.write(
                self.style.ERROR(f"Connection failed: {getattr(MT5Manager, 'LastError', lambda: '')()}")
            )
            return

        self.stdout.write(self.style.SUCCESS("Connected to MT5 Manager successfully."))

        symbols_to_add = requested_symbols
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
            manager.Disconnect()
            return

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
            try:
                self._refresh_open_prices(selected_symbols)
                initial_snapshot = load_market_snapshot_from_db(selected_symbols)
                self._latest_ticks = {}
                for tick in initial_snapshot:
                    symbol = tick.get("symbol")
                    if not symbol:
                        continue
                    if symbol not in self._symbol_digits and tick.get("digits") is not None:
                        self._symbol_digits[symbol] = tick["digits"]
                    # Rebuild with the website's reference prices so the change matches it.
                    self._latest_ticks[symbol] = self._build_tick(symbol, tick.get("bid"), tick.get("ask"))

                save_market_snapshot(self._latest_ticks.values())
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Prepared initial market snapshot for {len(self._latest_ticks)} subscribed symbol(s)."
                    )
                )
            except Exception as exc:
                logger.exception("Failed to prepare initial market snapshot: %s", exc)

        if not manager.TickSubscribe(sink):
            self.stderr.write(
                self.style.ERROR(
                    f"Tick subscription failed: {getattr(MT5Manager, 'LastError', lambda: '')()}"
                )
            )
            manager.Disconnect()
            return

        self.stdout.write(self.style.SUCCESS("Market data websocket broadcasting is live."))
        self._dispatcher_thread.start()
        threading.Thread(
            target=self._open_prices_refresh_loop,
            args=(selected_symbols,),
            name="signals-market-open-prices",
            daemon=True,
        ).start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Stopping market data stream..."))
        finally:
            self._stop_dispatcher.set()
            if self._dispatcher_thread.is_alive():
                self._dispatcher_thread.join(timeout=2.0)
            try:
                manager.TickUnsubscribe(sink)
            except Exception:
                logger.exception("Failed to unsubscribe MT5 tick sink cleanly.")
            manager.Disconnect()
            self.stdout.write(self.style.SUCCESS("Disconnected from MT5 Manager."))

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

    def _open_prices_refresh_loop(self, symbols):
        while not self._stop_dispatcher.wait(timeout=OPEN_PRICES_REFRESH_SECONDS):
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

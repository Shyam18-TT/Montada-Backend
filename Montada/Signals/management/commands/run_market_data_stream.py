import atexit
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

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

# --- Windows Task Scheduler support -------------------------------------------------------
# Own log file (rotated) so every connection event can be traced after the fact.
STREAM_LOG_NAME = "market_stream.log"
STREAM_LOG_FORMAT = "%(asctime)s %(levelname)s pid=%(process)d %(threadName)s %(message)s"
# Periodic "still alive" line with counters; a gap in these lines shows when the process froze.
HEARTBEAT_SECONDS = 60
# If the main loop makes no progress for this long (a blocking MT5/Redis/DB call), the stack
# of every thread is written to market_stream_faults.log to show exactly where it is stuck.
# Must be longer than the MT5 connect timeout.
HANG_DUMP_SECONDS = 600
# A single Disconnect/Unsubscribe on a dead MT5 connection can block; give up after this long.
CLEANUP_TIMEOUT_SECONDS = 15
# Warn when one websocket broadcast (Redis group_send) takes longer than this.
SLOW_BROADCAST_SECONDS = 2.0


def _stream_log_dir():
    log_dir = Path(getattr(settings, "LOG_DIR", Path(settings.BASE_DIR) / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _acquire_single_instance_lock(lock_path):
    """
    Task Scheduler can start a second copy while the first is still running (a "run every N
    minutes" trigger, or a manual run). Two copies would both write the snapshot and double
    every websocket tick. Returns an open file handle that must be kept alive, or None if
    another instance already holds the lock. The OS drops the lock when the process dies.
    """
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


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

        # Counters reported by the heartbeat line.
        self._started_at = time.monotonic()
        self._connected_at = None
        self._connect_attempts = 0
        self._connect_successes = 0
        self._tick_count = 0
        self._ticks_at_last_heartbeat = 0
        self._broadcast_count = 0
        self._last_broadcast_ok_at = None
        self._stop_reason = "unknown"

        # Task Scheduler: refuse to run twice, then log to a file.
        lock_handle = _acquire_single_instance_lock(_stream_log_dir() / "market_stream.lock")
        if lock_handle is None:
            message = "Another run_market_data_stream instance is already running; exiting."
            logger.warning(message)
            self.stderr.write(self.style.WARNING(message))
            return
        self._lock_handle = lock_handle  # keep open: closing it would release the lock
        self._setup_run_logging()
        self._install_signal_handlers()

        logger.info(
            "Market data stream starting: server=%s login=%s symbols=%s timeout_ms=%s "
            "publish_interval_ms=%s stale_tick_seconds=%s python=%s",
            self._server,
            self._login,
            ",".join(self._requested_symbols) or "<all allowed>",
            self._timeout_ms,
            publish_interval_ms,
            stale_tick_seconds,
            sys.version.split()[0],
        )

        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_ticks_loop,
            name="signals-market-data-dispatcher",
            daemon=True,
        )
        self._dispatcher_thread.start()
        threading.Thread(
            target=self._open_prices_refresh_loop,
            name="signals-market-open-prices",
            daemon=True,
        ).start()

        backoff = RECONNECT_BACKOFF_START_SECONDS
        next_heartbeat_at = time.monotonic() + HEARTBEAT_SECONDS
        failure = None
        try:
            while True:
                self._arm_hang_dump()
                now = time.monotonic()
                if now >= next_heartbeat_at:
                    self._log_heartbeat()
                    next_heartbeat_at = now + HEARTBEAT_SECONDS

                if self._manager is None:
                    if self._connect_and_subscribe():
                        backoff = RECONNECT_BACKOFF_START_SECONDS
                    else:
                        logger.warning("MT5 connection attempt failed; retrying in %ss.", backoff)
                        self.stderr.write(self.style.WARNING(f"Retrying MT5 connection in {backoff}s..."))
                        time.sleep(backoff)
                        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)
                    continue

                time.sleep(1)
                if not self._dispatcher_thread.is_alive():
                    # The loop catches its own errors, so this should never happen; if it does
                    # no tick would ever be published, so fail loudly and let the scheduler restart.
                    raise RuntimeError("Tick dispatcher thread died unexpectedly.")

                idle_seconds = time.monotonic() - self._last_tick_at
                if self._connection_lost.is_set():
                    reason = "MT5 Manager reported a disconnect"
                elif idle_seconds >= stale_tick_seconds:
                    reason = f"no ticks for {int(idle_seconds)}s"
                else:
                    continue

                connected_for = int(time.monotonic() - (self._connected_at or time.monotonic()))
                logger.warning(
                    "Market data stream reconnecting: %s (connection was up %ss, %s ticks total).",
                    reason,
                    connected_for,
                    self._tick_count,
                )
                self.stderr.write(self.style.WARNING(f"Reconnecting to MT5 Manager ({reason})..."))
                self._disconnect()
        except KeyboardInterrupt:
            self._stop_reason = self._stop_reason if self._stop_reason != "unknown" else "keyboard interrupt"
            self.stdout.write(self.style.WARNING("Stopping market data stream..."))
        except Exception as exc:
            self._stop_reason = f"crashed: {exc!r}"
            logger.exception("Market data stream crashed.")
            failure = exc
        finally:
            faulthandler.cancel_dump_traceback_later()
            self._stop_dispatcher.set()
            if self._dispatcher_thread.is_alive():
                self._dispatcher_thread.join(timeout=2.0)
            self._disconnect()
            logger.info(
                "Market data stream stopped: reason=%s uptime=%ss ticks=%s connects=%s/%s.",
                self._stop_reason,
                int(time.monotonic() - self._started_at),
                self._tick_count,
                self._connect_successes,
                self._connect_attempts,
            )
            self.stdout.write(self.style.SUCCESS("Disconnected from MT5 Manager."))

        if failure is not None:
            # Non-zero exit code so Task Scheduler's "restart on failure" kicks in.
            raise CommandError(f"Market data stream crashed: {failure!r}")

    # -- run logging / Task Scheduler helpers ---------------------------------------------

    def _setup_run_logging(self):
        """File logging for this run, independent of the Django LOGGING config."""
        log_dir = _stream_log_dir()

        # Under Task Scheduler (or pythonw) there may be no console at all.
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name, None)
            if stream is None:
                setattr(sys, name, open(os.devnull, "w"))
            else:
                try:
                    stream.reconfigure(line_buffering=True)
                except Exception:
                    pass

        # One process owns this file (single-instance lock), so in-process rotation is safe.
        handler = RotatingFileHandler(
            log_dir / STREAM_LOG_NAME,
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
            delay=False,
        )
        handler.setFormatter(logging.Formatter(STREAM_LOG_FORMAT))
        handler.setLevel(logging.INFO)
        signals_logger = logging.getLogger("Signals")
        if signals_logger.level == logging.NOTSET or signals_logger.level > logging.INFO:
            signals_logger.setLevel(logging.INFO)
        signals_logger.addHandler(handler)

        # Native crashes / hangs: tracebacks of all threads go to their own file.
        self._fault_file = open(log_dir / "market_stream_faults.log", "a", encoding="utf-8")
        faulthandler.enable(file=self._fault_file, all_threads=True)
        logger.info("Logging to %s (pid=%s).", log_dir / STREAM_LOG_NAME, os.getpid())

        atexit.register(lambda: logger.info("Process exiting (atexit)."))

    def _install_signal_handlers(self):
        """Log (and cleanly stop on) termination signals such as Task Scheduler's End / logoff."""

        def _handle(signum, frame):
            self._stop_reason = f"signal {signum}"
            logger.warning("Received signal %s; stopping.", signum)
            raise KeyboardInterrupt

        for name in ("SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _handle)
            except (ValueError, OSError):
                logger.debug("Could not install handler for %s.", name, exc_info=True)

    def _arm_hang_dump(self):
        """(Re)start the watchdog; it only fires if the main loop stops making progress."""
        try:
            faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, repeat=True, file=self._fault_file)
        except Exception:
            logger.debug("Could not arm hang watchdog.", exc_info=True)

    def _log_heartbeat(self):
        now = time.monotonic()
        ticks = self._tick_count
        ticks_since = ticks - self._ticks_at_last_heartbeat
        self._ticks_at_last_heartbeat = ticks
        last_ok = (
            f"{int(now - self._last_broadcast_ok_at)}s ago"
            if self._last_broadcast_ok_at is not None
            else "never"
        )
        with self._pending_ticks_lock:
            pending = len(self._pending_ticks)
        logger.info(
            "Heartbeat: connected=%s up=%ss last_tick=%ss ago ticks_last_%ss=%s ticks_total=%s "
            "pending=%s last_broadcast_ok=%s broadcasts=%s symbols=%s connects=%s/%s threads=%s",
            self._manager is not None,
            int(now - self._connected_at) if self._manager is not None and self._connected_at else 0,
            int(now - self._last_tick_at),
            HEARTBEAT_SECONDS,
            ticks_since,
            ticks,
            pending,
            last_ok,
            self._broadcast_count,
            len(self._selected_symbols),
            self._connect_successes,
            self._connect_attempts,
            threading.active_count(),
        )

    def _connect_and_subscribe(self):
        """Open a fresh MT5 Manager connection and subscribe to ticks. True on success."""
        self._connect_attempts += 1
        attempt = self._connect_attempts
        started = time.monotonic()
        logger.info(
            "MT5 connect attempt #%s to %s (login=%s, timeout_ms=%s).",
            attempt, self._server, self._login, self._timeout_ms,
        )
        manager = None
        try:
            manager = MT5Manager.ManagerAPI()
            ok = self._do_connect(manager)
        except Exception:
            # Previously an unexpected error here killed the whole process; retry instead.
            logger.exception("MT5 connect attempt #%s raised an unexpected error.", attempt)
            if manager is not None:
                self._safe_disconnect(manager)
            return False

        elapsed = time.monotonic() - started
        if ok:
            self._connect_successes += 1
            self._connected_at = time.monotonic()
            logger.info(
                "MT5 connect attempt #%s succeeded in %.1fs: %s symbol(s) subscribed (connection #%s).",
                attempt, elapsed, len(self._selected_symbols), self._connect_successes,
            )
        else:
            logger.warning("MT5 connect attempt #%s failed after %.1fs.", attempt, elapsed)
        return ok

    def _do_connect(self, manager):
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
            logger.error("MT5 Manager Connect failed: %s", getattr(MT5Manager, "LastError", lambda: "")())
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
            logger.error("MT5 TickSubscribe failed: %s", getattr(MT5Manager, "LastError", lambda: "")())
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
            ("TickUnsubscribe", lambda: manager.TickUnsubscribe(self._tick_sink)),
            ("Unsubscribe", lambda: manager.Unsubscribe(self._manager_sink)),
            ("Disconnect", manager.Disconnect),
        )

        def run_steps():
            for name, step in steps:
                try:
                    step()
                except Exception:
                    logger.debug("MT5 Manager cleanup step %s failed.", name, exc_info=True)

        # Calls on a dead MT5 connection can block forever; run them in a helper thread so
        # the reconnect is never held up by them.
        worker = threading.Thread(target=run_steps, name="signals-mt5-cleanup", daemon=True)
        worker.start()
        worker.join(timeout=CLEANUP_TIMEOUT_SECONDS)
        if worker.is_alive():
            logger.warning(
                "MT5 Manager cleanup did not finish within %ss; abandoning the old connection.",
                CLEANUP_TIMEOUT_SECONDS,
            )

    def _disconnect(self):
        manager, self._manager = self._manager, None
        if manager is not None:
            logger.info("Disconnecting from MT5 Manager.")
            self._safe_disconnect(manager)
            logger.info("Disconnected from MT5 Manager.")

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
                self_outer._tick_count += 1
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
            try:
                self._flush_pending_ticks()
            except Exception:
                # An uncaught error would silently end this thread and freeze all prices.
                logger.exception("Tick dispatcher iteration failed; continuing.")

        try:
            self._flush_pending_ticks()
        except Exception:
            logger.exception("Final tick flush failed.")

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
            send_started = time.monotonic()
            try:
                async_to_sync(self._channel_layer.group_send)(
                    MARKET_DATA_GROUP_NAME,
                    {
                        "type": "market.ticks",
                        "ticks": chunk,
                    },
                )
                send_elapsed = time.monotonic() - send_started
                self._broadcast_count += 1
                self._last_broadcast_ok_at = time.monotonic()
                if send_elapsed >= SLOW_BROADCAST_SECONDS:
                    logger.warning(
                        "Slow websocket broadcast: %.1fs for %s tick(s) (Redis slow?).",
                        send_elapsed, len(chunk),
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

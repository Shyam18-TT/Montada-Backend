"""
Price alert worker: checks open signals against live MT5 prices.
When price hits take_profit or stop_loss, closes the signal, notifies the analyst
(in-app + FCM push).

Price source (choose one):
  --use-mt5           MetaTrader5 Python package (local terminal, must be running).
  --use-mt5-manager   MT5 Manager API: connect to server (server:port, login, password).
  (default)           mt5clients DB (mt5_prices table). No MT5 terminal needed.

For --use-mt5-manager set in settings/env: MT5_MANAGER_SERVER, MT5_MANAGER_LOGIN, MT5_MANAGER_PASSWORD.

Usage:
  python manage.py run_price_alerts                         # loop every 15s, use DB
  python manage.py run_price_alerts --use-mt5               # use local MT5 terminal
  python manage.py run_price_alerts --use-mt5-manager       # use MT5 Manager API
  python manage.py run_price_alerts --interval 30 --once
"""
import logging
import os
import threading
import time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import connections

logger = logging.getLogger(__name__)

# Optional: MetaTrader5 package (pip install MetaTrader5)
try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    _MT5_AVAILABLE = False

# Optional: MT5 Manager API (pip install MT5Manager) for server connection + tick stream
try:
    import MT5Manager
    _MT5_MANAGER_AVAILABLE = True
except ImportError:
    MT5Manager = None
    _MT5_MANAGER_AVAILABLE = False

# Shared state for MT5 Manager background thread
_manager_tick_cache = {}
_manager_cache_lock = threading.Lock()
_manager_required_symbols = set()
_manager_shutdown = threading.Event()
_manager_thread = None
_manager_connected = False
_manager_first_tick_logged = set()  # symbols we already printed "first tick" for


def _normalize_mt5_symbol(instrument_symbol):
    """Convert Instrument.symbol (e.g. EUR/USD) to MT5 format (e.g. EURUSD)."""
    if not instrument_symbol:
        return ""
    return (instrument_symbol or "").replace("/", "").replace(" ", "").upper()


# Map normalized symbol (from Instrument) -> extra names to try (mt5_prices / MT5 may use different names)
SYMBOL_ALIASES_DB = {
    "GOLDUSD": ["GOLD", "XAUUSD"],
    "SILVERUSD": ["SILVER", "XAGUSD"],
    "XAUUSD": ["GOLD"],
    "XAGUSD": ["SILVER"],
    "S&P500": ["US500", "SP500"],
    "SP500": ["US500"],
    "DOWJONES": ["US30", "DOW"],
    "NIFTY50": ["NIFTY", "NIFTY50"],
    "DOGEUSD": ["DOGE", "DOGUSD"],
}


def _manager_tick_sink_class():
    """Build a TickSink class that writes into _manager_tick_cache (thread-safe)."""

    class TickSink:
        def OnTick(self, symbol, tick):  # noqa: N802 - MT5Manager callback name
            global _manager_first_tick_logged
            try:
                bid = getattr(tick, "bid", None)
                ask = getattr(tick, "ask", None)
                if symbol and (bid is not None or ask is not None):
                    with _manager_cache_lock:
                        _manager_tick_cache[symbol] = {"bid": float(bid) if bid is not None else None, "ask": float(ask) if ask is not None else None}
                        if symbol not in _manager_first_tick_logged:
                            _manager_first_tick_logged.add(symbol)
                            print("[CHECK] MT5 Manager: first tick received for %s (bid=%s)" % (symbol, bid))
            except Exception as e:
                logger.debug("Manager OnTick for %s: %s", symbol, e)

        def OnTickStat(self, stat):  # noqa: N802 - MT5Manager callback
            pass

    return TickSink


def _manager_thread_func(server, login, password, command_stdout, style_success, style_warning):
    """Background thread: connect to MT5 server, subscribe to ticks, keep adding required symbols."""
    global _manager_connected
    if not _MT5_MANAGER_AVAILABLE or MT5Manager is None:
        return
    try:
        manager = MT5Manager.ManagerAPI()
        sink = _manager_tick_sink_class()()
        if not manager.TickSubscribe(sink):
            err = getattr(MT5Manager, "LastError", lambda: "")()
            with _manager_cache_lock:
                _manager_connected = False
            return
        # Connect(server, login, password, pump_mode, timeout_ms)
        EnPump = getattr(MT5Manager.ManagerAPI, "EnPumpModes", None)
        pump_mode = getattr(EnPump, "PUMP_MODE_SYMBOLS", None) or getattr(EnPump, "PUMP_MODE_USERS", 0)
        timeout_ms = 60000
        if not manager.Connect(server, int(login), str(password), pump_mode, timeout_ms):
            err = getattr(MT5Manager, "LastError", lambda: "")()
            if command_stdout:
                command_stdout.write(style_warning("MT5 Manager Connect failed: %s" % err))
            manager.TickUnsubscribe(sink)
            with _manager_cache_lock:
                _manager_connected = False
            return
        with _manager_cache_lock:
            _manager_connected = True
        if command_stdout:
            command_stdout.write(style_success("[CHECK] MT5 Manager: Connected to server. Waiting for ticks..."))
        added = set()
        failed_logged = set()  # symbols we already printed "SelectedAdd failed" for
        while not _manager_shutdown.is_set():
            with _manager_cache_lock:
                to_add = _manager_required_symbols - added
            for sym in to_add:
                if manager.SelectedAdd(sym):
                    added.add(sym)
                    if command_stdout:
                        command_stdout.write(style_success("[CHECK] MT5 Manager: subscribed to symbol %s" % sym))
                else:
                    if sym not in failed_logged and command_stdout:
                        err = getattr(MT5Manager, "LastError", lambda: "")()
                        command_stdout.write(style_warning("[CHECK] MT5 Manager: SelectedAdd(%s) failed: %s" % (sym, err)))
                        failed_logged.add(sym)
            _manager_shutdown.wait(timeout=1.0)
        manager.Disconnect()
        manager.TickUnsubscribe(sink)
    except Exception as e:
        logger.exception("MT5 Manager thread error: %s", e)
    finally:
        with _manager_cache_lock:
            _manager_connected = False


def _get_prices_from_mt5_manager(symbols):
    """Read last tick (bid/ask) from Manager API cache. Returns dict symbol -> {'bid', 'ask'}."""
    result = {}
    with _manager_cache_lock:
        for sym in symbols:
            if sym in _manager_tick_cache:
                result[sym] = dict(_manager_tick_cache[sym])
            else:
                for alt in SYMBOL_ALIASES_DB.get(sym, []):
                    if alt in _manager_tick_cache:
                        result[sym] = dict(_manager_tick_cache[alt])
                        break
    return result


def _get_prices_from_mt5_lib(symbols):
    """
    Get bid/ask from MetaTrader5 Python package (mt5.symbol_info_tick).
    symbols: list of str (e.g. ['EURUSD', 'XAUUSD']).
    Returns dict: symbol -> {'bid': float, 'ask': float or None}.
    Tries broker aliases (e.g. GOLD for GOLDUSD) via SYMBOL_ALIASES_DB.
    """
    if not _MT5_AVAILABLE or not symbols:
        return {}
    result = {}
    for sym in symbols:
        try:
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                for variant in [sym + ".", sym + "m", sym + "M"]:
                    tick = mt5.symbol_info_tick(variant)
                    if tick is not None:
                        break
            if tick is None:
                for alt in SYMBOL_ALIASES_DB.get(sym, []):
                    tick = mt5.symbol_info_tick(alt)
                    if tick is not None:
                        break
            if tick is not None:
                result[sym] = {"bid": getattr(tick, "bid", None), "ask": getattr(tick, "ask", None)}
        except Exception as e:
            logger.debug("MT5 tick for %s failed: %s", sym, e)
    return result


def _get_prices_from_mt5_db(symbols):
    """
    Query mt5clients.mt5_prices for BidLast (and AskLast) by Symbol.
    symbols: list of str (e.g. ['EURUSD', 'XAUUSD']).
    Returns dict: symbol -> {'bid': float, 'ask': float or None}.
    """
    if not symbols:
        return {}
    result = {}
    try:
        placeholders = ",".join(["%s"] * len(symbols))
        sql = "SELECT Symbol, BidLast, AskLast FROM mt5_prices WHERE Symbol IN (%s)" % placeholders
        with connections["mt5clients"].cursor() as cursor:
            cursor.execute(sql, symbols)
            for row in cursor.fetchall():
                sym = (row[0] or "").strip()
                try:
                    bid = float(row[1]) if row[1] is not None else None
                except (TypeError, ValueError):
                    bid = None
                try:
                    ask = float(row[2]) if row[2] is not None else None
                except (TypeError, ValueError):
                    ask = None
                result[sym] = {"bid": bid, "ask": ask}
    except Exception as e:
        logger.exception("MT5 DB query failed: %s", e)
    return result


def _get_mt5_db_available_symbols(limit=80):
    """Return a list of Symbol values present in mt5_prices (for diagnostic when 0 prices)."""
    try:
        # MSSQL: TOP n; MySQL: LIMIT n
        with connections["mt5clients"].cursor() as cursor:
            cursor.execute("SELECT DISTINCT Symbol FROM mt5_prices")
            rows = cursor.fetchall()
        symbols = [(str(r[0] or "").strip()) for r in (rows[:limit] if rows else []) if r and (r[0] or "").strip()]
        return symbols
    except Exception as e:
        logger.debug("Could not list mt5_prices symbols: %s", e)
        return []


def _check_signal_hit(signal, bid, ask=None):
    """
    Return 'tp' if take_profit hit, 'sl' if stop_loss hit, None otherwise.
    BUY (close by selling): use bid. TP when bid >= take_profit, SL when bid <= stop_loss.
    SELL (close by buying): use ask when available else bid. TP when price <= take_profit, SL when price >= stop_loss.
    """
    direction = (signal.direction or "").upper()
    # For SELL we close by buying → relevant price is ask; fall back to bid
    use_price = ask if direction == "SELL" and ask is not None else bid
    if use_price is None:
        return None
    try:
        price = Decimal(str(use_price))
        tp = signal.take_profit
        sl = signal.stop_loss
    except Exception:
        return None

    if direction == "BUY":
        if tp is not None and price >= tp:
            return "tp"
        if sl is not None and price <= sl:
            return "sl"
    elif direction == "SELL":
        if tp is not None and price <= tp:
            return "tp"
        if sl is not None and price >= sl:
            return "sl"
    return None


def _close_signal_and_notify(signal, hit_type, current_price):
    """Close the signal, create UserNotification for analyst, send FCM push (only once per signal)."""
    from Signals.models import TradingSignal
    from Mainapp.models import UserNotification

    signal.refresh_from_db()
    if getattr(signal, "price_alert_fcm_sent", False):
        # FCM already sent for this signal (e.g. previous run or duplicate); only ensure closed
        if signal.status != TradingSignal.Status.CLOSED:
            signal.status = TradingSignal.Status.CLOSED
            signal.is_win = hit_type == "tp"
            signal.is_loss = hit_type == "sl"
            signal.save(update_fields=["status", "is_win", "is_loss", "updated_at"])
        logger.debug("Signal %s: price_alert_fcm_sent already True, skipping notification.", signal.id)
        return

    is_win = hit_type == "tp"
    is_loss = hit_type == "sl"
    label = "Take profit" if is_win else "Stop loss"
    instrument = getattr(signal, "instrument", None)
    symbol = instrument.symbol if instrument else "Unknown"
    analyst = signal.analyst

    print("[CHECK] Step 4 (Notify): Closing signal %s (%s at %s), notifying analyst %s" % (signal.id, hit_type, current_price, getattr(analyst, "email", analyst.pk)))

    # Update signal (close it)
    signal.status = TradingSignal.Status.CLOSED
    signal.is_win = is_win
    signal.is_loss = is_loss
    signal.save(update_fields=["status", "is_win", "is_loss", "updated_at"])
    print("[CHECK] Step 4a: Signal closed in DB (status=CLOSED, is_win=%s, is_loss=%s)." % (is_win, is_loss))

    title = f"{label} reached – {symbol}"
    message = (
        f"Price reached {label.lower()} for {symbol} at {current_price}. "
        f"Entry: {signal.entry_price} | TP: {signal.take_profit} | SL: {signal.stop_loss}"
    )

    # In-app notification (once)
    try:
        UserNotification.objects.create(
            user=analyst,
            title=title,
            message=message,
            notification_type="SUCCESS" if is_win else "WARNING",
        )
        print("[CHECK] Step 4b: UserNotification created for analyst.")
    except Exception as e:
        logger.warning("UserNotification create failed: %s", e)
        print("[CHECK] Step 4b: UserNotification create FAILED: %s" % e)

    # FCM push to analyst (once)
    try:
        from firebase import send_push_to_users
        send_push_to_users(
            users=[analyst],
            title=title,
            body=message,
            data={
                "type": "price_alert",
                "signal_id": str(signal.id),
                "hit": hit_type,
                "symbol": symbol,
                "price": str(current_price),
            },
        )
        signal.price_alert_fcm_sent = True
        signal.save(update_fields=["price_alert_fcm_sent"])
        print("[CHECK] Step 4c: FCM push sent to analyst.")
    except Exception as e:
        logger.warning("FCM push to analyst failed: %s", e)
        print("[CHECK] Step 4c: FCM push FAILED: %s" % e)

    logger.info("Signal %s %s hit for %s at %s – analyst notified", signal.id, hit_type, symbol, current_price)


def _trigger_user_price_alert(alert, current_price):
    """Mark alert as triggered, create UserNotification and send FCM to the user who set the alert."""
    from Signals.models import PriceAlert
    from Mainapp.models import UserNotification

    alert.refresh_from_db()
    if alert.is_triggered:
        logger.debug("PriceAlert %s already triggered, skipping.", alert.id)
        return
    symbol = alert.instrument.symbol if alert.instrument else "Unknown"
    cond = (alert.condition or "above").lower()
    label = alert.label or f"{symbol} {cond} {alert.target_price}"

    from django.utils import timezone as tz
    now = tz.now()
    alert.is_triggered = True
    alert.triggered_at = now
    alert.save(update_fields=["is_triggered", "triggered_at", "updated_at"])

    title = f"Price alert – {symbol}"
    message = f"{symbol} reached your target: price is {current_price} ({cond} {alert.target_price})."

    try:
        UserNotification.objects.create(
            user=alert.user,
            title=title,
            message=message,
            notification_type="SUCCESS",
        )
    except Exception as e:
        logger.warning("UserNotification create for price alert failed: %s", e)

    try:
        from firebase import send_push_to_users
        send_push_to_users(
            users=[alert.user],
            title=title,
            body=message,
            data={
                "type": "user_price_alert",
                "alert_id": str(alert.id),
                "symbol": symbol,
                "target_price": str(alert.target_price),
                "condition": cond,
                "current_price": str(current_price),
            },
        )
    except Exception as e:
        logger.warning("FCM push for user price alert failed: %s", e)

    logger.info("PriceAlert %s triggered for %s at %s – user %s notified", alert.id, symbol, current_price, alert.user_id)


def _check_user_alert_hit(alert, bid):
    """Return True if current price meets the alert condition (above/below target)."""
    if bid is None:
        return False
    target = alert.target_price
    cond = (alert.condition or "above").lower()
    if cond == "above":
        return bid >= target
    if cond == "below":
        return bid <= target
    return False


class Command(BaseCommand):
    help = "Run price alerts: check open signals against MT5 prices and notify analysts when TP/SL is hit."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=15,
            help="Seconds between each check (default 15).",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one check and exit (for cron).",
        )
        parser.add_argument(
            "--use-mt5",
            action="store_true",
            help="Use MetaTrader5 Python package (mt5.symbol_info_tick). Requires MT5 terminal running.",
        )
        parser.add_argument(
            "--use-mt5-manager",
            action="store_true",
            help="Use MT5 Manager API (connect to server). Set MT5_MANAGER_SERVER, MT5_MANAGER_LOGIN, MT5_MANAGER_PASSWORD in settings/env.",
        )
        parser.add_argument(
            "--mt5-path",
            type=str,
            default="",
            help="Path to MT5 terminal executable (e.g. C:\\Program Files\\MetaTrader 5\\terminal64.exe). Use if initialize() fails.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print each signal and current price on every check.",
        )

    def handle(self, *args, **options):
        global _manager_thread, _manager_shutdown, _manager_required_symbols

        interval = max(1, options["interval"])
        run_once = options["once"]
        use_mt5 = options["use_mt5"]
        use_mt5_manager = options.get("use_mt5_manager", False)
        mt5_path = (options.get("mt5_path") or "").strip()
        self._verbose = options.get("verbose", False)

        self._use_mt5_lib = False
        self._use_mt5_manager = False

        if use_mt5_manager:
            if not _MT5_MANAGER_AVAILABLE:
                self.stderr.write(
                    self.style.WARNING("MT5Manager package not installed. pip install MT5Manager. Using DB.")
                )
            else:
                from django.conf import settings
                server = getattr(settings, "MT5_MANAGER_SERVER", None) or os.environ.get("MT5_MANAGER_SERVER", "")
                login = getattr(settings, "MT5_MANAGER_LOGIN", None) or os.environ.get("MT5_MANAGER_LOGIN", "")
                password = getattr(settings, "MT5_MANAGER_PASSWORD", None) or os.environ.get("MT5_MANAGER_PASSWORD", "")
                if not server or not str(login).strip() or not password:
                    self.stderr.write(
                        self.style.WARNING(
                            "MT5 Manager requires MT5_MANAGER_SERVER, MT5_MANAGER_LOGIN, MT5_MANAGER_PASSWORD (settings or env). Using DB."
                        )
                    )
                else:
                    _manager_shutdown.clear()
                    _manager_required_symbols.clear()
                    with _manager_cache_lock:
                        _manager_tick_cache.clear()
                    _manager_thread = threading.Thread(
                        target=_manager_thread_func,
                        args=(server.strip(), login, password, self.stdout, self.style.SUCCESS, self.style.WARNING),
                        daemon=True,
                    )
                    _manager_thread.start()
                    # Give manager time to connect and receive first ticks
                    time.sleep(3)
                    self._use_mt5_manager = True
                    self.stdout.write(self.style.SUCCESS("Using MT5 Manager API for prices (server=%s)." % server.strip()))

        if use_mt5 and not self._use_mt5_manager:
            if not _MT5_AVAILABLE:
                self.stderr.write(self.style.WARNING("MetaTrader5 package not installed. Install with: pip install MetaTrader5. Using DB."))
            else:
                # 1) Try attach to already-running terminal first (no path). Passing path often
                #    makes the library try to *launch* the terminal and fail with -10003.
                ok = mt5.initialize()
                if not ok and mt5_path:
                    ok = mt5.initialize(path=mt5_path)
                if not ok:
                    for try_path in [
                        "C:/Program Files/MetaTrader 5/terminal64.exe",
                        "C:/Program Files (x86)/MetaTrader 5/terminal64.exe",
                    ]:
                        ok = mt5.initialize(path=try_path)
                        if ok:
                            break
                if ok:
                    self._use_mt5_lib = True
                    self.stdout.write(self.style.SUCCESS("Using MetaTrader5 (mt5.symbol_info_tick) for prices."))
                else:
                    err = getattr(mt5, "last_error", lambda: None)()
                    if err and isinstance(err, (tuple, list)) and len(err) >= 2:
                        err_msg = "code=%s %s" % (err[0], err[1])
                    else:
                        err_msg = str(err) if err else "unknown"
                    self.stderr.write(
                        self.style.WARNING("MT5 initialize() failed. Error: %s. Using DB." % err_msg)
                    )
                    self.stderr.write(
                        "\nTo use MT5:\n"
                        "  1. Start MetaTrader 5 and log in to your broker.\n"
                        "  2. Run this command again without --mt5-path: python manage.py run_price_alerts --use-mt5\n"
                        "  3. In MT5: Tools → Options → Community → enable 'Allow Python integration'.\n"
                        "  4. Run Python as the same Windows user as the MT5 terminal.\n"
                    )
        if not self._use_mt5_lib and not self._use_mt5_manager:
            self.stdout.write("Using mt5clients DB (mt5_prices) for prices.")

        # [CHECK] Connection in use
        if self._use_mt5_manager:
            self.stdout.write("[CHECK] Connection: MT5 Manager API (server from settings/env).")
        elif self._use_mt5_lib:
            self.stdout.write("[CHECK] Connection: MetaTrader5 terminal (local).")
        else:
            self.stdout.write("[CHECK] Connection: mt5clients DB (mt5_prices table).")

        try:
            while True:
                try:
                    self._run_check()
                except Exception as e:
                    logger.exception("Price alert check failed: %s", e)

                if run_once:
                    break
                time.sleep(interval)
        finally:
            if self._use_mt5_manager and _MT5_MANAGER_AVAILABLE:
                _manager_shutdown.set()
                if _manager_thread and _manager_thread.is_alive():
                    _manager_thread.join(timeout=5.0)

        if self._use_mt5_lib and _MT5_AVAILABLE:
            try:
                mt5.shutdown()
            except Exception:
                pass

    def _run_check(self):
        from django.utils import timezone as tz

        from Signals.models import TradingSignal, PriceAlert

        now = tz.now().strftime("%Y-%m-%d %H:%M:%S")
        if getattr(self, "_use_mt5_manager", False):
            price_source = "MT5 Manager"
        elif getattr(self, "_use_mt5_lib", False):
            price_source = "MT5"
        else:
            price_source = "DB (mt5_prices)"

        # [CHECK] Step 1: Get open signals and active user price alerts
        self.stdout.write("[CHECK] Step 1: Fetching open signals and active price alerts from DB...")
        signals = list(
            TradingSignal.active.filter(status=TradingSignal.Status.OPEN)
            .select_related("analyst", "instrument")
            .only(
                "id", "analyst_id", "direction", "entry_price", "take_profit", "stop_loss",
                "instrument_id", "status", "is_win", "is_loss",
            )
        )
        alerts = list(
            PriceAlert.objects.filter(is_triggered=False)
            .select_related("instrument", "user")
        )
        self.stdout.write("[CHECK] Step 1 done: %d open signal(s), %d active price alert(s)." % (len(signals), len(alerts)))

        # Unique MT5 symbols from signals and alerts; mappings for both
        mt5_symbols = set()
        signal_to_mt5 = {}
        alert_to_mt5 = {}
        for s in signals:
            inst = s.instrument
            if not inst or not inst.symbol:
                continue
            mt5_sym = _normalize_mt5_symbol(inst.symbol)
            if not mt5_sym:
                continue
            mt5_symbols.add(mt5_sym)
            signal_to_mt5[s.id] = mt5_sym
        for a in alerts:
            inst = a.instrument
            if not inst or not inst.symbol:
                continue
            mt5_sym = _normalize_mt5_symbol(inst.symbol)
            if not mt5_sym:
                continue
            mt5_symbols.add(mt5_sym)
            alert_to_mt5[a.id] = mt5_sym

        if not mt5_symbols:
            self.stdout.write("[%s] No open signals or active alerts with valid symbols. Skipping." % now)
            return

        symbol_list = sorted(mt5_symbols)
        self.stdout.write("[CHECK] Step 2: Getting current prices for symbols: %s" % symbol_list)
        if getattr(self, "_use_mt5_manager", False):
            with _manager_cache_lock:
                _manager_required_symbols.update(symbol_list)
                for s in symbol_list:
                    _manager_required_symbols.update(SYMBOL_ALIASES_DB.get(s, []))
            prices = _get_prices_from_mt5_manager(symbol_list)
        elif getattr(self, "_use_mt5_lib", False):
            prices = _get_prices_from_mt5_lib(symbol_list)
        else:
            # DB may use different names (e.g. GOLD not GOLDUSD); try aliases
            expanded = set(symbol_list)
            for s in symbol_list:
                expanded.update(SYMBOL_ALIASES_DB.get(s, []))
            raw = _get_prices_from_mt5_db(list(expanded))
            prices = {}
            for s in symbol_list:
                if s in raw:
                    prices[s] = raw[s]
                else:
                    for alt in SYMBOL_ALIASES_DB.get(s, []):
                        if alt in raw:
                            prices[s] = raw[alt]
                            break

        # If MT5 Manager returned 0 prices, try DB (mt5_prices) as fallback so alerts still work
        if not prices and getattr(self, "_use_mt5_manager", False):
            expanded = set(symbol_list)
            for s in symbol_list:
                expanded.update(SYMBOL_ALIASES_DB.get(s, []))
            raw = _get_prices_from_mt5_db(list(expanded))
            prices = {}
            for s in symbol_list:
                if s in raw:
                    prices[s] = raw[s]
                else:
                    for alt in SYMBOL_ALIASES_DB.get(s, []):
                        if alt in raw:
                            prices[s] = raw[alt]
                            break
            if prices:
                price_source = "DB (mt5_prices, fallback)"
                self.stdout.write(
                    self.style.WARNING(
                        "[%s] MT5 Manager had no prices for %s (symbols not on server). Using DB fallback: got %d price(s)."
                        % (now, symbol_list, len(prices))
                    )
                )

        if not prices:
            self.stdout.write(
                self.style.WARNING(
                    "[%s] Fetched 0 prices from %s for symbols %s." % (now, price_source, symbol_list)
                )
            )
            if price_source == "DB (mt5_prices)":
                available = _get_mt5_db_available_symbols()
                if available:
                    self.stdout.write("  Symbols in mt5_prices (sample): %s" % available[:30])
            elif "MT5 Manager" in price_source:
                with _manager_cache_lock:
                    connected = _manager_connected
                    cached = list(_manager_tick_cache.keys())[:10]
                self.stdout.write(
                    "  Manager connected=%s. Cached symbols (sample): %s. Requested: %s."
                    % (connected, cached if cached else "none", symbol_list)
                )
                self.stdout.write(
                    "  Server does not have these symbols (Not found). Use a server that has them, or run without --use-mt5-manager to use DB only."
                )
            else:
                self.stdout.write("  Check DB/MT5 connection and symbol names.")
            return

        self.stdout.write("[CHECK] Step 2 done: Got prices from %s for %d symbol(s)." % (price_source, len(prices)))

        # Stats line every run
        self.stdout.write(
            "[%s] Open signals: %d | Alerts: %d | Symbols: %s | Prices from %s: %s"
            % (now, len(signals), len(alerts), symbol_list, price_source, {s: prices[s].get("bid") for s in symbol_list if s in prices})
        )

        self.stdout.write("[CHECK] Step 3: Checking each signal (current price vs TP/SL)...")
        if self._verbose:
            self.stdout.write("  Signals being checked:")

        for signal in signals:
            mt5_sym = signal_to_mt5.get(signal.id)
            if not mt5_sym:
                if self._verbose:
                    self.stdout.write("  - Signal %s: no instrument/symbol, skip" % signal.id)
                continue
            quote = prices.get(mt5_sym)
            if not quote:
                if self._verbose:
                    self.stdout.write("  - Signal %s %s: no price for %s" % (signal.id, mt5_sym, mt5_sym))
                continue
            bid = quote.get("bid")
            ask = quote.get("ask")
            hit = _check_signal_hit(signal, bid, ask)
            # Price used for the hit (bid for BUY, ask for SELL when available)
            used_price = ask if (signal.direction or "").upper() == "SELL" and ask is not None else bid
            self.stdout.write(
                "[CHECK]   Signal %s | %s | price=%s | TP=%s SL=%s -> %s"
                % (signal.id, mt5_sym, used_price, signal.take_profit, signal.stop_loss, hit.upper() if hit else "ok")
            )
            if self._verbose:
                inst = signal.instrument
                sym = inst.symbol if inst else mt5_sym
                status = "HIT %s" % hit.upper() if hit else "ok"
                self.stdout.write(
                    "  - %s | %s %s | entry=%s TP=%s SL=%s | bid=%s | %s"
                    % (signal.id, sym, signal.direction, signal.entry_price, signal.take_profit, signal.stop_loss, used_price, status)
                )
            if hit:
                _close_signal_and_notify(signal, hit, used_price)
                self.stdout.write(self.style.SUCCESS("  >>> Closed signal %s (%s) at %s" % (signal.id, hit, used_price)))

        # Step 4: Check user price alerts
        if alerts:
            self.stdout.write("[CHECK] Step 4: Checking user price alerts...")
            for alert in alerts:
                mt5_sym = alert_to_mt5.get(alert.id)
                if not mt5_sym:
                    continue
                quote = prices.get(mt5_sym)
                if not quote:
                    continue
                bid = quote.get("bid")
                if _check_user_alert_hit(alert, bid):
                    _trigger_user_price_alert(alert, bid)
                    self.stdout.write(
                        self.style.SUCCESS("  >>> Price alert %s triggered for %s at %s (user %s)" % (alert.id, mt5_sym, bid, alert.user_id))
                    )

"""
Price alert worker: checks open signals against live MT5 prices.
When price hits take_profit or stop_loss, closes the signal, notifies the analyst
(in-app + FCM push).

Price source (choose one):
  --use-mt5   Use MetaTrader5 Python package (mt5.symbol_info_tick). Requires MT5
              terminal installed and running on this machine. Real-time tick data.
  (default)   Use mt5clients DB (mt5_prices table). No MT5 terminal needed.

Usage:
  python manage.py run_price_alerts                    # loop every 15s, use DB
  python manage.py run_price_alerts --use-mt5           # use MT5 package (like your script)
  python manage.py run_price_alerts --interval 30
  python manage.py run_price_alerts --once              # run once and exit (for cron)
"""
import logging
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


def _normalize_mt5_symbol(instrument_symbol):
    """Convert Instrument.symbol (e.g. EUR/USD) to MT5 format (e.g. EURUSD)."""
    if not instrument_symbol:
        return ""
    return (instrument_symbol or "").replace("/", "").replace(" ", "").upper()


# Map normalized symbol (from Instrument) -> extra DB symbol names to try (mt5_prices may use different names)
SYMBOL_ALIASES_DB = {
    "GOLDUSD": ["GOLD", "XAUUSD"],
    "SILVERUSD": ["SILVER", "XAGUSD"],
    "XAUUSD": ["GOLD"],
    "XAGUSD": ["SILVER"],
    "S&P500": ["US500", "SP500"],
    "SP500": ["US500"],
    "DOWJONES": ["US30", "DOW"],
    "NIFTY50": ["NIFTY", "NIFTY50"],
    "DOGEUSD": ["DOGE"],
}


def _get_prices_from_mt5_lib(symbols):
    """
    Get bid/ask from MetaTrader5 Python package (mt5.symbol_info_tick).
    symbols: list of str (e.g. ['EURUSD', 'XAUUSD']).
    Returns dict: symbol -> {'bid': float, 'ask': float or None}.
    """
    if not _MT5_AVAILABLE or not symbols:
        return {}
    result = {}
    for sym in symbols:
        try:
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                # Some brokers use suffix, e.g. EURUSD. or EURUSDm
                for variant in [sym + ".", sym + "m", sym + "M"]:
                    tick = mt5.symbol_info_tick(variant)
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


def _check_signal_hit(signal, bid):
    """
    Return 'tp' if take_profit hit, 'sl' if stop_loss hit, None otherwise.
    BUY: TP when bid >= take_profit, SL when bid <= stop_loss.
    SELL: TP when bid <= take_profit, SL when bid >= stop_loss.
    """
    if bid is None:
        return None
    try:
        price = Decimal(str(bid))
        tp = signal.take_profit
        sl = signal.stop_loss
        direction = (signal.direction or "").upper()
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
    """Close the signal, create UserNotification for analyst, send FCM push."""
    from Signals.models import TradingSignal
    from Mainapp.models import UserNotification

    is_win = hit_type == "tp"
    is_loss = hit_type == "sl"
    label = "Take profit" if is_win else "Stop loss"
    instrument = getattr(signal, "instrument", None)
    symbol = instrument.symbol if instrument else "Unknown"

    # Update signal
    signal.status = TradingSignal.Status.CLOSED
    signal.is_win = is_win
    signal.is_loss = is_loss
    signal.save(update_fields=["status", "is_win", "is_loss", "updated_at"])

    analyst = signal.analyst
    title = f"{label} reached – {symbol}"
    message = (
        f"Price reached {label.lower()} for {symbol} at {current_price}. "
        f"Entry: {signal.entry_price} | TP: {signal.take_profit} | SL: {signal.stop_loss}"
    )

    # In-app notification
    try:
        UserNotification.objects.create(
            user=analyst,
            title=title,
            message=message,
            notification_type="SUCCESS" if is_win else "WARNING",
        )
    except Exception as e:
        logger.warning("UserNotification create failed: %s", e)

    # FCM push to analyst
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
    except Exception as e:
        logger.warning("FCM push to analyst failed: %s", e)

    logger.info("Signal %s %s hit for %s at %s – analyst notified", signal.id, hit_type, symbol, current_price)


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
        interval = max(1, options["interval"])
        run_once = options["once"]
        use_mt5 = options["use_mt5"]
        mt5_path = (options.get("mt5_path") or "").strip()
        self._verbose = options.get("verbose", False)

        self._use_mt5_lib = False
        if use_mt5:
            if not _MT5_AVAILABLE:
                self.stderr.write(self.style.WARNING("MetaTrader5 package not installed. Install with: pip install MetaTrader5. Using DB."))
            else:
                # Prefer connecting to an already-running terminal (no path). -10003 often
                # happens when the library tries to *launch* the terminal; attaching works.
                ok = False
                if mt5_path:
                    ok = mt5.initialize(path=mt5_path)
                if not ok:
                    ok = mt5.initialize()  # no path: attach to running MT5
                if not ok:
                    # Last resort: try common Windows install paths
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
                        self.style.WARNING(
                            "MT5 initialize() failed. Error: %s. Using DB." % err_msg
                        )
                    )
                    self.stderr.write(
                        "\nTip: Start the MetaTrader 5 terminal and log in, then run this command again (no --mt5-path). "
                        "Enable Tools → Options → Community → Allow Python integration.\n"
                    )
        if not self._use_mt5_lib:
            self.stdout.write("Using mt5clients DB (mt5_prices) for prices.")

        while True:
            try:
                self._run_check()
            except Exception as e:
                logger.exception("Price alert check failed: %s", e)

            if run_once:
                break
            time.sleep(interval)

        if self._use_mt5_lib and _MT5_AVAILABLE:
            try:
                mt5.shutdown()
            except Exception:
                pass

    def _run_check(self):
        from django.utils import timezone as tz

        from Signals.models import TradingSignal

        now = tz.now().strftime("%Y-%m-%d %H:%M:%S")
        price_source = "MT5" if getattr(self, "_use_mt5_lib", False) else "DB (mt5_prices)"

        # Open, non-deleted signals with instrument
        signals = list(
            TradingSignal.active.filter(status=TradingSignal.Status.OPEN)
            .select_related("analyst", "instrument")
            .only(
                "id", "analyst_id", "direction", "entry_price", "take_profit", "stop_loss",
                "instrument_id", "status", "is_win", "is_loss",
            )
        )

        if not signals:
            self.stdout.write("[%s] No open signals in DB. Skipping check." % now)
            return

        # Unique MT5 symbols and mapping signal -> mt5_symbol
        mt5_symbols = set()
        signal_to_mt5 = {}
        for s in signals:
            inst = s.instrument
            if not inst or not inst.symbol:
                continue
            mt5_sym = _normalize_mt5_symbol(inst.symbol)
            if not mt5_sym:
                continue
            mt5_symbols.add(mt5_sym)
            signal_to_mt5[s.id] = mt5_sym

        if not mt5_symbols:
            self.stdout.write("[%s] %d open signal(s) but no valid symbols. Skipping." % (now, len(signals)))
            return

        symbol_list = sorted(mt5_symbols)
        if getattr(self, "_use_mt5_lib", False):
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

        if not prices:
            self.stdout.write(
                self.style.WARNING(
                    "[%s] Fetched 0 prices from %s for symbols %s. Check DB/MT5." % (now, price_source, symbol_list)
                )
            )
            available = _get_mt5_db_available_symbols()
            if available:
                self.stdout.write("  Symbols in mt5_prices (sample): %s" % available[:30])
            return

        # Stats line every run
        self.stdout.write(
            "[%s] Open signals: %d | Symbols: %s | Prices from %s: %s"
            % (now, len(signals), symbol_list, price_source, {s: prices[s].get("bid") for s in symbol_list if s in prices})
        )

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
            hit = _check_signal_hit(signal, bid)
            if self._verbose:
                inst = signal.instrument
                sym = inst.symbol if inst else mt5_sym
                status = "HIT %s" % hit.upper() if hit else "ok"
                self.stdout.write(
                    "  - %s | %s %s | entry=%s TP=%s SL=%s | bid=%s | %s"
                    % (signal.id, sym, signal.direction, signal.entry_price, signal.take_profit, signal.stop_loss, bid, status)
                )
            if hit:
                _close_signal_and_notify(signal, hit, bid)
                self.stdout.write(self.style.SUCCESS("  >>> Closed signal %s (%s) at %s" % (signal.id, hit, bid)))

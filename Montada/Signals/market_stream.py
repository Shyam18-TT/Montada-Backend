import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import connections


MARKET_DATA_GROUP_NAME = "signals_market_data_stream"


def normalize_market_symbols(symbols):
    normalized = set()
    for symbol in symbols or []:
        cleaned = str(symbol or "").strip().lower()
        if cleaned:
            normalized.add(cleaned)
    return normalized


def should_deliver_market_tick(selected_symbols, symbol):
    if not selected_symbols:
        return True
    return str(symbol or "").strip().lower() in selected_symbols


DEFAULT_TRUSTCAPITAL_PRICE_URL = "https://trustcapital.com/api/get-MT5-price"


def _normalize_symbol_for_api(symbol):
    return str(symbol or "").strip().upper()


def fetch_trustcapital_open_prices(symbols=None, url=DEFAULT_TRUSTCAPITAL_PRICE_URL, timeout=15):
    normalized_symbols = {
        _normalize_symbol_for_api(symbol)
        for symbol in (symbols or [])
        if _normalize_symbol_for_api(symbol)
    }

    try:
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError, OSError):
        return {}

    if not isinstance(payload, dict):
        return {}

    live_quote = (payload.get("data") or {}).get("live_quote") or {}
    if not isinstance(live_quote, dict):
        return {}

    open_prices = {}
    for raw_symbol, quote in live_quote.items():
        symbol = _normalize_symbol_for_api(raw_symbol)
        if not symbol:
            continue
        if normalized_symbols and symbol not in normalized_symbols:
            continue
        if not isinstance(quote, dict):
            continue

        ask_today = quote.get("ask_today")
        bid_today = quote.get("bid_today")
        if ask_today is None or bid_today is None:
            continue

        open_prices[symbol] = {
            "ask_today": float(ask_today),
            "bid_today": float(bid_today),
        }

    return open_prices


TRUSTCAPITAL_OPEN_PRICES_CACHE_KEY = "market:trustcapital-open-prices"
TRUSTCAPITAL_OPEN_PRICES_CACHE_SECONDS = 60


def get_trustcapital_open_prices_cached(timeout=15):
    """
    bid_today / ask_today for every symbol, as published by the website's price API
    (the same reference prices the website's change_percentage uses). Cached briefly so
    API requests do not each call the website; failures are not cached.
    """
    from django.core.cache import cache

    try:
        cached = cache.get(TRUSTCAPITAL_OPEN_PRICES_CACHE_KEY)
    except Exception:
        cached = None
    if cached:
        return cached

    open_prices = fetch_trustcapital_open_prices(timeout=timeout)
    if open_prices:
        try:
            cache.set(TRUSTCAPITAL_OPEN_PRICES_CACHE_KEY, open_prices, TRUSTCAPITAL_OPEN_PRICES_CACHE_SECONDS)
        except Exception:
            pass
    return open_prices


def php_round(value, precision):
    """
    PHP round(): half away from zero, applied to the float's shortest decimal form
    (PHP pre-rounds, so round(0.285, 2) == 0.29, where Python's round() gives 0.28).
    """
    rounded = Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-int(precision)), rounding=ROUND_HALF_UP)
    return float(rounded)


def _php_float_to_string(value):
    """PHP's float-to-string conversion (precision=14): 2.0 -> "2", 7.3 -> "7.3", -0.0 -> "-0"."""
    return "%.14G" % value


def calculate_daily_change(bid, bid_today, digits):
    """
    Exact port of the website's PHP GetLiveQuotesMT5 daily change:

        $bid_today   = round(today bid, digits);  $bid_current = round(BidLast, digits);
        $change      = $bid_current - $bid_today;
        $change_percentage = (abs($change) / $bid_today) * 100;
        change            = ("+" if change >= 0 else "") . round($change, 4)
        change_percentage = ("" if change >= 0 else "-") . round($change_percentage, 2)

    Returns None when there is no reference (today) price, like PHP, which then omits the
    change fields instead of inventing one.
    """
    if bid is None or bid_today is None:
        return None
    try:
        round_digits = int(digits)
        bid_current = php_round(bid, round_digits)
        bid_today_rounded = php_round(bid_today, round_digits)
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not bid_today_rounded:
        return None

    change = bid_current - bid_today_rounded
    change_percentage = (abs(change) / bid_today_rounded) * 100
    change_rounded = php_round(change, 4)
    change_percentage_rounded = php_round(change_percentage, 2)
    change_symbol = "+" if change >= 0 else ""
    percentage_symbol = "" if change >= 0 else "-"
    return {
        "change": change_rounded,
        "change_percentage": change_percentage_rounded,
        "change_text": change_symbol + _php_float_to_string(change_rounded),
        "change_percentage_text": percentage_symbol + _php_float_to_string(change_percentage_rounded),
    }


def build_market_tick_payload(symbol, bid=None, ask=None, ask_open=None, bid_open=None, digits=None):
    # Determine rounding digits (match Dashboard default of 4 when unknown)
    try:
        round_digits = int(digits) if digits is not None else 4
    except (TypeError, ValueError):
        round_digits = 4

    payload = {
        "symbol": str(symbol or "").strip(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "digits": round_digits,
    }

    # Raw bid/ask and today's reference prices (preserve existing payload shape).
    for key, value in (("bid", bid), ("ask", ask), ("ask_open", ask_open), ("bid_open", bid_open)):
        try:
            payload[key] = float(value) if value is not None else None
        except (TypeError, ValueError):
            payload[key] = None

    # Daily change exactly as the website computes it (bid vs today's bid). bid_open here is
    # the website's bid_today. Without it the change fields are omitted, as in PHP.
    daily = calculate_daily_change(bid, bid_open, round_digits)
    if daily is not None:
        payload["daily_change"] = daily["change"]
        payload["daily_change_percentage"] = daily["change_percentage"]
        payload["change"] = daily["change_text"]
        payload["change_percentage"] = daily["change_percentage_text"]

    return payload


def _market_snapshot_file_path():
    runtime_dir = Path(getattr(settings, "BASE_DIR", Path.cwd())) / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir / "market_snapshot.json"


def save_market_snapshot(ticks):
    snapshot_path = _market_snapshot_file_path()
    normalized_ticks = sorted(
        [
            {
                "symbol": str((tick or {}).get("symbol") or "").strip(),
                "bid": (tick or {}).get("bid"),
                "ask": (tick or {}).get("ask"),
                "digits": (tick or {}).get("digits"),
                "ask_open": (tick or {}).get("ask_open"),
                "bid_open": (tick or {}).get("bid_open"),
                "daily_change": (tick or {}).get("daily_change"),
                "daily_change_percentage": (tick or {}).get("daily_change_percentage"),
                "change": (tick or {}).get("change"),
                "change_percentage": (tick or {}).get("change_percentage"),
                "received_at": (tick or {}).get("received_at"),
            }
            for tick in (ticks or [])
            if str((tick or {}).get("symbol") or "").strip()
        ],
        key=lambda item: item["symbol"],
    )

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(snapshot_path.parent),
        delete=False,
    ) as temp_file:
        json.dump({"ticks": normalized_ticks}, temp_file)
        temp_path = Path(temp_file.name)

    temp_path.replace(snapshot_path)


def load_market_snapshot(selected_symbols=None):
    """
    Load the latest known subscribed MT5 prices captured by the stream command.
    """
    normalized_symbols = normalize_market_symbols(selected_symbols)
    snapshot_path = _market_snapshot_file_path()
    if not snapshot_path.exists():
        return []

    with snapshot_path.open("r", encoding="utf-8") as snapshot_file:
        payload = json.load(snapshot_file)

    ticks = payload.get("ticks") or []
    if not normalized_symbols:
        return ticks

    return [
        tick
        for tick in ticks
        if should_deliver_market_tick(normalized_symbols, tick.get("symbol"))
    ]


def load_market_snapshot_from_db(symbols):
    normalized_symbols = [
        str(symbol or "").strip()
        for symbol in (symbols or [])
        if str(symbol or "").strip()
    ]
    if not normalized_symbols:
        return []

    placeholders = ",".join(["%s"] * len(normalized_symbols))
    # Include Digits column so we can round open prices consistently with MT5
    sql = f"SELECT Symbol, BidLast, AskLast, Digits FROM mt5_prices WHERE Symbol IN ({placeholders})"

    with connections["mt5clients"].cursor() as cursor:
        cursor.execute(sql, normalized_symbols)
        rows = cursor.fetchall()

    ticks = []
    for row in rows:
        symbol = str(row[0] or "").strip()
        if not symbol:
            continue

        digits = row[3] if len(row) > 3 else None

        ticks.append(
            build_market_tick_payload(
                symbol=symbol,
                bid=row[1],
                ask=row[2],
                digits=digits,
            )
        )

    ticks.sort(key=lambda item: item["symbol"])
    return ticks

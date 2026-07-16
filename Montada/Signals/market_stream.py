import json
from datetime import datetime, timezone
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


def calculate_daily_change(bid, bid_open):
    try:
        bid_value = float(bid) if bid is not None else None
        bid_open_value = float(bid_open) if bid_open is not None else None
    except (TypeError, ValueError):
        return None, None

    if bid_value is None or bid_open_value is None:
        return None, None

    # Match PHP logic used by the price API:
    # change = bid_current - bid_today (rounded to 4 decimals)
    # change_percentage = (abs(change) / bid_today) * 100 (rounded to 2 decimals)
    change = round(bid_value - bid_open_value, 4)
    change_percentage = round((abs(change) / bid_open_value) * 100, 2) if bid_open_value else 0.0
    return change, change_percentage


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

    # Keep raw bid/ask values in the payload (preserve existing behaviour/tests),
    # but compute rounded copies for change calculations to match PHP logic.
    if bid is not None:
        try:
            bid_val = float(bid)
            payload["bid"] = bid_val
            bid_rounded = round(bid_val, round_digits)
        except (TypeError, ValueError):
            payload["bid"] = None
            bid_rounded = None
    else:
        payload["bid"] = None
        bid_rounded = None

    if ask is not None:
        try:
            ask_val = float(ask)
            payload["ask"] = ask_val
            ask_rounded = round(ask_val, round_digits)
        except (TypeError, ValueError):
            payload["ask"] = None
            ask_rounded = None
    else:
        payload["ask"] = None
        ask_rounded = None

    # Round and include open values as well
    if ask_open is not None:
        try:
            ask_open_val = float(ask_open)
            payload["ask_open"] = ask_open_val
            ask_open_rounded = round(ask_open_val, round_digits)
        except (TypeError, ValueError):
            payload["ask_open"] = None
            ask_open_rounded = None
    else:
        payload["ask_open"] = None
        ask_open_rounded = None

    if bid_open is not None:
        try:
            bid_open_val = float(bid_open)
            payload["bid_open"] = bid_open_val
            bid_open_rounded = round(bid_open_val, round_digits)
        except (TypeError, ValueError):
            payload["bid_open"] = None
            bid_open_rounded = None
    else:
        payload["bid_open"] = None
        bid_open_rounded = None

    # Calculate daily change using rounded values (bid_rounded and bid_open_rounded)
    daily_change, daily_change_percentage = calculate_daily_change(bid_rounded, bid_open_rounded)
    if daily_change is not None:
        payload["daily_change"] = daily_change
    if daily_change_percentage is not None:
        payload["daily_change_percentage"] = daily_change_percentage

    # Also include string-formatted values matching the PHP API behavior:
    # - "change": signed number with "+" prefix for non-negative values (negative numbers keep their "-" sign)
    # - "change_percentage": percentage as absolute value prefixed with "-" when negative (no "+" for positive)
    if daily_change is not None:
        change_symbol = "+" if daily_change >= 0 else ""
        payload["change"] = f"{change_symbol}{round(daily_change, 4)}"
    if daily_change_percentage is not None:
        percentage_symbol = "" if daily_change >= 0 else "-"
        payload["change_percentage"] = f"{percentage_symbol}{round(daily_change_percentage, 2)}"

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

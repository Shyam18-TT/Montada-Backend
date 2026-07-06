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


def calculate_daily_change(ask, ask_open):
    try:
        ask_value = float(ask) if ask is not None else None
        ask_open_value = float(ask_open) if ask_open is not None else None
    except (TypeError, ValueError):
        return None, None

    if ask_value is None or ask_open_value is None:
        return None, None

    change = round(ask_value - ask_open_value, 4)
    change_percentage = round((abs(change) / ask_open_value) * 100, 2) if ask_open_value else 0.0
    return change, change_percentage


def build_market_tick_payload(symbol, bid=None, ask=None, ask_open=None, bid_open=None):
    payload = {
        "symbol": str(symbol or "").strip(),
        "bid": float(bid) if bid is not None else None,
        "ask": float(ask) if ask is not None else None,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }

    if ask_open is not None:
        payload["ask_open"] = float(ask_open)
    if bid_open is not None:
        payload["bid_open"] = float(bid_open)

    daily_change, daily_change_percentage = calculate_daily_change(ask, ask_open)
    if daily_change is not None:
        payload["daily_change"] = daily_change
    if daily_change_percentage is not None:
        payload["daily_change_percentage"] = daily_change_percentage

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
                "ask_open": (tick or {}).get("ask_open"),
                "bid_open": (tick or {}).get("bid_open"),
                "daily_change": (tick or {}).get("daily_change"),
                "daily_change_percentage": (tick or {}).get("daily_change_percentage"),
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
    sql = f"SELECT Symbol, BidLast, AskLast FROM mt5_prices WHERE Symbol IN ({placeholders})"

    with connections["mt5clients"].cursor() as cursor:
        cursor.execute(sql, normalized_symbols)
        rows = cursor.fetchall()

    ticks = []
    for row in rows:
        symbol = str(row[0] or "").strip()
        if not symbol:
            continue

        ticks.append(
            build_market_tick_payload(
                symbol=symbol,
                bid=row[1],
                ask=row[2],
            )
        )

    ticks.sort(key=lambda item: item["symbol"])
    return ticks

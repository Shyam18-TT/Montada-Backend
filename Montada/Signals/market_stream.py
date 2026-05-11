import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

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

def build_market_tick_payload(symbol, bid=None, ask=None):
    return {
        "symbol": str(symbol or "").strip(),
        "bid": float(bid) if bid is not None else None,
        "ask": float(ask) if ask is not None else None,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


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
        ticks.append(build_market_tick_payload(symbol=symbol, bid=row[1], ask=row[2]))

    ticks.sort(key=lambda item: item["symbol"])
    return ticks

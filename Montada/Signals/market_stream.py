from datetime import datetime, timezone


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

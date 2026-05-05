import json
import logging
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer

from .market_stream import (
    MARKET_DATA_GROUP_NAME,
    normalize_market_symbols,
    should_deliver_market_tick,
)


logger = logging.getLogger(__name__)


class MarketDataConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self._joined_group = False

        query = parse_qs(self.scope.get("query_string", b"").decode())
        symbols = []
        for raw_value in query.get("symbols", []):
            symbols.extend(raw_value.split(","))
        self.selected_symbols = normalize_market_symbols(symbols)

        await self.channel_layer.group_add(MARKET_DATA_GROUP_NAME, self.channel_name)
        self._joined_group = True
        await self.accept()
        await self.send(
            text_data=json.dumps(
                {
                    "type": "market.connected",
                    "message": "Market data stream connected.",
                    "symbols": sorted(self.selected_symbols),
                }
            )
        )

    async def disconnect(self, close_code):
        if getattr(self, "_joined_group", False):
            await self.channel_layer.group_discard(MARKET_DATA_GROUP_NAME, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            logger.debug("Ignoring invalid market data websocket payload.")
            return

        message_type = data.get("type")
        if message_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

        if message_type == "market.subscribe":
            self.selected_symbols = normalize_market_symbols(data.get("symbols") or [])
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "market.subscribed",
                        "symbols": sorted(self.selected_symbols),
                    }
                )
            )
            return

        if message_type == "market.unsubscribe":
            symbols_to_remove = normalize_market_symbols(data.get("symbols") or [])
            if symbols_to_remove:
                self.selected_symbols -= symbols_to_remove
            else:
                self.selected_symbols = set()
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "market.unsubscribed",
                        "symbols": sorted(self.selected_symbols),
                    }
                )
            )

    async def market_tick(self, event):
        tick = event.get("tick")
        if not tick:
            return
        if not should_deliver_market_tick(self.selected_symbols, tick.get("symbol")):
            return
        await self.send(
            text_data=json.dumps(
                {
                    "type": "market.tick",
                    "tick": tick,
                }
            )
        )

    async def market_ticks(self, event):
        ticks = event.get("ticks") or []
        if not ticks:
            return

        filtered_ticks = [
            tick
            for tick in ticks
            if should_deliver_market_tick(self.selected_symbols, tick.get("symbol"))
        ]
        if not filtered_ticks:
            return

        await self.send(
            text_data=json.dumps(
                {
                    "type": "market.ticks",
                    "ticks": filtered_ticks,
                }
            )
        )

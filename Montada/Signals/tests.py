from django.test import SimpleTestCase

from Signals.management.commands.run_market_data_stream import _is_allowed_symbol
from Signals.market_stream import (
    build_market_tick_payload,
    normalize_market_symbols,
    should_deliver_market_tick,
)


class MarketStreamTests(SimpleTestCase):
    def test_is_allowed_symbol_uses_mt5_path_prefixes(self):
        allowed_symbol = type("SymbolInfo", (), {"Path": r"Forex\Majors\EURUSD"})()
        blocked_symbol = type("SymbolInfo", (), {"Path": r"Futures\Other\BTC"})()

        self.assertTrue(_is_allowed_symbol(allowed_symbol))
        self.assertFalse(_is_allowed_symbol(blocked_symbol))

    def test_normalize_market_symbols(self):
        self.assertEqual(
            normalize_market_symbols([" GBPUSDc ", "dogusd.e", "", None]),
            {"gbpusdc", "dogusd.e"},
        )

    def test_should_deliver_market_tick(self):
        self.assertTrue(should_deliver_market_tick(set(), "GBPUSDc"))
        self.assertTrue(should_deliver_market_tick({"gbpusdc"}, "GBPUSDc"))
        self.assertFalse(should_deliver_market_tick({"eurusd"}, "GBPUSDc"))

    def test_build_market_tick_payload(self):
        payload = build_market_tick_payload("GBPUSDc", bid=1.35, ask=1.35012)
        self.assertEqual(payload["symbol"], "GBPUSDc")
        self.assertEqual(payload["bid"], 1.35)
        self.assertEqual(payload["ask"], 1.35012)
        self.assertIn("received_at", payload)

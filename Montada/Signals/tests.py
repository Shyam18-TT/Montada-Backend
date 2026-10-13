from unittest.mock import Mock, patch
from decimal import Decimal
import json
import time
import uuid

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from firebase import send_push_to_tokens
from Followers.models import Follow
from Signals.management.commands.poll_signal_change_notifications import Command
from Signals.management.commands.run_price_alerts import (
    ENTRY_WATCH_DOWN,
    ENTRY_WATCH_UP,
    _ensure_signal_entry_state,
    _ensure_user_alert_activation_state,
    _check_user_alert_hit,
    Command as PriceAlertCommand,
)
from Signals.management.commands.run_market_data_stream import _is_allowed_symbol
from Signals.market_stream import (
    build_market_tick_payload,
    fetch_trustcapital_open_prices,
    normalize_market_symbols,
    should_deliver_market_tick,
)
from Signals.models import AssetClass, Instrument, Timeframe, TradingSignal, PriceAlert
from Signals.views import (
    _get_signal_notification_recipients,
    _notify_signal_closed,
    _notify_signal_published,
    _reset_signal_lifecycle_if_needed,
)

User = get_user_model()


class SignalChangeNotificationThresholdTests(TestCase):
    def setUp(self):
        from Signals.price_move_config import build_fallback_config

        self.command = Command(stdout=Mock(), stderr=Mock())
        self.command.verbose = False
        self.command._config = build_fallback_config()

    def _threshold(self, symbol, signals=()):
        return self.command._resolve_rule(symbol, signals).threshold

    def _use_db_config(self):
        from Signals.price_move_config import load_config_from_db

        self.command._config = load_config_from_db()

    def test_share_levels_start_at_five_percent_then_every_five_percent(self):
        from Signals.management.commands.poll_signal_change_notifications import (
            _step_count_for,
            _step_percentage,
        )

        start, step = Decimal("5.0"), Decimal("5.0")
        self.assertEqual(_step_count_for(Decimal("4.9"), start, step), 0)
        self.assertEqual(_step_count_for(Decimal("5.0"), start, step), 1)
        self.assertEqual(_step_count_for(Decimal("15.2"), start, step), 3)
        self.assertEqual(
            [_step_percentage(n, start, step) for n in (1, 2, 3)],
            [Decimal("5.0"), Decimal("10.0"), Decimal("15.0")],
        )

    def test_duplicate_futures_contracts_are_skipped(self):
        from Signals.management.commands.poll_signal_change_notifications import _drop_duplicate_futures

        symbols = {
            "US100", "US100_Z26", "US100.Z26", "GER40.Z26", "GER40_Z26",
            "COCOA_Z26", "SUGAR_H27", "EMAAR.DEVEL", "NBD.BANK",
        }
        skipped = _drop_duplicate_futures(symbols)
        self.assertEqual(skipped, {"US100_Z26", "US100.Z26", "GER40_Z26"})

    def test_fx2_and_forex_minors_are_excluded(self):
        from Signals.management.commands.poll_signal_change_notifications import _EXCLUDED_STOCK_SYMBOLS

        for symbol in ("USDSAR", "USDAED", "AUDNOK", "AUDNOK.P", "AUDNOK.", "AUDNOKP", "ZARJPY.P"):
            self.assertIn(symbol, _EXCLUDED_STOCK_SYMBOLS)
            self.assertFalse(self.command._resolve_rule(symbol).enabled, symbol)
        for symbol in ("ETHUSD", "LTCUSD", "DSHUSD", "ZECUSD"):
            self.assertIn(symbol, _EXCLUDED_STOCK_SYMBOLS)
            self.assertFalse(self.command._resolve_rule(symbol).enabled, symbol)
        for symbol in ("EURUSD", "GBPUSD", "USDJPY", "GOLD", "BTCUSD", "XRPUSD"):
            self.assertNotIn(symbol, _EXCLUDED_STOCK_SYMBOLS)
            self.assertTrue(self.command._resolve_rule(symbol).enabled, symbol)

    def test_agricultural_cfds_use_five_percent_threshold(self):
        for symbol in ("WHEAT_Z26", "COCOA_Z26", "SUGAR_H27", "SOYBEAN_X26", "COFFEE"):
            self.assertEqual(self._threshold(symbol), Decimal("5.0"))
        self.assertEqual(self._threshold("NATGAS_X26"), Decimal("0.5"))

    def test_crypto_and_metals_thresholds(self):
        self.assertEqual(self._threshold("BTCUSD"), Decimal("5.0"))
        self.assertEqual(self._threshold("GOLD"), Decimal("0.5"))
        self.assertEqual(self._threshold("SILVER"), Decimal("0.5"))

    def test_seeded_db_config_matches_legacy_thresholds(self):
        _ensure_seeded()
        self._use_db_config()
        for symbol in ("BTCUSD", "AAPL", "DEWA", "ADNOC.GAS", "WHEAT_Z26", "COFFEE"):
            self.assertEqual(self._threshold(symbol), Decimal("5.0"), symbol)
        for symbol in ("EURUSD", "GOLD", "US500", "NATGAS_X26"):
            self.assertEqual(self._threshold(symbol), Decimal("0.5"), symbol)
        for symbol in ("USDSAR", "AUDNOK", "AUDNOK.P", "AUDNOKP", "ETHUSD", "LTCUSD", "DSHUSD"):
            self.assertFalse(self.command._resolve_rule(symbol).enabled, symbol)

    def _signal_on(self, asset_class, symbol, email):
        instrument = Instrument.objects.create(asset_class=asset_class, symbol=symbol)
        return TradingSignal.objects.create(
            analyst=User.objects.create_user(
                email=email,
                username=email,
                password="Testpass123!",
                user_type="analyst",
            ),
            asset_class=asset_class,
            instrument=instrument,
            timeframe=Timeframe.objects.get_or_create(code="H1", defaults={"name": "1 Hour"})[0],
            direction=TradingSignal.Direction.BUY,
            entry_price="1.00000",
            stop_loss="0.95000",
            take_profit="1.10000",
            confidence_level=80,
            status=TradingSignal.Status.OPEN,
        )

    def _share_class_rule(self, asset_class):
        from Signals.models import PriceMoveNotificationRule

        PriceMoveNotificationRule.objects.create(
            scope="asset_class", asset_class=asset_class,
            threshold_percent=Decimal("5"), step_percent=Decimal("5"),
        )

    def test_share_asset_class_uses_five_percent_threshold(self):
        asset_class = AssetClass.objects.create(name="Shares")
        self._share_class_rule(asset_class)
        signal = self._signal_on(asset_class, "NEWCO", "analyst@example.com")
        self._use_db_config()

        self.assertEqual(self._threshold("NEWCO", [signal]), Decimal("5.0"))

    def test_non_share_asset_class_keeps_half_percent_threshold(self):
        asset_class = AssetClass.objects.create(name="Forex")
        signal = self._signal_on(asset_class, "EURUSD", "analyst2@example.com")
        self._use_db_config()

        self.assertEqual(self._threshold("EURUSD", [signal]), Decimal("0.5"))

    def test_mena_share_asset_class_uses_five_percent_threshold(self):
        asset_class = AssetClass.objects.create(name="Mena Shares")
        self._share_class_rule(asset_class)
        signal = self._signal_on(asset_class, "NEWMENA", "analyst3@example.com")
        self._use_db_config()

        self.assertEqual(self._threshold("NEWMENA", [signal]), Decimal("5.0"))

    def test_share_without_open_signal_uses_five_percent_threshold(self):
        asset_class = AssetClass.objects.create(name="Mena Shares")
        self._share_class_rule(asset_class)
        Instrument.objects.create(asset_class=asset_class, symbol="New.Listing")
        self._use_db_config()

        self.assertEqual(self._threshold("NEW.LISTING"), Decimal("5.0"))
        self.assertEqual(self._threshold("EURUSD"), Decimal("0.5"))

    def test_level_notification_does_not_repeat_within_cooldown_window(self):
        self.command.notification_cooldown_seconds = 300
        current_time = int(time.time())
        previous_state = {
            "notification_history": {
                "up": {"1": current_time - 60},
            }
        }

        self.assertTrue(self.command._is_level_notification_in_cooldown("up", "1", previous_state, current_time))
        self.assertFalse(self.command._is_level_notification_in_cooldown("up", "1", previous_state, current_time + 600))

    def _make_poll_command(self, config=None):
        from django.core.cache import cache
        from Signals.management.commands.poll_signal_change_notifications import BASELINE_DONE_CACHE_KEY
        from Signals.price_move_config import build_fallback_config

        cache.clear()
        cache.set(BASELINE_DONE_CACHE_KEY, True, timeout=None)
        command = Command(stdout=Mock(), stderr=Mock())
        command.url = "https://example.invalid/prices"
        command.timeout = 1
        command.verbose = False
        command._config_provider = Mock()
        command._config_provider.get.return_value = config or build_fallback_config()
        command.state_ttl = 3600
        command.lock_ttl = 60
        command.max_users_per_notification = 0
        command.reset_daily = True
        command.symbol_filter = set()
        command.excluded_symbols = set()
        command._fallback_state_by_symbol = {}
        command._fallback_locks = set()
        return command

    def test_poll_sends_push_to_all_active_users_once_per_level(self):
        users = [
            User.objects.create_user(
                email="poll%d@example.com" % i, username="poll%d@example.com" % i,
                password="Testpass123!", user_type="trader",
            )
            for i in range(3)
        ]
        command = self._make_poll_command()
        quotes = {"EURUSD": {"change_percentage": "0.62", "dir": "up", "bid": "1.1", "ask": "1.1001"}}
        push = Mock(return_value={"success_count": 3, "failure_count": 0, "failed_tokens": [], "errors": []})
        with patch(
            "Signals.management.commands.poll_signal_change_notifications._fetch_live_quotes",
            return_value=quotes,
        ), patch("firebase.send_push_to_users", push):
            command._run_poll()
            command._run_poll()  # same level again: no duplicate push

        push.assert_called_once()
        self.assertEqual(
            {user.id for user in push.call_args.kwargs["users"]},
            {user.id for user in users},
        )
        self.assertEqual(push.call_args.kwargs["title"], "EURUSD up 0.5%")
        self.assertEqual(push.call_args.kwargs["data"]["type"], "signal_change_threshold")

    def test_poll_jump_across_many_levels_sends_single_notification(self):
        from Mainapp.models import UserNotification

        User.objects.create_user(
            email="jump@example.com", username="jump@example.com",
            password="Testpass123!", user_type="trader",
        )
        command = self._make_poll_command()
        push = Mock(return_value={"success_count": 1, "failure_count": 0, "failed_tokens": [], "errors": []})

        def poll(change, bid):
            quotes = {"BCHUSD": {"change_percentage": change, "dir": "down", "bid": bid, "ask": bid}}
            with patch(
                "Signals.management.commands.poll_signal_change_notifications._fetch_live_quotes",
                return_value=quotes,
            ), patch("firebase.send_push_to_users", push):
                command._run_poll()

        poll("-7.37", "308.615")  # crypto: first level is 5%
        push.assert_called_once()
        self.assertEqual(push.call_args.kwargs["title"], "BCHUSD down 5%")
        self.assertEqual(push.call_args.kwargs["body"], "BCHUSD is down 7.37%, trading at 308.615.")
        self.assertEqual(UserNotification.objects.count(), 1)

        poll("-9.9", "300.1")  # still below the next level (10%): nothing new
        self.assertEqual(push.call_count, 1)

        poll("-15.2", "282.5")  # crosses 10% and 15% in one move: one push for 15%
        self.assertEqual(push.call_count, 2)
        self.assertEqual(push.call_args.kwargs["title"], "BCHUSD down 15%")
        self.assertEqual(push.call_args.kwargs["body"], "BCHUSD is down 15.2%, trading at 282.5.")

    def test_current_price_comes_from_same_quote_as_change(self):
        command = self._make_poll_command()
        command._market_prices = {"BCHUSD": {"bid": 311.2, "digits": 3}}
        # Quote price wins (same source as change_percentage); snapshot supplies the digits.
        self.assertEqual(command._get_current_price("BCHUSD", {"bid": "308.6150000"}), "308.615")
        # No bid in the quote: fall back to the fresh stream price.
        self.assertEqual(command._get_current_price("BCHUSD", {}), "311.200")
        command._market_prices = {}
        self.assertEqual(command._get_current_price("BCHUSD", {"bid": "308.6150000"}), "308.615")
        self.assertEqual(command._get_current_price("BCHUSD", {}), "")

    def test_poll_push_payload_stays_under_fcm_limit_with_many_signals(self):
        User.objects.create_user(
            email="payload@example.com", username="payload@example.com",
            password="Testpass123!", user_type="trader",
        )
        command = self._make_poll_command()
        signal_ids = [uuid.uuid4() for _ in range(300)]
        push = Mock(return_value={"success_count": 1, "failure_count": 0, "failed_tokens": [], "errors": []})
        with patch("firebase.send_push_to_users", push):
            command._notify_all_users(
                symbol="XAUUSD", direction="up",
                signed_crossed_percentage=Decimal("0.5"), crossed_percentage=Decimal("0.5"),
                current_percentage=Decimal("0.61"), quote={"bid": "2350.1", "ask": "2350.4"},
                signal_ids=signal_ids,
            )
        data = push.call_args.kwargs["data"]
        payload_bytes = len(json.dumps({k: str(v) for k, v in data.items()}).encode("utf-8"))
        self.assertLess(payload_bytes, 4096)
        self.assertEqual(data["signal_count"], "300")


def _ensure_seeded():
    """Seed rows from migration 0013 (no-op when the migration already ran)."""
    from Signals.models import PriceMoveNotificationRule, PriceMoveNotificationSettings
    from Signals.price_move_config import seed_price_move_config

    seed_price_move_config(PriceMoveNotificationSettings, PriceMoveNotificationRule, AssetClass, Instrument)


def _move_rule(enabled=None, threshold=None, step=None, reset=None, cooldown_seconds=None, label="test"):
    from Signals.price_move_config import _rule_dict

    return _rule_dict(
        enabled=enabled,
        threshold=None if threshold is None else Decimal(threshold),
        step=None if step is None else Decimal(step),
        reset=None if reset is None else Decimal(reset),
        cooldown_seconds=cooldown_seconds,
        label=label,
    )


class PriceMoveConfigResolveTests(SimpleTestCase):
    def test_symbol_rule_overrides_muted_asset_class_only_when_explicitly_enabled(self):
        from Signals.price_move_config import PriceMoveConfig

        config = PriceMoveConfig()
        config.asset_class_rules["crypto"] = _move_rule(enabled=False)
        config.instrument_asset_classes = {"BTCUSD": {"crypto"}, "ETHUSD": {"crypto"}}
        config.symbol_rules["BTCUSD"] = _move_rule(enabled=True)
        config.symbol_rules["ETHUSD"] = _move_rule(threshold="2")

        self.assertTrue(config.resolve("BTCUSD").enabled)
        eth = config.resolve("ETHUSD")
        self.assertFalse(eth.enabled)  # threshold-only rule inherits the class mute
        self.assertEqual((eth.threshold, eth.step), (Decimal("2"), Decimal("2")))

        config.enabled = False  # global switch beats everything
        self.assertFalse(config.resolve("BTCUSD").enabled)

    def test_symbol_in_several_asset_classes_takes_strictest_values(self):
        from Signals.price_move_config import PriceMoveConfig

        config = PriceMoveConfig()
        config.asset_class_rules = {"a": _move_rule(threshold="1"), "b": _move_rule(threshold="3")}
        config.instrument_asset_classes = {"XYZ": {"a", "b"}}
        self.assertEqual(config.resolve("XYZ").threshold, Decimal("3"))
        config.asset_class_rules["a"] = _move_rule(enabled=False)
        self.assertFalse(config.resolve("XYZ").enabled)

    def test_reset_not_below_threshold_is_clamped_with_warning(self):
        from Signals.price_move_config import PriceMoveConfig

        config = PriceMoveConfig(reset=Decimal("0.3"))
        config.symbol_rules["EURUSD"] = _move_rule(threshold="0.2")
        rule = config.resolve("EURUSD")
        self.assertEqual(rule.reset, Decimal("0.12"))
        self.assertTrue(rule.warnings)

    def test_rules_match_futures_aliases_and_broker_variants(self):
        from Signals.price_move_config import PriceMoveConfig

        config = PriceMoveConfig()
        config.symbol_rules = {
            "WHEAT": _move_rule(threshold="5"),
            "AUDNOK": _move_rule(enabled=False),
            "GOLD": _move_rule(threshold="1"),
            "SHO": _move_rule(enabled=False),
        }
        self.assertEqual(config.resolve("WHEAT_Z26").threshold, Decimal("5"))
        self.assertEqual(config.resolve("XAUUSD").threshold, Decimal("1"))
        for variant in ("AUDNOK.P", "AUDNOK.", "AUDNOKP"):
            self.assertFalse(config.resolve(variant).enabled, variant)
        self.assertTrue(config.resolve("SHOP").enabled)  # short names keep their trailing P

    def test_exact_symbol_rule_wins_over_alias_rule(self):
        from Signals.price_move_config import PriceMoveConfig

        config = PriceMoveConfig()
        config.symbol_rules = {"XAUUSD": _move_rule(threshold="2"), "GOLD": _move_rule(threshold="1")}
        self.assertEqual(config.resolve("XAUUSD").threshold, Decimal("2"))
        self.assertEqual(config.resolve("GOLD").threshold, Decimal("1"))


class PriceMoveMidDayChangeTests(TestCase):
    """Admin changes, mutes, cache flushes and Redis outages must never send a burst of pushes."""

    def setUp(self):
        User.objects.create_user(
            email="midday@example.com", username="midday@example.com",
            password="Testpass123!", user_type="trader",
        )
        self.push = Mock(return_value={"success_count": 1, "failure_count": 0, "failed_tokens": [], "errors": []})

    def _command(self, config=None):
        return SignalChangeNotificationThresholdTests._make_poll_command(self, config)

    def _poll(self, command, change, symbol="EURUSD"):
        direction = "down" if str(change).startswith("-") else "up"
        quotes = {symbol: {"change_percentage": change, "dir": direction, "bid": "1.1", "ask": "1.1001"}}
        with patch(
            "Signals.management.commands.poll_signal_change_notifications._fetch_live_quotes",
            return_value=quotes,
        ), patch("firebase.send_push_to_users", self.push):
            command._run_poll()

    def _set_config(self, command, config):
        command._config_provider.get.return_value = config

    def test_lowered_threshold_rebaselines_from_current_change(self):
        from Signals.price_move_config import build_fallback_config

        command = self._command()
        self._poll(command, "1.3")
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.push.call_args.kwargs["title"], "EURUSD up 1%")

        # 0.25% grid: 0.25..1.25 are already behind the price, so nothing is sent for them.
        self._set_config(command, build_fallback_config(threshold=Decimal("0.25"), reset=Decimal("0.15")))
        self._poll(command, "1.3")
        self.assertEqual(self.push.call_count, 1)

        self._poll(command, "1.55")
        self.assertEqual(self.push.call_count, 2)
        self.assertEqual(self.push.call_args.kwargs["title"], "EURUSD up 1.5%")

    def test_raised_threshold_waits_for_next_new_level(self):
        from Signals.price_move_config import build_fallback_config

        command = self._command()
        self._poll(command, "0.7")
        self.assertEqual(self.push.call_count, 1)

        self._set_config(command, build_fallback_config(threshold=Decimal("1")))
        self._poll(command, "0.8")
        self.assertEqual(self.push.call_count, 1)
        self._poll(command, "1.1")
        self.assertEqual(self.push.call_count, 2)
        self.assertEqual(self.push.call_args.kwargs["title"], "EURUSD up 1%")

    def test_muted_symbol_is_tracked_silently_and_unmute_starts_from_current_level(self):
        from Signals.price_move_config import build_fallback_config

        muted = build_fallback_config()
        muted.symbol_rules["EURUSD"] = _move_rule(enabled=False)
        command = self._command(muted)
        self._poll(command, "0.6")
        self._poll(command, "2.1")
        self.assertEqual(self.push.call_count, 0)

        self._set_config(command, build_fallback_config())
        self._poll(command, "2.1")  # unmuted at 2.1%: 0.5..2 are not sent
        self.assertEqual(self.push.call_count, 0)
        self._poll(command, "2.6")
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.push.call_args.kwargs["title"], "EURUSD up 2.5%")

    def test_global_switch_off_sends_nothing(self):
        from Signals.price_move_config import build_fallback_config

        config = build_fallback_config()
        config.enabled = False
        command = self._command(config)
        self._poll(command, "3.2")
        self.assertEqual(self.push.call_count, 0)

    def test_flushed_cache_records_levels_silently_then_notifies_next_level(self):
        from django.core.cache import cache
        from Signals.management.commands.poll_signal_change_notifications import BASELINE_DONE_CACHE_KEY

        command = self._command()
        cache.delete(BASELINE_DONE_CACHE_KEY)
        self._poll(command, "1.3")
        self.assertEqual(self.push.call_count, 0)
        self.assertTrue(cache.get(BASELINE_DONE_CACHE_KEY))

        self._poll(command, "1.6")
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.push.call_args.kwargs["title"], "EURUSD up 1.5%")

    def test_redis_outage_does_not_resend_levels(self):
        command = self._command()
        broken_cache = Mock()
        for name in ("get", "set", "add", "delete"):
            getattr(broken_cache, name).side_effect = ConnectionError("redis down")
        with patch("Signals.management.commands.poll_signal_change_notifications.cache", broken_cache):
            self._poll(command, "1.3")  # no state readable anywhere: recorded silently
            self.assertEqual(self.push.call_count, 0)
            self.assertIn("EURUSD", command._fallback_state_by_symbol)

            self._poll(command, "1.6")  # in-process state is used for the next level
            self.assertEqual(self.push.call_count, 1)

            command._fallback_state_last_cleanup = 0
            command._cleanup_fallback_state()  # today's state survives the periodic cleanup
            self._poll(command, "1.7")
            self.assertEqual(self.push.call_count, 1)

    def test_fallback_cleanup_drops_only_previous_days(self):
        command = self._command()
        today = command._current_state_day()
        command._fallback_state_by_symbol = {
            "EURUSD": {"trading_day": today},
            "GBPUSD": {"trading_day": "2000-01-01"},
        }
        command._fallback_state_last_cleanup = 0
        command._cleanup_fallback_state()
        self.assertEqual(set(command._fallback_state_by_symbol), {"EURUSD"})


class PriceMoveConfigCacheTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_provider_reads_db_only_after_version_changes(self):
        from django.core.cache import cache
        from Signals.price_move_config import (
            VERSION_CACHE_KEY,
            PriceMoveConfig,
            PriceMoveConfigProvider,
            build_fallback_config,
        )

        with patch(
            "Signals.price_move_config.load_config_from_db",
            side_effect=lambda version=None: PriceMoveConfig(version=version),
        ) as load:
            provider = PriceMoveConfigProvider(build_fallback_config)
            provider.get()
            provider.get()
            self.assertEqual(load.call_count, 1)

            cache.set(VERSION_CACHE_KEY, "v2")
            self.assertEqual(provider.get().version, "v2")
            provider.get()
            self.assertEqual(load.call_count, 2)

    def test_provider_keeps_last_good_config_when_db_fails(self):
        from django.core.cache import cache
        from Signals.price_move_config import (
            VERSION_CACHE_KEY,
            PriceMoveConfig,
            PriceMoveConfigProvider,
            build_fallback_config,
        )

        good = PriceMoveConfig(threshold=Decimal("2"))
        with patch("Signals.price_move_config.load_config_from_db", side_effect=[good, RuntimeError("db down")]):
            provider = PriceMoveConfigProvider(build_fallback_config)
            self.assertIs(provider.get(), good)
            cache.set(VERSION_CACHE_KEY, "v2")
            self.assertIs(provider.get(), good)

        with patch("Signals.price_move_config.load_config_from_db", side_effect=RuntimeError("no table")):
            self.assertEqual(PriceMoveConfigProvider(build_fallback_config).get().source, "fallback")

    def test_version_is_bumped_only_after_commit(self):
        from django.core.cache import cache
        from Signals.models import PriceMoveNotificationRule
        from Signals.price_move_config import VERSION_CACHE_KEY

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            PriceMoveNotificationRule.objects.create(scope="symbol", symbol="NEWSYM", is_enabled=False)
        self.assertIsNone(cache.get(VERSION_CACHE_KEY))
        self.assertTrue(callbacks)

        with self.captureOnCommitCallbacks(execute=True):
            PriceMoveNotificationRule.objects.filter(symbol="NEWSYM").first().delete()
        self.assertIsNotNone(cache.get(VERSION_CACHE_KEY))

    def test_seed_creates_settings_and_legacy_rules(self):
        from Signals.models import PriceMoveNotificationRule, PriceMoveNotificationSettings

        _ensure_seeded()
        _ensure_seeded()  # idempotent
        self.assertEqual(PriceMoveNotificationRule.objects.filter(scope="symbol", symbol="AUDNOK").count(), 1)

        settings_row = PriceMoveNotificationSettings.objects.get(pk=1)
        self.assertEqual(settings_row.default_threshold_percent, Decimal("0.5"))
        self.assertEqual(settings_row.reset_percent, Decimal("0.3"))
        self.assertFalse(PriceMoveNotificationRule.objects.get(scope="symbol", symbol="AUDNOK").is_enabled)
        # Excluded crypto pairs get only the mute rule, not also a 5% threshold rule.
        eth = PriceMoveNotificationRule.objects.get(scope="symbol", symbol="ETHUSD")
        self.assertEqual((eth.is_enabled, eth.threshold_percent), (False, None))
        btc = PriceMoveNotificationRule.objects.get(scope="symbol", symbol="BTCUSD")
        self.assertEqual((btc.threshold_percent, btc.step_percent), (Decimal("5"), Decimal("5")))
        self.assertTrue(PriceMoveNotificationRule.objects.filter(scope="symbol", symbol="WHEAT").exists())


class SeedPriceMoveRulesCommandTests(TestCase):
    def _run(self, *args):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("seed_price_move_rules", *args, stdout=out)
        return out.getvalue()

    def test_fills_missing_rules_and_mutes_untouched_seeded_rules_only(self):
        from Signals.models import PriceMoveNotificationRule

        PriceMoveNotificationRule.objects.all().delete()
        # State left by an earlier seed: ETHUSD as a 5% rule, LTCUSD edited by an admin.
        PriceMoveNotificationRule.objects.create(
            scope="symbol", symbol="ETHUSD", threshold_percent=Decimal("5"), step_percent=Decimal("5"),
            note="Seeded: share/crypto symbol uses 5%, then every 5%.",
        )
        admin = User.objects.create_user(email="seedadmin@example.com", username="seedadmin@example.com",
                                         password="Testpass123!", is_staff=True)
        PriceMoveNotificationRule.objects.create(
            scope="symbol", symbol="LTCUSD", threshold_percent=Decimal("3"), updated_by=admin,
            note="Seeded: share/crypto symbol uses 5%, then every 5%.",
        )

        output = self._run("--dry-run")
        self.assertIn("would mute seeded symbol rule ETHUSD", output)
        self.assertIsNone(PriceMoveNotificationRule.objects.get(symbol="ETHUSD").is_enabled)
        self.assertFalse(PriceMoveNotificationRule.objects.filter(symbol="AUDNOK").exists())

        self._run()
        eth = PriceMoveNotificationRule.objects.get(symbol="ETHUSD")
        self.assertEqual((eth.is_enabled, eth.threshold_percent), (False, None))
        ltc = PriceMoveNotificationRule.objects.get(symbol="LTCUSD")
        self.assertEqual((ltc.is_enabled, ltc.threshold_percent), (None, Decimal("3")))  # admin edit kept
        self.assertFalse(PriceMoveNotificationRule.objects.get(symbol="AUDNOK").is_enabled)

        self.assertIn("0 rule(s) created, 0 seeded rule(s) muted", self._run())  # re-run is a no-op


class AdminPriceMoveApiTests(TestCase):
    BASE = "/api/admin/signals/move-notifications/"

    def setUp(self):
        from rest_framework.test import APIClient

        self.admin = User.objects.create_user(
            email="moveadmin@example.com", username="moveadmin@example.com",
            password="Testpass123!", is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_non_admin_is_rejected(self):
        from rest_framework.test import APIClient

        trader = User.objects.create_user(
            email="movetrader@example.com", username="movetrader@example.com",
            password="Testpass123!", user_type="trader",
        )
        client = APIClient()
        client.force_authenticate(trader)
        self.assertEqual(client.get(self.BASE + "settings/").status_code, 403)

    def test_settings_validation_and_update(self):
        response = self.client.patch(self.BASE + "settings/", {"reset_percent": "0.6"}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(self.BASE + "settings/", {"poll_interval_seconds": 1}, format="json")
        self.assertEqual(response.status_code, 400)

        response = self.client.patch(
            self.BASE + "settings/",
            {"default_threshold_percent": "0.25", "reset_percent": "0.1", "poll_interval_seconds": 60},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["settings"]["default_threshold_percent"], "0.250")
        self.assertEqual(response.data["settings"]["updated_by_email"], self.admin.email)

    def test_rule_create_normalizes_and_rejects_duplicates_and_empty_rules(self):
        response = self.client.post(
            self.BASE + "rules/", {"scope": "symbol", "symbol": "eur/usd", "threshold_percent": "1"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["rule"]["symbol"], "EURUSD")
        rule_id = response.data["rule"]["id"]

        duplicate = self.client.post(
            self.BASE + "rules/", {"scope": "symbol", "symbol": "EURUSD", "is_enabled": False}, format="json"
        )
        self.assertEqual(duplicate.status_code, 400)
        empty = self.client.post(self.BASE + "rules/", {"scope": "symbol", "symbol": "GBPUSD"}, format="json")
        self.assertEqual(empty.status_code, 400)
        bad_reset = self.client.post(
            self.BASE + "rules/",
            {"scope": "symbol", "symbol": "GBPUSD", "threshold_percent": "1", "reset_percent": "1"},
            format="json",
        )
        self.assertEqual(bad_reset.status_code, 400)

        retarget = self.client.patch(self.BASE + "rules/%s/" % rule_id, {"symbol": "GBPUSD"}, format="json")
        self.assertEqual(retarget.status_code, 400)
        update = self.client.patch(self.BASE + "rules/%s/" % rule_id, {"threshold_percent": "2"}, format="json")
        self.assertEqual(update.status_code, 200)
        self.assertEqual(self.client.delete(self.BASE + "rules/%s/" % rule_id).status_code, 200)

    def test_mute_asset_class_then_unmute_one_symbol(self):
        from Signals.models import PriceMoveNotificationRule

        crypto = AssetClass.objects.create(name="Crypto Test")
        Instrument.objects.create(asset_class=crypto, symbol="NEWCOIN")
        response = self.client.post(
            self.BASE + "mute/", {"scope": "asset_class", "asset_class": str(crypto.id), "muted": True}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        resolved = self.client.get(self.BASE + "resolve/", {"symbol": "NEWCOIN"})
        self.assertFalse(resolved.data["enabled"])

        response = self.client.post(self.BASE + "mute/", {"scope": "symbol", "symbol": "newcoin", "muted": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["effective"]["enabled"])
        self.assertTrue(PriceMoveNotificationRule.objects.get(scope="symbol", symbol="NEWCOIN").is_enabled)

    def test_unmuting_a_pure_mute_rule_deletes_it(self):
        from Signals.models import PriceMoveNotificationRule

        self.client.post(self.BASE + "mute/", {"scope": "symbol", "symbol": "XYZUSD", "muted": True}, format="json")
        self.assertFalse(PriceMoveNotificationRule.objects.get(symbol="XYZUSD").is_enabled)
        response = self.client.post(self.BASE + "mute/", {"scope": "symbol", "symbol": "XYZUSD", "muted": False}, format="json")
        self.assertIsNone(response.data["rule"])
        self.assertFalse(PriceMoveNotificationRule.objects.filter(symbol="XYZUSD").exists())

    def test_resolve_shows_seeded_mutes_and_levels(self):
        _ensure_seeded()
        muted = self.client.get(self.BASE + "resolve/", {"symbol": "AUDNOKp"})
        self.assertEqual(muted.status_code, 200)
        self.assertFalse(muted.data["enabled"])
        btc = self.client.get(self.BASE + "resolve/", {"symbol": "BTCUSD"})
        self.assertEqual(btc.data["next_levels_percent"][:3], ["5", "10", "15"])


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

    def test_build_market_tick_payload_includes_daily_change(self):
        payload = build_market_tick_payload(
            "EURUSD", bid=1.1005, ask=1.1010, ask_open=1.0950, bid_open=1.0945
        )
        self.assertEqual(payload["symbol"], "EURUSD")
        self.assertEqual(payload["ask_open"], 1.0950)
        self.assertEqual(payload["bid_open"], 1.0945)
        self.assertEqual(payload["daily_change"], round(1.1005 - 1.0945, 4))
        self.assertEqual(
            payload["daily_change_percentage"],
            round(abs(1.1005 - 1.0945) / 1.0945 * 100, 2),
        )
        self.assertIn("received_at", payload)

    # (symbol, bid, bid_today, digits, change, change_percentage) exactly as returned by the
    # website's PHP GetLiveQuotesMT5 (trustcapital.com/api/get-MT5-price) on 2026-09-28.
    WEBSITE_CHANGE_SAMPLES = [
        ("EURUSD", 1.13721, 1.13903, "5", "-0.0018", "-0.16"),
        ("USDCAD", 1.41649, 1.41387, "5", "+0.0026", "0.19"),
        ("AUDUSD", 0.7022, 0.70234, "5", "-0.0001", "-0.02"),
        ("NZDUSD", 0.56668, 0.56614, "5", "+0.0005", "0.1"),
        ("CADCHF", 0.58747, 0.58571, "5", "+0.0018", "0.3"),
        ("XLMUSD", 0.22782, 0.21581, "5", "+0.012", "5.57"),
        ("DOGUSD", 0.09413, 0.0968, "5", "-0.0027", "-2.76"),
        ("BMWG", 57.45, 57.45, "2", "+0", "0"),
        ("BCHUSD", 313.12, 333.18, "3", "-20.06", "-6.02"),
        ("USDJPY", 157.063, 157.26, "3", "-0.197", "-0.13"),
        ("BTCUSD", 83281.57, 84517.41, "2", "-1235.84", "-1.46"),
    ]

    def test_daily_change_matches_website_php_exactly(self):
        from Signals.market_stream import calculate_daily_change

        for symbol, bid, bid_today, digits, change, change_percentage in self.WEBSITE_CHANGE_SAMPLES:
            result = calculate_daily_change(bid, bid_today, digits)
            self.assertEqual(result["change_text"], change, symbol)
            self.assertEqual(result["change_percentage_text"], change_percentage, symbol)

    def test_daily_change_uses_php_half_up_rounding(self):
        from Signals.market_stream import php_round

        self.assertEqual(php_round(0.285, 2), 0.29)  # Python round() gives 0.28
        self.assertEqual(php_round(-0.125, 2), -0.13)
        self.assertEqual(php_round(1.00005, 4), 1.0001)

    def test_tick_without_todays_price_has_no_change(self):
        payload = build_market_tick_payload("EURUSD", bid=1.1005, ask=1.1010, digits=5)
        for key in ("change", "change_percentage", "daily_change", "daily_change_percentage"):
            self.assertNotIn(key, payload)

    def test_stream_tick_uses_website_reference_price_for_mixed_case_symbol(self):
        from Signals.management.commands.run_market_data_stream import Command as StreamCommand

        command = StreamCommand()
        command._latest_ticks = {}
        command._symbol_digits = {"ADNOC.Gas": 3}
        # fetch_trustcapital_open_prices keys are uppercased.
        command._open_prices = {"ADNOC.GAS": {"bid_today": 3.2, "ask_today": 3.21}}
        payload = command._build_tick("ADNOC.Gas", 3.264, 3.27)
        self.assertEqual(payload["bid_open"], 3.2)
        self.assertEqual(payload["change_percentage"], "2")
        self.assertEqual(payload["change"], "+0.064")

        # No reference price: no invented 0% change.
        command._open_prices = {}
        self.assertNotIn("change_percentage", command._build_tick("ADNOC.Gas", 3.264, 3.27))

    def test_live_quote_endpoint_row_uses_bid_vs_website_bid_today(self):
        from Dashboard.views import _enrich_market_row

        row = {"Symbol": "BCHUSD", "BidLast": 313.12, "AskLast": 313.5, "AskDir": 0, "Digits": 3,
               "AskLow": 300.0, "BidLow": 299.9}
        _enrich_market_row(row, open_prices={"BCHUSD": {"bid_today": 333.18, "ask_today": 333.6}})
        self.assertEqual(row["bid_today"], 333.18)
        self.assertEqual(row["change"], "-20.06")
        self.assertEqual(row["change_percentage"], "-6.02")  # not measured from the day's low

    @patch("Signals.market_stream.urlopen")
    def test_fetch_trustcapital_open_prices(self, mock_urlopen):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "data": {
                            "live_quote": {
                                "EURUSD": {
                                    "ask_today": 1.14339,
                                    "bid_today": 1.14329,
                                },
                                "GBPUSD": {
                                    "ask_today": 1.3347,
                                    "bid_today": 1.33454,
                                },
                            }
                        }
                    }
                ).encode("utf-8")

        mock_urlopen.return_value = FakeResponse()
        open_prices = fetch_trustcapital_open_prices(["EURUSD", "GBPUSD"])

        self.assertEqual(
            open_prices,
            {
                "EURUSD": {"ask_today": 1.14339, "bid_today": 1.14329},
                "GBPUSD": {"ask_today": 1.3347, "bid_today": 1.33454},
            },
        )


class FirebasePushPayloadTests(SimpleTestCase):
    @patch("firebase.messaging.send_each_for_multicast")
    @patch("firebase.messaging.MulticastMessage")
    def test_send_push_to_tokens_includes_source_payload(self, mock_multicast_message, mock_send_each):
        mock_response = Mock(success_count=1, failure_count=0, responses=[Mock(success=True, exception=None)])
        mock_send_each.return_value = mock_response

        send_push_to_tokens(
            tokens=["test-token"],
            title="Market change",
            body="Price crossed threshold",
            data={"type": "signal_change_threshold"},
        )

        message_kwargs = mock_multicast_message.call_args.kwargs
        self.assertEqual(message_kwargs["data"]["source"], "montada-app")
        self.assertEqual(message_kwargs["data"]["type"], "signal_change_threshold")


class FirebasePushSoundTests(SimpleTestCase):
    def _encoded_message(self, data):
        """Send through send_push_to_tokens and return the FCM v1 JSON the SDK would post."""
        from firebase_admin import _messaging_encoder, messaging

        response = Mock(success_count=1, failure_count=0, responses=[Mock(success=True, exception=None)])
        with patch("firebase.messaging.send_each_for_multicast", return_value=response) as send_each:
            send_push_to_tokens(tokens=["device-token"], title="Title", body="Body", data=data)
        multicast = send_each.call_args.args[0]
        message = messaging.Message(
            token=multicast.tokens[0],
            notification=multicast.notification,
            android=multicast.android,
            apns=multicast.apns,
            data=multicast.data,
        )
        return _messaging_encoder.MessageEncoder().default(message)

    def test_payload_has_ios_and_android_sound_fields(self):
        encoded = self._encoded_message({"type": "admin_broadcast"})

        self.assertEqual(encoded["notification"], {"title": "Title", "body": "Body"})
        self.assertEqual(encoded["apns"]["headers"], {"apns-priority": "10", "apns-push-type": "alert"})
        self.assertEqual(encoded["apns"]["payload"]["aps"]["sound"], "montada_push.wav")
        self.assertEqual(encoded["android"]["priority"], "high")
        self.assertEqual(encoded["android"]["notification"]["sound"], "montada_push")
        self.assertEqual(encoded["android"]["notification"]["channel_id"], "montada_broadcasts")
        self.assertNotIn("mutable-content", encoded["apns"]["payload"]["aps"])  # no image

    def test_android_channel_per_notification_type(self):
        from firebase import android_channel_for

        expected = {
            "news_update": "montada_news",
            "economic_reminder": "montada_economic_reminders",
            "economic_global_reminder": "montada_economic_reminders",
            "user_price_alert": "montada_price_alerts",
            "signal_change_threshold": "montada_price_alerts",
            "signal_published": "montada_trade_ideas",
            "signal_closed": "montada_trade_ideas",
            "signal_alert": "montada_trade_ideas",
            "price_alert": "montada_trade_ideas",
            "admin_broadcast": "montada_broadcasts",
            "signal_applied": "montada_notifications",
            "": "montada_notifications",
        }
        for push_type, channel in expected.items():
            self.assertEqual(android_channel_for({"type": push_type}), channel, push_type)
        self.assertEqual(android_channel_for({"type": "economic_event", "importance": "high"}), "montada_economic_high")
        self.assertEqual(android_channel_for({"type": "economic_event", "importance": "medium"}), "montada_economic")
        self.assertEqual(android_channel_for(None), "montada_notifications")


class FirebaseBackgroundPushTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="bg-push@example.com", username="bg-push@example.com", password="Testpass123!",
        )

    def test_push_is_queued_after_commit_and_request_does_not_wait(self):
        import firebase

        with patch.object(firebase._PUSH_EXECUTOR, "submit") as submit, \
                patch("firebase.send_push_to_users") as send:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                result = firebase.send_push_to_users_in_background(
                    User.objects.filter(id=self.user.id), "t", "b", data={"type": "x"},
                )
            self.assertIsNone(result)
            submit.assert_not_called()  # nothing sent before the transaction commits
            for callback in callbacks:
                callback()
        submit.assert_called_once()
        func, label, kwargs = submit.call_args.args[1:]
        self.assertIs(func, send)
        self.assertEqual(kwargs["users"], [self.user])  # queryset evaluated in the caller
        send.assert_not_called()  # the actual FCM call happens on the worker

    def test_sync_mode_setting_sends_immediately(self):
        import firebase

        with self.settings(FCM_PUSH_ASYNC=False), patch(
            "firebase.send_push_to_tokens", return_value={"success_count": 1, "failure_count": 0}
        ) as send:
            result = firebase.send_push_to_tokens_in_background(["tok"], "t", "b")
        send.assert_called_once()
        self.assertEqual(result["success_count"], 1)

    def test_worker_job_logs_errors_and_releases_db_connection(self):
        import firebase

        failing = Mock(side_effect=RuntimeError("fcm down"))
        with patch("django.db.connections.close_all") as close_all, \
                self.assertLogs("firebase", level="ERROR"):
            firebase._run_push_job(failing, "send_push_to_tokens", {"tokens": ["tok"]})
        close_all.assert_called_once()


class SignalFollowerNotificationTests(TestCase):
    def setUp(self):
        self.analyst = User.objects.create_user(
            email="analyst@example.com",
            username="analyst@example.com",
            password="Testpass123!",
            user_type="analyst",
        )
        self.follower = User.objects.create_user(
            email="follower@example.com",
            username="follower@example.com",
            password="Testpass123!",
            user_type="trader",
        )
        self.inactive_follower = User.objects.create_user(
            email="inactive@example.com",
            username="inactive@example.com",
            password="Testpass123!",
            user_type="trader",
            is_active=False,
        )
        Follow.objects.create(
            follower=self.follower,
            followed=self.analyst,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        )
        Follow.objects.create(
            follower=self.inactive_follower,
            followed=self.analyst,
            status=Follow.Status.ACCEPTED,
            is_active=True,
        )
        self.asset_class = AssetClass.objects.create(name="Forex")
        self.instrument = Instrument.objects.create(
            asset_class=self.asset_class,
            symbol="EURUSD",
            name="Euro / US Dollar",
        )
        self.timeframe = Timeframe.objects.create(code="H1", name="1 Hour")

    def test_signal_notification_recipients_include_active_followers(self):
        recipient_ids = {user.id for user in _get_signal_notification_recipients(self.analyst)}
        self.assertEqual(recipient_ids, {self.follower.id})

    @patch("Signals.views._send_push_notifications")
    @patch("Signals.views._create_and_broadcast_notifications")
    def test_notify_signal_published_targets_followers(self, mock_broadcast, mock_push):
        signal = TradingSignal.objects.create(
            analyst=self.analyst,
            asset_class=self.asset_class,
            instrument=self.instrument,
            timeframe=self.timeframe,
            direction=TradingSignal.Direction.BUY,
            entry_price="1.10000",
            stop_loss="1.09000",
            take_profit="1.12000",
            confidence_level=80,
            status=TradingSignal.Status.OPEN,
        )

        _notify_signal_published(signal)

        recipients = mock_broadcast.call_args.args[0]
        self.assertEqual([user.id for user in recipients], [self.follower.id])
        self.assertEqual(mock_push.call_args.args[0], recipients)

    @patch("Signals.views._send_push_notifications")
    @patch("Signals.views._create_and_broadcast_notifications")
    def test_notify_signal_closed_targets_followers(self, mock_broadcast, mock_push):
        signal = TradingSignal.objects.create(
            analyst=self.analyst,
            asset_class=self.asset_class,
            instrument=self.instrument,
            timeframe=self.timeframe,
            direction=TradingSignal.Direction.SELL,
            entry_price="1.20000",
            stop_loss="1.21000",
            take_profit="1.18000",
            confidence_level=75,
            status=TradingSignal.Status.CLOSED,
            is_win=True,
            is_loss=False,
            is_neutral=False,
        )

        _notify_signal_closed(signal, old_status=TradingSignal.Status.OPEN)

        recipients = mock_broadcast.call_args.args[0]
        self.assertEqual([user.id for user in recipients], [self.follower.id])
        self.assertEqual(mock_push.call_args.args[0], recipients)
        self.assertEqual(mock_push.call_args.kwargs["data"]["close_outcome"], "profit")


class SignalEntryLifecycleTests(TestCase):
    def setUp(self):
        self.analyst = User.objects.create_user(
            email="entry-analyst@example.com",
            username="entry-analyst@example.com",
            password="Testpass123!",
            user_type="analyst",
        )
        self.asset_class = AssetClass.objects.create(name="Forex")
        self.instrument = Instrument.objects.create(
            asset_class=self.asset_class,
            symbol="EURUSD",
            name="Euro / US Dollar",
        )
        self.timeframe = Timeframe.objects.create(code="H1", name="1 Hour")

    def test_buy_signal_waits_for_entry_before_activation(self):
        signal = TradingSignal.objects.create(
            analyst=self.analyst,
            asset_class=self.asset_class,
            instrument=self.instrument,
            timeframe=self.timeframe,
            direction=TradingSignal.Direction.BUY,
            entry_price="1.10000",
            stop_loss="1.09000",
            take_profit="1.12000",
            confidence_level=80,
            status=TradingSignal.Status.OPEN,
        )

        is_entered, _, entered_now = _ensure_signal_entry_state(signal, bid=1.09400, ask=1.09500)
        signal.refresh_from_db()
        self.assertFalse(is_entered)
        self.assertFalse(entered_now)
        self.assertEqual(signal.entry_watch_direction, ENTRY_WATCH_UP)
        self.assertIsNone(signal.entry_triggered_at)

        is_entered, _, entered_now = _ensure_signal_entry_state(signal, bid=1.09950, ask=1.10020)
        signal.refresh_from_db()
        self.assertTrue(is_entered)
        self.assertTrue(entered_now)
        self.assertIsNotNone(signal.entry_triggered_at)

    def test_sell_signal_waits_for_entry_before_activation(self):
        signal = TradingSignal.objects.create(
            analyst=self.analyst,
            asset_class=self.asset_class,
            instrument=self.instrument,
            timeframe=self.timeframe,
            direction=TradingSignal.Direction.SELL,
            entry_price="1.20000",
            stop_loss="1.21000",
            take_profit="1.18000",
            confidence_level=75,
            status=TradingSignal.Status.OPEN,
        )

        is_entered, _, entered_now = _ensure_signal_entry_state(signal, bid=1.20500, ask=1.20520)
        signal.refresh_from_db()
        self.assertFalse(is_entered)
        self.assertFalse(entered_now)
        self.assertEqual(signal.entry_watch_direction, ENTRY_WATCH_DOWN)
        self.assertIsNone(signal.entry_triggered_at)

        is_entered, _, entered_now = _ensure_signal_entry_state(signal, bid=1.19980, ask=1.20000)
        signal.refresh_from_db()
        self.assertTrue(is_entered)
        self.assertTrue(entered_now)
        self.assertIsNotNone(signal.entry_triggered_at)

    def test_reset_signal_lifecycle_clears_entry_state_for_reopened_signal(self):
        signal = TradingSignal.objects.create(
            analyst=self.analyst,
            asset_class=self.asset_class,
            instrument=self.instrument,
            timeframe=self.timeframe,
            direction=TradingSignal.Direction.BUY,
            entry_price="1.10000",
            stop_loss="1.09000",
            take_profit="1.12000",
            confidence_level=80,
            status=TradingSignal.Status.OPEN,
            entry_watch_direction=ENTRY_WATCH_UP,
            entry_triggered_at=timezone.now(),
            price_alert_fcm_sent=True,
            is_win=True,
            is_loss=False,
            is_neutral=False,
        )

        signal.status = TradingSignal.Status.OPEN
        signal.entry_price = "1.10100"
        signal.save(update_fields=["status", "entry_price", "updated_at"])

        _reset_signal_lifecycle_if_needed(
            signal,
            old_status=TradingSignal.Status.CLOSED,
            old_direction=TradingSignal.Direction.BUY,
            old_entry_price="1.10000",
            old_instrument_id=signal.instrument_id,
        )
        signal.refresh_from_db()

        self.assertIsNone(signal.entry_triggered_at)
        self.assertIsNone(signal.entry_watch_direction)
        self.assertFalse(signal.price_alert_fcm_sent)
        self.assertIsNone(signal.is_win)
        self.assertIsNone(signal.is_loss)
        self.assertIsNone(signal.is_neutral)


class PriceAlertCreateSerializerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="alert-create@example.com",
            username="alert-create@example.com",
            password="Testpass123!",
            user_type="trader",
        )
        self.asset_class = AssetClass.objects.create(name="Forex")
        self.instrument = Instrument.objects.create(
            asset_class=self.asset_class,
            symbol="XAUUSD",
            name="Gold",
        )

    def _serializer(self, payload):
        from rest_framework.test import APIRequestFactory

        from Signals.serializers import PriceAlertCreateSerializer

        request = APIRequestFactory().post("/signals/price-alerts/create/", payload, format="json")
        request.user = self.user
        return PriceAlertCreateSerializer(data=payload, context={"request": request})

    def test_target_price_keeps_client_decimal_places(self):
        serializer = self._serializer(
            {
                "instrument": str(self.instrument.id),
                "target_price": "2650.5",
                "condition": "above",
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        alert = serializer.save()
        self.assertEqual(alert.target_price, Decimal("2650.5"))
        self.assertEqual(serializer.data["target_price"], "2650.5")

    def test_reference_price_and_percentage_keep_client_precision(self):
        serializer = self._serializer(
            {
                "instrument": str(self.instrument.id),
                "target_percentage": "5",
                "reference_price": "100.25",
                "condition": "above",
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        alert = serializer.save()
        self.assertEqual(alert.target_percentage, Decimal("5"))
        self.assertEqual(alert.reference_price, Decimal("100.25"))
        self.assertEqual(serializer.data["target_percentage"], "5")
        self.assertEqual(serializer.data["reference_price"], "100.25")


class PriceAlertLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="alert-user@example.com",
            username="alert-user@example.com",
            password="Testpass123!",
            user_type="trader",
        )
        self.asset_class = AssetClass.objects.create(name="Forex")
        self.instrument = Instrument.objects.create(
            asset_class=self.asset_class,
            symbol="EURUSD",
            name="Euro / US Dollar",
        )

    def test_percentage_alert_waits_for_reference_before_target(self):
        alert = PriceAlert.objects.create(
            user=self.user,
            instrument=self.instrument,
            target_percentage="5.0000",
            reference_price="100.00000",
            condition=PriceAlert.Condition.ABOVE,
            label="EURUSD breakout",
        )

        is_armed, _, armed_now = _ensure_user_alert_activation_state(alert, Decimal("98.00000"))
        alert.refresh_from_db()
        self.assertFalse(is_armed)
        self.assertFalse(armed_now)
        self.assertEqual(alert.activation_price, Decimal("100.00000"))
        self.assertEqual(alert.activation_watch_direction, ENTRY_WATCH_UP)
        self.assertIsNone(alert.armed_at)
        self.assertFalse(_check_user_alert_hit(alert, Decimal("105.00000")))

        is_armed, _, armed_now = _ensure_user_alert_activation_state(alert, Decimal("100.00000"))
        alert.refresh_from_db()
        self.assertTrue(is_armed)
        self.assertTrue(armed_now)
        self.assertIsNotNone(alert.armed_at)
        self.assertFalse(_check_user_alert_hit(alert, Decimal("104.99000")))
        self.assertTrue(_check_user_alert_hit(alert, Decimal("105.00000")))

    def test_fixed_price_alert_arms_immediately_on_first_observation(self):
        alert = PriceAlert.objects.create(
            user=self.user,
            instrument=self.instrument,
            target_price="1.12000",
            condition=PriceAlert.Condition.ABOVE,
            label="EURUSD above 1.12",
        )

        is_armed, current_price, armed_now = _ensure_user_alert_activation_state(alert, Decimal("1.10000"))
        alert.refresh_from_db()
        self.assertTrue(is_armed)
        self.assertTrue(armed_now)
        self.assertEqual(current_price, Decimal("1.10000"))
        self.assertEqual(alert.activation_price, Decimal("1.10000"))
        self.assertIsNotNone(alert.armed_at)
        self.assertFalse(_check_user_alert_hit(alert, Decimal("1.11999")))
        self.assertTrue(_check_user_alert_hit(alert, Decimal("1.12000")))

    def test_percentage_alert_arms_when_market_already_past_reference(self):
        # Market moved slightly past the reference before the worker saw it: the alert must
        # not wait for a pull-back to the exact reference price.
        alert = PriceAlert.objects.create(
            user=self.user,
            instrument=self.instrument,
            target_percentage="5.0000",
            reference_price="100.00000",
            condition=PriceAlert.Condition.ABOVE,
        )
        is_armed, _, armed_now = _ensure_user_alert_activation_state(alert, 100.02)
        alert.refresh_from_db()
        self.assertTrue(is_armed)
        self.assertTrue(armed_now)
        self.assertIsNotNone(alert.armed_at)
        self.assertTrue(_check_user_alert_hit(alert, 105.0))

    def test_stuck_percentage_below_alert_is_released(self):
        # Alert saved by the old logic with watch direction "up" while market is between
        # reference and target of a "below" alert.
        alert = PriceAlert.objects.create(
            user=self.user,
            instrument=self.instrument,
            target_percentage="5.0000",
            reference_price="100.00000",
            condition=PriceAlert.Condition.BELOW,
            activation_price="100.00000",
            activation_watch_direction=ENTRY_WATCH_UP,
        )
        is_armed, _, _ = _ensure_user_alert_activation_state(alert, 99.9)
        self.assertTrue(is_armed)
        self.assertTrue(_check_user_alert_hit(alert, 95.0))

    def test_percentage_alert_already_past_target_still_waits(self):
        alert = PriceAlert.objects.create(
            user=self.user,
            instrument=self.instrument,
            target_percentage="5.0000",
            reference_price="100.00000",
            condition=PriceAlert.Condition.ABOVE,
        )
        is_armed, _, _ = _ensure_user_alert_activation_state(alert, 106.0)
        alert.refresh_from_db()
        self.assertFalse(is_armed)
        self.assertEqual(alert.activation_watch_direction, ENTRY_WATCH_DOWN)

    def _run_check_with_price(self, bid, push):
        prices = {"EURUSD": {"bid": bid, "ask": bid + 0.0001}}
        with patch(
            "Signals.management.commands.run_price_alerts._get_prices_from_mt5_db",
            return_value=prices,
        ), patch("firebase.send_push_to_users", push):
            cmd = PriceAlertCommand(stdout=Mock(), stderr=Mock())
            cmd._verbose = False
            cmd._use_mt5_lib = False
            cmd._use_mt5_manager = False
            cmd._run_check()

    def test_run_check_triggers_alert_and_sends_push_once(self):
        from Mainapp.models import UserNotification

        alert = PriceAlert.objects.create(
            user=self.user,
            instrument=self.instrument,
            target_price="1.12000",
            condition=PriceAlert.Condition.ABOVE,
        )
        push = Mock()

        self._run_check_with_price(1.10000, push)  # arms, below target
        alert.refresh_from_db()
        self.assertIsNotNone(alert.armed_at)
        self.assertFalse(alert.is_triggered)
        push.assert_not_called()

        self._run_check_with_price(1.12005, push)  # crosses target
        alert.refresh_from_db()
        self.assertTrue(alert.is_triggered)
        self.assertIsNotNone(alert.triggered_at)
        push.assert_called_once()
        self.assertEqual(push.call_args.kwargs["users"], [self.user])
        self.assertEqual(push.call_args.kwargs["data"]["type"], "user_price_alert")
        self.assertEqual(
            UserNotification.objects.filter(user=self.user, category="PRICE_ALERT").count(), 1
        )

        self._run_check_with_price(1.13000, push)  # already triggered: no duplicate push
        push.assert_called_once()

    def test_run_check_percentage_alert_end_to_end(self):
        alert = PriceAlert.objects.create(
            user=self.user,
            instrument=self.instrument,
            target_percentage="1.0000",
            reference_price="1.10000",
            condition=PriceAlert.Condition.ABOVE,
        )
        push = Mock()
        self._run_check_with_price(1.10020, push)  # just above reference
        self._run_check_with_price(1.11100, push)  # target = 1.111
        alert.refresh_from_db()
        self.assertTrue(alert.is_triggered)
        push.assert_called_once()

    def test_manager_health_falls_back_to_db_when_no_ticks(self):
        import Signals.management.commands.run_price_alerts as rpa

        cmd = PriceAlertCommand(stdout=Mock(), stderr=Mock())
        cmd._start_manager_thread = Mock()
        cmd._run_check = Mock()
        alive_thread = Mock(is_alive=Mock(return_value=True))
        with patch.object(rpa, "_manager_thread", alive_thread),                 patch.object(rpa, "_manager_connected", True),                 patch.object(rpa, "_manager_last_tick_at", time.monotonic() - 3600),                 patch.object(rpa, "_manager_required_symbols", {"EURUSD"}):
            cmd._check_manager_health()
        cmd._start_manager_thread.assert_not_called()
        cmd._run_check.assert_called_once_with(force_db=True)

    def test_manager_health_restarts_dead_thread(self):
        import Signals.management.commands.run_price_alerts as rpa

        cmd = PriceAlertCommand(stdout=Mock(), stderr=Mock())
        cmd._start_manager_thread = Mock()
        cmd._run_check = Mock()
        with patch.object(rpa, "_manager_thread", Mock(is_alive=Mock(return_value=False))),                 patch.object(rpa, "_manager_connected", True),                 patch.object(rpa, "_manager_last_tick_at", time.monotonic()),                 patch.object(rpa, "_manager_required_symbols", {"EURUSD"}):
            cmd._check_manager_health()
        cmd._start_manager_thread.assert_called_once()
        cmd._run_check.assert_not_called()

    def test_alert_fires_when_float_price_touches_exact_target(self):
        above = PriceAlert.objects.create(
            user=self.user, instrument=self.instrument,
            target_percentage=Decimal("1"), reference_price=Decimal("1.10000"),
            condition=PriceAlert.Condition.ABOVE,
        )
        below = PriceAlert.objects.create(
            user=self.user, instrument=self.instrument,
            target_price=Decimal("1.11100"), condition=PriceAlert.Condition.BELOW,
        )
        # float(1.111) is 1.11099999..., which is < Decimal("1.111")
        above.armed_at = below.armed_at = timezone.now()
        self.assertTrue(_check_user_alert_hit(above, 1.111))
        self.assertTrue(_check_user_alert_hit(below, 1.111))


class FirebaseInitTests(SimpleTestCase):
    def test_credential_path_is_absolute_and_exists(self):
        import os
        import firebase

        self.assertTrue(os.path.isabs(firebase._CREDENTIAL_PATH))
        self.assertTrue(os.path.exists(firebase._CREDENTIAL_PATH))

    def test_push_result_summary(self):
        from Signals.management.commands.run_price_alerts import _describe_push_result

        self.assertIn("no device tokens", _describe_push_result({"success_count": 0, "failure_count": 0}))
        self.assertEqual(
            _describe_push_result({"success_count": 1, "failure_count": 1, "errors": ["Requested entity was not found."]}),
            "success=1 failure=1 first_error=Requested entity was not found.",
        )

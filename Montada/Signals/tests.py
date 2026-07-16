from unittest.mock import Mock, patch
from decimal import Decimal
import json
import time

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
        self.command = Command()
        self.command.threshold = Decimal("0.5")

    def test_share_asset_class_uses_one_percent_threshold(self):
        asset_class = AssetClass.objects.create(name="Shares")
        instrument = Instrument.objects.create(asset_class=asset_class, symbol="AAPL")
        signal = TradingSignal.objects.create(
            analyst=User.objects.create_user(
                email="analyst@example.com",
                username="analyst@example.com",
                password="Testpass123!",
                user_type="analyst",
            ),
            asset_class=asset_class,
            instrument=instrument,
            timeframe=Timeframe.objects.create(code="H1", name="1 Hour"),
            direction=TradingSignal.Direction.BUY,
            entry_price="1.00000",
            stop_loss="0.95000",
            take_profit="1.10000",
            confidence_level=80,
            status=TradingSignal.Status.OPEN,
        )

        self.assertEqual(self.command._get_notification_threshold([signal]), Decimal("1.0"))

    def test_non_share_asset_class_keeps_half_percent_threshold(self):
        asset_class = AssetClass.objects.create(name="Forex")
        instrument = Instrument.objects.create(asset_class=asset_class, symbol="EURUSD")
        signal = TradingSignal.objects.create(
            analyst=User.objects.create_user(
                email="analyst2@example.com",
                username="analyst2@example.com",
                password="Testpass123!",
                user_type="analyst",
            ),
            asset_class=asset_class,
            instrument=instrument,
            timeframe=Timeframe.objects.create(code="H4", name="4 Hours"),
            direction=TradingSignal.Direction.SELL,
            entry_price="1.10000",
            stop_loss="1.05000",
            take_profit="1.00000",
            confidence_level=75,
            status=TradingSignal.Status.OPEN,
        )

        self.assertEqual(self.command._get_notification_threshold([signal]), Decimal("0.5"))

    def test_mena_share_asset_class_keeps_half_percent_threshold(self):
        asset_class = AssetClass.objects.create(name="Mena Shares")
        instrument = Instrument.objects.create(asset_class=asset_class, symbol="DEWA")
        signal = TradingSignal.objects.create(
            analyst=User.objects.create_user(
                email="analyst3@example.com",
                username="analyst3@example.com",
                password="Testpass123!",
                user_type="analyst",
            ),
            asset_class=asset_class,
            instrument=instrument,
            timeframe=Timeframe.objects.create(code="D1", name="1 Day"),
            direction=TradingSignal.Direction.BUY,
            entry_price="10.00000",
            stop_loss="9.50000",
            take_profit="11.00000",
            confidence_level=70,
            status=TradingSignal.Status.OPEN,
        )

        self.assertEqual(self.command._get_notification_threshold([signal]), Decimal("0.5"))

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

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from Followers.models import Follow
from Signals.models import AssetClass, Instrument, SignalStopLossChange, Timeframe, TradingSignal

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        self.analyst = User.objects.create_user(
            username="an", email="an@example.com", password="x", user_type="analyst", name="Ann Analyst",
        )
        self.other_analyst = User.objects.create_user(
            username="an2", email="an2@example.com", password="x", user_type="analyst",
        )
        self.follower = User.objects.create_user(username="tr", email="tr@example.com", password="x")
        Follow.objects.create(
            follower=self.follower, followed=self.analyst, status=Follow.Status.ACCEPTED, is_active=True,
        )
        self.asset = AssetClass.objects.create(name="Forex")
        self.instrument = Instrument.objects.create(asset_class=self.asset, symbol="EURUSD")
        self.timeframe = Timeframe.objects.create(code="H1", name="1 Hour")
        self.client = APIClient()
        self.client.force_authenticate(self.analyst)

    def body(self, **overrides):
        data = {
            "asset_class": str(self.asset.pk),
            "instrument": str(self.instrument.pk),
            "direction": "BUY",
            "entry_price": "1.08500",
            "stop_loss": "1.08000",
            "take_profit": "1.09500",
            "timeframe": str(self.timeframe.pk),
            "confidence_level": 80,
            "status": "OPEN",
        }
        data.update(overrides)
        return data

    def make_signal(self, **fields):
        values = dict(
            analyst=self.analyst, asset_class=self.asset, instrument=self.instrument,
            direction="BUY", entry_price=Decimal("1.08500"), stop_loss=Decimal("1.08000"),
            take_profit=Decimal("1.09500"), timeframe=self.timeframe, confidence_level=80,
            status=TradingSignal.Status.OPEN,
        )
        values.update(fields)
        return TradingSignal.objects.create(**values)


class TargetPriceTests(_Base):
    def create(self, **overrides):
        with patch("Signals.views._notify_signal_published"):
            return self.client.post("/api/signals/create/", self.body(**overrides), format="json")

    def test_one_target_returns_null_for_the_others(self):
        res = self.create()
        self.assertEqual(res.status_code, 201, res.content)
        signal = res.json()["signal"]
        self.assertIn("take_profit_2", signal)
        self.assertIn("take_profit_3", signal)
        self.assertIsNone(signal["take_profit_2"])
        self.assertIsNone(signal["take_profit_3"])

    def test_two_and_three_targets(self):
        res = self.create(take_profit_2="1.10000")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Decimal(res.json()["signal"]["take_profit_2"]), Decimal("1.1"))
        self.assertIsNone(res.json()["signal"]["take_profit_3"])

        res = self.create(take_profit_2="1.10000", take_profit_3="1.11000")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Decimal(res.json()["signal"]["take_profit_3"]), Decimal("1.11"))

    def test_sell_targets_descend(self):
        res = self.create(
            direction="SELL", stop_loss="1.09000", take_profit="1.07500",
            take_profit_2="1.07000", take_profit_3="1.06000",
        )
        self.assertEqual(res.status_code, 201, res.content)

    def test_buy_ordering_errors_are_field_keyed(self):
        res = self.create(take_profit_2="1.09000")  # below TP1
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit_2", res.json())
        res = self.create(take_profit_2="1.10000", take_profit_3="1.10000")  # equal to TP2
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit_3", res.json())
        res = self.create(take_profit="1.08000")  # TP1 below entry
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit", res.json())

    def test_sell_ordering_errors(self):
        sell = dict(direction="SELL", stop_loss="1.09000", take_profit="1.07500")
        res = self.create(take_profit_2="1.08000", **sell)  # above TP1
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit_2", res.json())
        res = self.create(take_profit_2="1.07000", take_profit_3="1.07200", **sell)
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit_3", res.json())

    def test_targets_must_be_positive(self):
        res = self.create(take_profit_2="-1")
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit_2", res.json())

    def test_tp3_requires_tp2(self):
        res = self.create(take_profit_3="1.11000")
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit_3", res.json())

    def test_draft_skips_ordering_checks(self):
        res = self.create(status="DRAFT", take_profit_2="1.00000")
        self.assertEqual(res.status_code, 201, res.content)

    def test_edit_adds_and_validates_targets(self):
        signal = self.make_signal()
        url = f"/api/signals/edit-my-signals/{signal.pk}/"
        res = self.client.put(url, self.body(take_profit_2="1.10000", take_profit_3="1.11000"), format="json")
        self.assertEqual(res.status_code, 200, res.content)
        signal.refresh_from_db()
        self.assertEqual(signal.take_profit_2, Decimal("1.10000"))
        self.assertEqual(signal.take_profit_3, Decimal("1.11000"))

        res = self.client.put(url, self.body(take_profit_2="1.09000", take_profit_3="1.11000"), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit_2", res.json())

        # Clearing TP2 while TP3 stays is rejected; clearing both is fine.
        res = self.client.put(url, self.body(take_profit_2=None, take_profit_3="1.11000"), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("take_profit_3", res.json())
        res = self.client.put(url, self.body(take_profit_2=None, take_profit_3=None), format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_list_and_detail_return_the_keys(self):
        self.make_signal(take_profit_2=Decimal("1.10000"))
        listing = self.client.get("/api/signals/my-signals/").json()
        self.assertEqual(Decimal(str(listing["results"][0]["take_profit_2"])), Decimal("1.1"))
        self.assertIsNone(listing["results"][0]["take_profit_3"])


class StopLossMoveNotifyTests(_Base):
    def notify(self, signal, category="move_sl_to_entry", client=None, **extra):
        captured = {}

        def fake_run(func, name, **kwargs):
            captured.update(kwargs)

        body = {"audience": "followers", "category": category, **extra}
        with patch("firebase.run_in_background", side_effect=fake_run), \
                patch("firebase.get_push_tokens_for_users", return_value=["tok"]):
            res = (client or self.client).post(f"/api/signals/{signal.pk}/notify/", body, format="json")
        return res, captured

    def test_move_to_entry_updates_stop_writes_history_and_pushes(self):
        signal = self.make_signal(take_profit_2=Decimal("1.10000"))
        res, sent = self.notify(signal)
        self.assertEqual(res.status_code, 200, res.content)

        payload = res.json()
        self.assertEqual(Decimal(str(payload["signal"]["stop_loss"])), Decimal("1.085"))
        signal.refresh_from_db()
        self.assertEqual(signal.stop_loss, Decimal("1.08500"))

        change = SignalStopLossChange.objects.get(signal=signal)
        self.assertEqual(change.category, "move_sl_to_entry")
        self.assertEqual(change.previous_stop_loss, Decimal("1.08000"))
        self.assertEqual(change.new_stop_loss, Decimal("1.08500"))
        self.assertEqual(change.analyst, self.analyst)

        data = sent["data"]
        self.assertEqual(data["type"], "signal_alert")
        self.assertEqual(data["category"], "move_sl_to_entry")
        self.assertEqual(data["signal_id"], str(signal.pk))
        self.assertEqual(data["trade_idea_id"], str(signal.pk))
        self.assertEqual(data["symbol"], "EURUSD")
        self.assertEqual(data["direction"], "BUY")
        self.assertEqual(data["previous_stop_loss"], "1.08000")
        self.assertEqual(data["new_stop_loss"], "1.08500")
        self.assertEqual(data["entry_price"], "1.08500")
        self.assertEqual(data["take_profit"], "1.09500")
        self.assertEqual(data["take_profit_2"], "1.10000")
        self.assertEqual(data["take_profit_3"], "")
        self.assertEqual(data["title"], "EURUSD: Move stop loss to entry")
        self.assertEqual(
            data["body"], "Analyst Ann Analyst moved the stop loss to 1.08500 (entry). Adjust your trade."
        )
        self.assertEqual([u.pk for u in sent["users"]], [self.follower.pk])
        self.assertTrue(all(isinstance(v, str) for v in data.values()))

    def test_each_target_category(self):
        signal = self.make_signal(take_profit_2=Decimal("1.10000"), take_profit_3=Decimal("1.11000"))
        expected = {
            "move_sl_to_tp1": "1.09500",
            "move_sl_to_tp2": "1.10000",
            "move_sl_to_tp3": "1.11000",
        }
        for category, new_stop in expected.items():
            res, sent = self.notify(signal, category)
            self.assertEqual(res.status_code, 200, (category, res.content))
            self.assertEqual(sent["data"]["new_stop_loss"], new_stop)
            signal.refresh_from_db()
            self.assertEqual(signal.stop_loss, Decimal(new_stop))
        self.assertEqual(SignalStopLossChange.objects.filter(signal=signal).count(), 3)

    def test_notification_ids_are_unique_per_push(self):
        signal = self.make_signal(take_profit_2=Decimal("1.10000"))
        _, first = self.notify(signal, "move_sl_to_entry")
        _, second = self.notify(signal, "move_sl_to_tp1")
        self.assertNotEqual(first["data"]["notification_id"], second["data"]["notification_id"])

    def test_missing_target_rejected_and_nothing_changes(self):
        signal = self.make_signal()
        for category in ("move_sl_to_tp2", "move_sl_to_tp3"):
            res, sent = self.notify(signal, category)
            self.assertEqual(res.status_code, 400)
            self.assertIn("target", res.json()["error"])
            self.assertEqual(sent, {})
        signal.refresh_from_db()
        self.assertEqual(signal.stop_loss, Decimal("1.08000"))
        self.assertFalse(SignalStopLossChange.objects.exists())

    def test_closed_and_draft_rejected(self):
        for state in (TradingSignal.Status.CLOSED, TradingSignal.Status.DRAFT):
            kwargs = {"is_neutral": True} if state == TradingSignal.Status.CLOSED else {}
            signal = self.make_signal(status=state, **kwargs)
            res, sent = self.notify(signal)
            self.assertEqual(res.status_code, 400, state)
            self.assertEqual(sent, {})
        self.assertFalse(SignalStopLossChange.objects.exists())

    def test_not_owner_gets_404(self):
        signal = self.make_signal()
        other = APIClient()
        other.force_authenticate(self.other_analyst)
        res, sent = self.notify(signal, client=other)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(sent, {})
        signal.refresh_from_db()
        self.assertEqual(signal.stop_loss, Decimal("1.08000"))

    def test_stop_still_moves_when_nobody_to_notify(self):
        Follow.objects.all().delete()
        signal = self.make_signal()
        res, sent = self.notify(signal)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["recipient_count"], 0)
        self.assertIn("signal", res.json())
        signal.refresh_from_db()
        self.assertEqual(signal.stop_loss, Decimal("1.08500"))
        self.assertEqual(sent, {})

    def test_existing_categories_unchanged(self):
        signal = self.make_signal()
        res, sent = self.notify(signal, "exit")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("signal", res.json())
        self.assertNotIn("previous_stop_loss", sent["data"])
        signal.refresh_from_db()
        self.assertEqual(signal.stop_loss, Decimal("1.08000"))
        self.assertFalse(SignalStopLossChange.objects.exists())

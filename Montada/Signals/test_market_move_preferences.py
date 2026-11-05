from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from Mainapp.notification_preferences import Category, save_user_modes
from Signals import market_move_preferences as prefs
from Signals.models import AssetClass, Instrument, MarketMovePreference

User = get_user_model()
BASE = "/api/signals/market-move-preferences/"


class CategoryForSymbolTests(SimpleTestCase):
    def test_maps_each_of_the_eight_categories(self):
        expected = {
            "EURUSD": "forex",
            "GOLD": "metals",
            "XAUUSD": "metals",
            "USOIL": "energy",
            "US30": "indices",
            "WHEAT_Z26": "agriculture",
            "SOYBEAN": "agriculture",
            "BTCUSD": "crypto",
            "ADNOC.Gas": "mena_shares",
            "AAPL": "share_cfds",
        }
        for symbol, category in expected.items():
            self.assertEqual(prefs.category_for_symbol(symbol), category, symbol)

    def test_unknown_symbol_falls_back_to_asset_class_name(self):
        self.assertIsNone(prefs.category_for_symbol("ZZZZ"))
        self.assertEqual(prefs.category_for_symbol("ZZZZ", ["Mena Shares"]), "mena_shares")

    def test_resolution_priority(self):
        self.assertEqual(prefs.resolve_enabled(symbol_choice=None, category_choice=None), (True, "default"))
        self.assertEqual(prefs.resolve_enabled(symbol_choice=None, category_choice=False), (False, "category"))
        self.assertEqual(prefs.resolve_enabled(symbol_choice=True, category_choice=False), (True, "symbol"))
        self.assertEqual(prefs.resolve_enabled(symbol_choice=False, category_choice=True), (False, "symbol"))


class MarketMovePreferenceTests(TestCase):
    def setUp(self):
        forex = AssetClass.objects.create(name="Forex")
        metals = AssetClass.objects.create(name="Metals")
        self.eur = Instrument.objects.create(asset_class=forex, symbol="EURUSD")
        self.gbp = Instrument.objects.create(asset_class=forex, symbol="GBPUSD")
        self.gold = Instrument.objects.create(asset_class=metals, symbol="GOLD")
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="x")
        self.other = User.objects.create_user(username="u2", email="u2@example.com", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # --- API -------------------------------------------------------------

    def test_get_lists_all_categories_inheriting_by_default(self):
        data = self.client.get(BASE).json()
        self.assertTrue(data["global"]["enabled"])
        self.assertEqual([c["key"] for c in data["categories"]], list(prefs.CATEGORY_KEYS))
        self.assertTrue(all(c["enabled"] and c["preference"] is None for c in data["categories"]))

    def test_global_off_is_reported(self):
        save_user_modes(self.user, {Category.SYMBOL_MOVES: "off"})
        self.assertFalse(self.client.get(BASE).json()["global"]["enabled"])

    def test_category_update_and_reset(self):
        res = self.client.put(BASE + "categories/", {"categories": {"forex": False, "metals": True}}, format="json")
        self.assertEqual(res.status_code, 200)
        rows = {c["key"]: c for c in res.json()["categories"]}
        self.assertFalse(rows["forex"]["enabled"])
        self.assertEqual(rows["metals"]["preference"], True)

        res = self.client.put(BASE + "categories/", {"categories": {"forex": None}}, format="json")
        rows = {c["key"]: c for c in res.json()["categories"]}
        self.assertTrue(rows["forex"]["enabled"])
        self.assertIsNone(rows["forex"]["preference"])
        self.assertFalse(MarketMovePreference.objects.filter(user=self.user, key="forex").exists())

    def test_unknown_category_rejected(self):
        res = self.client.put(BASE + "categories/", {"categories": {"bonds": False}}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_symbol_choice_overrides_category_and_reset_restores_inheritance(self):
        self.client.put(BASE + "categories/", {"categories": {"forex": False}}, format="json")

        row = self.client.get(BASE + "symbols/GBPUSD/").json()
        self.assertFalse(row["enabled"])
        self.assertEqual(row["source"], "category")

        res = self.client.put(BASE + "symbols/EURUSD/", {"enabled": True}, format="json").json()
        self.assertTrue(res["enabled"])
        self.assertEqual(res["source"], "symbol")
        self.assertEqual(res["category"], "forex")

        res = self.client.delete(BASE + "symbols/EURUSD/").json()
        self.assertFalse(res["enabled"])
        self.assertEqual(res["source"], "category")
        self.assertIsNone(res["preference"])

    def test_symbol_disabled_while_category_enabled(self):
        res = self.client.put(BASE + "symbols/GBPUSD/", {"enabled": False}, format="json").json()
        self.assertFalse(res["enabled"])
        self.assertTrue(self.client.get(BASE + "symbols/EURUSD/").json()["enabled"])

    def test_search_filter_and_pagination_shape(self):
        res = self.client.get(BASE + "symbols/", {"search": "usd"}).json()
        self.assertEqual({r["symbol"] for r in res["results"]}, {"EURUSD", "GBPUSD"})
        res = self.client.get(BASE + "symbols/", {"category": "metals"}).json()
        self.assertEqual([r["symbol"] for r in res["results"]], ["GOLD"])
        self.client.put(BASE + "symbols/GOLD/", {"enabled": False}, format="json")
        res = self.client.get(BASE + "symbols/", {"preference": "set"}).json()
        self.assertEqual([r["symbol"] for r in res["results"]], ["GOLD"])

    def test_unknown_symbol_404(self):
        self.assertEqual(self.client.put(BASE + "symbols/NOPE/", {"enabled": True}, format="json").status_code, 404)

    def test_requires_auth(self):
        self.assertEqual(APIClient().get(BASE).status_code, 401)

    def test_other_users_unaffected(self):
        self.client.put(BASE + "symbols/EURUSD/", {"enabled": False}, format="json")
        other = APIClient()
        other.force_authenticate(self.other)
        self.assertTrue(other.get(BASE + "symbols/EURUSD/").json()["enabled"])

    # --- alert delivery ----------------------------------------------------

    def test_opted_out_user_ids_follows_priority(self):
        prefs.set_category_choices(self.user, {"forex": False})
        prefs.set_category_choices(self.other, {"forex": False})
        prefs.set_symbol_choice(self.other, "EURUSD", True)  # symbol beats category

        self.assertEqual(prefs.opted_out_user_ids("EURUSD"), {self.user.pk})
        self.assertEqual(prefs.opted_out_user_ids("GBPUSD"), {self.user.pk, self.other.pk})
        self.assertEqual(prefs.opted_out_user_ids("GOLD"), set())

        prefs.set_symbol_choice(self.user, "GOLD", False)  # symbol off inside an enabled category
        self.assertEqual(prefs.opted_out_user_ids("GOLD"), {self.user.pk})

        prefs.set_symbol_choice(self.other, "EURUSD", None)  # reset -> inherits the category
        self.assertEqual(prefs.opted_out_user_ids("EURUSD"), {self.user.pk, self.other.pk})

    def test_alert_skips_opted_out_users_for_in_app_and_push(self):
        from Mainapp.models import UserNotification
        from Signals.management.commands.poll_signal_change_notifications import Command

        prefs.set_symbol_choice(self.user, "EURUSD", False)
        command = Command()
        command.max_users_per_notification = 0
        command.verbose = False
        with patch.object(Command, "_get_current_price", return_value="1.1"), \
                patch("firebase.send_push_to_users") as send_push:
            sent = command._notify_all_users(
                symbol="EURUSD", direction="up", signed_crossed_percentage=0.5,
                crossed_percentage=0.5, current_percentage=0.6, quote={}, signal_ids=[],
            )
        self.assertTrue(sent)
        notified = set(UserNotification.objects.values_list("user_id", flat=True))
        self.assertNotIn(self.user.pk, notified)
        self.assertIn(self.other.pk, notified)
        pushed = {u.pk for u in send_push.call_args.kwargs["users"]}
        self.assertNotIn(self.user.pk, pushed)
        self.assertIn(self.other.pk, pushed)

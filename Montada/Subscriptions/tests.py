from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from Subscriptions.access import check_active_subscription, market_news_and_data_free_access_enabled


class MarketNewsAndDataFreeAccessTests(SimpleTestCase):
    @override_settings(MARKET_NEWS_AND_DATA_FREE_ACCESS=True)
    def test_free_access_bypasses_subscription_check(self):
        user = SimpleNamespace(
            admin_granted_in_app_access=False,
            admin_in_app_access_expires_at=None,
        )
        self.assertTrue(market_news_and_data_free_access_enabled())
        self.assertIsNone(check_active_subscription(user))

    @override_settings(MARKET_NEWS_AND_DATA_FREE_ACCESS=False)
    @patch("Subscriptions.models.Subscription.objects.get", side_effect=Exception("DoesNotExist"))
    def test_paywall_when_free_access_disabled(self, _mock_get):
        from Subscriptions.models import Subscription

        _mock_get.side_effect = Subscription.DoesNotExist
        user = SimpleNamespace(
            admin_granted_in_app_access=False,
            admin_in_app_access_expires_at=None,
        )
        self.assertFalse(market_news_and_data_free_access_enabled())
        denied = check_active_subscription(user)
        self.assertIsNotNone(denied)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.data.get("code"), "subscription_required")

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

import firebase
from Followers.models import Follow
from Mainapp.models import DeviceToken, UserNotification

User = get_user_model()


def make_user(email, **extra):
    return User.objects.create_user(
        email=email, username=email, password="Testpass123!", is_verified=True, **extra
    )


@override_settings(FCM_PUSH_ENABLED=True, FCM_PUSH_ASYNC=False)
class FollowPushTests(APITestCase):
    def setUp(self):
        self.analyst = make_user("analyst@example.com", user_type="analyst", name="Ann Analyst")
        self.trader = make_user("trader@example.com", user_type="trader", name="Tom Trader")
        self.client = APIClient()
        self.client.force_authenticate(self.trader)
        DeviceToken.objects.create(user=self.analyst, fcm_token="analyst-token", device_id="dev-1")

    def _follow(self):
        return self.client.post(
            reverse("Followers:follow_request"), {"user_id": str(self.analyst.id)}, format="json"
        )

    def test_follow_sends_push_to_analyst_device(self):
        sent = []

        def fake_send(**kwargs):
            sent.append(kwargs)
            return {"success_count": len(kwargs["tokens"]), "failure_count": 0, "failed_tokens": [], "errors": []}

        with patch.object(firebase, "send_push_to_tokens", side_effect=fake_send):
            res = self._follow()

        self.assertEqual(res.status_code, 201)
        # In-app notification is still created.
        self.assertTrue(UserNotification.objects.filter(user=self.analyst, category="FOLLOWER").exists())
        # ...and now the push reaches the analyst's device.
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["tokens"], ["analyst-token"])
        self.assertEqual(sent[0]["data"]["type"], "new_follower")
        self.assertEqual(sent[0]["data"]["follower_id"], str(self.trader.id))
        self.assertTrue(sent[0]["sound"])

    def test_push_failure_does_not_break_follow(self):
        with patch.object(firebase, "send_push_to_users_in_background", side_effect=RuntimeError("fcm down")):
            res = self._follow()
        self.assertEqual(res.status_code, 201)
        self.assertTrue(Follow.objects.filter(follower=self.trader, followed=self.analyst).exists())
        self.assertTrue(UserNotification.objects.filter(user=self.analyst, category="FOLLOWER").exists())

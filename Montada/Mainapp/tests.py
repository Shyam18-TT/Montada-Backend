from django.test import TestCase

from firebase import get_push_tokens_for_users
from .models import DeviceToken, User
from .serializers import UserProfileSerializer, UserRegistrationSerializer
from News.management.commands.run_fxstreet_news_stream import _get_news_notification_recipients


class UserNewsLanguagePreferenceTests(TestCase):
    def test_registration_defaults_to_arabic_and_english(self):
        serializer = UserRegistrationSerializer(
            data={
                "email": "default@example.com",
                "password": "Testpass123!",
                "user_type": "trader",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()

        self.assertTrue(user.news_notify_ar)
        self.assertTrue(user.news_notify_en)
        self.assertFalse(user.news_notify_zh)
        serialized_user = UserRegistrationSerializer(user).data
        self.assertEqual(serialized_user["news_notification_languages"], ["ar", "en"])
        self.assertEqual(serialized_user["news_notification_selection_limit"], 2)
        self.assertEqual(
            serialized_user["news_notification_language_options"],
            [
                {"code": "ar", "label": "Arabic"},
                {"code": "en", "label": "English"},
                {"code": "zh", "label": "Chinese"},
            ],
        )

    def test_registration_requires_exactly_two_languages_when_explicitly_set(self):
        serializer = UserRegistrationSerializer(
            data={
                "email": "invalid@example.com",
                "password": "Testpass123!",
                "user_type": "trader",
                "news_notify_ar": True,
                "news_notify_en": True,
                "news_notify_zh": True,
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("news_language_preferences", serializer.errors)

    def test_profile_update_requires_exactly_two_languages(self):
        user = User.objects.create_user(
            email="profile@example.com",
            username="profile@example.com",
            password="Testpass123!",
            news_notify_ar=True,
            news_notify_en=True,
            news_notify_zh=False,
        )

        serializer = UserProfileSerializer(
            user,
            data={
                "news_notify_ar": False,
            },
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("news_language_preferences", serializer.errors)

    def test_news_notification_recipients_follow_saved_language_preferences(self):
        english_user = User.objects.create_user(
            email="english@example.com",
            username="english@example.com",
            password="Testpass123!",
            news_notify_ar=False,
            news_notify_en=True,
            news_notify_zh=True,
        )
        arabic_user = User.objects.create_user(
            email="arabic@example.com",
            username="arabic@example.com",
            password="Testpass123!",
            news_notify_ar=True,
            news_notify_en=False,
            news_notify_zh=True,
        )

        english_ids = {user.id for user in _get_news_notification_recipients("en")}
        arabic_ids = {user.id for user in _get_news_notification_recipients("ar")}

        self.assertEqual(english_ids, {english_user.id})
        self.assertEqual(arabic_ids, {arabic_user.id})

    def test_profile_serializer_exposes_selected_language_codes(self):
        user = User.objects.create_user(
            email="codes@example.com",
            username="codes@example.com",
            password="Testpass123!",
            news_notify_ar=False,
            news_notify_en=True,
            news_notify_zh=True,
        )

        data = UserProfileSerializer(user).data

        self.assertEqual(data["news_notification_languages"], ["en", "zh"])
        self.assertEqual(data["news_notification_selection_limit"], 2)


class DeviceTokenSelectionTests(TestCase):
    def test_get_push_tokens_for_users_returns_all_distinct_tokens_for_users(self):
        user = User.objects.create_user(
            email="tokens@example.com",
            username="tokens@example.com",
            password="Testpass123!",
        )
        other_user = User.objects.create_user(
            email="other@example.com",
            username="other@example.com",
            password="Testpass123!",
        )

        old_token = DeviceToken.objects.create(user=user, fcm_token="old-token")
        new_token = DeviceToken.objects.create(user=user, fcm_token="new-token")
        other_token = DeviceToken.objects.create(user=other_user, fcm_token="other-token")
        DeviceToken.objects.create(user=user, fcm_token="new-token")

        tokens = get_push_tokens_for_users([user, other_user])

        self.assertIn(old_token.fcm_token, tokens)
        self.assertIn(new_token.fcm_token, tokens)
        self.assertIn(other_token.fcm_token, tokens)
        self.assertEqual(len(tokens), 3)

    def test_get_push_tokens_for_users_deduplicates_shared_device_across_users(self):
        user1 = User.objects.create_user(
            email="device1@example.com",
            username="device1@example.com",
            password="Testpass123!",
        )
        user2 = User.objects.create_user(
            email="device2@example.com",
            username="device2@example.com",
            password="Testpass123!",
        )

        DeviceToken.objects.create(
            user=user1,
            fcm_token="first-token",
            device_id="shared-device-1",
        )
        DeviceToken.objects.create(
            user=user2,
            fcm_token="second-token",
            device_id="shared-device-1",
        )

        tokens = get_push_tokens_for_users([user1, user2])

        self.assertEqual(len(tokens), 1)
        self.assertIn(tokens[0], ["first-token", "second-token"])


class NotificationPreferenceTests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(
            email="prefs@example.com", username="prefs@example.com", password="Testpass123!",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = "/api/auth/notification-preferences/"

    def _modes(self, response):
        return {item["category"]: item["mode"] for item in response.data["preferences"]}

    def test_defaults_to_sound_and_hides_analyst_only_categories_from_traders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        modes = self._modes(response)
        self.assertEqual(set(modes.values()), {"sound"})
        self.assertIn("NEWS", modes)
        self.assertNotIn("MY_SIGNAL_ACTIVITY", modes)
        self.assertEqual(response.data["modes"], ["sound", "silent", "off"])

    def test_update_with_mapping_and_with_booleans(self):
        response = self.client.put(
            self.url, {"preferences": {"NEWS": "silent", "MESSAGES": "off"}}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        modes = self._modes(response)
        self.assertEqual((modes["NEWS"], modes["MESSAGES"], modes["SYMBOL_MOVES"]), ("silent", "off", "sound"))

        response = self.client.patch(
            self.url,
            {"preferences": [
                {"category": "NEWS", "enabled": True, "sound": True},
                {"category": "SYMBOL_MOVES", "enabled": True, "sound": False},
            ]},
            format="json",
        )
        modes = self._modes(response)
        self.assertEqual((modes["NEWS"], modes["MESSAGES"], modes["SYMBOL_MOVES"]), ("sound", "off", "silent"))
        # Back to default deletes the row; only the two non-default choices are stored.
        self.assertEqual(self.user.notification_preferences.count(), 2)

    def test_rejects_unknown_category_and_mode(self):
        response = self.client.put(self.url, {"preferences": {"BOGUS": "off"}}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.put(self.url, {"preferences": {"NEWS": "loud"}}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_filters_and_sound_split(self):
        from .notification_preferences import (
            Category, category_for_push, filter_recipients, notifications_enabled, save_user_modes,
            split_by_sound,
        )

        quiet = User.objects.create_user(email="q@example.com", username="q@example.com", password="x")
        muted = User.objects.create_user(email="m@example.com", username="m@example.com", password="x")
        save_user_modes(quiet, {Category.NEWS: "silent"})
        save_user_modes(muted, {Category.NEWS: "off"})
        users = [self.user, quiet, muted]

        self.assertEqual(filter_recipients(users, Category.NEWS), [self.user, quiet])
        self.assertEqual(filter_recipients(users, Category.MESSAGES), users)
        self.assertEqual(split_by_sound(users, Category.NEWS), ([self.user], [quiet]))
        self.assertFalse(notifications_enabled(muted, Category.NEWS))
        self.assertTrue(notifications_enabled(muted, Category.MESSAGES))

        self.assertEqual(category_for_push({"type": "news_update"}), Category.NEWS)
        self.assertEqual(category_for_push({"type": "chat_message"}), Category.MESSAGES)
        self.assertEqual(category_for_push({"type": "admin_broadcast", "category": "promotional"}), Category.ANNOUNCEMENTS)
        self.assertIsNone(category_for_push({"type": "admin_broadcast", "category": "system_alert"}))
        self.assertIsNone(category_for_push({"type": "unknown"}))

    def test_push_is_split_by_preference(self):
        from unittest.mock import patch

        import firebase
        from .notification_preferences import Category, save_user_modes

        quiet = User.objects.create_user(email="q2@example.com", username="q2@example.com", password="x")
        muted = User.objects.create_user(email="m2@example.com", username="m2@example.com", password="x")
        for user, token in ((self.user, "tok-loud"), (quiet, "tok-quiet"), (muted, "tok-muted")):
            DeviceToken.objects.create(user=user, fcm_token=token, device_id=token)
        save_user_modes(quiet, {Category.NEWS: "silent"})
        save_user_modes(muted, {Category.NEWS: "off"})

        calls = []

        def fake_send(**kwargs):
            calls.append((kwargs["tokens"], kwargs["sound"], kwargs["data"]["notification_category"]))
            return {"success_count": len(kwargs["tokens"]), "failure_count": 0, "failed_tokens": [], "errors": []}

        with self.settings(FCM_PUSH_ENABLED=True), \
                patch.object(firebase, "send_push_to_tokens", side_effect=fake_send):
            result = firebase.send_push_to_users(
                [self.user, quiet, muted], "t", "b", data={"type": "news_update"},
            )

        self.assertEqual(calls, [(["tok-loud"], True, "NEWS"), (["tok-quiet"], False, "NEWS")])
        self.assertEqual(result["success_count"], 2)

    def test_silent_push_uses_silent_channel_and_no_ios_sound(self):
        import firebase

        self.assertEqual(firebase.android_channel_for({"type": "news_update"}), "montada_news")
        self.assertEqual(firebase.android_channel_for({"type": "news_update"}, sound=False), "montada_news_silent")

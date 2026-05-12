from django.test import TestCase

from .models import User
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

        english_ids = set(_get_news_notification_recipients("en").values_list("id", flat=True))
        arabic_ids = set(_get_news_notification_recipients("ar").values_list("id", flat=True))

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

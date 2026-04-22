from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from News.live_news_service import (
    detect_news_language,
    is_frontend_live_news_language,
    save_live_news_payload,
    is_supported_live_news_language,
)
from News.models import LiveNews


class LiveNewsLanguageDetectionTests(SimpleTestCase):
    def test_prefers_short_translated_body_over_english_title(self):
        language = detect_news_language(
            "Breaking news",
            "",
            "I mercati salgono dopo i nuovi dati economici",
        )
        self.assertEqual(language, "it")

    def test_prefers_translated_teaser_over_english_title(self):
        language = detect_news_language(
            "Market update",
            "Die Maerkte steigen nach den neuen Daten",
            "",
        )
        self.assertEqual(language, "de")

    def test_detects_spanish_article_content(self):
        language = detect_news_language(
            "Stocks to watch today",
            "Los mercados suben mientras los inversores esperan el informe",
            "",
        )
        self.assertEqual(language, "es")

    def test_keeps_english_when_article_is_english(self):
        language = detect_news_language(
            "Market update",
            "Markets rise as investors await the inflation report",
            "",
        )
        self.assertEqual(language, "en")

    def test_supports_detected_european_language_codes(self):
        self.assertTrue(is_supported_live_news_language("it"))
        self.assertTrue(is_supported_live_news_language("de"))
        self.assertTrue(is_supported_live_news_language("es"))

    def test_supports_general_detected_language_codes(self):
        self.assertTrue(is_supported_live_news_language("ru"))
        self.assertTrue(is_supported_live_news_language("nl"))
        self.assertTrue(is_supported_live_news_language("zh-cn"))

    def test_only_english_arabic_and_chinese_are_frontend_visible(self):
        self.assertTrue(is_frontend_live_news_language("en"))
        self.assertTrue(is_frontend_live_news_language("ar"))
        self.assertTrue(is_frontend_live_news_language("zh-cn"))
        self.assertFalse(is_frontend_live_news_language("it"))
        self.assertFalse(is_frontend_live_news_language("de"))


class LiveNewsPersistenceTests(TestCase):
    def test_non_frontend_language_is_not_saved(self):
        payload = {
            "id": 51925248,
            "action": "Created",
            "title": "Le azioni tecnologiche sono crollate",
            "teaser": "Se nell'ultimo anno avete tenuto in portafoglio i titoli tecnologici",
            "body": "Il primo trimestre del 2026 non e stato generoso con il settore.",
        }

        instance, created, changed = save_live_news_payload(payload, broadcast=False)

        self.assertIsNone(instance)
        self.assertFalse(created)
        self.assertFalse(
            LiveNews.objects.filter(provider_content_id=payload["id"]).exists()
        )
        self.assertFalse(changed)

    @patch("News.live_news_service.broadcast_live_news")
    def test_non_frontend_update_deletes_existing_row_and_broadcasts_deleted(self, mock_broadcast):
        existing = LiveNews.objects.create(
            provider_content_id=51925249,
            title="Market update",
            teaser="Markets rise on inflation optimism",
            body="Markets rise on inflation optimism and policy hopes.",
            language="en",
            is_active=True,
        )
        payload = {
            "id": 51925249,
            "action": "Updated",
            "title": "Le azioni tecnologiche sono crollate",
            "teaser": "Se nell'ultimo anno avete tenuto in portafoglio i titoli tecnologici",
            "body": "Il primo trimestre del 2026 non e stato generoso con il settore.",
        }

        instance, created, changed = save_live_news_payload(payload, broadcast=True)

        self.assertIsNone(instance)
        self.assertFalse(created)
        self.assertTrue(changed)
        self.assertFalse(
            LiveNews.objects.filter(provider_content_id=payload["id"]).exists()
        )
        mock_broadcast.assert_called_once_with(existing, event_name="deleted")

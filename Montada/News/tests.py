from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from Mainapp.models import UserNotification
from News.management.commands.run_fxstreet_news_stream import _flush_news_notification_batch
from News.live_news_service import (
    detect_news_language,
    fetch_actionforex_rss_items,
    fetch_alyaum_arabic_rss_items,
    fetch_cnn_business_arabic_rss_items,
    fetch_rss_article_details,
    fetch_forexcrunch_rss_items,
    fetch_open_graph_image_url,
    fetch_rss_items,
    is_frontend_live_news_language,
    is_supported_live_news_language,
    normalize_fxstreet_payload,
    normalize_rss_payload,
    _enrich_rss_details,
    save_live_news_payload,
)
from News.models import LiveNews

User = get_user_model()


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


class LiveNewsProviderWrapperTests(SimpleTestCase):
    @patch("News.live_news_service.fetch_rss_items")
    def test_actionforex_wrapper_uses_provider_specific_defaults(self, mock_fetch_rss_items):
        fetch_actionforex_rss_items()

        mock_fetch_rss_items.assert_called_once_with(
            feed_url=["https://www.actionforex.com/feed/"],
            provider_slug="actionforex",
            news_type="actionforex_rss",
            channel="actionforex",
            timeout=20,
        )

    @patch("News.live_news_service.fetch_rss_items")
    def test_forexcrunch_wrapper_uses_provider_specific_defaults(self, mock_fetch_rss_items):
        fetch_forexcrunch_rss_items()

        mock_fetch_rss_items.assert_called_once_with(
            feed_url=["https://www.forexcrunch.com/feed/"],
            provider_slug="forexcrunch",
            news_type="forexcrunch_rss",
            channel="forexcrunch",
            timeout=20,
        )

    @patch("News.live_news_service.fetch_rss_items")
    def test_cnn_business_arabic_wrapper_uses_provider_specific_defaults(self, mock_fetch_rss_items):
        fetch_cnn_business_arabic_rss_items()

        mock_fetch_rss_items.assert_called_once_with(
            feed_url=["https://cnnbusinessarabic.com/rssFeed/279/197"],
            provider_slug="cnn_business_ar",
            news_type="cnn_business_ar_rss",
            channel="cnn_business_ar",
            timeout=20,
        )

    @patch("News.live_news_service.fetch_rss_items")
    def test_alyaum_arabic_wrapper_uses_provider_specific_defaults(self, mock_fetch_rss_items):
        fetch_alyaum_arabic_rss_items()

        mock_fetch_rss_items.assert_called_once_with(
            feed_url=["https://www.alyaum.com/rssFeed/1005"],
            provider_slug="alyaum_ar",
            news_type="alyaum_ar_rss",
            channel="alyaum_ar",
            timeout=20,
        )

    @patch("News.live_news_service.fetch_rss_article_details")
    def test_alyaum_rss_uses_embedded_teaser_body_without_article_crawl(self, mock_fetch_rss_article_details):
        normalized = {
            "news_type": "alyaum_ar_rss",
            "title": "عنوان الخبر",
            "teaser": "<p>ملخص <strong>الخبر</strong> من التغذية.</p>",
            "body": None,
            "tags": ["العربية"],
            "primary_image_url": None,
            "images": [],
        }

        enriched = _enrich_rss_details(normalized)

        mock_fetch_rss_article_details.assert_not_called()
        self.assertEqual(enriched["body"], "<p>ملخص <strong>الخبر</strong> من التغذية.</p>")
        self.assertEqual(enriched["language"], "ar")


class LiveNewsPersistenceTests(TestCase):
    def test_fetch_open_graph_image_url_reads_meta_tag(self):
        html = b"""
        <html>
          <head>
            <meta property="og:image" content="https://cdn.fxstreet.com/image.jpg" />
          </head>
          <body></body>
        </html>
        """

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return html

        with patch("urllib.request.urlopen", return_value=DummyResponse()):
            image_url = fetch_open_graph_image_url("https://www.fxstreet.com/news/example")

        self.assertEqual(image_url, "https://cdn.fxstreet.com/image.jpg")

    def test_fetch_rss_article_details_reads_article_body_and_tags(self):
        html = b"""
        <html>
          <head>
            <meta property="og:image" content="https://cdn.fxstreet.com/image.jpg" />
            <meta name="keywords" content="EUR/USD, Euro, Rabobank" />
            <script type="application/ld+json">
              {"@context":"https://schema.org","@type":"NewsArticle","articleBody":"Fallback body"}
            </script>
          </head>
          <body>
            <article>
              <h2 class="fxs_headline_from_medium_to_large">Euro gains capped by growth risks</h2>
              <p>While we do expect interest rate differentials to allow an upward bias in EUR/USD.</p>
              <p>Our central view remains that EUR/USD 1.20 will be beyond reach this year.</p>
            </article>
          </body>
        </html>
        """

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return html

        with patch("urllib.request.urlopen", return_value=DummyResponse()):
            details = fetch_rss_article_details("https://www.fxstreet.com/news/example")

        self.assertEqual(details["image_url"], "https://cdn.fxstreet.com/image.jpg")
        self.assertIn("<h2", details["body"])
        self.assertIn("<p>While we do expect", details["body"])
        self.assertEqual(details["tags"], ["EUR/USD", "Euro", "Rabobank"])

    def test_fetch_rss_article_details_unescapes_body_and_tags(self):
        html = b"""
        <html>
          <head>
            <meta name="keywords" content="&#40643;&#37329;, &#32654;&#32879;&#20786;" />
            <script type="application/ld+json">
              {"@context":"https://schema.org","@type":"NewsArticle","articleBody":"&#40643;&#37329;&#32173;&#25345;&#22312;&#39640;&#40670;&#38468;&#36817;"}
            </script>
          </head>
          <body></body>
        </html>
        """

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return html

        with patch("urllib.request.urlopen", return_value=DummyResponse()):
            details = fetch_rss_article_details("https://www.fxstreet.hk/news/example")

        self.assertEqual(details["body"], "<p>黃金維持在高點附近</p>")
        self.assertEqual(details["tags"], ["黃金", "美聯儲"])

    def test_fxstreet_payload_normalizes_into_live_news_shape(self):
        payload = {
            "guid": "cbbf7314-53f4-4806-bfcc-2f62a3a40783",
            "link": "https://www.fxstreet.com/news/silver-price-today-silver-rises-according-to-fxstreet-data-202605050936",
            "title": "Silver price today: Silver rises, according to FXStreet data",
            "description": (
                "Silver prices (XAG/USD) rose on Tuesday, according to FXStreet data. "
                "Silver trades at $73.70 per troy ounce."
            ),
            "pubDate": "Tue, 05 May 2026 09:36:27 Z",
        }

        normalized = normalize_fxstreet_payload(payload)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["news_type"], "fxstreet_rss")
        self.assertEqual(normalized["channels"], ["fxstreet"])
        self.assertEqual(normalized["language"], "en")
        self.assertEqual(
            normalized["source_url"],
            "https://www.fxstreet.com/news/silver-price-today-silver-rises-according-to-fxstreet-data-202605050936",
        )
        self.assertIsNotNone(normalized["provider_content_id"])
        self.assertIsNotNone(normalized["source_created_at"])

    def test_dailyforex_payload_normalizes_into_provider_specific_shape(self):
        payload = {
            "_provider_slug": "dailyforex",
            "_news_type": "dailyforex_rss",
            "_channel": "dailyforex",
            "guid": "dailyforex-1",
            "link": "https://www.dailyforex.com/forex-news/example",
            "title": "Forex Today: RBA Hikes Rates to 4.35%",
            "description": "The Australian dollar advanced after the RBA surprised markets.",
            "pubDate": "Tue, 05 May 2026 09:36:27 Z",
            "author": "DailyForex",
        }

        normalized = normalize_rss_payload(payload)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["news_type"], "dailyforex_rss")
        self.assertEqual(normalized["channels"], ["dailyforex"])
        self.assertEqual(normalized["authors"], ["DailyForex"])
        self.assertEqual(normalized["language"], "en")

    def test_arabic_rss_payload_normalizes_into_arabic_shape(self):
        payload = {
            "_provider_slug": "fxstreet_ar",
            "_news_type": "fxstreet_ar_rss",
            "_channel": "fxstreet_ar",
            "guid": "ar-1",
            "link": "https://ar.fxstreet.com/news/example-ar",
            "title": "مؤشر الدولار الأمريكي DXY: نظرة مستقبلية محددة النطاق بعد ارتداد الحرب – BBH",
            "description": "تحديثات مباشرة حول تحركات الدولار والأسواق العالمية.",
            "pubDate": "Wed, 07 May 2026 11:29:00 Z",
        }

        normalized = normalize_rss_payload(payload)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["news_type"], "fxstreet_ar_rss")
        self.assertEqual(normalized["channels"], ["fxstreet_ar"])
        self.assertEqual(normalized["language"], "ar")

    def test_chinese_rss_payload_normalizes_into_chinese_shape(self):
        payload = {
            "_provider_slug": "fxstreet_zh",
            "_news_type": "fxstreet_zh_rss",
            "_channel": "fxstreet_zh",
            "guid": "zh-1",
            "link": "https://www.fxstreet.hk/news/example-zh",
            "title": "美聯儲的柯林斯：預計利率將維持更長時間不變",
            "description": "外匯市場最新消息與分析。",
            "pubDate": "Wed, 07 May 2026 11:15:00 Z",
        }

        normalized = normalize_rss_payload(payload)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["news_type"], "fxstreet_zh_rss")
        self.assertEqual(normalized["channels"], ["fxstreet_zh"])
        self.assertEqual(normalized["language"], "zh")

    def test_fetch_rss_items_falls_back_to_regex_for_malformed_xml(self):
        malformed_rss = """
        <?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <guid>ar-1</guid>
              <link>https://ar.fxstreet.com/news/example-ar</link>
              <title>مؤشر الدولار الأمريكي DXY</title>
              <description>تحديث مباشر & غير صالح</description>
              <pubDate>Wed, 07 May 2026 11:29:00 Z</pubDate>
              <category>الدولار</category>
            </item>
          </channel>
        </rss>
        """

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return malformed_rss.encode("utf-8")

        with patch("urllib.request.urlopen", return_value=DummyResponse()):
            items = fetch_rss_items(
                feed_url="https://ar.fxstreet.com/rss/news",
                provider_slug="fxstreet_ar",
                news_type="fxstreet_ar_rss",
                channel="fxstreet_ar",
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["_provider_slug"], "fxstreet_ar")
        self.assertEqual(items[0]["link"], "https://ar.fxstreet.com/news/example-ar")

    @patch(
        "News.live_news_service.fetch_rss_article_details",
        return_value={
            "body": "<p>DBS Group Research notes that Japan has stepped up intervention.</p>",
            "image_url": "https://cdn.fxstreet.com/news/usd-jpy.jpg",
            "tags": ["USD/JPY", "Japan"],
        },
    )
    def test_fxstreet_english_item_is_saved_with_article_details(self, mock_fetch_rss_article_details):
        payload = {
            "guid": "192aa1b9-b7f5-4c9d-b84b-56da9aa21362",
            "link": "https://www.fxstreet.com/news/usd-jpy-intervention-battles-rising-oil-dbs-202605050932",
            "title": "USD/JPY: Intervention battles rising Oil - DBS",
            "description": (
                "DBS Group Research notes that Japan has stepped up intervention "
                "to support the Japanese Yen after the latest oil move."
            ),
            "pubDate": "Tue, 05 May 2026 09:32:06 Z",
        }

        instance, created, changed = save_live_news_payload(payload, broadcast=False)

        self.assertIsNotNone(instance)
        self.assertTrue(created)
        self.assertTrue(changed)
        self.assertEqual(
            instance.body,
            "<p>DBS Group Research notes that Japan has stepped up intervention.</p>",
        )
        self.assertEqual(instance.primary_image_url, "https://cdn.fxstreet.com/news/usd-jpy.jpg")
        self.assertEqual(instance.images, [{"size": "og", "url": "https://cdn.fxstreet.com/news/usd-jpy.jpg"}])
        self.assertEqual(instance.tags, ["USD/JPY", "Japan"])
        mock_fetch_rss_article_details.assert_called_once_with(
            "https://www.fxstreet.com/news/usd-jpy-intervention-battles-rising-oil-dbs-202605050932"
        )
        self.assertTrue(
            LiveNews.objects.filter(provider_content_id=instance.provider_content_id).exists()
        )

    @patch(
        "News.live_news_service.fetch_rss_article_details",
        return_value={
            "body": "<p>Updated body from the article page.</p>",
            "image_url": None,
            "tags": ["RBA", "AUD/USD"],
        },
    )
    def test_rss_payload_matches_existing_row_by_source_url(self, mock_fetch_rss_article_details):
        existing = LiveNews.objects.create(
            provider_content_id=123456789,
            news_type="fxstreet_rss",
            title="Old title",
            teaser="Old teaser",
            source_url="https://www.fxstreet.com/news/aud-usd-consolidation-risk-after-rba-pause-societe-generale-202605050906",
            language="en",
            is_active=True,
        )
        payload = {
            "_provider_slug": "fxstreet",
            "_news_type": "fxstreet_rss",
            "_channel": "fxstreet",
            "guid": "new-guid-that-hashes-differently",
            "link": "https://www.fxstreet.com/news/aud-usd-consolidation-risk-after-rba-pause-societe-generale-202605050906",
            "title": "AUD/USD: Consolidation risk after RBA pause - Societe Generale",
            "description": "Updated teaser from the rss feed.",
            "pubDate": "Tue, 05 May 2026 09:06:23 Z",
        }

        instance, created, changed = save_live_news_payload(payload, broadcast=False)

        self.assertIsNotNone(instance)
        self.assertFalse(created)
        self.assertTrue(changed)
        self.assertEqual(instance.id, existing.id)
        self.assertEqual(instance.provider_content_id, existing.provider_content_id)
        self.assertEqual(instance.title, "AUD/USD: Consolidation risk after RBA pause - Societe Generale")
        self.assertEqual(instance.body, "<p>Updated body from the article page.</p>")
        self.assertEqual(instance.tags, ["RBA", "AUD/USD"])
        mock_fetch_rss_article_details.assert_called_once()

    @patch("News.live_news_service.fetch_rss_article_details")
    def test_unchanged_rss_item_skips_article_recrawl(self, mock_fetch_rss_article_details):
        existing = LiveNews.objects.create(
            provider_content_id=987654321,
            news_type="fxstreet_rss",
            title="EUR/USD: Upside seen limited in H2 - Rabobank",
            teaser="Rabobank expects rate differentials to support an upward bias in EUR/USD.",
            body="<p>Existing crawled article body.</p>",
            source_url="https://www.fxstreet.com/news/eur-usd-upside-seen-limited-in-h2-rabobank-202605051226",
            authors=["FXStreet Insights Team"],
            tags=["EUR/USD", "Rabobank"],
            channels=["fxstreet"],
            images=[{"size": "og", "url": "https://editorial.fxsstatic.com/images/i/eur-usd-fix-01.jpg"}],
            primary_image_url="https://editorial.fxsstatic.com/images/i/eur-usd-fix-01.jpg",
            language="en",
            is_active=True,
            source_created_at="2026-05-05T12:26:16.923000+00:00",
            source_updated_at="2026-05-05T12:26:16.923000+00:00",
            source_timestamp="2026-05-05T12:26:16.923000+00:00",
        )
        payload = {
            "_provider_slug": "fxstreet",
            "_news_type": "fxstreet_rss",
            "_channel": "fxstreet",
            "guid": "https://www.fxstreet.com/news/eur-usd-upside-seen-limited-in-h2-rabobank-202605051226",
            "link": "https://www.fxstreet.com/news/eur-usd-upside-seen-limited-in-h2-rabobank-202605051226",
            "title": "EUR/USD: Upside seen limited in H2 - Rabobank",
            "description": "Rabobank expects rate differentials to support an upward bias in EUR/USD.",
            "pubDate": "Tue, 05 May 2026 12:26:16 Z",
            "author": "FXStreet Insights Team",
            "categories": ["EUR/USD", "Rabobank"],
        }

        instance, created, changed = save_live_news_payload(payload, broadcast=False)

        self.assertEqual(instance.id, existing.id)
        self.assertFalse(created)
        self.assertFalse(changed)
        mock_fetch_rss_article_details.assert_not_called()

    @patch("News.live_news_service.fetch_rss_article_details")
    def test_unchanged_rss_item_with_extra_crawled_tags_still_skips_recrawl(
        self,
        mock_fetch_rss_article_details,
    ):
        existing = LiveNews.objects.create(
            provider_content_id=777777777,
            news_type="fxstreet_rss",
            title="EUR/USD: Upside seen limited in H2 - Rabobank",
            teaser="Rabobank expects rate differentials to support an upward bias in EUR/USD.",
            body="<p>Existing crawled article body.</p>",
            source_url="https://www.fxstreet.com/news/eur-usd-upside-seen-limited-in-h2-rabobank-202605051226",
            authors=["FXStreet Insights Team"],
            tags=["EUR/USD", "Rabobank", "Eurozone", "USD"],
            channels=["fxstreet"],
            images=[{"size": "og", "url": "https://editorial.fxsstatic.com/images/i/eur-usd-fix-01.jpg"}],
            primary_image_url="https://editorial.fxsstatic.com/images/i/eur-usd-fix-01.jpg",
            language="en",
            is_active=True,
            source_created_at="2026-05-05T12:26:16.923000+00:00",
            source_updated_at="2026-05-05T12:26:16.923000+00:00",
            source_timestamp="2026-05-05T12:26:16.923000+00:00",
        )
        payload = {
            "_provider_slug": "fxstreet",
            "_news_type": "fxstreet_rss",
            "_channel": "fxstreet",
            "guid": "https://www.fxstreet.com/news/eur-usd-upside-seen-limited-in-h2-rabobank-202605051226",
            "link": "https://www.fxstreet.com/news/eur-usd-upside-seen-limited-in-h2-rabobank-202605051226",
            "title": "EUR/USD: Upside seen limited in H2 - Rabobank",
            "description": "Rabobank expects rate differentials to support an upward bias in EUR/USD.",
            "pubDate": "Tue, 05 May 2026 12:26:16 Z",
            "author": "FXStreet Insights Team",
            "categories": ["EUR/USD", "Rabobank"],
        }

        instance, created, changed = save_live_news_payload(payload, broadcast=False)

        self.assertEqual(instance.id, existing.id)
        self.assertFalse(created)
        self.assertFalse(changed)
        mock_fetch_rss_article_details.assert_not_called()

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


class LiveNewsNotificationTests(TestCase):
    @patch("firebase.send_push_to_users")
    def test_flush_news_notification_batch_skips_recent_duplicates(self, mock_send_push_to_users):
        first_user = User.objects.create_user(
            email="first@example.com",
            username="first@example.com",
            password="Testpass123!",
        )
        second_user = User.objects.create_user(
            email="second@example.com",
            username="second@example.com",
            password="Testpass123!",
        )
        UserNotification.objects.create(
            user=first_user,
            title="FXStreet update",
            message="Gold remains bid near the highs.",
            notification_type="INFO",
            redirect_url="https://example.com/news/gold",
        )

        from unittest.mock import Mock

        broadcast_mock = Mock()
        instance = type("NewsInstance", (), {"provider_content_id": 12345})()

        _flush_news_notification_batch(
            [first_user, second_user],
            title="FXStreet update",
            body="Gold remains bid near the highs.",
            redirect_url="https://example.com/news/gold",
            data={"provider_content_id": "12345"},
            image_url=None,
            broadcast_notifications=broadcast_mock,
            user_notification_model=UserNotification,
            instance=instance,
        )

        self.assertEqual(
            UserNotification.objects.filter(
                user=first_user,
                title="FXStreet update",
                message="Gold remains bid near the highs.",
                redirect_url="https://example.com/news/gold",
            ).count(),
            1,
        )
        self.assertEqual(
            UserNotification.objects.filter(
                user=second_user,
                title="FXStreet update",
                message="Gold remains bid near the highs.",
                redirect_url="https://example.com/news/gold",
            ).count(),
            1,
        )
        broadcast_mock.assert_called_once()
        sent_users = mock_send_push_to_users.call_args.kwargs["users"]
        self.assertEqual([user.id for user in sent_users], [second_user.id])

import logging
import re
import time
from datetime import timedelta
from html import unescape

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import ProgrammingError
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import Truncator

from News.live_news_service import (
    fetch_actionforex_rss_items,
    fetch_alyaum_arabic_rss_items,
    fetch_cnn_business_arabic_rss_items,
    fetch_dailyforex_rss_items,
    fetch_forexcrunch_rss_items,
    fetch_forexlive_rss_items,
    fetch_fxstreet_chinese_rss_items,
    fetch_fxstreet_rss_items,
    save_live_news_payload,
)


logger = logging.getLogger(__name__)
User = get_user_model()
NOTIFICATION_BATCH_SIZE = 500
NEWS_NOTIFICATION_DEDUPLICATION_WINDOW = timedelta(minutes=30)
NEWS_LANGUAGE_RECIPIENT_FIELDS = {
    "ar": "news_notify_ar",
    "en": "news_notify_en",
    "zh": "news_notify_zh",
}

PROVIDER_FETCHERS = {
    "fxstreet": fetch_fxstreet_rss_items,
    "fxstreet_zh": fetch_fxstreet_chinese_rss_items,
    "dailyforex": fetch_dailyforex_rss_items,
    "forexlive": fetch_forexlive_rss_items,
    "actionforex": fetch_actionforex_rss_items,
    "forexcrunch": fetch_forexcrunch_rss_items,
    "cnn_business_ar": fetch_cnn_business_arabic_rss_items,
    "alyaum_ar": fetch_alyaum_arabic_rss_items,
}


def _clean_notification_text(value):
    cleaned = re.sub(r"<[^>]+>", " ", unescape(str(value or "")))
    return " ".join(cleaned.split()).strip()


def _build_news_notification_summary(instance):
    summary_source = (
        _clean_notification_text(getattr(instance, "teaser", None))
        or _clean_notification_text(getattr(instance, "body", None))
        or _clean_notification_text(getattr(instance, "title", None))
        or "Live market news updated."
    )
    return Truncator(summary_source).chars(180)


def _normalize_notification_language(language):
    normalized = str(language or "").strip().lower().replace("_", "-")
    return normalized.split("-", 1)[0] if normalized else ""


def _is_english_news_language(language):
    return _normalize_notification_language(language) == "en"


def _get_news_notification_recipients(language):
    preference_field = NEWS_LANGUAGE_RECIPIENT_FIELDS.get(
        _normalize_notification_language(language)
    )
    if not preference_field:
        return User.objects.none()
    return User.objects.filter(is_active=True, **{preference_field: True}).only("id")


def _notify_users_about_news(instance, *, event_name):
    if not instance:
        return
    if not _is_english_news_language(getattr(instance, "language", None)):
        return

    try:
        from Dashboard.realtime import broadcast_notifications
        from Mainapp.models import UserNotification
        from firebase import send_push_to_users
    except Exception:
        logger.exception(
            "News notification dependencies unavailable for provider_content_id=%s",
            getattr(instance, "provider_content_id", None),
        )
        return

    recipients = _get_news_notification_recipients(getattr(instance, "language", None))
    title = Truncator(
        _clean_notification_text(getattr(instance, "title", None)) or "Live market news update"
    ).chars(255)
    body = _build_news_notification_summary(instance)
    redirect_url = getattr(instance, "source_url", None) or None
    data = {
        "type": "news_update",
        "event": str(event_name or "updated"),
        "news_id": str(getattr(instance, "id", "") or ""),
        "provider_content_id": str(getattr(instance, "provider_content_id", "") or ""),
        "language": str(getattr(instance, "language", "") or ""),
        "news_type": str(getattr(instance, "news_type", "") or ""),
        "source_url": str(redirect_url or ""),
    }
    image_url = getattr(instance, "image_url", None) or None

    batch_users = []
    for user in recipients.iterator(chunk_size=NOTIFICATION_BATCH_SIZE):
        batch_users.append(user)
        if len(batch_users) < NOTIFICATION_BATCH_SIZE:
            continue
        _flush_news_notification_batch(
            batch_users,
            title=title,
            body=body,
            redirect_url=redirect_url,
            data=data,
            image_url=image_url,
            broadcast_notifications=broadcast_notifications,
            user_notification_model=UserNotification,
            instance=instance,
        )
        batch_users = []

    if batch_users:
        _flush_news_notification_batch(
            batch_users,
            title=title,
            body=body,
            redirect_url=redirect_url,
            data=data,
            image_url=image_url,
            broadcast_notifications=broadcast_notifications,
            user_notification_model=UserNotification,
            instance=instance,
        )


def _flush_news_notification_batch(
    users,
    *,
    title,
    body,
    redirect_url,
    data,
    image_url,
    broadcast_notifications,
    user_notification_model,
    instance,
):
    if not users:
        return

    users = _dedupe_recent_news_notification_users(
        users,
        title=title,
        body=body,
        redirect_url=redirect_url,
        user_notification_model=user_notification_model,
    )
    if not users:
        return

    created_notifications = [
        user_notification_model(
            user=user,
            title=title,
            message=body,
            notification_type="INFO",
            category="NEWS",
            redirect_url=redirect_url,
        )
        for user in users
    ]
    user_notification_model.objects.bulk_create(created_notifications)
    broadcast_notifications(
        user_notification_model.objects.filter(
            id__in=[notification.id for notification in created_notifications]
        ),
        event_name="created",
    )

    try:
        from firebase import send_push_to_users

        send_push_to_users(
            users=users,
            title=title,
            body=body,
            data=data,
            image_url=image_url,
        )
    except Exception:
        logger.exception(
            "FCM push failed for news provider_content_id=%s user_count=%s",
            getattr(instance, "provider_content_id", None),
            len(users),
        )


def _dedupe_recent_news_notification_users(
    users,
    *,
    title,
    body,
    redirect_url,
    user_notification_model,
):
    if not users or not redirect_url:
        return list(users)

    user_ids = [user.id for user in users]
    recent_cutoff = timezone.now() - NEWS_NOTIFICATION_DEDUPLICATION_WINDOW
    already_notified_user_ids = set(
        user_notification_model.objects.filter(
            user_id__in=user_ids,
            title=title,
            message=body,
            notification_type="INFO",
            redirect_url=redirect_url,
            created_at__gte=recent_cutoff,
        ).values_list("user_id", flat=True)
    )
    return [user for user in users if user.id not in already_notified_user_ids]


class Command(BaseCommand):
    help = "Poll public RSS news providers and broadcast newly saved live news items."

    def add_arguments(self, parser):
        parser.add_argument(
            "--providers",
            type=str,
            default="fxstreet,fxstreet_zh,dailyforex,forexlive,actionforex,forexcrunch,cnn_business_ar,alyaum_ar",
            help=(
                "Comma-separated provider keys to poll: "
                "fxstreet,fxstreet_zh,dailyforex,forexlive,"
                "actionforex,forexcrunch,cnn_business_ar,alyaum_ar"
            ),
        )
        parser.add_argument(
            "--fxstreet-feed-url",
            type=str,
            default="https://www.fxstreet.com/rss/news",
            help="Override the FXStreet RSS feed URL.",
        )
        parser.add_argument(
            "--fxstreet-chinese-feed-url",
            type=str,
            default="https://www.fxstreet.hk/rss/news",
            help="Override the Chinese FXStreet RSS feed URL.",
        )
        parser.add_argument(
            "--dailyforex-feed-url",
            type=str,
            default="https://www.dailyforex.com/rss/forexnews.xml",
            help="Override the DailyForex RSS feed URL.",
        )
        parser.add_argument(
            "--forexlive-feed-url",
            type=str,
            default="https://www.forexlive.com/feed/",
            help="Override the ForexLive RSS feed URL.",
        )
        parser.add_argument(
            "--actionforex-feed-url",
            type=str,
            default="https://www.actionforex.com/feed/",
            help="Override the ActionForex RSS feed URL.",
        )
        parser.add_argument(
            "--forexcrunch-feed-url",
            type=str,
            default="https://www.forexcrunch.com/feed/",
            help="Override the Forex Crunch RSS feed URL.",
        )
        parser.add_argument(
            "--cnn-business-ar-feed-url",
            type=str,
            default="https://cnnbusinessarabic.com/rssFeed/279/197",
            help="Override the CNN Business Arabic currencies RSS feed URL.",
        )
        parser.add_argument(
            "--alyaum-ar-feed-url",
            type=str,
            default="https://www.alyaum.com/rssFeed/1005",
            help="Override the Alyaum Arabic RSS feed URL.",
        )
        parser.add_argument(
            "--poll-interval-seconds",
            type=int,
            default=30,
            help="How often to poll the RSS feed.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum number of RSS items to process per poll.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Fetch the feed once and exit.",
        )
        parser.add_argument(
            "--broadcast-initial",
            action="store_true",
            help="Broadcast items from the first poll instead of silently backfilling them.",
        )

    def handle(self, *args, **options):
        poll_interval_seconds = max(5, int(options.get("poll_interval_seconds") or 30))
        limit = max(1, int(options.get("limit") or 50))
        broadcast_initial = bool(options.get("broadcast_initial"))
        provider_names = self._parse_provider_names(options.get("providers") or "")
        feed_urls = {
            "fxstreet": (options.get("fxstreet_feed_url") or "").strip() or "https://www.fxstreet.com/rss/news",
            "fxstreet_zh": (options.get("fxstreet_chinese_feed_url") or "").strip() or "https://www.fxstreet.hk/rss/news",
            "dailyforex": (options.get("dailyforex_feed_url") or "").strip() or "https://www.dailyforex.com/rss/forexnews.xml",
            "forexlive": (options.get("forexlive_feed_url") or "").strip() or "https://www.forexlive.com/feed/",
            "actionforex": (options.get("actionforex_feed_url") or "").strip() or "https://www.actionforex.com/feed/",
            "forexcrunch": (options.get("forexcrunch_feed_url") or "").strip() or "https://www.forexcrunch.com/feed/",
            "cnn_business_ar": (options.get("cnn_business_ar_feed_url") or "").strip() or "https://cnnbusinessarabic.com/rssFeed/279/197",
            "alyaum_ar": (options.get("alyaum_ar_feed_url") or "").strip() or "https://www.alyaum.com/rssFeed/1005",
        }

        first_cycle = True
        while True:
            try:
                should_broadcast = broadcast_initial or not first_cycle
                self.stdout.write(
                    "Polling RSS providers: %s"
                    % ", ".join(provider_names)
                )
                stats = self._run_poll_cycle(
                    provider_names=provider_names,
                    feed_urls=feed_urls,
                    limit=limit,
                    broadcast=should_broadcast,
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        "RSS news poll complete. "
                        f"providers={','.join(provider_names)} "
                        f"created={stats['created']} updated={stats['updated']} "
                        f"skipped={stats['skipped']} failed={stats['failed']}"
                    )
                )
                first_cycle = False

                if options.get("once"):
                    return
                time.sleep(poll_interval_seconds)
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("Stopping RSS news stream..."))
                return
            except Exception as exc:
                logger.exception("RSS news poll failed: %s", exc)
                self.stderr.write(self.style.ERROR(f"RSS news poll failed: {exc}"))
                if options.get("once"):
                    raise
                time.sleep(poll_interval_seconds)

    def _parse_provider_names(self, raw_value):
        names = []
        for value in str(raw_value or "").split(","):
            cleaned = value.strip().lower()
            if cleaned and cleaned in PROVIDER_FETCHERS and cleaned not in names:
                names.append(cleaned)
        if not names:
            raise CommandError(
                "No valid providers selected. Use: "
                "fxstreet,fxstreet_zh,dailyforex,forexlive,"
                "actionforex,forexcrunch,cnn_business_ar,alyaum_ar"
            )
        return names

    def _run_poll_cycle(self, *, provider_names, feed_urls, limit, broadcast):
        created_count = 0
        updated_count = 0
        skipped_count = 0
        failed_count = 0

        for provider_name in provider_names:
            self.stdout.write(f"Fetching {provider_name} feed...")
            fetcher = PROVIDER_FETCHERS[provider_name]
            try:
                items = fetcher(feed_url=feed_urls[provider_name])
            except Exception as exc:
                failed_count += 1
                if isinstance(exc, RuntimeError) and "All RSS feed URLs failed" in str(exc):
                    logger.warning("RSS provider fetch failed provider=%s: %s", provider_name, exc)
                else:
                    logger.exception("RSS provider fetch failed provider=%s: %s", provider_name, exc)
                self.stderr.write(
                    self.style.WARNING(
                        f"Skipping {provider_name} for this cycle because feed fetch failed: {exc}"
                    )
                )
                continue
            items = list(reversed((items or [])[:limit]))
            self.stdout.write(
                f"Fetched {len(items)} item(s) from {provider_name}."
            )

            for item in items:
                try:
                    instance, created, changed = save_live_news_payload(
                        item,
                        broadcast=broadcast,
                    )
                except ProgrammingError as exc:
                    if "live_news" in str(exc).lower():
                        raise CommandError(
                            "The live_news table does not exist yet. Run `python manage.py migrate` first."
                        ) from exc
                    raise

                if not instance or not changed:
                    skipped_count += 1
                    continue

                if created:
                    created_count += 1
                else:
                    updated_count += 1

                if broadcast and _is_english_news_language(getattr(instance, "language", None)):
                    _notify_users_about_news(
                        instance,
                        event_name="created" if created else "updated",
                    )

        return {
            "created": created_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "failed": failed_count,
        }

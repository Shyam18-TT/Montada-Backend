import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import ProgrammingError

from News.live_news_service import (
    fetch_dailyforex_rss_items,
    fetch_forexlive_rss_items,
    fetch_fxstreet_rss_items,
    save_live_news_payload,
)


logger = logging.getLogger(__name__)

PROVIDER_FETCHERS = {
    "fxstreet": fetch_fxstreet_rss_items,
    "dailyforex": fetch_dailyforex_rss_items,
    "forexlive": fetch_forexlive_rss_items,
}


class Command(BaseCommand):
    help = "Poll public RSS news providers and broadcast newly saved live news items."

    def add_arguments(self, parser):
        parser.add_argument(
            "--providers",
            type=str,
            default="fxstreet,dailyforex,forexlive",
            help="Comma-separated provider keys to poll: fxstreet,dailyforex,forexlive",
        )
        parser.add_argument(
            "--fxstreet-feed-url",
            type=str,
            default="https://www.fxstreet.com/rss/news",
            help="Override the FXStreet RSS feed URL.",
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
            "dailyforex": (options.get("dailyforex_feed_url") or "").strip() or "https://www.dailyforex.com/rss/forexnews.xml",
            "forexlive": (options.get("forexlive_feed_url") or "").strip() or "https://www.forexlive.com/feed/",
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
                        f"created={stats['created']} updated={stats['updated']} skipped={stats['skipped']}"
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
            raise CommandError("No valid providers selected. Use: fxstreet,dailyforex,forexlive")
        return names

    def _run_poll_cycle(self, *, provider_names, feed_urls, limit, broadcast):
        created_count = 0
        updated_count = 0
        skipped_count = 0

        for provider_name in provider_names:
            self.stdout.write(f"Fetching {provider_name} feed...")
            fetcher = PROVIDER_FETCHERS[provider_name]
            items = fetcher(feed_url=feed_urls[provider_name])
            items = list(reversed((items or [])[:limit]))
            self.stdout.write(
                f"Fetched {len(items)} item(s) from {provider_name}."
            )

            for item in items:
                try:
                    instance, created, changed = save_live_news_payload(item, broadcast=broadcast)
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

        return {
            "created": created_count,
            "updated": updated_count,
            "skipped": skipped_count,
        }

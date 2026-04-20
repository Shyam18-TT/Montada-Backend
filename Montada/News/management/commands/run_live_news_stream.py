import json
import logging
import time

import websocket
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import ProgrammingError

from News.live_news_service import (
    build_benzinga_stream_url,
    fetch_benzinga_news_page,
    save_live_news_payload,
)


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill Benzinga news and keep a live websocket stream running."

    def add_arguments(self, parser):
        parser.add_argument("--skip-backfill", action="store_true")
        parser.add_argument("--once", action="store_true", help="Run only the REST backfill and exit.")
        parser.add_argument("--page-size", type=int, default=getattr(settings, "BENZINGA_NEWS_DEFAULT_PAGE_SIZE", 50))
        parser.add_argument("--backfill-pages", type=int, default=1)
        parser.add_argument("--tickers", type=str, default="")
        parser.add_argument("--channels", type=str, default="")
        parser.add_argument("--reconnect-delay", type=int, default=5)
        parser.add_argument("--ping-interval", type=int, default=30)

    def handle(self, *args, **options):
        tickers = (options.get("tickers") or "").strip()
        channels = (options.get("channels") or "").strip()
        page_size = max(1, min(int(options.get("page_size") or 50), 100))
        backfill_pages = max(1, int(options.get("backfill_pages") or 1))
        reconnect_delay = max(1, int(options.get("reconnect_delay") or 5))
        ping_interval = max(5, int(options.get("ping_interval") or 30))

        if not options.get("skip_backfill"):
            self._run_backfill(
                page_size=page_size,
                backfill_pages=backfill_pages,
                tickers=tickers,
                channels=channels,
            )

        if options.get("once"):
            self.stdout.write(self.style.SUCCESS("Live news backfill completed."))
            return

        self.stdout.write(self.style.SUCCESS("Starting Benzinga live news websocket listener..."))
        while True:
            try:
                self._run_stream(
                    tickers=tickers,
                    channels=channels,
                    ping_interval=ping_interval,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.exception("Live news websocket loop failed: %s", exc)
                self.stderr.write(self.style.ERROR(f"Live news websocket loop failed: {exc}"))
            self.stdout.write(f"Reconnecting to live news stream in {reconnect_delay}s...")
            time.sleep(reconnect_delay)

    def _run_backfill(self, *, page_size, backfill_pages, tickers, channels):
        created_count = 0
        updated_count = 0
        skipped_count = 0

        for page in range(backfill_pages):
            items = fetch_benzinga_news_page(
                page=page,
                page_size=page_size,
                tickers=tickers or None,
                channels=channels or None,
            )
            if not items:
                break
            for item in items:
                try:
                    instance, created, changed = save_live_news_payload(item, broadcast=False)
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

        self.stdout.write(
            self.style.SUCCESS(
                f"Live news backfill complete. created={created_count} updated={updated_count} skipped={skipped_count}"
            )
        )

    def _run_stream(self, *, tickers, channels, ping_interval):
        stream_url = build_benzinga_stream_url(
            tickers=tickers or None,
            channels=channels or None,
        )

        def on_open(ws_app):
            logger.info("Connected to Benzinga live news websocket.")
            self.stdout.write(self.style.SUCCESS("Connected to Benzinga live news websocket."))

        def on_message(ws_app, message):
            if not message:
                return
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="ignore")
            if message.lower() == "pong":
                return

            payload = json.loads(message)
            instance, created, changed = save_live_news_payload(payload, broadcast=True)
            if not instance or not changed:
                return
            action = "created" if created else str(getattr(instance, "action", "updated") or "updated").lower()
            logger.info(
                "Live news processed provider_content_id=%s action=%s",
                instance.provider_content_id,
                action,
            )

        def on_error(ws_app, error):
            logger.exception("Benzinga live news websocket error: %s", error)
            self.stderr.write(self.style.ERROR(f"Benzinga live news websocket error: {error}"))

        def on_close(ws_app, close_status_code, close_msg):
            logger.warning(
                "Benzinga live news websocket closed code=%s msg=%s",
                close_status_code,
                close_msg,
            )
            self.stdout.write(
                f"Benzinga live news websocket closed code={close_status_code} msg={close_msg}"
            )

        ws_app = websocket.WebSocketApp(
            stream_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws_app.run_forever(ping_interval=ping_interval, ping_payload="ping")

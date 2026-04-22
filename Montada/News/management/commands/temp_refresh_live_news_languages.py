from django.core.management.base import BaseCommand

from News.live_news_service import detect_news_language
from News.models import LiveNews


class Command(BaseCommand):
    help = "Temporary command to refresh detected language for existing LiveNews rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=500,
            help="Number of rows to stream from the database at a time.",
        )
        parser.add_argument(
            "--only-language",
            action="append",
            dest="only_languages",
            default=[],
            help="Limit updates to rows currently using one of these language codes. Repeatable.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximum number of rows to scan. Default scans all rows.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing to the database.",
        )

    def handle(self, *args, **options):
        chunk_size = max(1, int(options.get("chunk_size") or 500))
        limit = max(0, int(options.get("limit") or 0))
        dry_run = bool(options.get("dry_run"))
        only_languages = {
            (language or "").strip().lower()
            for language in (options.get("only_languages") or [])
            if (language or "").strip()
        }

        queryset = LiveNews.objects.all().order_by("id")
        if only_languages:
            queryset = queryset.filter(language__in=only_languages)
        if limit:
            queryset = queryset[:limit]

        scanned_count = 0
        updated_count = 0
        unchanged_count = 0

        for item in queryset.iterator(chunk_size=chunk_size):
            scanned_count += 1
            detected_language = detect_news_language(item.title, item.teaser, item.body)
            current_language = (item.language or "").strip().lower() or "unknown"

            if detected_language == current_language:
                unchanged_count += 1
                continue

            updated_count += 1
            if dry_run:
                self.stdout.write(
                    f"[dry-run] id={item.id} provider_content_id={item.provider_content_id} "
                    f"{current_language} -> {detected_language}"
                )
            else:
                LiveNews.objects.filter(pk=item.pk).update(language=detected_language)

            if updated_count % 100 == 0:
                self.stdout.write(
                    f"Processed scanned={scanned_count} updated={updated_count} unchanged={unchanged_count}"
                )

        mode = "dry-run completed" if dry_run else "refresh completed"
        self.stdout.write(
            self.style.SUCCESS(
                "Temporary live news language refresh "
                f"{mode}. scanned={scanned_count} updated={updated_count} unchanged={unchanged_count}"
            )
        )

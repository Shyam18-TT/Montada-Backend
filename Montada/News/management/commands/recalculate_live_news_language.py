from django.core.paginator import Paginator
from django.core.management.base import BaseCommand

from News.live_news_service import detect_news_language
from News.models import LiveNews


class Command(BaseCommand):
    help = "Recalculate language for existing LiveNews rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--only-unknown",
            action="store_true",
            help="Recalculate only rows currently marked as unknown.",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=500,
            help="Number of rows to stream from the database at a time.",
        )

    def handle(self, *args, **options):
        chunk_size = max(1, int(options.get("chunk_size") or 500))
        only_unknown = bool(options.get("only_unknown"))

        queryset = LiveNews.objects.all().order_by("id")
        if only_unknown:
            queryset = queryset.filter(language="unknown")
        paginator = Paginator(queryset, chunk_size)

        scanned_count = 0
        updated_count = 0
        unchanged_count = 0

        for page_number in paginator.page_range:
            for item in paginator.page(page_number).object_list:
                scanned_count += 1
                detected_language = detect_news_language(item.title, item.teaser, item.body)
                current_language = (item.language or "").strip() or "unknown"

                if detected_language == current_language:
                    unchanged_count += 1
                    continue

                LiveNews.objects.filter(pk=item.pk).update(language=detected_language)
                updated_count += 1

                if updated_count % 100 == 0:
                    self.stdout.write(
                        f"Updated {updated_count} rows so far (scanned={scanned_count})."
                    )

        self.stdout.write(
            self.style.SUCCESS(
                "Live news language recalculation completed. "
                f"scanned={scanned_count} updated={updated_count} unchanged={unchanged_count}"
            )
        )

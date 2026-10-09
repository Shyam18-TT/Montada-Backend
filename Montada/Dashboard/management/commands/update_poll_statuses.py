import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from Dashboard.polls import sync_poll_statuses

logger = logging.getLogger("polls")


class Command(BaseCommand):
    help = (
        "Activate scheduled poll sessions whose start time has arrived and close sessions whose end "
        "time has passed. Idempotent; safe to run repeatedly. Use --loop to run continuously (PM2)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, syncing every --interval seconds.")
        parser.add_argument("--interval", type=int, default=60, help="Seconds between runs in --loop mode (default 60).")

    def handle(self, *args, **options):
        if not options["loop"]:
            result = self._run_once(verbose=True)
            if result is None:
                raise SystemExit(1)
            return

        interval = max(5, options["interval"])
        self.stdout.write(f"update_poll_statuses: running every {interval}s")
        while True:
            # Drop connections the DB server may have closed while we slept.
            close_old_connections()
            self._run_once()
            time.sleep(interval)

    def _run_once(self, verbose=False):
        try:
            result = sync_poll_statuses()
        except Exception:
            logger.exception("update_poll_statuses failed")
            self.stderr.write("update_poll_statuses failed; see logs/polls.log")
            return None
        changed = any(result.values())
        if changed:
            logger.info("update_poll_statuses %s", result)
        if changed or verbose:
            self.stdout.write(
                f"activated={result['activated']} closed={result['closed']} is_active_synced={result['is_active_synced']}"
            )
        return result

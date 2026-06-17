import time
import re
import json
import requests
import logging
from datetime import datetime, timezone, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from News.models import EconomicCalendarEvent

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Fetches economic calendar data from Tradays (20-day window from today) and saves/updates in the database.'

    TRADAYS_URL = "https://www.tradays.com/en/economic-calendar/widget?mode=2"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    DATE_WINDOW_DAYS = 20
    MAX_RETRIES = 3
    RETRY_DELAY = 0.5  # seconds
    VALID_IMPORTANCE_LEVELS = ('low', 'medium', 'high')

    def add_arguments(self, parser):
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Delete events outside the current window after sync.'
        )
        parser.add_argument(
            '--retry-delay',
            type=float,
            default=self.RETRY_DELAY,
            help='Retry delay in seconds for deadlock handling.'
        )

    def get_date_window(self):
        """Calculate and return the date window for event filtering."""
        now = datetime.now(tz=timezone.utc)
        window_start = now - timedelta(days=1)  # include today's past events
        window_end = now + timedelta(days=self.DATE_WINDOW_DAYS)
        return now, window_start, window_end

    def get_safe_value(self, value):
        """Convert value to string, handling None/empty cases gracefully."""
        if value is None or value == '':
            return None
        return str(value).strip()

    def cleanup_old_events(self, window_start, window_end):
        """Delete events outside the current window."""
        deleted_count, _ = EconomicCalendarEvent.objects.exclude(
            release_date__gte=window_start,
            release_date__lte=window_end
        ).delete()
        if deleted_count > 0:
            self.stdout.write(self.style.WARNING(f'Cleaned up {deleted_count} old events outside window.'))

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting economic calendar fetch...'))
        
        # Get retry delay from options
        retry_delay = options.get('retry_delay', self.RETRY_DELAY)
        cleanup = options.get('cleanup', False)

        # Calculate date window (single calculation)
        now, window_start, window_end = self.get_date_window()

        date_from_str = window_start.strftime("%Y-%m-%dT%H:%M:%S")
        date_to_str = window_end.strftime("%Y-%m-%dT%H:%M:%S")
        
        dynamic_url = f"{self.TRADAYS_URL}&from={date_from_str}&to={date_to_str}"

        # --- 1. Fetch raw HTML from Tradays widget ---
        try:
            response = requests.get(dynamic_url, headers=self.HEADERS, timeout=15)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            self.stderr.write(self.style.ERROR(f'Failed to reach Tradays: {e}'))
            return

        # --- 2. Extract Calendar.Data JSON from the page ---
        match = re.search(r'Calendar\.Data\s*=\s*(\[.*?\]);', response.text, re.DOTALL)
        if not match:
            self.stderr.write(self.style.ERROR('Could not find Calendar.Data in the page source.'))
            return

        try:
            all_events = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            self.stderr.write(self.style.ERROR(f'JSON parse error: {e}'))
            return

        self.stdout.write(f'Fetched {len(all_events)} total events from provider.')

        # --- 3. Filter to a 20-day window from today ---
        filtered_events = []
        skipped_count = 0
        
        for ev in all_events:
            ts = ev.get('ReleaseDate')
            if not ts:
                continue
            
            release_dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            if not (window_start <= release_dt <= window_end):
                continue
            
            # Validate importance level
            importance = ev.get('Importance', 'none')
            if importance not in self.VALID_IMPORTANCE_LEVELS:
                skipped_count += 1
                logger.warning(
                    f"Event '{ev.get('EventName')}' has invalid importance '{importance}', skipping."
                )
                continue
            
            filtered_events.append(ev)

        self.stdout.write(f'Filtered to {len(filtered_events)} events within {self.DATE_WINDOW_DAYS}-day window.')
        if skipped_count > 0:
            self.stdout.write(self.style.WARNING(f'Skipped {skipped_count} events with invalid importance level.'))

        # --- 4. Upsert each event into the database using bulk operations ---
        provider_ids = [ev.get('Id') for ev in filtered_events if ev.get('Id')]
        
        # Fetch existing events from DB
        existing_events_qs = EconomicCalendarEvent.objects.filter(provider_id__in=provider_ids)
        existing_events_dict = {ev.provider_id: ev for ev in existing_events_qs}

        events_to_create = []
        events_to_update = []
        
        for event_data in filtered_events:
            provider_id = event_data.get('Id')
            if not provider_id:
                continue

            release_date = datetime.fromtimestamp(
                event_data['ReleaseDate'] / 1000.0, tz=timezone.utc
            )

            importance = event_data.get('Importance', 'none')

            defaults = {
                'event_name':     event_data.get('EventName') or '',
                'currency_code':  event_data.get('CurrencyCode') or '',
                'country_name':   event_data.get('CountryName') or '',
                'importance':     importance,
                'actual_value':   self.get_safe_value(event_data.get('ActualValue')),
                'forecast_value': self.get_safe_value(event_data.get('ForecastValue')),
                'previous_value': self.get_safe_value(event_data.get('PreviousValue')),
                'release_date':   release_date,
            }

            if provider_id in existing_events_dict:
                # Update existing instance
                existing_event = existing_events_dict[provider_id]
                needs_update = False
                for field, value in defaults.items():
                    if getattr(existing_event, field) != value:
                        setattr(existing_event, field, value)
                        needs_update = True
                
                if needs_update:
                    events_to_update.append(existing_event)
            else:
                # Create new instance
                new_event = EconomicCalendarEvent(provider_id=provider_id, **defaults)
                events_to_create.append(new_event)
                
        # Retry block for bulk operations in case of deadlocks
        for attempt in range(self.MAX_RETRIES):
            try:
                with transaction.atomic():
                    if events_to_create:
                        EconomicCalendarEvent.objects.bulk_create(events_to_create, batch_size=500)
                    if events_to_update:
                        update_fields = [
                            'event_name', 'currency_code', 'country_name', 'importance',
                            'actual_value', 'forecast_value', 'previous_value', 'release_date'
                        ]
                        EconomicCalendarEvent.objects.bulk_update(events_to_update, update_fields, batch_size=500)
                break  # success
            except Exception as e:
                if '1205' in str(e) or 'deadlock' in str(e).lower():
                    if attempt < self.MAX_RETRIES - 1:
                        time.sleep(retry_delay)
                        continue
                    else:
                        self.stderr.write(self.style.ERROR(f'Deadlock failed after {self.MAX_RETRIES} retries during bulk operations.'))
                        return
                else:
                    raise

        for ev in events_to_create:
            self.stdout.write(self.style.SUCCESS(f'  Created: {ev.event_name} | {ev.currency_code} | {ev.release_date}'))
        for ev in events_to_update:
            self.stdout.write(f'  Updated: {ev.event_name} | {ev.currency_code} | {ev.release_date}')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone! Created: {len(events_to_create)} | Updated: {len(events_to_update)}'
        ))

        # --- 5. Optional cleanup of old events ---
        if cleanup:
            self.cleanup_old_events(window_start, window_end)

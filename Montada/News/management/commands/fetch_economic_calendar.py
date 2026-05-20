import time
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from News.models import EconomicCalendarEvent


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

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting economic calendar fetch...'))

        now = datetime.now(tz=timezone.utc)
        window_start = now - timedelta(days=1)  # include today's past events
        window_end = now + timedelta(days=self.DATE_WINDOW_DAYS)

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
        now = datetime.now(tz=timezone.utc)
        window_start = now - timedelta(days=1)  # include today's past events
        window_end = now + timedelta(days=self.DATE_WINDOW_DAYS)

        filtered_events = []
        for ev in all_events:
            ts = ev.get('ReleaseDate')
            if not ts:
                continue
            release_dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            if window_start <= release_dt <= window_end:
                filtered_events.append(ev)

        self.stdout.write(f'Filtered to {len(filtered_events)} events within {self.DATE_WINDOW_DAYS}-day window.')

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
            if importance not in ('low', 'medium', 'high'):
                continue

            defaults = {
                'event_name':     event_data.get('EventName') or '',
                'currency_code':  event_data.get('CurrencyCode') or '',
                'country_name':   event_data.get('CountryName') or '',
                'importance':     importance,
                'actual_value':   str(event_data.get('ActualValue', '')),
                'forecast_value': str(event_data.get('ForecastValue', '')),
                'previous_value': str(event_data.get('PreviousValue', '')),
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
                break # success
            except Exception as e:
                if '1205' in str(e) or 'deadlock' in str(e).lower():
                    if attempt < self.MAX_RETRIES - 1:
                        time.sleep(self.RETRY_DELAY)
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

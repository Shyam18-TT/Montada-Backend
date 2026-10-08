"""
Management command to periodically check economic calendar reminders and send notifications.

This command:
1. Checks for reminders that should trigger (reminder_time <= now, is_active, not is_sent)
2. Sends FCM push + in-app notifications to users for their reminders
3. Sends admin-configured global advance reminders to all active users (MontadaAdmin settings)
4. Sends a release notification per high/medium event to all active users, including the
   actual value re-fetched from Tradays if it is published within 5 minutes; otherwise a
   follow-up with the actual value goes out when it arrives (up to 30 minutes after release)
5. Claims every notification in the DB before sending so it goes out at most once

Usage (one-time run):
    python manage.py run_economic_calendar_reminders
    python manage.py run_economic_calendar_reminders --dry-run
    python manage.py run_economic_calendar_reminders --verbose

Usage (scheduled background task - requires django-apscheduler):
    python manage.py run_economic_calendar_reminders --schedule --interval 2
    
    Runs every 2 minutes in background. Add to Procfile:
    scheduler: python manage.py run_economic_calendar_reminders --schedule --interval 2
"""

import logging
import signal
import sys
import time
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Exists, OuterRef
from django.contrib.auth import get_user_model

from News.models import EconomicCalendarEvent, EconomicCalendarReminder, EconomicCalendarEventNotification
from Mainapp.models import UserNotification
from Mainapp.notification_preferences import (
    Category as NotificationCategory,
    filter_recipients,
    notifications_enabled,
)
from Mainapp.notifications import bulk_create_user_notifications
from News.management.commands.fetch_economic_calendar import TradaysFetchError, fetch_tradays_events
from firebase import send_push_to_users

try:
    from MontadaAdmin.models import EconomicCalendarGlobalReminderSettings
except ImportError:
    EconomicCalendarGlobalReminderSettings = None

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(BaseCommand):
    # Importance levels that get a release notification (with the actual value) to all users.
    RELEASE_IMPORTANCES = (
        EconomicCalendarEvent.Importance.HIGH,
        EconomicCalendarEvent.Importance.MEDIUM,
    )
    # How long after release to wait for Tradays to publish the actual value before the
    # release notification goes out without it ("has just taken place").
    ACTUAL_VALUE_WAIT = timedelta(minutes=5)
    # Events released longer ago than this are not notified (e.g. after scheduler downtime),
    # and an actual value published later than this gets no follow-up notification.
    RELEASE_WINDOW = timedelta(minutes=30)
    # Tradays EventType for data releases; 0 (speeches/meetings) and 2 (reports) never get an actual.
    TRADAYS_INDICATOR_TYPE = 1
    # A personal reminder this close to the global advance reminder replaces it for that user.
    REMINDER_OVERLAP = timedelta(minutes=5)

    help = 'Check economic calendar reminders and send notifications when they trigger or events occur.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Simulate running without making actual changes.'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed logging output.'
        )
        parser.add_argument(
            '--schedule',
            action='store_true',
            help='Run as a scheduled background task (runs indefinitely).'
        )
        parser.add_argument(
            '--interval',
            type=int,
            default=2,
            help='Interval in minutes for scheduled task (default: 2).'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        verbose = options['verbose']
        schedule = options['schedule']
        interval = options['interval']

        if verbose:
            logging.getLogger('News.management.commands.run_economic_calendar_reminders').setLevel(logging.DEBUG)

        if schedule:
            # Run as a scheduled background task
            self._run_scheduled(interval, dry_run, verbose)
        else:
            # Run once and exit
            self._run_once(dry_run, verbose)

    def _run_scheduled(self, interval_minutes, dry_run, verbose):
        """
        Run the check periodically in a loop (background scheduler).
        Handles graceful shutdown via SIGTERM/SIGINT.
        """
        self.stdout.write(
            self.style.SUCCESS(
                f'Starting economic calendar reminders scheduler (interval: {interval_minutes} min)...\n'
            )
        )

        def signal_handler(signum, frame):
            self.stdout.write(self.style.WARNING('\n\nShutting down gracefully...'))
            sys.exit(0)

        # Register signal handlers
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        interval_seconds = interval_minutes * 60

        try:
            while True:
                self.stdout.write(f"\n[{timezone.now()}] Running check...\n")
                self._run_once(dry_run, verbose)
                
                # Sleep for the specified interval
                self.stdout.write(f"Waiting {interval_minutes} minutes until next check...")
                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('\n\nScheduler stopped by user.'))
            sys.exit(0)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Scheduler error: {str(e)}'))
            logger.exception('Scheduler error', exc_info=e)
            sys.exit(1)

    def _run_once(self, dry_run, verbose):
        """
        Run the check once and exit.
        """
        now = timezone.now()

        if verbose:
            self.stdout.write(f"Current time: {now}")

        # --- Step 1: Check and send per-user reminders that should trigger ---
        reminders_sent = self._process_reminders(now, dry_run, verbose)

        # --- Step 2: Admin global advance reminders (all users, N minutes before) ---
        global_reminders_sent = self._process_global_admin_reminders(now, dry_run, verbose)

        # --- Step 3: Release notifications with the actual value (all users) ---
        release_notifications_sent, follow_ups_sent = self._process_release_notifications(
            now, dry_run, verbose,
        )

        # --- Summary ---
        self.stdout.write(
            self.style.SUCCESS(
                f'\n✓ Completed.\n'
                f'  Per-user reminders sent: {reminders_sent}\n'
                f'  Global advance reminders sent: {global_reminders_sent}\n'
                f'  Release notifications sent: {release_notifications_sent}\n'
                f'  Actual value follow-ups sent: {follow_ups_sent}'
            )
        )

    def _process_reminders(self, now, dry_run, verbose):
        """
        Check for reminders that should trigger and send notifications.
        
        Returns count of reminders processed.
        """
        count = 0

        # Find active reminders where reminder_time <= now and not yet sent.
        # Only high-importance events should trigger reminder notifications.
        pending_reminders = EconomicCalendarReminder.objects.filter(
            is_active=True,
            is_sent=False,
            reminder_time__lte=now,
        ).select_related('user', 'event')

        if verbose:
            self.stdout.write(f"Found {pending_reminders.count()} pending reminders to process.")

        for reminder in pending_reminders:
            try:
                if not dry_run:
                    # Atomically claim before sending: only the run whose UPDATE flips is_sent
                    # sends, so overlapping runs cannot double-send (and a failed push is not retried).
                    claimed = EconomicCalendarReminder.objects.filter(
                        pk=reminder.pk, is_sent=False,
                    ).update(is_sent=True, sent_at=now, updated_at=now)
                    if not claimed:
                        continue
                    if reminder.event.release_date <= now:
                        # Missed while the scheduler was down; a "starts in" reminder would be wrong.
                        logger.info(
                            "Skipped stale reminder %s: event %s already released",
                            reminder.id, reminder.event.id,
                        )
                        continue

                    self._send_reminder_notification(reminder)

                count += 1

                if verbose:
                    self.stdout.write(
                        f"  ✓ Reminder {reminder.id} for {reminder.user.username} - "
                        f"{reminder.event.event_name} ({reminder.reminder_type})"
                    )

            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(
                        f'Error processing reminder {reminder.id}: {str(e)}'
                    )
                )
                logger.exception(f"Error processing reminder {reminder.id}", exc_info=e)
                # Note: reminder is already marked as sent, so we won't retry even if push failed
                # This prevents duplicate sends in subsequent runs

        return count

    def _send_reminder_notification(self, reminder):
        """
        Send FCM push + in-app notification for a reminder.
        """
        event = reminder.event
        user = reminder.user
        now = timezone.now()

        # Calculate time remaining until event
        time_delta = event.release_date - now
        total_seconds = int(time_delta.total_seconds())
        
        # Format time remaining in human-readable format
        if total_seconds < 60:
            time_str = f"{total_seconds} seconds"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            time_str = f"{minutes} minute{'s' if minutes != 1 else ''}"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            time_str = f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            days = total_seconds // 86400
            time_str = f"{days} day{'s' if days != 1 else ''}"

        # Get event details
        event_title = event.event_name
        country = event.country_name or "Unknown"
        impact = event.get_importance_display()

        # Build notification title and body for reminder
        title = f"Reminder: {event_title}"
        body = (
            f"Reminder: The economic event '{event_title}' "
            f"({country} - {impact} Impact) "
            f"will start in {time_str}. "
            f"Market volatility may increase around the event time."
        )

        # Determine notification type based on importance
        importance = event.importance
        if importance == "high":
            notification_type = "WARNING"
        elif importance == "medium":
            notification_type = "INFO"
        else:
            notification_type = "INFO"

        # Create in-app notification (the push below is skipped centrally for opted-out users)
        if notifications_enabled(user, NotificationCategory.ECONOMIC_REMINDERS):
            UserNotification.objects.create(
                user=user,
                title=title,
                message=body,
                notification_type=notification_type,
                category="ECONOMIC_EVENT",
                redirect_url=f"/economic-calendar/{event.id}/",
            )

        # Send FCM push notification
        data_payload = {
            "type": "economic_reminder",
            "event_id": str(event.id),
            "event_name": event.event_name,
            "importance": event.importance,
            "currency_code": event.currency_code or "",
            "country": country,
            "reminder_type": reminder.reminder_type,
            "custom_minutes_before": str(reminder.custom_minutes_before or ""),
            "time_remaining": time_str,
        }

        send_push_to_users(
            users=[user],
            title=title,
            body=body,
            data=data_payload,
        )

        logger.info(
            f"Sent reminder notification to {user.username} for event {event.event_name} ({event.id}) - Event in {time_str}"
        )

    def _process_global_admin_reminders(self, now, dry_run, verbose):
        """
        Notify all active users N minutes before each economic event, per admin settings.
        Uses the same 5-minute catch-up window as event-time notifications.
        """
        if EconomicCalendarGlobalReminderSettings is None:
            return 0

        settings_obj = EconomicCalendarGlobalReminderSettings.load()
        if not settings_obj.is_enabled:
            if verbose:
                self.stdout.write("Global economic reminders are disabled — skipping.")
            return 0

        minutes_before = settings_obj.minutes_before
        if minutes_before <= 0:
            return 0

        # trigger_time = release_date - minutes_before; fire when trigger_time is in [now-5m, now]
        window_start = now - timedelta(minutes=5)
        window_end = now
        release_start = window_start + timedelta(minutes=minutes_before)
        release_end = window_end + timedelta(minutes=minutes_before)

        notification_type = EconomicCalendarEventNotification.NotificationType.ADMIN_ADVANCE
        already_sent = EconomicCalendarEventNotification.objects.filter(
            event_id=OuterRef("pk"),
            user__isnull=True,
            notification_type=notification_type,
            sent_to_all_users=True,
            is_sent=True,
        )

        upcoming_events = EconomicCalendarEvent.objects.filter(
            release_date__gte=release_start,
            release_date__lte=release_end,
            importance=EconomicCalendarEvent.Importance.HIGH,
        ).exclude(Exists(already_sent))

        # The feed holds one row per country/currency, so "GDP q/q" at 09:00 can exist many
        # times. Users only need one notification per name + time.
        sent_keys = self._already_notified_keys(
            release_start, release_end, notification_type, sent_to_all_users=True,
        )
        event_groups = self._group_duplicate_events(upcoming_events)

        if verbose:
            self.stdout.write(
                f"Found {len(event_groups)} distinct events for global {minutes_before}-min advance reminders."
            )

        count = 0

        for group in event_groups:
            event = group[0]
            try:
                if dry_run:
                    count += 1
                    if verbose:
                        self.stdout.write(
                            f"  [dry-run] Would send global advance reminder for "
                            f"{event.event_name} ({minutes_before} min before)"
                        )
                    continue

                # Claim every row in DB before push so overlapping scheduler ticks cannot double-send.
                claimed = [
                    ev for ev in group
                    if EconomicCalendarEventNotification.claim_admin_advance_notification(ev)
                ]
                if not claimed:
                    if verbose:
                        self.stdout.write(
                            f"  ⊘ Global advance reminder already sent for {event.event_name} ({event.id})"
                        )
                    continue
                if self._event_key(event) in sent_keys:
                    if verbose:
                        self.stdout.write(
                            f"  ⊘ Same-name event already notified for {event.event_name} — skipping duplicate"
                        )
                    continue

                # Users whose own reminder fires at about the same time already got that one.
                trigger_time = event.release_date - timedelta(minutes=minutes_before)
                personal_reminder_user_ids = set(
                    EconomicCalendarReminder.objects.filter(
                        event__in=group,
                        is_active=True,
                        reminder_time__gte=trigger_time - self.REMINDER_OVERLAP,
                        reminder_time__lte=trigger_time + self.REMINDER_OVERLAP,
                    ).values_list("user_id", flat=True)
                )
                users = [
                    user for user in User.objects.filter(is_active=True)
                    if user.id not in personal_reminder_user_ids
                ]
                if not users:
                    if verbose:
                        self.stdout.write(
                            f"  ⊘ No active users — skipped global advance for {event.event_name}"
                        )
                    continue

                try:
                    self._send_global_advance_notification(event, users, minutes_before, events=group)
                except Exception as send_err:
                    # Keep the claim row so the next scheduler tick cannot double-send.
                    EconomicCalendarEventNotification.objects.filter(
                        event__in=group,
                        user=None,
                        notification_type=notification_type,
                        sent_to_all_users=True,
                    ).update(error_message=str(send_err)[:1000])
                    logger.exception(
                        "Global advance reminder delivery failed for event %s",
                        event.id,
                        exc_info=send_err,
                    )
                    self.stderr.write(
                        self.style.ERROR(
                            f"Delivery failed for {event.event_name} ({event.id}); "
                            "marked sent to prevent duplicate retries."
                        )
                    )
                    continue

                count += 1
                if verbose:
                    self.stdout.write(
                        f"  ✓ Global advance reminder sent to {len(users)} users — "
                        f"{event.event_name} ({minutes_before} min before)"
                    )
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(
                        f"Error processing global advance reminder for event {event.id}: {str(e)}"
                    )
                )
                logger.exception(
                    f"Error processing global advance reminder for event {event.id}",
                    exc_info=e,
                )

        return count

    @staticmethod
    def _event_key(event):
        return ((event.event_name or "").strip().lower(), event.release_date)

    def _group_duplicate_events(self, events):
        """Group events sharing the same name + release time; returns a list of lists."""
        groups = {}
        for event in events:
            groups.setdefault(self._event_key(event), []).append(event)
        return list(groups.values())

    def _already_notified_keys(self, start, end, notification_type, sent_to_all_users):
        """Name + time keys of events in [start, end] that already have a broadcast claim."""
        rows = EconomicCalendarEventNotification.objects.filter(
            user__isnull=True,
            notification_type=notification_type,
            sent_to_all_users=sent_to_all_users,
            event__release_date__gte=start,
            event__release_date__lte=end,
        ).values_list("event__event_name", "event__release_date")
        return {((name or "").strip().lower(), released) for name, released in rows}

    @staticmethod
    def _event_location_label(events):
        """Country names for the group, falling back to currency codes when the feed has none."""
        labels = []
        for ev in events:
            label = (ev.country_name or ev.currency_code or "").strip()
            if label and label not in labels:
                labels.append(label)
        if not labels:
            return "Unknown"
        if len(labels) > 3:
            return f"{', '.join(labels[:3])} +{len(labels) - 3} more"
        return ", ".join(labels)

    def _send_global_advance_notification(self, event, users, minutes_before, events=None):
        """FCM + in-app notification to all active users before an economic event."""
        users = filter_recipients(users, NotificationCategory.ECONOMIC_EVENTS)
        if not users:
            return
        country = self._event_location_label(events or [event])
        impact = event.get_importance_display()
        title = f"Upcoming: {event.event_name}"
        body = (
            f"The economic event '{event.event_name}' "
            f"({country} - {impact} Impact) "
            f"starts in {minutes_before} minute{'s' if minutes_before != 1 else ''}. "
            f"Market volatility may increase around the event time."
        )

        if event.importance == "high":
            notification_type = "WARNING"
        else:
            notification_type = "INFO"

        notifications_to_create = [
            UserNotification(
                user=user,
                title=title,
                message=body,
                notification_type=notification_type,
                category="ECONOMIC_EVENT",
                redirect_url=f"/economic-calendar/{event.id}/",
            )
            for user in users
        ]

        data_payload = {
            "type": "economic_global_reminder",
            "event_id": str(event.id),
            "event_name": event.event_name,
            "importance": event.importance,
            "currency_code": event.currency_code or "",
            "country": country,
            "minutes_before": str(minutes_before),
        }

        # Short committed chunks; the FCM call runs outside any transaction so the
        # notification rows are not left locked while the push goes out.
        bulk_create_user_notifications(notifications_to_create)
        send_push_to_users(
            users=users,
            title=title,
            body=body,
            data=data_payload,
        )

        logger.info(
            f"Sent global advance reminder ({minutes_before} min) to {len(users)} users "
            f"for event {event.event_name} ({event.id})"
        )

    @classmethod
    def _expects_actual(cls, event):
        """Whether Tradays will publish an actual value (speeches and reports never get one)."""
        event_type = getattr(event, "provider_event_type", None)
        if event_type is not None:
            return event_type == cls.TRADAYS_INDICATOR_TYPE
        # Feed unavailable this tick: fall back to whether the event has any figures.
        return bool(event.forecast_value or event.previous_value)

    def _awaiting_actual(self, event):
        return self._expects_actual(event) and not event.actual_value

    def _process_release_notifications(self, now, dry_run, verbose):
        """
        Notify all active users about released high/medium events, in up to two steps:

        1. Release notification: sent as soon as the actual value is published, or after
           ACTUAL_VALUE_WAIT without it ("has just taken place").
        2. Actual-value follow-up: when step 1 went out without the value and Tradays
           publishes it within RELEASE_WINDOW of the release.

        Values are pulled fresh from Tradays because the scheduled calendar sync usually ran
        before the release. Events that never get a value (speeches, reports) only get step 1.

        Returns (release notifications sent, actual-value follow-ups sent).
        """
        window_start = now - self.RELEASE_WINDOW
        types = EconomicCalendarEventNotification.NotificationType

        def claim_exists(notification_type):
            return Exists(EconomicCalendarEventNotification.objects.filter(
                event_id=OuterRef("pk"),
                user__isnull=True,
                notification_type=notification_type,
                sent_to_all_users=True,
            ))

        # An ACTUAL_VALUE claim means users already have this event's value: nothing left to send.
        events = list(
            EconomicCalendarEvent.objects.filter(
                release_date__gte=window_start,
                release_date__lte=now,
                importance__in=self.RELEASE_IMPORTANCES,
            )
            .exclude(claim_exists(types.ACTUAL_VALUE))
            .annotate(release_notified=claim_exists(types.BROADCAST))
        )
        if not events:
            return 0, 0

        if any(not ev.actual_value for ev in events):
            self._refresh_actual_values(events, window_start, now, dry_run, verbose)

        release_sent = self._send_first_release_notifications(
            [ev for ev in events if not ev.release_notified], window_start, now, dry_run, verbose,
        )
        follow_ups_sent = self._send_actual_value_follow_ups(
            [ev for ev in events if ev.release_notified and ev.actual_value], dry_run, verbose,
        )
        return release_sent, follow_ups_sent

    def _claim_group(self, group, notification_type):
        """Claim every row in DB before push so overlapping scheduler ticks cannot double-send."""
        return [
            ev for ev in group
            if EconomicCalendarEventNotification.claim_broadcast_notification(ev, notification_type)
        ]

    @staticmethod
    def _record_send_error(events, notification_type, error):
        # Keep the claim rows so the next scheduler tick cannot double-send.
        EconomicCalendarEventNotification.objects.filter(
            event__in=events,
            user=None,
            notification_type=notification_type,
            sent_to_all_users=True,
        ).update(error_message=str(error)[:1000])

    def _send_first_release_notifications(self, pending_events, window_start, now, dry_run, verbose):
        types = EconomicCalendarEventNotification.NotificationType
        # The feed holds one row per country/currency, so "GDP q/q" at 09:00 can exist many
        # times. Users only need one notification per name + time.
        sent_keys = self._already_notified_keys(
            window_start, now, types.BROADCAST, sent_to_all_users=True,
        )
        count = 0

        for group in self._group_duplicate_events(pending_events):
            event = group[0]
            try:
                waited = now - event.release_date >= self.ACTUAL_VALUE_WAIT
                if any(self._awaiting_actual(ev) for ev in group) and not waited:
                    if verbose:
                        self.stdout.write(f"  … Waiting for the actual value of {event.event_name}")
                    continue

                released = [ev for ev in group if ev.actual_value]
                if released:
                    event = released[0]

                if dry_run:
                    count += 1
                    if verbose:
                        self.stdout.write(
                            f"  [dry-run] Would send release notification for "
                            f"{event.event_name}: {event.actual_value or 'no actual value'}"
                        )
                    continue

                claimed = self._claim_group(group, types.BROADCAST)
                if not claimed or self._event_key(event) in sent_keys:
                    if verbose:
                        self.stdout.write(
                            f"  ⊘ Release notification already sent for {event.event_name} ({event.id})"
                        )
                    continue
                # Values included in this notification must not be repeated by a follow-up.
                self._claim_group(released, types.ACTUAL_VALUE)

                users = list(User.objects.filter(is_active=True))
                try:
                    self._send_release_notification(
                        event, users, released or group,
                        value_to_follow=any(self._awaiting_actual(ev) for ev in group),
                    )
                except Exception as send_err:
                    self._record_send_error(group, types.BROADCAST, send_err)
                    raise

                count += 1
                if verbose:
                    self.stdout.write(
                        f"  ✓ Release notification sent to {len(users)} users — "
                        f"{event.event_name}: {event.actual_value or 'no actual value'}"
                    )
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(
                        f"Error processing release notification for event {event.id}: {str(e)}"
                    )
                )
                logger.exception(
                    f"Error processing release notification for event {event.id}",
                    exc_info=e,
                )

        return count

    def _send_actual_value_follow_ups(self, released_events, dry_run, verbose):
        """Send the actual value for events whose release notification went out without it."""
        notification_type = EconomicCalendarEventNotification.NotificationType.ACTUAL_VALUE
        count = 0

        for group in self._group_duplicate_events(released_events):
            event = group[0]
            try:
                if dry_run:
                    count += 1
                    if verbose:
                        self.stdout.write(
                            f"  [dry-run] Would send actual value follow-up for "
                            f"{event.event_name}: {event.actual_value}"
                        )
                    continue

                claimed = self._claim_group(group, notification_type)
                if not claimed:
                    continue
                event = claimed[0]

                users = list(User.objects.filter(is_active=True))
                try:
                    self._send_release_notification(event, users, claimed, follow_up=True)
                except Exception as send_err:
                    self._record_send_error(claimed, notification_type, send_err)
                    raise

                count += 1
                if verbose:
                    self.stdout.write(
                        f"  ✓ Actual value follow-up sent to {len(users)} users — "
                        f"{event.event_name}: {event.actual_value}"
                    )
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(
                        f"Error processing actual value follow-up for event {event.id}: {str(e)}"
                    )
                )
                logger.exception(
                    f"Error processing actual value follow-up for event {event.id}",
                    exc_info=e,
                )

        return count

    def _refresh_actual_values(self, events, window_start, now, dry_run, verbose):
        """Update actual/forecast/previous values on `events` in place from the Tradays feed."""
        try:
            provider_events = fetch_tradays_events(
                window_start - timedelta(minutes=1), now + timedelta(minutes=1),
            )
        except TradaysFetchError as e:
            self.stderr.write(self.style.WARNING(f"Could not refresh actual values: {e}"))
            logger.warning("Could not refresh economic calendar actual values: %s", e)
            return

        provider_by_id = {data.get("Id"): data for data in provider_events if data.get("Id")}
        value_fields = (
            ("actual_value", "ActualValue"),
            ("forecast_value", "ForecastValue"),
            ("previous_value", "PreviousValue"),
        )
        changed = []
        for event in events:
            data = provider_by_id.get(event.provider_id)
            if not data:
                continue
            event.provider_event_type = data.get("EventType")
            updated = False
            for field, key in value_fields:
                value = str(data.get(key) or "").strip() or None
                if value is not None and getattr(event, field) != value:
                    setattr(event, field, value)
                    updated = True
            if updated:
                changed.append(event)

        if verbose:
            self.stdout.write(f"Refreshed values for {len(changed)} recently released events from Tradays.")
        if changed and not dry_run:
            EconomicCalendarEvent.objects.bulk_update(
                changed, [field for field, _ in value_fields], batch_size=500,
            )

    @staticmethod
    def _format_event_values(event):
        parts = [f"Actual: {event.actual_value}"]
        if event.forecast_value:
            parts.append(f"Forecast: {event.forecast_value}")
        if event.previous_value:
            parts.append(f"Previous: {event.previous_value}")
        return " | ".join(parts)

    def _send_release_notification(self, event, users, events, value_to_follow=False, follow_up=False):
        """
        FCM + in-app notification for a released economic event, with the actual value(s)
        when the provider has published them. `follow_up` marks the later actual-value update
        for an event already announced without it.
        """
        users = filter_recipients(users, NotificationCategory.ECONOMIC_EVENTS)
        if not users:
            return

        impact = event.get_importance_display()
        released = [ev for ev in events if ev.actual_value]
        if not released:
            title = f"Economic event: {event.event_name}"
            body = (
                f"{event.event_name} ({self._event_location_label(events)} - {impact} Impact) "
                f"has just taken place."
            )
            if value_to_follow:
                body += " The actual value will follow once it is released."
        elif follow_up and len(released) == 1:
            title = f"Actual released: {event.event_name}: {event.actual_value}"
            body = (
                f"{event.event_name} ({self._event_location_label(released)} - {impact} Impact) — "
                f"{self._format_event_values(event)}"
            )
        elif len(released) == 1:
            title = f"{event.event_name}: {event.actual_value}"
            body = (
                f"{event.event_name} ({self._event_location_label(released)} - {impact} Impact) — "
                f"{self._format_event_values(event)}"
            )
        else:
            # Same event name released for several countries at once; list each result.
            title = f"Released: {event.event_name}"
            body = f"{event.event_name} ({impact} Impact) — " + "; ".join(
                f"{ev.country_name or ev.currency_code or 'N/A'}: {self._format_event_values(ev)}"
                for ev in released
            )

        notification_type = "SUCCESS" if event.importance == "high" else "INFO"

        data_payload = {
            "type": "economic_event",
            "event_id": str(event.id),
            "event_name": event.event_name,
            "importance": event.importance,
            "currency_code": event.currency_code or "",
            "country_name": event.country_name or "",
            "actual_value": event.actual_value or "",
            "forecast_value": event.forecast_value or "",
            "previous_value": event.previous_value or "",
            "is_follow_up": "true" if follow_up else "false",
        }

        notifications_to_create = [
            UserNotification(
                user=user,
                title=title,
                message=body,
                notification_type=notification_type,
                category="ECONOMIC_EVENT",
                redirect_url=f"/economic-calendar/{event.id}/",
            )
            for user in users
        ]

        # Short committed chunks; the FCM call runs outside any transaction so the
        # notification rows are not left locked while the push goes out.
        bulk_create_user_notifications(notifications_to_create)
        send_push_to_users(
            users=users,
            title=title,
            body=body,
            data=data_payload,
        )

        logger.info(
            f"Sent release notification to {len(users)} users for event "
            f"{event.event_name} ({event.id}): {event.actual_value or 'no actual value'}"
        )

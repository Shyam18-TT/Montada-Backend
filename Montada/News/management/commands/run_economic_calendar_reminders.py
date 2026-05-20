"""
Management command to periodically check economic calendar reminders and send notifications.

This command:
1. Checks for reminders that should trigger (reminder_time <= now, is_active, not is_sent)
2. Sends FCM push + in-app notifications to users for their reminders
3. Sends event-time notifications to all subscribed users when events occur
4. Marks reminders as sent to avoid duplicate notifications

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
from django.db import transaction
from django.contrib.auth import get_user_model

from News.models import EconomicCalendarEvent, EconomicCalendarReminder, EconomicCalendarEventNotification
from Mainapp.models import UserNotification
from firebase import send_push_to_users

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(BaseCommand):
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

        # --- Step 1: Check and send reminders that should trigger ---
        reminders_sent = self._process_reminders(now, dry_run, verbose)

        # --- Step 2: Check and send event-time notifications ---
        event_notifications_sent = self._process_event_notifications(now, dry_run, verbose)

        # --- Summary ---
        self.stdout.write(
            self.style.SUCCESS(
                f'\n✓ Completed.\n'
                f'  Reminders sent: {reminders_sent}\n'
                f'  Event notifications sent: {event_notifications_sent}'
            )
        )

    def _process_reminders(self, now, dry_run, verbose):
        """
        Check for reminders that should trigger and send notifications.
        
        Returns count of reminders processed.
        """
        count = 0

        # Find active reminders where reminder_time <= now and not yet sent
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
                    self._send_reminder_notification(reminder)
                    reminder.is_sent = True
                    reminder.sent_at = now
                    reminder.save(update_fields=['is_sent', 'sent_at', 'updated_at'])

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

        # Create in-app notification
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

    def _process_event_notifications(self, now, dry_run, verbose):
        """
        Send notifications to all subscribed users when economic events occur.
        
        An event notification is sent for any event where:
        - release_date is very close to now (within a 5-minute window)
        - We haven't already sent a broadcast notification for this event
        
        Returns count of events with notifications sent.
        """
        count = 0

        # Find events that just occurred (within last 5 minutes from event release)
        event_window_start = now - timedelta(minutes=5)
        event_window_end = now

        recent_events = EconomicCalendarEvent.objects.filter(
            release_date__gte=event_window_start,
            release_date__lte=event_window_end,
        )

        if verbose:
            self.stdout.write(f"Found {recent_events.count()} events occurring in the last 5 minutes.")

        for event in recent_events:
            try:
                # Check if we already sent a broadcast notification for this event
                notification_already_sent = EconomicCalendarEventNotification.check_notification_sent(
                    event=event,
                    user=None,
                    notification_type=EconomicCalendarEventNotification.NotificationType.BROADCAST
                )

                if notification_already_sent:
                    if verbose:
                        self.stdout.write(
                            f"  ⊘ Event notification already sent for {event.event_name} ({event.id}) - skipping"
                        )
                    continue

                # Get all users with active reminders or subscriptions for this event
                users_to_notify = self._get_users_for_event_notification(event)

                if not dry_run:
                    self._send_event_notification(event, users_to_notify)

                count += 1

                if verbose:
                    self.stdout.write(
                        f"  ✓ Event notification sent to {len(users_to_notify)} users - "
                        f"{event.event_name} ({event.importance})"
                    )

            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(
                        f'Error processing event {event.id}: {str(e)}'
                    )
                )
                logger.exception(f"Error processing event {event.id}", exc_info=e)

        return count

    def _get_users_for_event_notification(self, event):
        """
        Get all users who should receive an event notification.
        Returns users who:
        - Have active reminders for this event
        - Are subscribed to market data/news
        """
        # Get users with active reminders for this event
        reminder_users = User.objects.filter(
            economic_event_reminders__event=event,
            economic_event_reminders__is_active=True,
        ).distinct()

        # Get subscribed users (have active subscription for market data/news)
        subscribed_users = User.objects.filter(
            is_subscribed=True,
        ).distinct()

        # Combine both sets
        all_users = reminder_users | subscribed_users

        return list(all_users)

    def _send_event_notification(self, event, users):
        """
        Send FCM push + in-app notifications for an economic event to all users.
        Also creates tracking record to prevent duplicate notifications.
        """
        if not users:
            return

        # Build notification content
        title = f"Economic event: {event.event_name}"
        body = (
            f"{event.event_name} just occurred. "
            f"Currency: {event.currency_code or 'N/A'} | "
            f"Country: {event.country_name or 'N/A'} | "
            f"Importance: {event.get_importance_display()}"
        )

        # Additional details if available
        if event.actual_value:
            body += f" | Actual: {event.actual_value}"
        if event.forecast_value:
            body += f" | Forecast: {event.forecast_value}"

        # Determine notification type based on importance
        if event.importance == "high":
            notification_type = "SUCCESS"
        elif event.importance == "medium":
            notification_type = "INFO"
        else:
            notification_type = "INFO"

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
        }

        # Bulk create in-app notifications
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

        with transaction.atomic():
            UserNotification.objects.bulk_create(notifications_to_create)

            # Send FCM push to all users
            send_push_to_users(
                users=users,
                title=title,
                body=body,
                data=data_payload,
            )

            # Create tracking record to prevent duplicate broadcast notifications
            EconomicCalendarEventNotification.create_notification_record(
                event=event,
                user=None,
                notification_type=EconomicCalendarEventNotification.NotificationType.BROADCAST,
                sent_to_all=True,
                is_sent=True
            )

        logger.info(
            f"Sent event notification to {len(users)} users for event {event.event_name} ({event.id})"
        )

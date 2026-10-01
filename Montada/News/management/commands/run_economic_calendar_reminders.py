"""
Management command to periodically check economic calendar reminders and send notifications.

This command:
1. Checks for reminders that should trigger (reminder_time <= now, is_active, not is_sent)
2. Sends FCM push + in-app notifications to users for their reminders
3. Sends admin-configured global advance reminders to all active users (MontadaAdmin settings)
4. Sends event-time notifications to all subscribed users when events occur (unchanged)
5. Marks reminders as sent to avoid duplicate notifications

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
from firebase import send_push_to_users

try:
    from MontadaAdmin.models import EconomicCalendarGlobalReminderSettings
except ImportError:
    EconomicCalendarGlobalReminderSettings = None

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

        # --- Step 1: Check and send per-user reminders that should trigger ---
        reminders_sent = self._process_reminders(now, dry_run, verbose)

        # --- Step 2: Admin global advance reminders (all users, N minutes before) ---
        global_reminders_sent = self._process_global_admin_reminders(now, dry_run, verbose)

        # --- Step 3: Event-time notifications (unchanged) ---
        event_notifications_sent = self._process_event_notifications(now, dry_run, verbose)

        # --- Summary ---
        self.stdout.write(
            self.style.SUCCESS(
                f'\n✓ Completed.\n'
                f'  Per-user reminders sent: {reminders_sent}\n'
                f'  Global advance reminders sent: {global_reminders_sent}\n'
                f'  Event notifications sent: {event_notifications_sent}'
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
                    # Mark as sent FIRST (before sending) to prevent duplicate sends if push fails
                    reminder.is_sent = True
                    reminder.sent_at = now
                    reminder.save(update_fields=['is_sent', 'sent_at', 'updated_at'])
                    
                    # Send notification after marking as sent
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

                users = list(User.objects.filter(is_active=True))
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
            importance=EconomicCalendarEvent.Importance.HIGH,
        )

        broadcast_type = EconomicCalendarEventNotification.NotificationType.BROADCAST
        sent_keys = self._already_notified_keys(
            event_window_start, event_window_end, broadcast_type, sent_to_all_users=True,
        )
        event_groups = self._group_duplicate_events(recent_events)

        if verbose:
            self.stdout.write(f"Found {len(event_groups)} distinct events occurring in the last 5 minutes.")

        for group in event_groups:
            event = group[0]
            try:
                # Already notified for this name + time (any row of the group)?
                notification_already_sent = self._event_key(event) in sent_keys or any(
                    EconomicCalendarEventNotification.check_notification_sent(
                        event=ev, user=None, notification_type=broadcast_type,
                    )
                    for ev in group
                )

                if notification_already_sent:
                    if verbose:
                        self.stdout.write(
                            f"  ⊘ Event notification already sent for {event.event_name} ({event.id}) - skipping"
                        )
                    continue

                if not dry_run:
                    # Claim notification FIRST (in separate transaction) before sending
                    # This prevents duplicate sends if FCM push fails
                    claimed = [
                        ev for ev in group
                        if EconomicCalendarEventNotification.create_notification_record(
                            event=ev,
                            user=None,
                            notification_type=broadcast_type,
                            sent_to_all=True,
                            is_sent=False  # Mark as in-progress, not yet sent
                        )
                    ]

                    if not claimed:
                        if verbose:
                            self.stdout.write(
                                f"  ⊘ Event notification already claimed by another process for {event.event_name} ({event.id})"
                            )
                        continue
                    
                    try:
                        # Get all users with active reminders or subscriptions for this event
                        users_to_notify = self._get_users_for_event_notification(event)
                        
                        # Send the actual notifications
                        self._send_event_notification(event, users_to_notify, mark_sent=True, events=group)

                    except Exception as send_err:
                        # Mark as failed but keep the claim to prevent retries
                        EconomicCalendarEventNotification.objects.filter(
                            event__in=group,
                            user=None,
                            notification_type=EconomicCalendarEventNotification.NotificationType.BROADCAST,
                            sent_to_all_users=True,
                        ).update(is_sent=True, error_message=str(send_err)[:1000])
                        logger.exception(f"Error sending event notification for event {event.id}", exc_info=send_err)
                        raise
                else:
                    users_to_notify = self._get_users_for_event_notification(event)

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

    def _send_event_notification(self, event, users, mark_sent=False, events=None):
        """
        Send FCM push + in-app notifications for an economic event to all users.
        
        Args:
            event: The economic calendar event
            users: List of users to notify
            mark_sent: If True, mark the tracking record as sent after successful delivery
        """
        users = filter_recipients(users, NotificationCategory.ECONOMIC_EVENTS)
        if not users:
            if mark_sent:
                # Everyone opted out: still close the claim so it is not retried.
                EconomicCalendarEventNotification.objects.filter(
                    event__in=events or [event],
                    user=None,
                    notification_type=EconomicCalendarEventNotification.NotificationType.BROADCAST,
                    sent_to_all_users=True,
                ).update(is_sent=True)
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

        # Short committed chunks; the FCM call runs outside any transaction so the
        # notification rows are not left locked while the push goes out.
        bulk_create_user_notifications(notifications_to_create)

        # Send FCM push to all users
        send_push_to_users(
            users=users,
            title=title,
            body=body,
            data=data_payload,
        )
        
        # Mark as sent AFTER successful delivery (in separate transaction)
        if mark_sent:
            EconomicCalendarEventNotification.objects.filter(
                event__in=events or [event],
                user=None,
                notification_type=EconomicCalendarEventNotification.NotificationType.BROADCAST,
                sent_to_all_users=True,
            ).update(is_sent=True)

        logger.info(
            f"Sent event notification to {len(users)} users for event {event.event_name} ({event.id})"
        )

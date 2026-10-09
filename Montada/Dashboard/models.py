import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


DEFAULT_POLL_DURATION_HOURS = 7 * 24


class PollQuestion(models.Model):
    """
    A poll. Its identity (id, options, responses) is stable across resets; each
    voting period is a PollSession and `current_session` points at the latest one.
    The schedule and status live on the session (see Dashboard/polls.py).
    """
    QUESTION_TYPES = (
        ("single", "Single Choice"),
        ("multiple", "Multiple Choice"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


    question_text = models.TextField()

    question_type = models.CharField(
        max_length=20,
        choices=QUESTION_TYPES,
        default="single"
    )

    order = models.PositiveIntegerField(default=0)

    # Mirror of "current session is active", kept in sync by Dashboard/polls.py and the
    # update_poll_statuses command. Voting never trusts it; it re-checks the session schedule.
    is_active = models.BooleanField(default=True, help_text="If False, poll is closed and no new votes accepted.")

    current_session = models.ForeignKey(
        "PollSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    duration_hours = models.PositiveIntegerField(
        default=DEFAULT_POLL_DURATION_HOURS,
        help_text="Default session length used when no explicit end date is given (create/reset).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_polls",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    # Polls with recorded votes are never hard-deleted, only hidden.
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.question_text


class PollOption(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(
        PollQuestion,
        on_delete=models.CASCADE,
        related_name="options"
    )

    option_text = models.CharField(max_length=255)

    # Removed options are soft-deleted so historical responses keep their FK.
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return self.option_text


class PollSession(models.Model):
    """One voting period of a poll. Closed sessions and their responses are never modified."""
    STATUS_SCHEDULED = "scheduled"
    STATUS_ACTIVE = "active"
    STATUS_CLOSED = "closed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_CANCELLED, "Cancelled"),
    )
    OPEN_STATUSES = (STATUS_SCHEDULED, STATUS_ACTIVE)

    REASON_INITIAL = "initial"
    REASON_RESET = "reset"
    REASON_MIGRATION = "migration"
    CREATED_REASONS = (
        (REASON_INITIAL, "Initial"),
        (REASON_RESET, "Reset"),
        (REASON_MIGRATION, "Migrated from legacy data"),
    )

    CLOSED_SCHEDULE = "schedule"
    CLOSED_MANUAL = "manual"
    CLOSED_RESET = "reset"
    CLOSED_CANCELLED = "cancelled"
    CLOSED_DELETED = "deleted"
    CLOSED_REASONS = (
        (CLOSED_SCHEDULE, "End time reached"),
        (CLOSED_MANUAL, "Closed by admin"),
        (CLOSED_RESET, "Poll reset"),
        (CLOSED_CANCELLED, "Cancelled by admin"),
        (CLOSED_DELETED, "Poll deleted"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(PollQuestion, on_delete=models.CASCADE, related_name="sessions")
    session_number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SCHEDULED)
    start_at = models.DateTimeField()
    # Null only for sessions migrated from legacy polls that had no end date.
    end_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    created_reason = models.CharField(max_length=20, choices=CREATED_REASONS, default=REASON_INITIAL)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_poll_sessions",
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(max_length=20, choices=CLOSED_REASONS, blank=True, default="")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_poll_sessions",
    )

    class Meta:
        ordering = ("question_id", "session_number")
        constraints = [
            models.UniqueConstraint(fields=("question", "session_number"), name="uniq_pollsession_question_number"),
        ]
        indexes = [
            models.Index(fields=("status", "start_at"), name="pollsession_status_start_idx"),
            models.Index(fields=("status", "end_at"), name="pollsession_status_end_idx"),
        ]

    def effective_status(self, now=None):
        """Status as of `now`, independent of whether the scheduler has run yet."""
        if self.status not in self.OPEN_STATUSES:
            return self.status
        now = now or timezone.now()
        if self.end_at is not None and self.end_at <= now:
            return self.STATUS_CLOSED
        if self.start_at > now:
            return self.STATUS_SCHEDULED
        return self.STATUS_ACTIVE

    def __str__(self):
        return f"{self.question_id} #{self.session_number} ({self.status})"


class PollSessionOption(models.Model):
    """Snapshot of an option as it was offered in a session, so later edits can't rewrite history."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(PollSession, on_delete=models.CASCADE, related_name="session_options")
    option = models.ForeignKey(PollOption, on_delete=models.PROTECT, related_name="session_snapshots")
    option_text = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("session_id", "order")
        constraints = [
            models.UniqueConstraint(fields=("session", "option"), name="uniq_pollsessionoption_session_option"),
        ]


class PollResponse(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(PollQuestion, on_delete=models.CASCADE, related_name="responses")
    session = models.ForeignKey(PollSession, on_delete=models.PROTECT, related_name="responses")
    option = models.ForeignKey(PollOption, on_delete=models.PROTECT, related_name="responses")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="poll_responses"
    )

    voted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # One row per option per user per session; a user may vote again after a reset.
            models.UniqueConstraint(fields=("user", "session", "option"), name="uniq_pollresponse_user_session_option"),
        ]
        indexes = [
            models.Index(fields=("session", "option"), name="pollresponse_session_opt_idx"),
            models.Index(fields=("session", "user"), name="pollresponse_session_user_idx"),
        ]

from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone
import uuid

class Follow(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        REJECTED = "REJECTED", "Rejected"
        BLOCKED = "BLOCKED", "Blocked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


    follower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_follow_requests"
    )
    followed = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_follow_requests"
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )

    requested_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    unfollowed_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=False)

    class Meta:
        unique_together = ("follower", "followed")
        indexes = [
            models.Index(fields=["follower"]),
            models.Index(fields=["followed"]),
            models.Index(fields=["status"]),
        ]

    def accept(self):
        self.status = self.Status.ACCEPTED
        self.is_active = True
        self.accepted_at = timezone.now()
        self.rejected_at = None
        self.unfollowed_at = None
        self.save(update_fields=[
            "status", "is_active", "accepted_at",
            "rejected_at", "unfollowed_at"
        ])

    def reject(self):
        self.status = self.Status.REJECTED
        self.is_active = False
        self.rejected_at = timezone.now()
        self.save(update_fields=["status", "is_active", "rejected_at"])

    def unfollow(self):
        self.status = self.Status.ACCEPTED
        self.is_active = False
        self.unfollowed_at = timezone.now()
        self.save(update_fields=["is_active", "unfollowed_at"])

    def block(self):
        self.status = self.Status.BLOCKED
        self.is_active = False
        self.save(update_fields=["status", "is_active"])

    def __str__(self):
        return f"{self.follower} → {self.followed} ({self.status})"


class Mute(models.Model):
    """User mutes another user (muter's feed won't show muted user's content)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    muter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="muted_users"
    )
    muted = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="muted_by_users"
    )
    muted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("muter", "muted")
        indexes = [
            models.Index(fields=["muter"]),
            models.Index(fields=["muted"]),
        ]

    def __str__(self):
        return f"{self.muter} mutes {self.muted}"


class AnalystReview(models.Model):
    """
    Star rating (1–5) and optional written review for an analyst profile.
    Each user can submit at most one review per analyst (update overwrites).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    analyst = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews_received",
        help_text="The analyst being reviewed (must have user_type=analyst).",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="analyst_reviews_written",
        help_text="The user submitting the review (typically a trader).",
    )

    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="1 (worst) to 5 (best) stars.",
    )

    title = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        help_text="Optional short headline for the review.",
    )

    review_text = models.TextField(
        max_length=4000,
        blank=True,
        null=True,
        help_text="Optional detailed feedback.",
    )

    is_public = models.BooleanField(
        default=True,
        help_text="If False, hidden from public listings (e.g. moderation).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Analyst review"
        verbose_name_plural = "Analyst reviews"
        constraints = [
            models.UniqueConstraint(
                fields=["reviewer", "analyst"],
                name="followers_analystreview_unique_reviewer_per_analyst",
            ),
        ]
        indexes = [
            models.Index(fields=["analyst", "-created_at"]),
            models.Index(fields=["reviewer"]),
            models.Index(fields=["analyst", "is_public"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reviewer_id} → {self.analyst_id}: {self.rating}★"

    def clean(self):
        super().clean()
        if self.analyst_id and self.reviewer_id and self.analyst_id == self.reviewer_id:
            raise ValidationError("You cannot review yourself.")
        analyst = getattr(self, "analyst", None)
        if analyst is not None and getattr(analyst, "user_type", None) != "analyst":
            raise ValidationError("Reviews can only be submitted for analyst accounts.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

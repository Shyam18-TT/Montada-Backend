from django.db import models
from django.conf import settings
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

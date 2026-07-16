import uuid

from django.conf import settings
from django.db import models


class ModerationReport(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        REVIEWED = "reviewed", "Reviewed"
        CONTENT_REMOVED = "content_removed", "Content removed"
        USER_BANNED = "user_banned", "User banned"
        DISMISSED = "dismissed", "Dismissed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports_filed",
    )
    reported_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports_against",
    )
    reported_user_id_raw = models.CharField(max_length=64)
    content_type = models.CharField(max_length=40)
    content_id = models.CharField(max_length=64, blank=True)
    content_excerpt = models.TextField(blank=True, max_length=500)
    reason = models.CharField(max_length=64)
    details = models.TextField(blank=True, max_length=300)
    platform = models.CharField(max_length=16, blank=True)
    reported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["reason"]),
            models.Index(fields=["reported_user"]),
        ]

    def __str__(self):
        return f"{self.content_type} report by {self.reporter_id or 'deleted'}"


class UserBlock(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    blocker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blocks",
    )
    blocked = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blocked_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("blocker", "blocked")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["blocker", "blocked"]),
            models.Index(fields=["blocked"]),
        ]

    def __str__(self):
        return f"{self.blocker_id} blocked {self.blocked_id}"


def users_are_blocked(user_a, user_b):
    if not user_a or not user_b:
        return False
    user_a_id = getattr(user_a, "id", user_a)
    user_b_id = getattr(user_b, "id", user_b)
    if not user_a_id or not user_b_id:
        return False
    return UserBlock.objects.filter(
        models.Q(blocker_id=user_a_id, blocked_id=user_b_id)
        | models.Q(blocker_id=user_b_id, blocked_id=user_a_id)
    ).exists()


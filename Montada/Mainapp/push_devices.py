"""
Device registration rules for push notifications (the single place that writes DeviceToken).

- One account can be signed in on several devices; every device receives every push.
- One physical device (``device_id``) belongs to at most one account at a time.
- One FCM token belongs to exactly one row.
"""
import hashlib
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import DeviceToken, hash_push_token

logger = logging.getLogger(__name__)

MAX_TOKEN_LENGTH = 4096
MAX_DEVICE_ID_LENGTH = 191
_PLATFORMS = {DeviceToken.PLATFORM_ANDROID, DeviceToken.PLATFORM_IOS}


def normalize_platform(value):
    value = str(value or "").strip().lower()
    return value if value in _PLATFORMS else DeviceToken.PLATFORM_UNKNOWN


def normalize_device_id(value):
    """Trimmed device id that always fits the column (an over-long id is replaced by its hash)."""
    value = str(value or "").strip()
    if len(value) > MAX_DEVICE_ID_LENGTH:
        value = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return value


def register_device(user, token, device_id="", platform=""):
    """
    Register or refresh a device for ``user``. Idempotent. Returns ``(row, moved_from_user_id)``.

    ``moved_from_user_id`` is set when the physical device was previously registered to another
    account (that account stops receiving pushes on this phone).
    """
    token = str(token or "").strip()
    device_id = normalize_device_id(device_id)
    platform = normalize_platform(platform)
    token_hash = hash_push_token(token)

    # A concurrent registration of the same device can lose the unique-constraint race once.
    for attempt in range(2):
        try:
            with transaction.atomic():
                return _register(user, token, token_hash, device_id, platform)
        except IntegrityError:
            if attempt:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _register(user, token, token_hash, device_id, platform):
    now = timezone.now()
    qs = DeviceToken.objects.select_for_update()
    moved_from = None

    if device_id:
        # The token moved to another device: the old registration is dead.
        qs.filter(token_hash=token_hash).exclude(device_id=device_id).delete()
        row = qs.filter(device_id=device_id).first()
    else:
        row = qs.filter(token_hash=token_hash).first()

    if row is None:
        row = DeviceToken.objects.create(
            user=user, fcm_token=token, device_id=device_id, platform=platform, last_seen_at=now,
        )
        return row, None

    if row.user_id != user.pk:
        moved_from = row.user_id
        logger.info("Push device %s moved from user %s to user %s.", device_id or row.pk, moved_from, user.pk)

    changed = []
    if row.user_id != user.pk:
        row.user = user
        changed.append("user")
    if row.fcm_token != token:
        row.fcm_token = token
        changed.append("fcm_token")
    if device_id and row.device_id != device_id:
        row.device_id = device_id
        changed.append("device_id")
    # Older app builds omit platform: never overwrite a known value with "unknown".
    if platform != DeviceToken.PLATFORM_UNKNOWN and row.platform != platform:
        row.platform = platform
        changed.append("platform")

    row.is_active = True
    row.failure_count = 0
    row.last_error = ""
    row.last_seen_at = now
    if changed:
        row.save(update_fields=changed + ["is_active", "failure_count", "last_error", "last_seen_at", "updated_at"])
    else:
        DeviceToken.objects.filter(pk=row.pk).update(
            is_active=True, failure_count=0, last_error="", last_seen_at=now,
        )
    return row, moved_from


def unregister_device(user_ids, device_id="", token=""):
    """
    Delete the device row for ``device_id`` / ``token`` only when it belongs to one of
    ``user_ids`` (the account identified by the access token and/or the refresh token).
    Returns the number of rows deleted.
    """
    user_ids = [u for u in set(user_ids) if u]
    device_id = str(device_id or "").strip()
    token = str(token or "").strip()
    if not user_ids or not (device_id or token):
        return 0
    from django.db.models import Q

    match = Q()
    if device_id:
        match |= Q(device_id=normalize_device_id(device_id))
    if token:
        match |= Q(token_hash=hash_push_token(token))
    deleted, _ = DeviceToken.objects.filter(user_id__in=user_ids).filter(match).delete()
    return deleted


def remove_devices_for_user(user, reason=""):
    """Drop every registered device of ``user`` (password change, ban, deactivation...)."""
    deleted, _ = DeviceToken.objects.filter(user=user).delete()
    if deleted:
        logger.info("Removed %d push device(s) of user %s (%s).", deleted, user.pk, reason or "no reason given")
    return deleted

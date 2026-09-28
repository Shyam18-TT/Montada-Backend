"""
Firebase Admin SDK initialisation and FCM push-notification helpers.

Usage
-----
from firebase import send_push_to_tokens, send_push_to_users

# Send to explicit FCM token strings
send_push_to_tokens(
    tokens=["fcm_token_1", "fcm_token_2"],
    title="New Signal",
    body="A new BUY signal has been posted.",
    data={"signal_id": "abc-123", "type": "signal"},
    image_url="https://example.com/image.png",  # optional
)

# Send to User model instances (looks up their DeviceToken rows automatically)
send_push_to_users(
    users=User.objects.filter(...),
    title="...",
    body="...",
    data={...},
    image_url="...",
)
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

import firebase_admin
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safe initialisation (idempotent; won't crash if already initialised)
# ---------------------------------------------------------------------------
# Absolute path: a relative one only resolved when the process was started from Montada/,
# so workers launched from any other directory silently never sent pushes.
_CREDENTIAL_PATH = str(
    Path(__file__).resolve().parent / "credentials" / "montada-86ba6-firebase-adminsdk-fbsvc-8df57cd800.json"
)


def _ensure_firebase_app() -> bool:
    if firebase_admin._apps:
        return True
    try:
        cred = credentials.Certificate(_CREDENTIAL_PATH)
        firebase_admin.initialize_app(cred)
        return True
    except Exception as exc:
        logger.error("Firebase Admin SDK init failed (%s): %s", _CREDENTIAL_PATH, exc)
        return False


_ensure_firebase_app()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

_FCM_MULTICAST_CHUNK = 500  # FCM limit per MulticastMessage


def _clean_tokens(tokens: Iterable[str]) -> list[str]:
    cleaned_tokens: list[str] = []
    seen_tokens: set[str] = set()
    for token in tokens or []:
        normalized = str(token or "").strip()
        if not normalized or normalized in seen_tokens:
            continue
        seen_tokens.add(normalized)
        cleaned_tokens.append(normalized)
    return cleaned_tokens


def get_push_tokens_for_users(users) -> list[str]:
    """
    Resolve all distinct FCM tokens for the given users.

    Deduplication strategy
    ----------------------
    - Rows with a non-null ``device_id``: only the **most recent** token per
      physical ``device_id`` is kept. This prevents duplicate notifications
      when the same device is registered under multiple users.
    - Rows with ``device_id=NULL``: treated as anonymous devices; deduplicated
      by token string only (legacy behaviour).
    - Identical token strings that appear more than once are always collapsed
      to a single send target regardless of device_id.
    """
    try:
        from Mainapp.models import DeviceToken
    except ImportError:
        logger.error("DeviceToken model not found – cannot resolve push tokens.")
        return []

    token_rows = (
        DeviceToken.objects.filter(user__in=users)
        .exclude(fcm_token__isnull=True)
        .exclude(fcm_token__exact="")
        .order_by("device_id", "-created_at", "-id")
        .values_list("device_id", "fcm_token")
    )

    resolved_tokens: list[str] = []
    seen_tokens: set[str] = set()
    seen_device_ids: set[str] = set()  # global across all users — one send per physical device

    for device_id, token in token_rows:
        normalized = str(token or "").strip()
        if not normalized or normalized in seen_tokens:
            continue

        device_key = str(device_id or "").strip().lower()
        if device_key:
            if device_key in seen_device_ids:
                continue
            seen_device_ids.add(device_key)

        seen_tokens.add(normalized)
        resolved_tokens.append(normalized)

    logger.debug(
        "FCM token resolution: %d unique token(s) from %d device_id(s) for %d user(s)",
        len(resolved_tokens),
        len(seen_device_ids),
        len(users) if hasattr(users, '__len__') else 0,
    )
    return resolved_tokens


def _source_user_id_from_payload(data):
    for key in ("source_user_id", "from_user_id", "sender_id", "analyst_id", "trader_id"):
        value = (data or {}).get(key)
        if value:
            return str(value)
    return None


def _filter_users_who_blocked_source(users, data):
    user_list = list(users)
    source_user_id = _source_user_id_from_payload(data)
    if not source_user_id:
        return user_list
    try:
        from Moderation.models import UserBlock

        blocked_recipient_ids = set(
            UserBlock.objects.filter(blocked_id=source_user_id)
            .values_list("blocker_id", flat=True)
        )
    except Exception:
        logger.exception("FCM block filtering failed; sending to original recipients.")
        return user_list

    filtered_users = [
        user for user in user_list
        if getattr(user, "id", None) not in blocked_recipient_ids
    ]
    skipped_count = len(user_list) - len(filtered_users)
    if skipped_count:
        logger.info("FCM: skipped %d recipient(s) who blocked source user %s.", skipped_count, source_user_id)
    return filtered_users


# Custom notification sound bundled in the mobile app.
# iOS plays a sound for background/closed-app pushes only when aps.sound is set; the value
# must include the extension and must never be "default".
IOS_PUSH_SOUND = "montada_push.wav"
# Android: no extension. On Android 8+ the channel decides the sound, so the channel_id
# must be one of the channels the app creates (an unknown id gets a channel with no sound).
ANDROID_PUSH_SOUND = "montada_push"
ANDROID_DEFAULT_CHANNEL_ID = "montada_notifications"
_ANDROID_CHANNEL_BY_TYPE = {
    "news_update": "montada_news",
    "economic_reminder": "montada_economic_reminders",
    "economic_global_reminder": "montada_economic_reminders",
    "user_price_alert": "montada_price_alerts",
    "signal_change_threshold": "montada_price_alerts",
    "signal_published": "montada_trade_ideas",
    "signal_closed": "montada_trade_ideas",
    "signal_alert": "montada_trade_ideas",
    "price_alert": "montada_trade_ideas",  # analyst's own signal hit TP/SL (signal closed)
    "admin_broadcast": "montada_broadcasts",
}


def android_channel_for(data: Optional[dict]) -> str:
    """Android notification channel for a push, based on its data payload."""
    data = data or {}
    push_type = str(data.get("type") or "").strip().lower()
    if push_type == "economic_event":
        importance = str(data.get("importance") or "").strip().lower()
        return "montada_economic_high" if importance == "high" else "montada_economic"
    return _ANDROID_CHANNEL_BY_TYPE.get(push_type, ANDROID_DEFAULT_CHANNEL_ID)


def send_push_to_tokens(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> dict:
    """
    Send an FCM push notification to a list of device tokens.

    Parameters
    ----------
    tokens    : list of FCM registration tokens (strings).
    title     : notification title shown on device.
    body      : notification body text.
    data      : optional dict of string key-value pairs sent as the data payload
                (all values must be strings).
    image_url : optional URL to an image shown in the notification.

    Returns
    -------
    dict with keys:
        success_count  – number of tokens that accepted the message.
        failure_count  – number of tokens that failed.
        failed_tokens  – list of tokens that produced errors.
        errors         – list of error strings for failed tokens.
    """
    tokens = _clean_tokens(tokens)
    if not tokens:
        return {"success_count": 0, "failure_count": 0, "failed_tokens": [], "errors": []}
    if not _ensure_firebase_app():
        return {
            "success_count": 0,
            "failure_count": len(tokens),
            "failed_tokens": tokens,
            "errors": ["Firebase Admin SDK not initialised"] * len(tokens),
        }

    # Ensure data values are all strings (FCM requirement)
    clean_data = {str(k): str(v) for k, v in (data or {}).items()}
    clean_data["source"] = "montada-app"

    notification = messaging.Notification(
        title=title,
        body=body,
        image=image_url or None,
    )
    android_config = messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(
            title=title,
            body=body,
            image=image_url or None,
            channel_id=android_channel_for(data),
            sound=ANDROID_PUSH_SOUND,
        ),
    )
    apns_config = messaging.APNSConfig(
        headers={"apns-priority": "10", "apns-push-type": "alert"},
        payload=messaging.APNSPayload(
            aps=messaging.Aps(
                alert=messaging.ApsAlert(title=title, body=body),
                sound=IOS_PUSH_SOUND,
                # Only image pushes need the Notification Service Extension; routing every
                # push through it risks the extension replacing the content and dropping the sound.
                mutable_content=True if image_url else None,
            )
        ),
        fcm_options=messaging.APNSFCMOptions(image=image_url) if image_url else None,
    )

    total_success = 0
    total_failure = 0
    failed_tokens: list[str] = []
    errors: list[str] = []

    # Chunk into groups of ≤500 (FCM multicast limit)
    for i in range(0, len(tokens), _FCM_MULTICAST_CHUNK):
        chunk = tokens[i : i + _FCM_MULTICAST_CHUNK]
        message = messaging.MulticastMessage(
            tokens=chunk,
            notification=notification,
            android=android_config,
            apns=apns_config,
            data=clean_data if clean_data else None,
        )
        try:
            response: messaging.BatchResponse = messaging.send_each_for_multicast(message)
            total_success += response.success_count
            total_failure += response.failure_count
            for idx, resp in enumerate(response.responses):
                if not resp.success:
                    failed_tokens.append(chunk[idx])
                    errors.append(str(resp.exception))
        except Exception as exc:
            logger.error("FCM multicast send failed for chunk starting at %d: %s", i, exc)
            total_failure += len(chunk)
            failed_tokens.extend(chunk)
            errors.extend([str(exc)] * len(chunk))

    logger.info(
        "FCM: sent to %d token(s) – success=%d  failure=%d",
        len(tokens),
        total_success,
        total_failure,
    )
    if total_failure > 0 and errors:
        logger.warning(
            "FCM failure reason(s): %s",
            "; ".join(errors),
            extra={"fcm_errors": errors, "failed_token_count": len(failed_tokens)},
        )
    return {
        "success_count": total_success,
        "failure_count": total_failure,
        "failed_tokens": failed_tokens,
        "errors": errors,
    }


def send_push_to_users(
    users,
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> dict:
    """
    Send an FCM push notification to all registered devices of the given users.

    Parameters
    ----------
    users     : iterable of User model instances (or a queryset).
    title, body, data, image_url: same as send_push_to_tokens.

    Returns
    -------
    Same dict as send_push_to_tokens.
    """
    users = _filter_users_who_blocked_source(users, data)
    tokens = get_push_tokens_for_users(users)
    if not tokens:
        logger.info("FCM: no device tokens found for the given users – skipping.")
        return {"success_count": 0, "failure_count": 0, "failed_tokens": [], "errors": []}

    return send_push_to_tokens(
        tokens=tokens,
        title=title,
        body=body,
        data=data,
        image_url=image_url,
    )


# ---------------------------------------------------------------------------
# Background sending (keeps FCM network calls out of the API request)
# ---------------------------------------------------------------------------

_PUSH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fcm-push")


def _push_async_enabled() -> bool:
    try:
        from django.conf import settings

        return bool(getattr(settings, "FCM_PUSH_ASYNC", True))
    except Exception:
        return True


def _run_push_job(func, label, kwargs):
    from django.db import connections

    try:
        result = func(**kwargs)
        logger.info(
            "FCM background %s done: success=%s failure=%s",
            label,
            result.get("success_count", 0),
            result.get("failure_count", 0),
        )
    except Exception:
        logger.exception("FCM background %s failed.", label)
    finally:
        # This worker thread opened its own DB connection (token lookup); release it.
        connections.close_all()


def _schedule_push(func, label, **kwargs):
    if not _push_async_enabled():
        return func(**kwargs)

    def submit():
        _PUSH_EXECUTOR.submit(_run_push_job, func, label, kwargs)

    try:
        from django.db import transaction

        # Send only after the request's DB work is committed (runs now if not in a transaction).
        transaction.on_commit(submit)
    except Exception:
        submit()
    return None


def send_push_to_users_in_background(users, title, body, data=None, image_url=None) -> None:
    """Queue send_push_to_users() on a worker thread and return immediately."""
    users = list(users or [])  # evaluate querysets in the caller's thread
    if not users:
        return None
    return _schedule_push(
        send_push_to_users, "send_push_to_users",
        users=users, title=title, body=body, data=data, image_url=image_url,
    )


def send_push_to_tokens_in_background(tokens, title, body, data=None, image_url=None) -> None:
    """Queue send_push_to_tokens() on a worker thread and return immediately."""
    tokens = list(tokens or [])
    if not tokens:
        return None
    return _schedule_push(
        send_push_to_tokens, "send_push_to_tokens",
        tokens=tokens, title=title, body=body, data=data, image_url=image_url,
    )

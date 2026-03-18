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
from typing import Iterable, Optional

import firebase_admin
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safe initialisation (idempotent; won't crash if already initialised)
# ---------------------------------------------------------------------------
_CREDENTIAL_PATH = "credentials/montada-86ba6-firebase-adminsdk-fbsvc-8df57cd800.json"

if not firebase_admin._apps:
    try:
        cred = credentials.Certificate(_CREDENTIAL_PATH)
        firebase_admin.initialize_app(cred)
    except Exception as exc:
        logger.error("Firebase Admin SDK init failed: %s", exc)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

_FCM_MULTICAST_CHUNK = 500  # FCM limit per MulticastMessage


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
    if not tokens:
        return {"success_count": 0, "failure_count": 0, "failed_tokens": [], "errors": []}

    # Ensure data values are all strings (FCM requirement)
    clean_data = {str(k): str(v) for k, v in (data or {}).items()}

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
        ),
    )
    apns_config = messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(
                alert=messaging.ApsAlert(title=title, body=body),
                mutable_content=True,
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
    try:
        from Mainapp.models import DeviceToken
    except ImportError:
        logger.error("DeviceToken model not found – cannot send push notifications.")
        return {"success_count": 0, "failure_count": 0, "failed_tokens": [], "errors": []}

    tokens = list(
        DeviceToken.objects.filter(user__in=users)
        .values_list("fcm_token", flat=True)
        .distinct()
    )
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

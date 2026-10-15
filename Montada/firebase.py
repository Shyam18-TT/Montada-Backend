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
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Optional

import firebase_admin
from firebase_admin import credentials
from firebase_admin import exceptions as fb_exceptions
from firebase_admin import messaging

import push_contract

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


def _setting(name: str, default):
    try:
        from django.conf import settings

        return getattr(settings, name, default)
    except Exception:
        return default


def push_enabled() -> bool:
    """Master switch: settings.FCM_PUSH_ENABLED (env FCM_PUSH_ENABLED). Defaults to on if unset."""
    try:
        from django.conf import settings

        return bool(getattr(settings, "FCM_PUSH_ENABLED", True))
    except Exception:
        return True


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


@dataclass(frozen=True)
class PushTarget:
    """One physical device to push to."""

    token: str
    platform: str  # "android" | "ios" | "unknown"
    device_id: str
    user_id: object
    mode: str  # "sound" | "silent"


_RESOLVE_CHUNK = 1000  # keeps IN (...) lists under SQL Server's 2100-parameter limit


def _resolve_push_targets(users, preference_category=None):
    """
    (targets, skipped_user_ids) for ``users``.

    - only active rows of active, non-deleted users
    - one target per physical device: rows grouped by device_id (case-insensitive), newest
      ``updated_at`` wins; rows without a device_id are grouped by token
    - users whose preference for ``preference_category`` is "off" are skipped
    """
    from Mainapp import notification_preferences as prefs
    from Mainapp.models import DeviceToken

    user_ids = []
    for user in users or []:
        user_id = getattr(user, "pk", user)
        if user_id is not None:
            user_ids.append(user_id)
    if not user_ids:
        return [], []

    rows = []
    for start in range(0, len(user_ids), _RESOLVE_CHUNK):
        rows.extend(
            DeviceToken.objects.filter(
                user_id__in=user_ids[start:start + _RESOLVE_CHUNK],
                is_active=True,
                user__is_active=True,
                user__is_soft_deleted=False,
            )
            .exclude(fcm_token="")
            .values_list("fcm_token", "platform", "device_id", "user_id", "updated_at", "created_at", "id")
        )
    # Newest first, so the first row seen per device is the one to keep.
    rows.sort(key=lambda r: (r[4], r[5], str(r[6])), reverse=True)

    modes = {}
    if preference_category:
        try:
            modes = prefs._mode_by_user_id(preference_category)
        except Exception:
            logger.exception("FCM: notification preference lookup failed; sending with sound.")

    targets: list[PushTarget] = []
    skipped: list = []
    seen_devices: set[str] = set()
    seen_tokens: set[str] = set()
    seen_installs: set[str] = set()
    for token, platform, device_id, user_id, _updated, _created, _pk in rows:
        token = str(token or "").strip()
        if not token or token in seen_tokens:
            continue
        device_key = str(device_id or "").strip().lower() or "token:" + token
        if device_key in seen_devices:
            continue
        # An FCM token is "<firebase installation id>:<secret>". Two rows with the same
        # installation id are the same app install (registered under two device ids), so
        # pushing to both would show every notification twice on that phone.
        install_key = token.split(":", 1)[0] if ":" in token else ""
        if install_key and install_key in seen_installs:
            continue
        mode = modes.get(user_id, prefs.DEFAULT_MODE)
        if mode == prefs.OFF:
            if user_id not in skipped:
                skipped.append(user_id)
            continue
        seen_devices.add(device_key)
        seen_tokens.add(token)
        if install_key:
            seen_installs.add(install_key)
        targets.append(PushTarget(
            token=token,
            platform=platform if platform in ("android", "ios") else "unknown",
            device_id=str(device_id or ""),
            user_id=user_id,
            mode=mode,
        ))
    return targets, skipped


def get_push_targets_for_users(users, preference_category=None) -> list:
    """One PushTarget per physical device for users who have not turned ``preference_category`` off."""
    return _resolve_push_targets(users, preference_category)[0]


def get_push_tokens_for_users(users) -> list[str]:
    """Distinct FCM tokens (one per physical device) for ``users``, ignoring preferences."""
    return [target.token for target in get_push_targets_for_users(users)]


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


# Channel ids, sounds and payload shape live in push_contract.py (the app depends on them).
IOS_PUSH_SOUND = push_contract.IOS_SOUND
ANDROID_PUSH_SOUND = push_contract.ANDROID_SOUND
ANDROID_DEFAULT_CHANNEL_ID = push_contract.DEFAULT_CHANNEL
ANDROID_SILENT_CHANNEL_SUFFIX = push_contract.SILENT_SUFFIX


def android_channel_for(data: Optional[dict], sound: bool = True) -> str:
    """Android notification channel for a push, based on its data payload."""
    return push_contract.android_channel_for(data, sound=sound)


_MAX_SEND_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0
_DEACTIVATE_AFTER_FAILURES = 5
# Module-level so tests can replace it.
_sleep = time.sleep

_KIND_DEAD = "dead"
_KIND_TRANSIENT = "transient"
_KIND_PAYLOAD = "payload"
_KIND_APNS_AUTH = "apns_auth"
_KIND_OTHER = "other"


def _classify_error(exc) -> str:
    """
    What an FCM send error means for the token.

    dead       – the token itself will never work again (delete the row)
    transient  – FCM / network trouble, retry
    payload    – our message is wrong (bad key, oversized): the token is fine, never delete
    apns_auth  – APNs credentials broken: affects every iOS device, never delete
    """
    if exc is None:
        return _KIND_OTHER
    if isinstance(exc, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
        return _KIND_DEAD
    if isinstance(exc, messaging.ThirdPartyAuthError):
        return _KIND_APNS_AUTH
    if isinstance(exc, (messaging.QuotaExceededError, fb_exceptions.UnavailableError, fb_exceptions.InternalError)):
        return _KIND_TRANSIENT
    if isinstance(exc, fb_exceptions.InvalidArgumentError):
        # Malformed token: FCM returns INVALID_ARGUMENT with this exact wording.
        if "not a valid fcm registration token" in str(exc or "").lower():
            return _KIND_DEAD
        return _KIND_PAYLOAD
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return _KIND_TRANSIENT
    return _KIND_OTHER


def _is_dead_token_error(exc) -> bool:
    """
    True when FCM says the token itself will never work again (app uninstalled, token
    rotated, or issued for another Firebase project). Payload/quota/server errors are
    not token problems and must not delete anything.
    """
    return _classify_error(exc) == _KIND_DEAD


def _error_code(exc) -> str:
    code = getattr(exc, "code", None)
    return str(code or type(exc).__name__)[:64]


def _retry_delay(attempt: int, exceptions) -> float:
    """Exponential backoff with jitter, never less than a Retry-After the server asked for."""
    delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** attempt)) + random.uniform(0, 0.5)
    for exc in exceptions:
        headers = getattr(getattr(exc, "http_response", None), "headers", None) or {}
        try:
            delay = max(delay, min(_BACKOFF_MAX_SECONDS, float(headers.get("Retry-After"))))
        except (TypeError, ValueError):
            continue
    return delay


def _send_chunk(chunk: list[str], message_kwargs: dict) -> list:
    """[(token, success, message_id, exception)] for one multicast of up to 500 tokens."""
    message = messaging.MulticastMessage(tokens=chunk, **message_kwargs)
    try:
        response = messaging.send_each_for_multicast(message)
    except Exception as exc:  # the whole call failed (network, auth); every token is affected
        logger.error("FCM multicast send failed for %d token(s): %s", len(chunk), exc)
        return [(token, False, None, exc) for token in chunk]
    outcome = []
    for token, resp in zip(chunk, response.responses):
        message_id = getattr(resp, "message_id", None)
        outcome.append((
            token,
            bool(resp.success),
            message_id if isinstance(message_id, str) else None,
            None if resp.success else resp.exception,
        ))
    return outcome


def _claim_key(notification_id: str, token: str) -> str:
    from Mainapp.models import hash_push_token

    return "push_sent:%s:%s" % (notification_id, hash_push_token(token)[:32])


def _claim_devices(notification_id: str, tokens: list[str], ttl_seconds: int):
    """
    Atomically claim (notification, device) pairs so the same notification is never sent to the
    same device twice, whichever process, retry or code path triggers it.
    Returns (tokens_to_send, claimed_keys). Fails open: if the cache is down everything is sent.
    """
    try:
        from django.core.cache import cache

        to_send, keys = [], []
        for token in tokens:
            key = _claim_key(notification_id, token)
            if cache.add(key, "1", timeout=ttl_seconds):
                to_send.append(token)
                keys.append((token, key))
        return to_send, dict(keys)
    except Exception:
        logger.exception("FCM: duplicate-send claim failed; sending without it.")
        return list(tokens), {}


def _release_claims(failed_tokens: list[str], claims: dict) -> None:
    """A device that did not get the push must be allowed to receive it on a later attempt."""
    keys = [claims[t] for t in failed_tokens if t in claims]
    if not keys:
        return
    try:
        from django.core.cache import cache

        cache.delete_many(keys)
    except Exception:
        logger.exception("FCM: releasing duplicate-send claims failed.")


def _record_outcomes(results: dict, payload: dict, platform: str) -> None:
    """
    Per-token bookkeeping after a send: delivery log, last_success_at / failure_count /
    is_active, and deletion of dead tokens. Best effort; never raises into the sender.
    """
    try:
        from django.db.models import F
        from django.utils import timezone
        from Mainapp.models import DeviceToken, PushDelivery, hash_push_token

        now = timezone.now()
        hashes = {token: hash_push_token(token) for token in results}
        rows = {
            row.token_hash: row
            for row in DeviceToken.objects.filter(token_hash__in=list(hashes.values()))
        }

        ok_hashes, dead_hashes = [], []
        transient = {}  # hash -> error code
        deliveries = []
        for token, (ok, message_id, exc) in results.items():
            token_hash = hashes[token]
            row = rows.get(token_hash)
            error_code = ""
            if ok:
                status = PushDelivery.STATUS_SENT
                ok_hashes.append(token_hash)
            else:
                kind = _classify_error(exc)
                error_code = _error_code(exc)
                if kind == _KIND_DEAD:
                    status = PushDelivery.STATUS_DELETED_TOKEN
                    dead_hashes.append(token_hash)
                else:
                    status = PushDelivery.STATUS_FAILED
                    if kind == _KIND_TRANSIENT:
                        transient[token_hash] = error_code
                    elif kind == _KIND_PAYLOAD:
                        logger.error(
                            "FCM rejected the payload (INVALID_ARGUMENT), token kept. type=%s keys=%s error=%s",
                            payload.get("type"), sorted(payload.keys()), exc,
                        )
                    elif kind == _KIND_APNS_AUTH:
                        logger.error(
                            "FCM THIRD_PARTY_AUTH_ERROR: APNs credentials are invalid; every iOS push fails. "
                            "Token kept. Check the APNs auth key in the Firebase console."
                        )
            deliveries.append(PushDelivery(
                notification_id=payload.get("notification_id", ""),
                user_id=getattr(row, "user_id", None),
                device_id=getattr(row, "device_id", "") or "",
                platform=getattr(row, "platform", "") or platform,
                push_type=str(payload.get("type", ""))[:64],
                status=status,
                fcm_message_id=message_id or "",
                error_code=error_code,
            ))

        if ok_hashes:
            DeviceToken.objects.filter(token_hash__in=ok_hashes).update(
                last_success_at=now, failure_count=0, last_error="",
            )
        if transient:
            DeviceToken.objects.filter(token_hash__in=list(transient)).update(failure_count=F("failure_count") + 1)
            for token_hash, code in transient.items():
                DeviceToken.objects.filter(token_hash=token_hash).update(last_error=code)
            DeviceToken.objects.filter(
                token_hash__in=list(transient), failure_count__gte=_DEACTIVATE_AFTER_FAILURES,
            ).update(is_active=False)
        if dead_hashes:
            deleted, _ = DeviceToken.objects.filter(token_hash__in=dead_hashes).delete()
            if deleted:
                logger.info("FCM: removed %d dead device token(s).", deleted)
        if deliveries:
            PushDelivery.objects.bulk_create(deliveries)
    except Exception:
        logger.exception("FCM: recording push outcomes failed.")


def _log_skipped_preferences(user_ids, payload: dict) -> None:
    if not user_ids:
        return
    try:
        from Mainapp.models import PushDelivery

        PushDelivery.objects.bulk_create([
            PushDelivery(
                notification_id=payload.get("notification_id", ""),
                user_id=user_id,
                push_type=str(payload.get("type", ""))[:64],
                status=PushDelivery.STATUS_SKIPPED_PREF,
            )
            for user_id in user_ids
        ])
    except Exception:
        logger.exception("FCM: logging skipped recipients failed.")


def _android_config(payload: dict, data: Optional[dict], sound: bool, image_url, title, body):
    notification_id = payload["notification_id"]
    return messaging.AndroidConfig(
        priority="high",
        ttl=timedelta(seconds=push_contract.ttl_seconds_for(data)),
        notification=messaging.AndroidNotification(
            title=title,
            body=body,
            image=image_url or None,
            channel_id=push_contract.android_channel_for(data, sound=sound),
            sound=push_contract.android_sound_for(data, sound=sound),
            default_sound=False,
            priority="high",
            visibility="public",
            tag=notification_id,
        ),
    )


def _apns_config(payload: dict, data: Optional[dict], sound: bool, image_url, title, body):
    notification_id = payload["notification_id"]
    ttl = push_contract.ttl_seconds_for(data)
    return messaging.APNSConfig(
        headers={
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": str(int(time.time()) + ttl),
            "apns-collapse-id": notification_id.encode("utf-8")[:64].decode("utf-8", "ignore"),
        },
        payload=messaging.APNSPayload(
            aps=messaging.Aps(
                alert=messaging.ApsAlert(title=title, body=body),
                # No aps.sound = iOS shows the notification silently.
                sound=push_contract.ios_sound_for(data, sound=sound),
                mutable_content=True,
                # Wakes the app's background handler on iOS even though the system already shows the
                # notification. Switch off (PUSH_IOS_CONTENT_AVAILABLE=False) if the app draws a
                # second copy from that handler.
                content_available=True if _setting("PUSH_IOS_CONTENT_AVAILABLE", True) else None,
                thread_id=push_contract.thread_id_for(data),
                custom_data={"interruption-level": "active"},
            )
        ),
        fcm_options=messaging.APNSFCMOptions(image=image_url) if image_url else None,
    )


def send_push_to_tokens(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
    sound: bool = True,
    platform: str = "unknown",
    notification_id: Optional[str] = None,
    dedupe: bool = False,
) -> dict:
    """
    Send an FCM push notification to a list of device tokens.

    Parameters
    ----------
    tokens    : list of FCM registration tokens (strings).
    title     : notification title shown on device.
    body      : notification body text.
    data      : dict of extra data (values are converted to strings). ``type`` selects the
                Android channel, sound, TTL and iOS thread (see push_contract.py).
    image_url : optional URL to an image shown in the notification.
    sound     : False delivers the notification silently (silent Android channel, no iOS sound).
    platform  : "android" or "ios" sends only that platform's block; anything else sends both.
    notification_id : stable id of the event (defaults to data["notification_id"]). It is also the
                Android tag / iOS collapse-id, so a repeated send replaces the shown notification.
    dedupe    : when True, a device that already received this notification_id inside its TTL is
                skipped. Only meaningful for ids that identify an event (e.g. "chat:<message id>").

    Transient FCM errors are retried (exponential backoff + jitter, max 3). Dead tokens are
    deleted; payload errors and APNs credential errors never delete a token.

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
    if not push_enabled():
        logger.info("FCM push disabled (FCM_PUSH_ENABLED=false) – skipped %d token(s).", len(tokens))
        return {"success_count": 0, "failure_count": 0, "failed_tokens": [], "errors": []}
    if not _ensure_firebase_app():
        return {
            "success_count": 0,
            "failure_count": len(tokens),
            "failed_tokens": tokens,
            "errors": ["Firebase Admin SDK not initialised"] * len(tokens),
        }

    notification_id = notification_id or (data or {}).get("notification_id") or None
    claims: dict = {}
    skipped_duplicates = 0
    if dedupe and notification_id:
        before = len(tokens)
        tokens, claims = _claim_devices(
            str(notification_id), tokens, max(push_contract.ttl_seconds_for(data), 3600),
        )
        skipped_duplicates = before - len(tokens)
        if skipped_duplicates:
            logger.info(
                "FCM: skipped %d device(s) that already received notification %s.",
                skipped_duplicates, notification_id,
            )
        if not tokens:
            return {
                "success_count": 0, "failure_count": 0, "failed_tokens": [], "errors": [],
                "skipped_duplicates": skipped_duplicates,
            }

    payload = push_contract.build_data(
        data, title=title, body=body, sound=sound, notification_id=notification_id,
        # Off by default: with title/body in `data` the released app draws a second, local copy
        # of the notification the system already shows (confirmed on device, 2026-10-15).
        include_text=_setting("PUSH_DATA_INCLUDE_TEXT", False),
    )
    platform = platform if platform in ("android", "ios") else "unknown"

    message_kwargs = {
        "notification": messaging.Notification(title=title, body=body, image=image_url or None),
        "android": None if platform == "ios" else _android_config(payload, data, sound, image_url, title, body),
        "apns": None if platform == "android" else _apns_config(payload, data, sound, image_url, title, body),
        "data": payload,
    }

    results: dict = {}  # token -> (success, message_id, exception)
    pending = tokens
    retries = 0
    while pending:
        outcome = []
        for i in range(0, len(pending), _FCM_MULTICAST_CHUNK):
            outcome.extend(_send_chunk(pending[i:i + _FCM_MULTICAST_CHUNK], message_kwargs))
        retry = []
        for token, ok, message_id, exc in outcome:
            results[token] = (ok, message_id, exc)
            if not ok and _classify_error(exc) == _KIND_TRANSIENT:
                retry.append(token)
        if not retry or retries >= _MAX_SEND_RETRIES:
            break
        _sleep(_retry_delay(retries, [results[t][2] for t in retry]))
        retries += 1
        pending = retry

    failed_tokens = [t for t, (ok, _mid, _exc) in results.items() if not ok]
    errors = [str(results[t][2]) for t in failed_tokens]
    total_success = len(results) - len(failed_tokens)

    logger.info(
        "FCM: sent to %d token(s) – success=%d  failure=%d%s",
        len(tokens), total_success, len(failed_tokens),
        " (after %d retr%s)" % (retries, "y" if retries == 1 else "ies") if retries else "",
    )
    if failed_tokens:
        logger.warning(
            "FCM failure reason(s): %s",
            "; ".join(errors),
            extra={"fcm_errors": errors, "failed_token_count": len(failed_tokens)},
        )
    _release_claims(failed_tokens, claims)
    _record_outcomes(results, payload, platform)
    return {
        "success_count": total_success,
        "failure_count": len(failed_tokens),
        "failed_tokens": failed_tokens,
        "errors": errors,
        "skipped_duplicates": skipped_duplicates,
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

    Applies each user's notification preference for the payload's category
    (Mainapp.notification_preferences): users who turned it off are skipped, and users who
    chose "without sound" get a silent push. One push per physical device, newest token wins.

    Parameters
    ----------
    users     : iterable of User model instances (or a queryset).
    title, body, data, image_url: same as send_push_to_tokens.

    Returns
    -------
    Same dict as send_push_to_tokens.
    """
    empty_result = {"success_count": 0, "failure_count": 0, "failed_tokens": [], "errors": []}
    if not push_enabled():
        logger.info("FCM push disabled (FCM_PUSH_ENABLED=false) – skipping send to users.")
        return empty_result
    users = _filter_users_who_blocked_source(users, data)

    category = None
    try:
        from Mainapp.notification_preferences import category_for_push

        category = category_for_push(data)
    except Exception:
        logger.exception("FCM: notification category lookup failed; sending with sound.")

    data = dict(data or {})
    if category:
        data["notification_category"] = category
    # One id per event, shared by every device and by the delivery log. A producer-supplied id
    # identifies the event itself, so sends are deduplicated per device on it.
    event_id = str(data.get("notification_id") or "").strip()
    data["notification_id"] = event_id or push_contract.new_notification_id()

    targets, skipped_user_ids = _resolve_push_targets(users, category)
    _log_skipped_preferences(skipped_user_ids, data)

    grouped: dict = {}
    for target in targets:
        grouped.setdefault((target.mode != "silent", target.platform), []).append(target.token)

    result = dict(empty_result, failed_tokens=[], errors=[])
    sent_any = False
    for sound in (True, False):
        for platform in ("android", "ios", "unknown"):
            group_tokens = grouped.get((sound, platform))
            if not group_tokens:
                continue
            sent_any = True
            group_result = send_push_to_tokens(
                tokens=group_tokens,
                title=title,
                body=body,
                data=data,
                image_url=image_url,
                sound=sound,
                platform=platform,
                dedupe=bool(event_id),
            )
            result["success_count"] += group_result.get("success_count", 0)
            result["failure_count"] += group_result.get("failure_count", 0)
            result["failed_tokens"].extend(group_result.get("failed_tokens", []))
            result["errors"].extend(group_result.get("errors", []))

    if not sent_any:
        logger.info("FCM: no device tokens found for the given users – skipping.")
    return result


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


def run_in_background(func, label, **kwargs) -> None:
    """
    Queue func(**kwargs) on the push worker after the current transaction commits.
    func should return a send_push_* result dict (it is logged).
    """
    return _schedule_push(func, label, **kwargs)


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
